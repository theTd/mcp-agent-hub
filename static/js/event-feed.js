// Sidebar feed of worker → parent events reported through the hub mailbox.
import { state, prefs, savePrefs, on, setActiveSession } from './console-state.js';
import { $, escapeHtml, formatClock } from './ui-kit.js';

const MAX_EVENTS = 30;
const TYPE_TONES = {
  completed: 'success',
  turn_completed: 'success',
  question: 'warning',
  message: 'info',
  message_to_parent: 'info',
  exited: 'muted',
};

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
    const id = item?.dataset.sessionId;
    if (id && state.sessions.some((s) => s.session_id === id)) setActiveSession(id);
  });
  on('sessions:changed', render);
}

function applyCollapsed(collapsed) {
  $('#eventsSection').classList.toggle('collapsed', collapsed);
  $('#eventsToggle').setAttribute('aria-expanded', String(!collapsed));
}

function toneOf(ev) {
  if (ev.status === 'error' || ev.status === 'failed') return 'danger';
  return TYPE_TONES[ev.type] || 'muted';
}

function render() {
  const events = [...state.recentEvents].reverse().slice(0, MAX_EVENTS);
  const titles = new Map(state.sessions.map((s) => [s.session_id, s.title]));
  const signature = `${state.globalSeq}|${events.length}|${[...titles].join()}`;
  if (signature === lastSignature) return;
  lastSignature = signature;

  $('#globalSeq').textContent = `#${state.globalSeq}`;
  const feed = $('#eventFeed');
  if (!events.length) {
    feed.innerHTML = '<div class="list-empty">暂无事件。Sub-Agent 向主 Agent 汇报进度、提问或完成任务时会显示在这里。</div>';
    return;
  }
  feed.innerHTML = events.map((ev) => {
    const source = titles.get(ev.session_id) || ev.session_id;
    return `
      <div class="event-item" data-session-id="${escapeHtml(ev.session_id)}" title="${escapeHtml(ev.content || '')}">
        <span class="event-type" data-tone="${toneOf(ev)}">${escapeHtml(ev.type)}</span>
        <span class="event-source">${escapeHtml(source)}</span>
        <span class="event-time">${escapeHtml(formatClock(ev.timestamp))}</span>
        ${ev.content ? `<div class="event-content">${escapeHtml(ev.content)}</div>` : ''}
      </div>`;
  }).join('');
}
