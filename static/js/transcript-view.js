// Structured transcript of one session: the console's default reading view.
//
// The Hub sends an engine-neutral entry list (transcript_feed.py). This view renders it as a
// conversation: the task, the agent's messages, runs of tool calls and reasoning folded into
// activity groups, questions and approvals with their answers, and the final reply.
//
// The pane polls every second, so rendering is keyed: an entry's node is created once and only
// replaced when its content changes. Expanded rows, a reply being typed, a text selection and the
// reading position therefore survive every poll. UI state lives in a plain object owned by the
// caller (one per session), so it also survives a pane being re-mounted.
import { escapeHtml, icon, formatClock, formatWhen, copyText } from './ui-kit.js';
import { renderMarkdown } from './markdown-lite.js';

const GROUP_MIN = 3; // consecutive tool/reasoning entries fold into one activity group from here
const GROUP_TAIL = 8; // an open group first shows only its newest rows
const CLAMP_CHARS = 1600; // longer agent messages start clamped
const CLAMP_LINES = 26;
// The task and the messages sent to the agent are often read whole: they clamp later and show
// more while clamped (transcript-view.css).
const PROMPT_CLAMP_CHARS = 2800;
const PROMPT_CLAMP_LINES = 40;
const PROMPT_KINDS = new Set(['task', 'parent']);
const FOLLOW_SLACK = 72; // px from the bottom that still counts as reading the newest entry
const LIVE_STATUSES = new Set(['starting', 'running']);
// Statuses whose session takes a message as it is; any other one is resumed by the send.
const RECEIVING_STATUSES = new Set(['starting', 'running', 'waiting_parent', 'completed']);
const COMPOSE_MAX_PX = 168; // the message box grows with its text up to about seven lines

const ROLE_LABELS = {
  task: '任务',
  parent: '主 Agent',
  assistant: 'Agent',
  final: '最终回复',
};
// Who sent a message to the agent (transcript_feed.py): a parent agent, this console, or the
// user typing straight into the TUI.
const ORIGIN_LABELS = { parent: '主 Agent', console: '你', user: '你 · TUI' };
const TOOL_STATUS_LABELS = { running: '运行中', ok: '完成', error: '失败', unknown: '无结果记录' };

/** Fresh per-session UI state; keep it with the session, not with the DOM. */
export function createTranscriptUiState() {
  return {
    open: new Set(), // tool / thinking / interaction rows the user expanded
    expanded: new Set(), // clamped messages the user expanded
    groups: new Map(), // group key -> open (only after the user toggled it)
    groupAll: new Set(), // groups showing all rows instead of the newest GROUP_TAIL
    follow: true,
    scrollTop: null,
    unseen: 0,
    draft: { requestId: '', text: '' },
    compose: '', // the message being written in the message box
  };
}

function fmtTime(ts) {
  return ts ? `<time title="${escapeHtml(formatWhen(ts))}">${escapeHtml(formatClock(ts))}</time>` : '';
}

