// Sidebar feed of worker → parent events reported through the hub mailbox.
import { state, prefs, savePrefs, on, emit, setActiveSession } from './console-state.js';
import { $, escapeHtml, formatClock } from './ui-kit.js';
import { activityLabel, eventTypeLabel, eventTypeTone, isLifecycleSentence } from './session-status-text.js';
import { sessionTitle } from './session-workspace.js';

const MAX_EVENTS = 30;

// Agent-facing "可调用 foo(...)" hints are noise for a human reading the feed.
const CALL_HINT_RE = /[，,。；;]?\s*(?:可|请)?调用\s*`?[A-Za-z_]+\([^)]*\)`?[^。]*。?/g;

const expanded = new Set();
let lastSignature = '';

export function initEventFeed() {
  applyCollapsed(!!prefs.eventsCollapsed);
  $('#eventsToggle').addEventListener('click', () => {
    const collapsed = !$('#eventsSection').classList.contains('collapsed');
    applyCollapsed(collapsed);
    savePrefs({ eventsCollapsed: collapsed });
  });
  $('#eventFeed').addEventListener('click', (ev) => {
    const item = ev.target.closest('.event-item');
    if (!item) return;
    const id = item.dataset.sessionId;
    const key = item.dataset.key;
    if (key) {
      if (expanded.has(key)) expanded.delete(key);
      else expanded.add(key);
      item.classList.toggle('is-expanded', expanded.has(key));
    }
    if (!id) return;
    // S1: a session stays in the live list until it is removed, but once its process is gone it is
    // history, so an event click opens the record instead of a dead pane.
    const session = state.sessions.find((s) => s.session_id === id);
    const alive = session
      ? (typeof session.process_alive === 'boolean'
        ? session.process_alive
        : ['starting', 'running', 'waiting_parent'].includes(session.status))
      : false;
    if (alive) setActiveSession(id);
    else emit('history:open', id);
  });
  on('sessions:changed', render);
}

function applyCollapsed(collapsed) {
  $('#eventsSection').classList.toggle('collapsed', collapsed);
  $('#eventsToggle').setAttribute('aria-expanded', String(!collapsed));
}

function toneOf(ev) {
  if (ev.status === 'error' || ev.status === 'failed') return 'danger';
  return eventTypeTone(ev.type);
}

// Q10: only lifecycle events (and a text that is entirely a lifecycle sentence) are translated.
// A message body from a sub-agent is shown as written: it is not a status line, and clipping it to
// 「任务已完成」 loses what the agent actually reported.
const LIFECYCLE_EVENT_TYPES = new Set(['exited', 'terminated']);

function cleanContent(ev) {
  // Render structured interactions for people; the event body contains English agent guidance.
  const interaction = ev?.interaction;
  if (interaction && ['question', 'approval_request'].includes(ev?.type)) {
    const lines = [
      ev.type === 'approval_request' ? '高风险操作审批请求' : '交互提问',
      `来源引擎：${interaction.engine || ''}`,
      `标题：${interaction.title || ''}`,
      `详情：${interaction.prompt || ''}`,
    ];
    const options = Array.isArray(interaction.options) ? interaction.options : [];
    if (options.length) {
      lines.push('可选操作／选项：', ...options.map((option, index) => `  [${index + 1}] ${option}`));
    } else lines.push('自由文本回复');
    return lines.join('\n');
  }
  // F2: the same status-text module the cards use, so an exit reads 进程已退出（退出码 N） here too.
  const text = String(ev?.content ?? '').replace(CALL_HINT_RE, '').replace(/\s+$/g, '').trim();
  if (!text) return '';
  // Persisted events from earlier Hub runs contain the body but not the live interaction object.
  if (['question', 'approval_request'].includes(ev?.type)
      && /^(?:Approval request|Interactive question)\n- Engine: /.test(text)) {
    const hint = text.lastIndexOf('\nUse respond_to_subagent_approval_or_choice(');
    const body = hint < 0 ? text : text.slice(0, hint);
    return body
      .replace(/^Approval request\n/, '高风险操作审批请求\n')
      .replace(/^Interactive question\n/, '交互提问\n')
      .replace(/\n- Engine: ([^\n]*)/, (_, engine) => `\n来源引擎：${engine}`)
      .replace(/\n- Title: /, '\n标题：')
      .replace(/\n- Details: /, '\n详情：')
      .replace(/\n- Options:\n/, '\n可选操作／选项：\n')
      .replace(/\n  \(free-text reply\)$/, '\n自由文本回复');
  }
  if (LIFECYCLE_EVENT_TYPES.has(ev?.type)) {
    const crash = text.match(/^Session crashed \(exit_code=(-?\d+)\)\. Last output:\n([\s\S]*)\nCall get_subagent_final_response\('[^']+'\)/);
    if (crash) return `异常退出（退出码 ${crash[1]}）。最后输出：\n${crash[2] === '(no output)' ? '（无输出）' : crash[2]}`;
  }
  if (LIFECYCLE_EVENT_TYPES.has(ev?.type) || isLifecycleSentence(text)) return activityLabel(text);
  return text;
}

function eventKey(ev) {
  return String(ev.seq ?? `${ev.timestamp ?? ''}-${ev.type ?? ''}`);
}

function sourceNames() {
  const names = new Map();
  for (const rec of [...state.persistedSessions, ...state.sessions]) {
    if (!names.has(rec.session_id)) names.set(rec.session_id, sessionTitle(rec));
  }
  return names;
}

function render() {
  const events = [...state.recentEvents].reverse().slice(0, MAX_EVENTS);
  const names = sourceNames();
  const signature = [
    state.globalSeq, events.length, expanded.size,
    [...names].map(([id, name]) => `${id}:${name}`).join(','),
    [...expanded].join(','),
  ].join('|');
  if (signature === lastSignature) return;
  lastSignature = signature;

  const feed = $('#eventFeed');
  if (!events.length) {
    feed.innerHTML = '<div class="list-empty">暂无事件。Sub-Agent 向主 Agent 汇报进度、提问或完成任务时会显示在这里。</div>';
    return;
  }
  feed.innerHTML = events.map((ev) => {
    const key = eventKey(ev);
    const content = cleanContent(ev);
    const isExpanded = expanded.has(key);
    return `
      <div class="event-item${isExpanded ? ' is-expanded' : ''}" data-session-id="${escapeHtml(ev.session_id || '')}" data-key="${escapeHtml(key)}" title="${escapeHtml(content)}">
        <span class="event-type" data-tone="${toneOf(ev)}">${escapeHtml(eventTypeLabel(ev.type))}</span>
        <span class="event-source">${escapeHtml(names.get(ev.session_id) || ev.session_id || '')}</span>
        <span class="event-time">${escapeHtml(formatClock(ev.timestamp))}</span>
        ${content ? `<div class="event-content">${escapeHtml(content)}</div>` : ''}
      </div>`;
  }).join('');
}