function fmtDuration(seconds) {
  if (!(seconds >= 0)) return '';
  if (seconds < 1) return `${Math.max(1, Math.round(seconds * 1000))}ms`;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m < 60 ? `${m}m${String(s).padStart(2, '0')}s` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

function firstLine(text, max = 200) {
  for (const line of String(text ?? '').split(/\r?\n/)) {
    const t = line.trim();
    if (t) return t.length > max ? `${t.slice(0, max - 1)}…` : t;
  }
  return '';
}

function shortSig(text) {
  const t = String(text ?? '');
  return `${t.length}:${t.slice(0, 24)}:${t.slice(-24)}`;
}

function copyButton(id, field, title = '复制') {
  return `<button type="button" class="tx-icon-btn" data-tx="copy" data-id="${escapeHtml(id)}" data-field="${field}" title="${title}" aria-label="${title}">${icon('copy')}</button>`;
}

function isClampable(text, kind) {
  const t = String(text ?? '');
  const prompt = PROMPT_KINDS.has(kind);
  return t.length > (prompt ? PROMPT_CLAMP_CHARS : CLAMP_CHARS)
    || t.split('\n').length > (prompt ? PROMPT_CLAMP_LINES : CLAMP_LINES);
}

// ------------------------------------------------------------------ blocks
function buildBlocks(entries) {
  const blocks = [];
  let run = [];
  const flush = () => {
    if (run.length >= GROUP_MIN) blocks.push({ type: 'group', key: `g:${run[0].id}`, entries: run });
    else for (const entry of run) blocks.push({ type: 'entry', key: entry.id, entry });
    run = [];
  };
  for (const entry of entries) {
    if (entry.kind === 'tool' || entry.kind === 'thinking') run.push(entry);
    else {
      flush();
      blocks.push({ type: 'entry', key: entry.id, entry });
    }
  }
  flush();
  return blocks;
}

function entrySig(entry, ui) {
  switch (entry.kind) {
    case 'tool': {
      const t = entry.tool || {};
      return `${t.status}|${t.ended_at || ''}|${shortSig(t.output)}|${shortSig(t.title)}|${ui.open.has(entry.id)}`;
    }
    case 'interaction':
      return `${entry.pending}|${entry.resolution?.text || ''}|${ui.open.has(entry.id)}|${shortSig(entry.interaction?.prompt)}`;
    default:
      return `${entry.kind}|${shortSig(entry.text)}|${entry.previous_turn ? 1 : 0}|${ui.expanded.has(entry.id)}|${ui.open.has(entry.id)}`;
  }
}

// ------------------------------------------------------------------ entry HTML
function messageHtml(entry, ui) {
  const kind = entry.kind;
  const label = kind === 'final' && entry.previous_turn ? '上一轮回复'
    : kind === 'assistant' && entry.channel === 'report' ? '汇报给主 Agent'
      : kind === 'parent' ? ORIGIN_LABELS[entry.origin] || ROLE_LABELS.parent : ROLE_LABELS[kind] || 'Agent';
  const clampable = kind !== 'final' && isClampable(entry.text, kind);
  const clamped = clampable && !ui.expanded.has(entry.id);
  const body = kind === 'parent'
    ? `<div class="tx-plain">${escapeHtml(entry.text)}</div>`
    : `<div class="tx-md">${renderMarkdown(entry.text)}</div>`;
  return `<article class="tx-msg" data-kind="${kind}"${kind === 'parent' ? ` data-origin="${escapeHtml(entry.origin || 'parent')}"` : ''}>
    <header class="tx-msg-head">
      ${kind === 'final' ? icon('check-circle', 'icon-xs') : ''}
      <span class="tx-role">${escapeHtml(label)}</span>
      ${fmtTime(entry.timestamp)}
      <span class="tx-msg-tools">${copyButton(entry.id, 'text')}</span>
    </header>
    <div class="tx-msg-body${clamped ? ' is-clamped' : ''}">${body}</div>
    ${clampable ? `<button type="button" class="tx-link" data-tx="clamp" data-id="${escapeHtml(entry.id)}">${clamped ? '展开全文' : '收起'}</button>` : ''}
  </article>`;
}

function toolStatusIcon(status) {
  if (status === 'running') return '<span class="tx-spin" aria-hidden="true"></span>';
  if (status === 'ok') return icon('check', 'icon-xs');
  if (status === 'error') return icon('x', 'icon-xs');
  return '<span class="tx-dot" aria-hidden="true"></span>';
}

function toolBodyHtml(entry) {
  const t = entry.tool || {};
  const parts = [];
  if (t.input) {
    parts.push(`<section class="tx-io"><header>输入${copyButton(entry.id, 'input', '复制输入')}</header><pre>${escapeHtml(t.input)}</pre></section>`);
  }
  if (t.status === 'running') parts.push('<p class="tx-io-note"><span class="tx-spin"></span>等待结果…</p>');
  else if (t.output) {
    parts.push(`<section class="tx-io" data-out="${t.status === 'error' ? 'error' : 'ok'}"><header>输出${copyButton(entry.id, 'output', '复制输出')}</header><pre>${escapeHtml(t.output)}</pre></section>`);
  } else if (t.status === 'unknown') parts.push('<p class="tx-io-note">没有记录到这次调用的结果。</p>');
  else parts.push('<p class="tx-io-note">（无输出）</p>');
  return parts.join('');
}

function toolHtml(entry, ui) {
  const t = entry.tool || {};
  const open = ui.open.has(entry.id);
  const duration = t.ended_at && t.started_at ? fmtDuration(t.ended_at - t.started_at) : '';
  const title = t.title || firstLine(t.input) || '';
  return `<div class="tx-row tx-tool" data-status="${escapeHtml(t.status || 'unknown')}">
    <button type="button" class="tx-row-head" data-tx="row" data-id="${escapeHtml(entry.id)}" aria-expanded="${open}"
      title="${escapeHtml(`${t.name || 'tool'} · ${TOOL_STATUS_LABELS[t.status] || ''}${t.started_at ? ` · ${formatWhen(t.started_at)}` : ''}`)}">
      <span class="tx-row-icon">${toolStatusIcon(t.status)}</span>
      <span class="tx-tool-name">${escapeHtml(t.name || 'tool')}</span>
      <span class="tx-row-title">${escapeHtml(title)}</span>
      <span class="tx-row-meta">${escapeHtml(duration)}</span>
      <span class="tx-chevron">${icon('chevron-down', 'icon-xs')}</span>
    </button>
    <div class="tx-row-body"${open ? '' : ' hidden'}>${open ? toolBodyHtml(entry) : ''}</div>
  </div>`;
}

function thinkingHtml(entry, ui) {
  const open = ui.open.has(entry.id);
  return `<div class="tx-row tx-thinking">
    <button type="button" class="tx-row-head" data-tx="row" data-id="${escapeHtml(entry.id)}" aria-expanded="${open}">
      <span class="tx-row-icon">${icon('compass', 'icon-xs')}</span>
      <span class="tx-tool-name">思考</span>
      <span class="tx-row-title">${escapeHtml(firstLine(entry.text))}</span>
      <span class="tx-row-meta">${fmtTime(entry.timestamp)}</span>
      <span class="tx-chevron">${icon('chevron-down', 'icon-xs')}</span>
    </button>
    <div class="tx-row-body"${open ? '' : ' hidden'}>${open ? `<div class="tx-thought">${escapeHtml(entry.text)}</div>` : ''}</div>
  </div>`;
}

function interactionState(entry) {
  if (entry.pending) return { state: 'pending', text: '等待回复' };
  const r = entry.resolution;
  if (r?.by === 'auto') return { state: 'auto', text: r.text || '已自动批准' };
  if (r) return { state: 'answered', text: firstLine(r.text, 80) };
  return { state: 'closed', text: '已结束' };
}

// A generic title ("需要审批", an engine's modal name) says less than the request itself.
function interactionHeadline(view, approval) {
  return view.summary || view.title || (approval ? '需要审批' : '需要回答');
}

function interactionHtml(entry, ui) {
  const view = entry.interaction || {};
  const approval = view.kind === 'approval_request';
  const { state, text } = interactionState(entry);
  const open = ui.open.has(entry.id);
  const options = (view.options || []).map((option) => `<li>${escapeHtml(option)}</li>`).join('');
  const glyph = state === 'closed' ? '<span class="tx-dot" aria-hidden="true"></span>'
    : icon(state === 'pending' ? (approval ? 'alert' : 'message') : 'check', 'icon-xs');
  return `<div class="tx-ask" data-state="${state}">
    <button type="button" class="tx-ask-head" data-tx="row" data-id="${escapeHtml(entry.id)}" aria-expanded="${open}"
      title="${escapeHtml(view.title || '')}">
      <span class="tx-row-icon">${glyph}</span>
      <span class="tx-ask-kind">${approval ? '审批' : '提问'}</span>
      <span class="tx-ask-title">${escapeHtml(interactionHeadline(view, approval))}</span>
      <span class="tx-ask-state">${escapeHtml(text)}</span>
      ${fmtTime(entry.resolution?.timestamp || entry.timestamp)}
      <span class="tx-chevron">${icon('chevron-down', 'icon-xs')}</span>
    </button>
    <div class="tx-row-body"${open ? '' : ' hidden'}>${open ? `
      ${view.prompt ? `<div class="tx-md tx-ask-prompt">${renderMarkdown(view.prompt)}</div>` : ''}
      ${options ? `<ul class="tx-ask-options">${options}</ul>` : ''}
      ${entry.resolution ? `<p class="tx-ask-answer">${escapeHtml(entry.resolution.by === 'auto' ? entry.resolution.text : `主 Agent 回复：${entry.resolution.text}`)}</p>` : ''}` : ''}
    </div>
  </div>`;
}

function noticeHtml(entry) {
  const tone = entry.tone || 'info';
  const glyph = tone === 'error' ? 'alert' : tone === 'warn' ? 'alert' : 'info';
  return `<div class="tx-notice" data-tone="${tone}">${icon(glyph, 'icon-xs')}<span>${escapeHtml(entry.text)}</span>${fmtTime(entry.timestamp)}</div>`;
}

function entryHtml(entry, ui) {
  switch (entry.kind) {
    case 'tool': return toolHtml(entry, ui);
    case 'thinking': return thinkingHtml(entry, ui);
    case 'interaction': return interactionHtml(entry, ui);
    case 'notice': return noticeHtml(entry);
    default: return messageHtml(entry, ui);
  }
}

// ------------------------------------------------------------------ groups
function groupSummary(entries) {
  const counts = new Map();
  let thinking = 0;
  let errors = 0;
  let running = false;
  for (const entry of entries) {
    if (entry.kind === 'thinking') { thinking += 1; continue; }
    const t = entry.tool || {};
    counts.set(t.name || 'tool', (counts.get(t.name || 'tool') || 0) + 1);
    if (t.status === 'error') errors += 1;
    if (t.status === 'running') running = true;
  }
  const tools = entries.length - thinking;
  const names = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([name, n]) => (n > 1 ? `${name} ×${n}` : name));
  if (counts.size > 3) names.push('…');
  return { tools, thinking, errors, running, names };
}

function groupHeadHtml(block, open) {
  const s = groupSummary(block.entries);
  const first = block.entries[0];
  const last = block.entries[block.entries.length - 1];
  const lastTool = [...block.entries].reverse().find((entry) => entry.kind === 'tool');
  const end = last.tool?.ended_at || last.timestamp;
  const span = first.timestamp && end ? fmtDuration(end - first.timestamp) : '';
  const parts = [];
  if (s.tools) parts.push(`${s.tools} 次工具调用`);
  if (s.thinking) parts.push(`${s.thinking} 段思考`);
  const preview = !open && lastTool ? (lastTool.tool?.title || firstLine(lastTool.tool?.input)) : '';
  return `<button type="button" class="tx-group-head" data-tx="group" data-key="${escapeHtml(block.key)}" aria-expanded="${open}">
    <span class="tx-row-icon">${s.running ? '<span class="tx-spin"></span>' : icon('layers', 'icon-xs')}</span>
    <span class="tx-group-count">${escapeHtml(parts.join(' · '))}</span>
    <span class="tx-group-names">${escapeHtml(s.names.join('、'))}</span>
    ${s.errors ? `<span class="tx-badge" data-tone="error">${s.errors} 个失败</span>` : ''}
    <span class="tx-group-preview">${escapeHtml(preview || '')}</span>
    <span class="tx-row-meta">${escapeHtml(span)}</span>
    <span class="tx-chevron">${icon('chevron-down', 'icon-xs')}</span>
  </button>`;
}

// ------------------------------------------------------------------ reconcile
function nodeFromHtml(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

/**
 * Make `container`'s keyed children match `items` in order. A node is reused while its signature
 * is unchanged (or patched in place when `patch` is given) and replaced only when it changed.
 */
function reconcile(container, items, { key, sig, render, patch }) {
  const existing = new Map();
  for (const child of [...container.children]) {
    if (child.dataset.key) existing.set(child.dataset.key, child);
    else child.remove();
  }
  let index = 0;
  for (const item of items) {
    const k = key(item);
    let node = existing.get(k);
    existing.delete(k);
    if (node && patch) {
      patch(node, item);
    } else {
      const s = sig(item);
      if (!node || node.dataset.sig !== s) {
        const fresh = render(item);
        fresh.dataset.key = k;
        fresh.dataset.sig = s;
        if (node) node.replaceWith(fresh);
        node = fresh;
      }
    }
    const at = container.children[index];
    if (at !== node) container.insertBefore(node, at || null);
    index += 1;
  }
  for (const stale of existing.values()) stale.remove();
}

// ------------------------------------------------------------------ the view
/**
 * `onSend(text)` (optional) adds the message box under the transcript; it resolves to
 * `{ ok, error? }` and the box keeps the text when the send fails.
 */
export function createTranscriptView(host, ui, { onRespond, onLoadMore, onRetry, onSend = null }) {
  host.innerHTML = `
    <div class="tx">
      <div class="tx-list">
        <div class="tx-top" data-ref="top"></div>
        <div class="tx-blocks" data-ref="blocks"></div>
        <div class="tx-live" data-ref="live" hidden><span class="tx-pulse"></span><span data-ref="live-text">Agent 正在工作…</span></div>
        <div class="tx-empty" data-ref="empty" hidden></div>
      </div>
      <div class="tx-dock" data-ref="dock">
        <button type="button" class="tx-jump" data-tx="jump" hidden>${icon('chevron-down', 'icon-xs')}<span data-ref="jump-text">回到最新</span></button>
        <div data-ref="pending"></div>
        ${onSend ? `<form class="tx-compose" data-ref="compose" autocomplete="off">
          <div class="tx-compose-row">
            <textarea class="tx-compose-input" data-ref="compose-input" rows="1" aria-label="发送消息给 Agent"></textarea>
            <button type="submit" class="btn btn-sm btn-primary tx-compose-send" data-ref="compose-send" title="发送（Enter）">发送</button>
          </div>
          <p class="tx-compose-note" data-ref="compose-note" hidden></p>
        </form>` : ''}
      </div>
    </div>`;
  const ref = (name) => host.querySelector(`[data-ref="${name}"]`);
  const els = {
    top: ref('top'), blocks: ref('blocks'), live: ref('live'), liveText: ref('live-text'),
    empty: ref('empty'), jump: ref('jump-text').parentElement, jumpText: ref('jump-text'), pending: ref('pending'),
    compose: ref('compose'), composeInput: ref('compose-input'), composeSend: ref('compose-send'),
    composeNote: ref('compose-note'),
  };
  if (els.composeInput) els.composeInput.value = ui.compose || '';
  let sending = false;
  let composeLock = null; // { hint } while the caller knows a send cannot succeed (see setCompose)
  const byId = new Map();
  let data = null;
  let lastEntryIds = new Set();
  // "Load earlier" keeps the block the reader was looking at in place (see captureAnchor).
  let anchor = null;
  let rendered = false;

  const nearBottom = () => host.scrollHeight - host.clientHeight - host.scrollTop < FOLLOW_SLACK;
  const scrollToBottom = () => { host.scrollTop = host.scrollHeight; };

  function updateJump() {
    const show = !ui.follow;
    els.jump.hidden = !show;
    els.jumpText.textContent = ui.unseen > 0 ? `${ui.unseen} 条新内容` : '回到最新';
    els.jump.classList.toggle('has-new', ui.unseen > 0);
  }

  // The first block below the top edge, skipping the topmost one: older entries can merge into
  // that block (an activity group is keyed by its first entry) and change its key.
  function captureAnchor() {
    const hostTop = host.getBoundingClientRect().top;
    const nodes = [...els.blocks.children];
    const first = nodes.findIndex((item, index) => index > 0 && item.getBoundingClientRect().bottom > hostTop);
    // Several candidates: the first activity group re-keys when earlier entries merge into it.
    const candidates = (first < 0 ? [] : nodes.slice(first, first + 4))
      .filter((item) => item.dataset.key)
      .map((item) => ({ key: item.dataset.key, offset: item.getBoundingClientRect().top - hostTop }));
    return { candidates, fromBottom: host.scrollHeight - host.scrollTop, limit: Number(data?.limit) || 0 };
  }

  function restoreAnchor(saved) {
    for (const { key, offset } of saved.candidates) {
      const node = els.blocks.querySelector(`:scope > [data-key="${CSS.escape(key)}"]`);
      if (!node) continue;
      host.scrollTop += node.getBoundingClientRect().top - host.getBoundingClientRect().top - offset;
      return;
    }
    host.scrollTop = host.scrollHeight - saved.fromBottom;
  }

  function onScroll() {
    // Collapsing to zero height clamps scrollTop and fires a scroll the reader never made.
    if (!host.clientHeight) return;
    ui.scrollTop = host.scrollTop;
    const follow = nearBottom();
    if (follow !== ui.follow) {
      ui.follow = follow;
      if (follow) ui.unseen = 0;
      updateJump();
    }
  }
  host.addEventListener('scroll', onScroll, { passive: true });

  // A layout switch, window resize or the reply card growing changes the viewport without a
  // scroll: keep a following reader pinned to the newest entry.
  const resizeObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => {
      if (!host.clientHeight) return;
      if (ui.follow) scrollToBottom();
      else if (anchor === null && ui.scrollTop != null && host.scrollTop === 0) host.scrollTop = ui.scrollTop;
    })
    : null;
  resizeObserver?.observe(host);

  // A group is keyed by its first entry, so older entries merging into it (load earlier) give it
  // a new key: carry the reader's open / show-all choice over from the group it grew out of.
  let groupOfEntry = new Map(); // entry id -> key of the group it was shown in
  function carryGroupState(blocks) {
    const next = new Map();
    for (const block of blocks) {
      if (block.type !== 'group') continue;
      for (const entry of block.entries) next.set(entry.id, block.key);
      if (ui.groups.has(block.key) || ui.groupAll.has(block.key)) continue;
      const before = block.entries.map((entry) => groupOfEntry.get(entry.id)).find((key) => key && key !== block.key);
      if (!before) continue;
      if (ui.groups.has(before)) ui.groups.set(block.key, ui.groups.get(before));
      if (ui.groupAll.has(before)) ui.groupAll.add(block.key);
    }
    groupOfEntry = next;
  }

  function groupOpen(block, isTail) {
    if (ui.groups.has(block.key)) return ui.groups.get(block.key);
    // The live tail stays open while the agent works; a row the user opened keeps its group open.
    return (isTail && LIVE_STATUSES.has(String(data?.status || ''))) || block.entries.some((entry) => ui.open.has(entry.id));
  }

  function renderGroupNode(block, isTail) {
    const node = document.createElement('div');
    node.className = 'tx-group';
    node.innerHTML = '<div data-part="head"></div><div class="tx-group-body" data-part="body"></div>';
    patchGroupNode(node, block, isTail);
    return node;
  }

  function patchGroupNode(node, block, isTail) {
    const open = groupOpen(block, isTail);
    const s = groupSummary(block.entries);
    node.dataset.open = open ? '1' : '0';
    node.dataset.status = s.running ? 'running' : s.errors ? 'error' : 'ok';
    const head = node.querySelector('[data-part="head"]');
    const headSig = `${open}|${block.entries.length}|${s.errors}|${s.running}|${block.entries.at(-1)?.tool?.ended_at || ''}`;
    if (head.dataset.sig !== headSig) {
      head.innerHTML = groupHeadHtml(block, open);
      head.dataset.sig = headSig;
    }
    const body = node.querySelector('[data-part="body"]');
    body.hidden = !open;
    if (!open) {
      body.replaceChildren();
      return;
    }
    const showAll = ui.groupAll.has(block.key) || block.entries.length <= GROUP_TAIL + 2;
    const visible = showAll ? block.entries : block.entries.slice(-GROUP_TAIL);
    const items = showAll ? visible : [{ id: `more:${block.key}`, kind: '__more', hidden: block.entries.length - visible.length }, ...visible];
    reconcile(body, items, {
      key: (entry) => entry.id,
      sig: (entry) => (entry.kind === '__more' ? `more|${entry.hidden}` : entrySig(entry, ui)),
      render: (entry) => (entry.kind === '__more'
        ? nodeFromHtml(`<button type="button" class="tx-link tx-group-more" data-tx="group-all" data-group="${escapeHtml(block.key)}">显示前面 ${entry.hidden} 个步骤</button>`)
        : nodeFromHtml(entryHtml(entry, ui))),
    });
  }

  function renderTop() {
    const parts = [];
    if (data?.has_more) {
      const atMax = (data.limit || 0) >= 400;
      parts.push(atMax
        ? '<p class="tx-top-note">更早的记录未载入（已达单页上限）。完整过程可在 TUI 或会话归档中查看。</p>'
        : `<button type="button" class="tx-load-more" data-tx="more">${icon('refresh', 'icon-xs')}载入更早的记录${data.hidden_count ? `（还有 ${data.hidden_count} 条）` : ''}</button>`);
    }
    if (data?.error && (data.entries || []).length) {
      parts.push(`<p class="tx-top-note" data-tone="warn">${icon('alert', 'icon-xs')}刷新失败：${escapeHtml(data.error)}（显示的是上次载入的内容）</p>`);
    }
    const html = parts.join('');
    if (els.top.dataset.html !== html) {
      els.top.innerHTML = html;
      els.top.dataset.html = html;
    }
  }

  function renderPending(entries) {
    // The response's pending_interaction is authoritative: its entry may lie before the loaded window.
    const pending = [...entries].reverse().find((entry) => entry.kind === 'interaction' && entry.pending);
    const view = data?.pending_interaction || pending?.interaction;
    if (!view) {
      els.pending.replaceChildren();
      els.pending.dataset.sig = '';
      return;
    }
    const sig = `${view.request_id}|${shortSig(view.prompt)}|${(view.options || []).join('\u0001')}`;
    if (els.pending.dataset.sig === sig) return; // keep the reply being typed
    els.pending.dataset.sig = sig;
    const approval = view.kind === 'approval_request';
    if (ui.draft.requestId !== view.request_id) ui.draft = { requestId: view.request_id, text: '' };
    const buttons = (view.options || []).map((option, index) => `<button type="button" class="tx-choice${approval && /reject|deny|拒绝/i.test(option) ? ' is-danger' : ''}" data-tx="option" data-index="${index + 1}">${escapeHtml(option)}</button>`).join('');
    els.pending.innerHTML = `<section class="tx-pending" data-request-id="${escapeHtml(view.request_id || '')}" data-kind="${approval ? 'approval' : 'question'}">
      <header class="tx-pending-head">
        <span class="tx-pending-dot"></span>
        <span class="tx-pending-kind">${approval ? '需要审批' : '需要回答'}</span>
        <strong>${escapeHtml(view.title || '')}</strong>
        ${fmtTime(pending?.timestamp)}
      </header>
      ${view.prompt ? `<div class="tx-md tx-pending-prompt">${renderMarkdown(view.prompt)}</div>` : ''}
      ${buttons ? `<div class="tx-choices">${buttons}</div>` : ''}
      <div class="tx-reply">
        <input class="tx-reply-input" type="text" data-ref="reply" placeholder="${buttons ? '或输入自定义回复…' : '输入回复…'}" value="${escapeHtml(ui.draft.text)}" />
        <button type="button" class="btn btn-sm btn-primary" data-tx="submit">发送</button>
      </div>
    </section>`;
  }

  // The message box stands in for the reply card's input while no question or approval waits.
  function renderCompose() {
    if (!els.compose) return;
    const waiting = Boolean(els.pending.firstElementChild);
    if (els.compose.hidden !== waiting) els.compose.hidden = waiting;
    const status = String(data?.status || '');
    const hint = composeLock ? composeLock.hint
      : !data || RECEIVING_STATUSES.has(status) ? '发消息给 Agent（Enter 发送，Shift+Enter 换行）'
        : '继续这个会话：发送后自动恢复（Enter 发送，Shift+Enter 换行）';
    if (els.composeInput.placeholder !== hint) els.composeInput.placeholder = hint;
    const locked = Boolean(composeLock);
    if (els.composeInput.disabled !== locked) {
      els.composeInput.disabled = locked;
      els.composeSend.disabled = locked || sending;
    }
  }

  function fitCompose() {
    const input = els.composeInput;
    if (!input || !input.isConnected) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight + 2, COMPOSE_MAX_PX)}px`;
    if (ui.follow && host.clientHeight) scrollToBottom();
  }

  function composeNote(text, tone = 'error') {
    if (!els.composeNote) return;
    els.composeNote.hidden = !text;
    els.composeNote.textContent = text || '';
    els.composeNote.dataset.tone = tone;
  }

  async function send() {
    const input = els.composeInput;
    if (!input || sending || composeLock) return;
    const text = input.value;
    if (!text.trim()) {
      input.focus();
      input.classList.add('is-invalid');
      setTimeout(() => input.classList.remove('is-invalid'), 900);
      return;
    }
    sending = true;
    input.readOnly = true;
    els.composeSend.disabled = true;
    els.composeSend.textContent = '发送中…';
    composeNote('');
    let result;
    try {
      result = await onSend(text);
    } catch (err) {
      result = { ok: false, error: err?.message || String(err) };
    }
    sending = false;
    input.readOnly = false;
    els.composeSend.disabled = false;
    els.composeSend.textContent = '发送';
    if (result?.ok) {
      // Only clear what was sent: text typed while the send was in flight stays.
      input.value = input.value === text ? '' : input.value;
      ui.compose = input.value;
      ui.follow = true;
      ui.unseen = 0;
      updateJump();
      if (result.note) composeNote(result.note, 'info');
    } else {
      composeNote(result?.error || '发送失败', 'error');
    }
    fitCompose();
    if (input.isConnected) input.focus();
  }

  function renderEmpty(entries) {
    let text = '';
    if (!entries.length) {
      if (data?.error) text = `<p>载入失败：${escapeHtml(data.error)}</p><button type="button" class="btn btn-sm" data-tx="retry">重试</button>`;
      else if (!data) text = '<p><span class="tx-spin"></span>正在载入记录…</p>';
      else text = LIVE_STATUSES.has(String(data.status || '')) ? '<p>Agent 尚未产生可读的记录，稍候会自动出现。</p>' : '<p>这个会话没有可读的记录。</p>';
    }
    els.empty.hidden = !text;
    if (els.empty.dataset.html !== text) {
      els.empty.innerHTML = text;
      els.empty.dataset.html = text;
    }
  }

  function update(next) {
    data = next;
    const entries = Array.isArray(next?.entries) ? next.entries : [];
    // A pane without height (hidden, or mid-layout) cannot measure: keep the reader's last intent.
    const measurable = host.clientHeight > 0;
    const wasNearBottom = !rendered || (measurable ? nearBottom() : ui.follow);
    byId.clear();
    for (const entry of entries) byId.set(entry.id, entry);

    const blocks = buildBlocks(entries);
    carryGroupState(blocks);
    const tailKey = blocks.at(-1)?.key;
    reconcile(els.blocks, blocks, {
      key: (block) => block.key,
      sig: (block) => (block.type === 'group' ? 'group' : entrySig(block.entry, ui)),
      render: (block) => (block.type === 'group' ? renderGroupNode(block, block.key === tailKey) : nodeFromHtml(entryHtml(block.entry, ui))),
      patch: null,
    });
    // Groups are persistent nodes patched in place (their rows are reconciled one level down).
    for (const block of blocks) {
      if (block.type !== 'group') continue;
      const node = els.blocks.querySelector(`:scope > [data-key="${CSS.escape(block.key)}"]`);
      if (node) patchGroupNode(node, block, block.key === tailKey);
    }

    // New entries after the newest one already shown (older pages prepended do not count), whether
    // they open a block of their own or join an activity group.
    let lastKnown = -1;
    entries.forEach((entry, index) => { if (lastEntryIds.has(entry.id)) lastKnown = index; });
    const added = lastEntryIds.size ? entries.length - 1 - lastKnown : 0;
    lastEntryIds = new Set(entries.map((entry) => entry.id));

    renderTop();
    renderPending(entries);
    renderCompose();
    if (!rendered && ui.compose) fitCompose(); // a draft kept from an earlier mount
    renderEmpty(entries);
    const live = LIVE_STATUSES.has(String(next?.status || '')) && !next?.pending_interaction
      && !entries.some((e) => e.kind === 'interaction' && e.pending);
    const lastEntry = entries.at(-1);
    const toolRunning = lastEntry?.kind === 'tool' && lastEntry.tool?.status === 'running';
    els.live.hidden = !live || toolRunning || !entries.length;

    if (!measurable) {
      if (rendered && added > 0 && !ui.follow) ui.unseen += added;
      rendered = true;
      updateJump();
      return; // the resize observer settles the position once the pane has a size
    }
    if (anchor && ((Number(next?.limit) || 0) > anchor.limit || next?.error)) {
      restoreAnchor(anchor);
      anchor = null;
    } else if (anchor) {
      // A poll that started before "load earlier" was clicked: keep the reader where they are
      // (and give up on the anchor if the larger page never comes).
      anchor.waits = (anchor.waits || 0) + 1;
      if (anchor.waits > 3) anchor = null;
    } else if (wasNearBottom && ui.follow) {
      scrollToBottom();
    } else if (rendered && added > 0) {
      ui.unseen += added;
    }
    rendered = true;
    ui.follow = nearBottom() || (wasNearBottom && ui.follow);
    updateJump();
  }

  function toggleRow(id, button) {
    const entry = byId.get(id);
    if (!entry) return;
    const open = !ui.open.has(id);
    if (open) ui.open.add(id); else ui.open.delete(id);
    const row = button.closest('.tx-row, .tx-ask');
    const body = row?.querySelector('.tx-row-body');
    button.setAttribute('aria-expanded', String(open));
    if (body) {
      body.hidden = !open;
      if (open) {
        body.innerHTML = entry.kind === 'tool' ? toolBodyHtml(entry)
          : entry.kind === 'thinking' ? `<div class="tx-thought">${escapeHtml(entry.text)}</div>`
            : nodeFromHtml(interactionHtml(entry, ui)).querySelector('.tx-row-body').innerHTML;
      } else body.replaceChildren();
    }
    const holder = row?.closest('[data-key]');
    if (holder && holder.parentElement && !holder.classList.contains('tx-group')) holder.dataset.sig = entrySig(entry, ui);
  }

  function readField(id, field) {
    const entry = byId.get(id);
    if (!entry) return '';
    if (field === 'input') return entry.tool?.input || '';
    if (field === 'output') return entry.tool?.output || '';
    return entry.text || '';
  }

  async function submit(trigger, optionIndex) {
    const card = els.pending.querySelector('.tx-pending');
    if (!card) return;
    const input = card.querySelector('[data-ref="reply"]');
    const custom = input?.value?.trim() || '';
    if (!optionIndex && !custom) {
      input?.focus();
      input?.classList.add('is-invalid');
      setTimeout(() => input?.classList.remove('is-invalid'), 900);
      return;
    }
    const buttons = [...card.querySelectorAll('button')];
    buttons.forEach((button) => { button.disabled = true; });
    const ok = await onRespond({
      request_id: card.dataset.requestId || undefined,
      ...(optionIndex ? { option_index: optionIndex } : { custom_input: custom }),
    }, trigger);
    if (ok) {
      ui.draft = { requestId: '', text: '' };
      els.pending.dataset.sig = '';
    } else buttons.forEach((button) => { button.disabled = false; });
  }

  function onClick(ev) {
    const target = ev.target.closest('[data-tx]');
    if (!target || !host.contains(target)) return;
    const action = target.dataset.tx;
    switch (action) {
      case 'row':
        toggleRow(target.dataset.id, target);
        break;
      case 'group': {
        const key = target.dataset.key;
        const node = target.closest('.tx-group');
        const open = node?.dataset.open !== '1';
        ui.groups.set(key, open);
        if (data) update(data);
        break;
      }
      case 'group-all':
        // Not data-key: reconcile() stamps the row key (`more:<group>`) on the button's data-key.
        ui.groupAll.add(target.dataset.group);
        if (data) update(data);
        break;
      case 'clamp': {
        const id = target.dataset.id;
        if (ui.expanded.has(id)) ui.expanded.delete(id); else ui.expanded.add(id);
        const body = target.parentElement?.querySelector('.tx-msg-body');
        const expanded = ui.expanded.has(id);
        body?.classList.toggle('is-clamped', !expanded);
        target.textContent = expanded ? '收起' : '展开全文';
        const holder = target.closest('[data-key]');
        const entry = byId.get(id);
        if (holder && entry) holder.dataset.sig = entrySig(entry, ui);
        break;
      }
      case 'copy': {
        const text = readField(target.dataset.id, target.dataset.field);
        if (text) copyText(text);
        break;
      }
      case 'jump':
        ui.follow = true;
        ui.unseen = 0;
        host.scrollTo({ top: host.scrollHeight, behavior: 'smooth' });
        updateJump();
        break;
      case 'more':
        anchor = captureAnchor();
        target.disabled = true;
        onLoadMore();
        break;
      case 'retry':
        onRetry();
        break;
      case 'option':
        submit(target, Number(target.dataset.index));
        break;
      case 'submit':
        submit(target, null);
        break;
      default:
        break;
    }
  }

  function onKeydown(ev) {
    // Enter sends; Shift+Enter is a new line; Enter that confirms an IME composition is neither.
    const plainEnter = ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229;
    if (ev.target.matches?.('[data-ref="reply"]')) {
      if (plainEnter) {
        ev.preventDefault();
        submit(els.pending.querySelector('[data-tx="submit"]'), null);
      }
    } else if (ev.target === els.composeInput && plainEnter && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
      ev.preventDefault();
      send();
    }
  }

  function onInput(ev) {
    if (ev.target.matches?.('[data-ref="reply"]')) ui.draft.text = ev.target.value;
    else if (ev.target === els.composeInput) {
      ui.compose = ev.target.value;
      fitCompose();
    }
  }

  function onSubmit(ev) {
    if (ev.target !== els.compose) return;
    ev.preventDefault();
    send();
  }

  host.addEventListener('click', onClick);
  host.addEventListener('keydown', onKeydown);
  host.addEventListener('input', onInput);
  host.addEventListener('submit', onSubmit);

  return {
    update,
    /** Called after the pane becomes visible again (a hidden pane cannot measure). */
    reveal() {
      fitCompose();
      if (ui.follow) scrollToBottom();
      else if (ui.scrollTop != null) host.scrollTop = ui.scrollTop;
    },
    /** `{ disabled: true, hint }` locks the message box with the reason; null unlocks it. */
    setCompose(lock) {
      composeLock = lock?.disabled ? { hint: String(lock.hint || '') } : null;
      renderCompose();
    },
    dispose() {
      resizeObserver?.disconnect();
      host.removeEventListener('scroll', onScroll);
      host.removeEventListener('click', onClick);
      host.removeEventListener('keydown', onKeydown);
      host.removeEventListener('input', onInput);
      host.removeEventListener('submit', onSubmit);
    },
  };
}
