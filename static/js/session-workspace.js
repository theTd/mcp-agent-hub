// Session list in the sidebar plus the terminal / web GUI / desktop panes in the workspace.
import { hubApi } from './hub-api.js';
import { state, on, emit, setActiveSession, removeSession } from './console-state.js';
import {
  $, $$, escapeHtml, icon, formatDuration, shortPath, toast, confirmDialog, setBusy,
  openModal, copyText, firstLine, formatWhen,
} from './ui-kit.js';
import { statusLabel, statusTone, activityLabel } from './session-status-text.js';

const HISTORY_MODAL = 'historyModal';
const LIVE_STATUSES = new Set(['starting', 'running', 'waiting_parent']);
const QUICK_KEYS = {
  enter: { text: '\r', label: '⏎', title: '发送 Enter' },
  yes: { text: 'y\r', label: 'y⏎', title: '发送 y + Enter（确认）' },
  esc: { text: '\x1b', label: 'Esc', title: '发送 Esc' },
  interrupt: { text: '\x03', label: '^C', title: '发送 Ctrl+C（中断）' },
};

// V3: xterm answers device-attribute / cursor-position queries found in replayed history through
// onData. Those answers are replies, not typing, and must never be forwarded into the PTY.
const TERMINAL_REPLY_RE = /^\x1b\[[?>]?[0-9;]*[cRn]$/;

/** The server's explicit end-of-replay marker; every other frame is raw terminal output. */
function isReplayEndFrame(data) {
  if (typeof data !== 'string' || !data.startsWith('{')) return false;
  try {
    const msg = JSON.parse(data);
    return !!msg && msg.type === 'replay_end';
  } catch {
    return false;
  }
}

/**
 * Q1: writes the buffered replay and clears `replaying` in xterm's write callback, i.e. only after
 * xterm has really parsed it. xterm's write callbacks fire in order, so the reply-drop window still
 * covers every query inside the replay and is already closed by the time live output is parsed.
 */
function finishReplay(t) {
  clearTimeout(t.replayTimer);
  const pending = t.replayChunks.join('');
  t.replayChunks = [];
  t.replayPending = false;
  if (!pending) {
    t.replaying = false;
    return;
  }
  t.term.write(pending, () => { t.replaying = false; });
}

const TERMINAL_THEME = {
  background: '#0b0e14',
  foreground: '#d7dce5',
  cursor: '#7aa7ff',
  cursorAccent: '#0b0e14',
  selectionBackground: 'rgba(77, 141, 255, 0.35)',
  black: '#1b2029',
  red: '#f26b6b',
  green: '#3fcf8e',
  yellow: '#f2b347',
  blue: '#5b9bff',
  magenta: '#b48cfa',
  cyan: '#4fc6d6',
  white: '#d7dce5',
  brightBlack: '#5c6577',
  brightRed: '#ff8a8a',
  brightGreen: '#5fe0a8',
  brightYellow: '#ffcb6b',
  brightBlue: '#82b1ff',
  brightMagenta: '#c9a8ff',
  brightCyan: '#7fdbe6',
  brightWhite: '#f5f7fa',
};

const cards = new Map(); // session_id -> { el, sig }
const historyRows = new Map(); // session_id -> { el, sig } for the 历史 list (R7 keeps scroll + focus)
const panes = new Map(); // session_id -> { el, head, body, refs, headSig, mounted }
const terminals = new Map(); // session_id -> { term, fit, ws, host, ro, opened, disposed, wsOpen }
const streams = new Map(); // session_id -> { stopped, timer }

let sessionsMode = 'live'; // 'live' | 'history'
let historyRecordId = null;

export function initSessionWorkspace() {
  $('#sessionList').addEventListener('click', onSessionListClick);
  $('#sessionList').addEventListener('keydown', onSessionListKeydown);
  $('#gridContainer').addEventListener('click', onWorkspaceClick);
  $('#gridContainer').addEventListener('dblclick', onWorkspaceDblClick);
  $$('[data-session-tab]').forEach((tab) => tab.addEventListener('click', () => setSessionsMode(tab.dataset.sessionTab)));
  $('#historyModal').addEventListener('click', onHistoryModalClick);

  on('sessions:changed', renderAll);
  on('session:activated', () => {
    renderAll();
    focusActiveSession();
  });
  on('layout:changed', renderWorkspace);
  on('session:open', openSessionOrHistory);
  on('history:open', openHistoryRecord);
  window.addEventListener('resize', scheduleFitAll);
  // Alt+↑ / Alt+↓ also works while the terminal has focus (capture phase sees it first).
  document.addEventListener('keydown', onGlobalKeydown, true);
  renderAll();
}

export function setSessionsMode(mode) {
  sessionsMode = mode === 'history' ? 'history' : 'live';
  renderSessionList();
}

function renderAll() {
  disposeGoneSessions();
  renderSessionList();
  renderWorkspace();
}

// ---------------------------------------------------------- session shapes
const isLive = (s) => (typeof s.process_alive === 'boolean' ? s.process_alive : LIVE_STATUSES.has(s.status));
const engineKey = (s) => (s.kind === 'desktop_gui' ? 'desktop' : s.engine || s.kind || 'tui');
const profileLabel = (s) => s.profile_name || s.profile_id || '会话';

function sessionTitle(s) {
  const task = firstLine(s.last_task);
  return `${profileLabel(s)} · ${task || '交互会话'}`;
}

function cwdBase(cwd) {
  const parts = String(cwd || '').split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

function orderedSessions() {
  return [...state.sessions].reverse(); // newest first
}

function historyEntries() {
  const live = new Set(state.sessions.map((s) => s.session_id));
  return state.persistedSessions
    .filter((rec) => !live.has(rec.session_id))
    .sort((a, b) => (b.ended_at || b.updated_at || b.created_at || 0) - (a.ended_at || a.updated_at || a.created_at || 0));
}

function modelLine(s) {
  return [s.effective_model, s.effective_thinking_level].filter(Boolean).join(' · ');
}

// ------------------------------------------------------------- session list
function renderSessionList() {
  const list = $('#sessionList');
  $('#liveCount').textContent = String(state.sessions.length);
  $('#historyCount').textContent = String(historyEntries().length);
  $$('[data-session-tab]').forEach((tab) =>
    tab.setAttribute('aria-pressed', String(tab.dataset.sessionTab === sessionsMode)));
  const waiting = state.sessions.filter((s) => s.status === 'waiting_parent').length;
  $('#sessionSummary').innerHTML = waiting ? `<span class="warn">${waiting} 个待回复</span>` : '';

  if (sessionsMode === 'history') {
    renderHistoryList(list);
    return;
  }

  const ordered = orderedSessions();
  if (!ordered.length) {
    list.innerHTML = state.sessionsLoaded ? '<div class="list-empty">暂无会话</div>' : '';
    cards.clear();
    return;
  }
  list.querySelector('.list-empty')?.remove();
  // Coming back from 历史: the list holds history rows, not cards.
  if (list.querySelector('.history-item')) {
    list.innerHTML = '';
    historyRows.clear();
  }

  ordered.forEach((s, index) => {
    let card = cards.get(s.session_id);
    if (!card) {
      const el = document.createElement('div');
      el.className = 'session-card';
      el.dataset.id = s.session_id;
      el.tabIndex = 0;
      el.setAttribute('role', 'option');
      card = { el, sig: '' };
      cards.set(s.session_id, card);
    }
    const sig = [
      sessionTitle(s), s.status, s.process_alive, s.engine, s.kind,
      s.effective_model, s.effective_thinking_level, s.cwd,
    ].join('|');
    if (sig !== card.sig) {
      card.el.innerHTML = sessionCardHtml(s);
      card.sig = sig;
    }
    card.el.setAttribute('aria-selected', String(s.session_id === state.activeSessionId));
    card.el.querySelector('[data-ref="activity"]').textContent = activityText(s);
    card.el.querySelector('[data-ref="idle"]').textContent = idleText(s);
    if (list.children[index] !== card.el) list.insertBefore(card.el, list.children[index] || null);
  });
}

/** Model · thinking on its own meta line so it stays readable (V-2). */
function modelMetaHtml(s) {
  const model = modelLine(s);
  return model ? `<span class="sc-meta-model" title="${escapeHtml(model)}">${escapeHtml(model)}</span>` : '';
}

/** Folder + short id on the line below the model (V-2). */
function pathMetaHtml(s) {
  const bits = [];
  const folder = cwdBase(s.cwd);
  if (folder) bits.push(`<span class="sc-meta-text" title="${escapeHtml(s.cwd || '')}">${icon('folder', 'icon-xs')}${escapeHtml(folder)}</span>`);
  const tail = String(s.session_id || '').slice(-6);
  if (tail) bits.push(`<span class="sc-meta-id" title="${escapeHtml(s.session_id || '')}">${escapeHtml(tail)}</span>`);
  return bits.map((bit) => `<span class="dot-sep"></span>${bit}`).join('');
}

function sessionCardHtml(s) {
  const live = isLive(s);
  const path = pathMetaHtml(s);
  return `
    <div class="sc-row">
      <span class="status-dot" data-status="${escapeHtml(s.status)}" title="${escapeHtml(statusLabel(s.status))}"></span>
      <span class="sc-title" title="${escapeHtml(sessionTitle(s))}">${escapeHtml(sessionTitle(s))}</span>
      <button type="button" class="icon-btn sm danger sc-close" data-action="close"
        title="${live ? '终止会话' : '移除'}" aria-label="${live ? '终止会话' : '移除'}">${icon('x')}</button>
    </div>
    <div class="sc-activity" data-ref="activity"></div>
    <div class="sc-meta">
      <span class="engine-badge" data-engine="${escapeHtml(engineKey(s))}">${escapeHtml(engineKey(s))}</span>
      <span class="status-text" data-tone="${statusTone(s.status)}">${escapeHtml(statusLabel(s.status))}</span>
      ${modelMetaHtml(s)}
      <span data-ref="idle"></span>
    </div>
    ${path ? `<div class="sc-meta sc-meta-sub">${path}</div>` : ''}`;
}

function activityText(s) {
  return activityLabel(s.current_activity);
}

function idleText(s) {
  if (isLive(s)) return `空闲 ${formatDuration(s.idle_seconds)}`;
  return s.exit_code !== null && s.exit_code !== undefined ? `退出码 ${s.exit_code}` : '';
}

function historySignature(entries) {
  return entries.map((rec) => [
    rec.session_id, sessionTitle(rec), rec.status, rec.exit_code,
    rec.ended_at || rec.updated_at || rec.created_at,
    rec.effective_model, rec.effective_thinking_level,
  ].join('|')).join('\n');
}

let historySig = '';

function renderHistoryList(list) {
  const entries = historyEntries();
  const sig = historySignature(entries);
  // R7: a poll that renders the same records must not rebuild the list — that is what threw away
  // the scroll position and the focused row every 2.5 s.
  if (sig === historySig && list.querySelector('.history-item, .list-empty')) return;
  historySig = sig;
  if (!entries.length) {
    list.innerHTML = '<div class="list-empty">暂无历史会话</div>';
    historyRows.clear();
    return;
  }
  list.querySelector('.list-empty')?.remove();
  if (list.querySelector('.session-card')) {
    list.innerHTML = '';
    historyRows.clear();
  }
  entries.forEach((rec, index) => {
    let row = historyRows.get(rec.session_id);
    if (!row) {
      const el = document.createElement('div');
      el.className = 'history-item';
      el.dataset.id = rec.session_id;
      el.tabIndex = 0;
      el.setAttribute('role', 'option');
      el.innerHTML = historyItemHtml(rec);
      row = { el, sig: historySignature([rec]) };
      historyRows.set(rec.session_id, row);
    } else {
      const rowSig = historySignature([rec]);
      if (row.sig !== rowSig) {
        row.el.innerHTML = historyItemHtml(rec);
        row.sig = rowSig;
      }
    }
    if (list.children[index] !== row.el) list.insertBefore(row.el, list.children[index] || null);
  });
  const keep = new Set(entries.map((rec) => rec.session_id));
  for (const [id, row] of historyRows) {
    if (keep.has(id)) continue;
    row.el.remove();
    historyRows.delete(id);
  }
}

function historyItemHtml(rec) {
  const model = [rec.effective_model, rec.effective_thinking_level].filter(Boolean).join(' · ');
  const when = formatWhen(rec.ended_at || rec.updated_at || rec.created_at);
  return `
      <div class="hi-row">
        <span class="status-dot" data-status="${escapeHtml(rec.status || '')}"></span>
        <span class="hi-title" title="${escapeHtml(sessionTitle(rec))}">${escapeHtml(sessionTitle(rec))}</span>
      </div>
      <div class="hi-meta">
        <span class="status-text" data-tone="${statusTone(rec.status)}">${escapeHtml(statusLabel(rec.status))}</span>
        ${when ? `<span class="dot-sep"></span><span>结束 ${escapeHtml(when)}</span>` : ''}
        ${model ? `<span class="dot-sep"></span><span class="hi-model" title="${escapeHtml(model)}">${escapeHtml(model)}</span>` : ''}
      </div>`;
}

async function onSessionListClick(ev) {
  if (sessionsMode === 'history') {
    const item = ev.target.closest('.history-item');
    if (item) openHistoryRecord(item.dataset.id);
    return;
  }
  const card = ev.target.closest('.session-card');
  if (!card) return;
  if (ev.target.closest('[data-action="close"]')) {
    ev.stopPropagation();
    await closeSession(card.dataset.id);
    return;
  }
  if (state.activeSessionId === card.dataset.id) focusActiveSession();
  else setActiveSession(card.dataset.id);
}

function onSessionListKeydown(ev) {
  if (sessionsMode === 'history') {
    const item = ev.target.closest('.history-item');
    if (!item || ev.target !== item) return;
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      openHistoryRecord(item.dataset.id);
    }
    return;
  }
  const card = ev.target.closest('.session-card');
  if (!card || ev.target !== card) return;
  if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    setActiveSession(card.dataset.id);
    focusActiveSession();
  } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    const next = ev.key === 'ArrowDown' ? card.nextElementSibling : card.previousElementSibling;
    next?.focus();
  } else if (ev.key === 'Delete') {
    ev.preventDefault();
    closeSession(card.dataset.id);
  }
}

// ---------------------------------------------------------------- workspace
function visibleSessions() {
  return state.layoutMode === 'single'
    ? state.sessions.filter((s) => s.session_id === state.activeSessionId)
    : state.sessions;
}

/**
 * R15: a pane created lazily goes to its newest-first slot — before the first existing pane of an
 * older session — while panes that are already attached are never moved.
 */
function insertPaneInOrder(grid, paneEl, sessionId) {
  const ranked = new Map(orderedSessions().map((s, index) => [s.session_id, index]));
  const rank = ranked.get(sessionId);
  if (rank === undefined) {
    grid.insertBefore(paneEl, grid.firstChild);
    return;
  }
  for (const child of grid.children) {
    if (!child.classList.contains('pane') || !child.dataset.id) continue;
    const childRank = ranked.get(child.dataset.id);
    if (childRank === undefined || childRank > rank) {
      grid.insertBefore(paneEl, child);
      return;
    }
  }
  grid.appendChild(paneEl);
}

function renderWorkspace() {
  const grid = $('#gridContainer');
  grid.className = `grid ${state.layoutMode === 'single' ? 'grid-single' : 'grid-matrix'}`;
  const visible = visibleSessions();
  const visibleIds = new Set(visible.map((s) => s.session_id));

  // Hidden panes stay in the DOM (display:none): detaching would reload web GUI iframes.
  for (const [id, pane] of panes) {
    if (!visibleIds.has(id)) {
      pane.el.hidden = true;
      stopStream(id);
    }
  }

  const placeholder = grid.querySelector('.empty-state, .loading-state');
  if (!visible.length) {
    const html = state.sessionsLoaded ? emptyStateHtml() : loadingStateHtml();
    if (!placeholder || placeholder.dataset.kind !== (state.sessionsLoaded ? 'empty' : 'loading')) {
      placeholder?.remove();
      grid.insertAdjacentHTML('beforeend', html);
    }
    return;
  }
  placeholder?.remove();

  // Walking oldest → newest and prepending keeps the DOM in newest-first order while
  // never moving a pane that is already attached (moving an iframe reloads it).
  for (const s of [...orderedSessions()].reverse()) {
    const isVisible = visibleIds.has(s.session_id);
    let pane = panes.get(s.session_id);
    if (!pane) {
      if (!isVisible) continue; // panes (and their terminals) are created lazily on first view
      pane = createPane(s);
      panes.set(s.session_id, pane);
    }
    pane.el.hidden = !isVisible;
    updatePane(pane, s);
    if (!pane.el.isConnected) insertPaneInOrder(grid, pane.el, s.session_id);
    if (isVisible && !pane.mounted) {
      mountPaneBody(pane, s);
      pane.mounted = true;
    }
    if (isVisible && s.kind === 'desktop_gui') ensureStream(s.session_id);
  }
  scheduleFitAll();
}

function loadingStateHtml() {
  return `<div class="loading-state" data-kind="loading"><span class="spinner"></span>正在连接 Hub…</div>`;
}

function emptyStateHtml() {
  return `
    <div class="empty-state" data-kind="empty">
      <div class="empty-illus">${icon('terminal', 'icon-xl')}</div>
      <h2 class="empty-title">还没有运行中的 Sub-Agent</h2>
      <p class="empty-desc">展开左侧「启动 Sub-Agent」选择 Profile 后启动，或让任意 MCP 客户端调用 <code>spawn_subagent</code>。</p>
      <div class="empty-actions">
        <button type="button" class="btn btn-primary" data-cmd="focus-launcher">${icon('play')}启动 Sub-Agent</button>
        <button type="button" class="btn" data-cmd="open-win32">${icon('monitor')}纳管桌面窗口</button>
        <button type="button" class="btn" data-cmd="open-mcp-guide">${icon('plug')}MCP 接入指南</button>
      </div>
    </div>`;
}

function createPane(s) {
  const el = document.createElement('section');
  el.className = 'pane';
  el.dataset.id = s.session_id;
  el.innerHTML = `
    <header class="pane-head" data-ref="head"></header>
    <div class="pane-body" data-ref="body"></div>
    <footer class="pane-foot">
      <span class="pf-status"><span class="status-dot" data-ref="dot"></span><span class="status-text" data-ref="status"></span></span>
      <span class="pf-step" data-ref="step"></span>
      <span class="pf-activity" data-ref="activity"></span>
      <span class="pf-cwd" data-ref="cwd">${icon('folder', 'icon-xs')}<span></span></span>
      <span class="pf-idle" data-ref="idle"></span>
    </footer>`;
  const ref = (name) => el.querySelector(`[data-ref="${name}"]`);
  return {
    el,
    head: ref('head'),
    body: ref('body'),
    refs: {
      dot: ref('dot'), status: ref('status'), step: ref('step'),
      activity: ref('activity'), cwd: ref('cwd'), idle: ref('idle'),
    },
    headSig: '',
    mounted: false,
  };
}

function paneHeadHtml(s) {
  const live = isLive(s);
  const wsOpen = !!terminals.get(s.session_id)?.wsOpen;
  const isDesktop = s.kind === 'desktop_gui';
  const keys = Object.entries(QUICK_KEYS).map(([key, k]) => {
    const enabled = live && wsOpen;
    const title = enabled ? k.title : '终端未连接';
    return `<button type="button" data-action="key" data-key="${key}" title="${escapeHtml(title)}" ${enabled ? '' : 'disabled'}>${k.label}</button>`;
  }).join('');
  const tuckButton = s.tucked_offscreen
    ? `<button type="button" class="btn btn-sm" data-action="tuck" data-tuck="false" title="把原窗口移回屏幕">${icon('eye', 'icon-sm')}<span class="btn-label">移回屏幕</span></button>`
    : `<button type="button" class="btn btn-sm" data-action="tuck" data-tuck="true" title="把原窗口移出屏幕，画面继续显示在总控台">${icon('archive', 'icon-sm')}<span class="btn-label">移出屏幕</span></button>`;
  return `
    <div class="pane-title">
      <span class="engine-badge" data-engine="${escapeHtml(engineKey(s))}">${escapeHtml(engineKey(s))}</span>
      <strong title="${escapeHtml(sessionTitle(s))}">${escapeHtml(sessionTitle(s))}</strong>
      ${pillsHtml(s)}
    </div>
    <div class="pane-actions">
      ${isDesktop ? tuckButton : `<div class="key-group" role="group" aria-label="快捷按键">${keys}</div>`}
      ${state.layoutMode === 'matrix'
        ? `<button type="button" class="icon-btn sm" data-action="focus-single" title="在单窗口视图中打开（或双击标题栏）" aria-label="在单窗口视图中打开">${icon('maximize')}</button>`
        : ''}
      <button type="button" class="icon-btn sm danger" data-action="close" title="${live ? '终止会话' : '移除'}" aria-label="${live ? '终止会话' : '移除'}">${icon('x')}</button>
    </div>`;
}

function pillsHtml(s) {
  const model = s.effective_model ? `<span class="pill pill-model" title="模型">${escapeHtml(s.effective_model)}</span>` : '';
  const effort = s.effective_thinking_level
    ? `<span class="pill pill-effort" title="思考等级">${icon('gauge')}${escapeHtml(s.effective_thinking_level)}</span>`
    : '';
  return model + effort;
}

function updatePane(pane, s) {
  const sig = [
    state.layoutMode, sessionTitle(s), s.status, s.process_alive, s.kind, s.engine,
    s.effective_model, s.effective_thinking_level, s.tucked_offscreen,
    terminals.get(s.session_id)?.wsOpen ? '1' : '0',
  ].join('|');
  if (sig !== pane.headSig) {
    pane.head.innerHTML = paneHeadHtml(s);
    pane.headSig = sig;
  }
  pane.el.dataset.status = s.status;
  pane.el.classList.toggle('focused', s.session_id === state.activeSessionId);
  pane.refs.dot.dataset.status = s.status;
  pane.refs.status.textContent = statusLabel(s.status);
  pane.refs.status.dataset.tone = statusTone(s.status);
  pane.refs.step.textContent = s.step_count ? `第 ${s.step_count} 步` : '';
  pane.refs.activity.textContent = activityText(s);
  pane.refs.activity.title = activityText(s);
  pane.refs.cwd.hidden = !s.cwd;
  pane.refs.cwd.title = s.cwd || '';
  pane.refs.cwd.querySelector('span').textContent = shortPath(s.cwd);
  pane.refs.idle.textContent = `空闲 ${formatDuration(s.idle_seconds)}`;
  updateWaitingBanner(pane, s);
}

function pendingQuestionText(s) {
  const interaction = s.pending_interaction;
  let text = '';
  if (typeof interaction === 'string') text = interaction;
  else if (interaction && typeof interaction === 'object') {
    text = interaction.question || interaction.text || interaction.prompt || interaction.title || '';
    const options = Array.isArray(interaction.options) ? interaction.options.filter(Boolean) : [];
    if (options.length) text += `\n选项：${options.map((opt, i) => `${i + 1}) ${opt}`).join('  ')}`;
  }
  text = String(text || '').trim();
  return text || activityText(s);
}

function updateWaitingBanner(pane, s) {
  const waiting = s.status === 'waiting_parent';
  let banner = pane.el.querySelector('.pane-banner');
  if (!waiting) {
    banner?.remove();
    return;
  }
  const question = pendingQuestionText(s);
  if (!banner) {
    banner = document.createElement('div');
    banner.className = 'pane-banner';
    banner.innerHTML = `
      <span class="pb-icon">${icon('message')}</span>
      <div class="pb-text"><div class="pb-title">等待主 Agent 回复</div><div class="pb-question"></div></div>
      <button type="button" class="icon-btn sm" data-action="copy-question" title="复制问题">${icon('copy')}</button>`;
    pane.el.insertBefore(banner, pane.body);
  }
  banner.querySelector('.pb-question').textContent = question;
  banner.dataset.question = question;
}

function safeGuiUrl(url) {
  try {
    const parsed = new URL(url, location.href);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
  } catch {
    return '';
  }
}

function mountPaneBody(pane, s) {
  const guiUrl = s.kind === 'web_gui' ? safeGuiUrl(s.gui_url) : '';
  if (guiUrl) {
    pane.body.innerHTML = `
      <div class="web-gui-split">
        <iframe src="${escapeHtml(guiUrl)}" title="${escapeHtml(sessionTitle(s))} Web GUI"></iframe>
        <div class="web-gui-term"></div>
      </div>`;
    attachTerminal(s.session_id, pane.body.querySelector('.web-gui-term'));
  } else if (s.kind === 'desktop_gui') {
    pane.body.innerHTML = `
      <div class="desktop-stream">
        <img alt="${escapeHtml(sessionTitle(s))} 窗口画面" draggable="false" hidden />
        <div class="stream-overlay"><span class="spinner"></span>正在获取窗口画面…</div>
      </div>`;
    pane.body.querySelector('img').addEventListener('click', (ev) => onDesktopClick(ev, s.session_id));
  } else {
    const slot = document.createElement('div');
    slot.className = 'term-slot';
    pane.body.appendChild(slot);
    attachTerminal(s.session_id, slot);
  }
}

function disposeGoneSessions() {
  const alive = new Set(state.sessions.map((s) => s.session_id));
  for (const [id, t] of terminals) {
    if (alive.has(id)) continue;
    t.disposed = true;
    clearTimeout(t.retryTimer);
    t.ro.disconnect();
    try { t.ws.close(); } catch { /* already closed */ }
    try { t.term.dispose(); } catch { /* already disposed */ }
    t.host.remove();
    terminals.delete(id);
  }
  for (const [id, pane] of panes) {
    if (alive.has(id)) continue;
    pane.el.remove();
    panes.delete(id);
  }
  for (const id of [...streams.keys()]) if (!alive.has(id)) stopStream(id);
  for (const [id, card] of cards) {
    if (alive.has(id)) continue;
    card.el.remove();
    cards.delete(id);
  }
}

// ---------------------------------------------------------------- terminals
function createTerminal(sessionId) {
  const host = document.createElement('div');
  host.className = 'term-host';
  const term = new window.Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, monospace',
    scrollback: 5000,
    theme: TERMINAL_THEME,
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);

  const t = {
    term, fit, host, ws: null, ro: null, opened: false, disposed: false, retries: 0, retryTimer: 0,
    wsOpen: false, replaying: false, replayPending: false, replayChunks: [], replayTimer: 0,
  };
  const sendResize = () => {
    if (t.opened && t.ws?.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    }
  };
  const connect = () => {
    const ws = new WebSocket(hubApi.ptySocketUrl(sessionId));
    t.ws = ws;
    ws.onopen = () => {
      // The server replays the whole history on every connect, so start from a clean screen.
      if (t.retries > 0) term.reset();
      t.retries = 0;
      t.wsOpen = true;
      // V3: the replay arrives first, then a replay_end marker. While the terminal is parsing the
      // history, the query replies xterm generates (DA / DSR) must not be typed into the session.
      t.replaying = true;
      t.replayPending = true;
      t.replayChunks = [];
      clearTimeout(t.replayTimer);
      // A hub that predates the marker never sends one; write what arrived and leave replay mode.
      t.replayTimer = setTimeout(() => finishReplay(t), 1500);
      sendResize();
      refreshPaneHead(sessionId);
    };
    ws.onmessage = (ev) => {
      const data = ev.data;
      // Q9: the end-of-replay marker is control data, never terminal output. Swallow it whatever the
      // replay flag says; before this fix a marker that arrived after the flag dropped was typed
      // into the terminal as JSON.
      if (isReplayEndFrame(data)) {
        finishReplay(t);
        return;
      }
      if (t.replayPending) {
        // Q1: hold the replay frames until the marker, so the terminal write and the end of the
        // reply-drop window can be one write callback. xterm parses writes asynchronously, so
        // clearing `replaying` when the marker frame arrives drops replies too early.
        t.replayChunks.push(data);
        return;
      }
      term.write(data);
    };
    ws.onclose = () => {
      t.wsOpen = false;
      refreshPaneHead(sessionId);
      if (t.disposed) return;
      const session = state.sessions.find((x) => x.session_id === sessionId);
      if (!session || !isLive(session)) {
        term.write('\r\n\x1b[90m── 终端连接已关闭 ──\x1b[0m\r\n');
        return;
      }
      if (t.retries === 0) term.write('\r\n\x1b[90m── 终端连接已断开，正在重连… ──\x1b[0m\r\n');
      t.retries += 1;
      t.retryTimer = setTimeout(() => { if (!t.disposed) connect(); }, Math.min(10000, 1000 * 2 ** (t.retries - 1)));
    };
  };
  term.onData((data) => {
    // V3: terminal queries in the replayed history make xterm answer by itself; those answers are
    // not user input. Live output still needs its answers, so only the replay window drops replies.
    if (t.replaying && TERMINAL_REPLY_RE.test(data)) return;
    if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({ type: 'input', data }));
  });
  term.onResize(sendResize);
  connect();

  t.ro = new ResizeObserver(() => scheduleFit(sessionId));
  t.ro.observe(host);
  terminals.set(sessionId, t);
  return t;
}

function refreshPaneHead(sessionId) {
  const pane = panes.get(sessionId);
  const session = state.sessions.find((x) => x.session_id === sessionId);
  if (pane && session) updatePane(pane, session);
}

function attachTerminal(sessionId, container) {
  const t = terminals.get(sessionId) || createTerminal(sessionId);
  container.appendChild(t.host);
  if (!t.opened) {
    t.term.open(t.host);
    t.opened = true;
  }
  scheduleFit(sessionId);
}

const fitQueue = new Set();
let fitFrame = 0;

function scheduleFit(sessionId) {
  fitQueue.add(sessionId);
  if (!fitFrame) fitFrame = requestAnimationFrame(flushFits);
}

function scheduleFitAll() {
  for (const id of terminals.keys()) fitQueue.add(id);
  if (!fitFrame) fitFrame = requestAnimationFrame(flushFits);
}

function flushFits() {
  fitFrame = 0;
  for (const id of fitQueue) {
    const t = terminals.get(id);
    if (!t || !t.opened || !t.host.isConnected || !t.host.clientWidth || !t.host.clientHeight) continue;
    try { t.fit.fit(); } catch { /* terminal not measurable yet */ }
  }
  fitQueue.clear();
}

function focusActiveSession() {
  const id = state.activeSessionId;
  const pane = panes.get(id);
  if (pane && state.layoutMode === 'matrix') pane.el.scrollIntoView({ block: 'nearest' });
  requestAnimationFrame(() => terminals.get(id)?.term.focus());
}

function onGlobalKeydown(ev) {
  if (!ev.altKey || (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown')) return;
  if (document.querySelector('.modal-backdrop:not([hidden])')) return;
  const ordered = orderedSessions();
  if (!ordered.length) return;
  ev.preventDefault();
  ev.stopPropagation();
  const delta = ev.key === 'ArrowDown' ? 1 : -1;
  const index = ordered.findIndex((s) => s.session_id === state.activeSessionId);
  let nextIndex;
  if (index < 0) nextIndex = delta > 0 ? 0 : ordered.length - 1;
  else nextIndex = Math.min(ordered.length - 1, Math.max(0, index + delta));
  if (nextIndex !== index) setActiveSession(ordered[nextIndex].session_id);
}

// ----------------------------------------------------------- desktop streams
function ensureStream(sessionId) {
  if (streams.has(sessionId)) return;
  const stream = { stopped: false, timer: 0 };
  streams.set(sessionId, stream);
  const tick = async () => {
    if (stream.stopped) return;
    const body = panes.get(sessionId)?.body;
    const img = body?.querySelector('img');
    const overlay = body?.querySelector('.stream-overlay');
    try {
      const frame = await hubApi.windowFrame(sessionId);
      if (frame.jpeg_b64 && img) {
        img.src = `data:image/jpeg;base64,${frame.jpeg_b64}`;
        img.dataset.winWidth = frame.width;
        img.dataset.winHeight = frame.height;
        img.hidden = false;
        if (overlay) overlay.hidden = true;
      }
    } catch (err) {
      if (overlay) {
        overlay.hidden = false;
        overlay.innerHTML = `${icon('alert')}${err.status === 410 ? '窗口已关闭或不可用' : '暂时无法获取窗口画面，正在重试…'}`;
      }
    }
    if (!stream.stopped) stream.timer = setTimeout(tick, 600);
  };
  tick();
}

function stopStream(sessionId) {
  const stream = streams.get(sessionId);
  if (!stream) return;
  stream.stopped = true;
  clearTimeout(stream.timer);
  streams.delete(sessionId);
}

async function onDesktopClick(ev, sessionId) {
  const img = ev.currentTarget;
  const rect = img.getBoundingClientRect();
  const winW = parseFloat(img.dataset.winWidth || img.naturalWidth || rect.width);
  const winH = parseFloat(img.dataset.winHeight || img.naturalHeight || rect.height);
  const x = Math.round(((ev.clientX - rect.left) / rect.width) * winW);
  const y = Math.round(((ev.clientY - rect.top) / rect.height) * winH);
  try {
    await hubApi.clickWindow(sessionId, x, y);
  } catch (err) {
    toast(`点击转发失败：${err.message}`, 'error');
  }
}

// ------------------------------------------------------------- pane actions
async function onWorkspaceClick(ev) {
  const paneEl = ev.target.closest('.pane');
  if (!paneEl) return;
  const id = paneEl.dataset.id;
  if (state.activeSessionId !== id) {
    state.activeSessionId = id;
    renderSessionList();
    for (const [pid, pane] of panes) pane.el.classList.toggle('focused', pid === id);
  }
  const action = ev.target.closest('[data-action]');
  if (!action) return;
  switch (action.dataset.action) {
    case 'key':
      sendKey(id, action.dataset.key);
      break;
    case 'close':
      await closeSession(id);
      break;
    case 'tuck':
      await toggleTuck(id, action.dataset.tuck === 'true', action);
      break;
    case 'focus-single':
      openInSingleView(id);
      break;
    case 'copy-question': {
      const question = paneEl.querySelector('.pane-banner')?.dataset.question || '';
      if (question) copyText(question, '已复制问题');
      break;
    }
    default:
      break;
  }
}

function onWorkspaceDblClick(ev) {
  if (state.layoutMode !== 'matrix' || ev.target.closest('button')) return;
  const head = ev.target.closest('.pane-head');
  if (head) openInSingleView(head.closest('.pane').dataset.id);
}

function openInSingleView(id) {
  setActiveSession(id);
  emit('layout:set', 'single');
  focusActiveSession();
}

/** Quick keys go through the pane's PTY WebSocket, never through the REST /input route. */
function sendKey(id, key) {
  const k = QUICK_KEYS[key];
  if (!k) return;
  const t = terminals.get(id);
  if (!t || t.ws?.readyState !== WebSocket.OPEN) {
    toast('终端未连接，无法发送按键', 'error', 2500);
    return;
  }
  t.ws.send(JSON.stringify({ type: 'input', data: k.text }));
  t.term.focus();
}

async function toggleTuck(id, tuck, button) {
  setBusy(button, true);
  try {
    const res = await hubApi.tuckWindow(id, tuck);
    if (res.tucked_offscreen !== tuck) toast('窗口状态没有改变，原窗口可能已关闭', 'error');
    emit('sessions:refresh-request');
  } catch (err) {
    toast(`操作失败：${err.message}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function closeSession(id) {
  const s = state.sessions.find((x) => x.session_id === id);
  if (!s) return;
  const live = isLive(s);
  const label = profileLabel(s);
  if (live) {
    const ok = await confirmDialog({
      title: '终止这个会话？',
      message: `「${label}」仍在运行。终止后进程会被结束，未完成的任务将中断。`,
      confirmText: '终止会话',
      danger: true,
    });
    if (!ok) return;
  }
  try {
    await hubApi.terminateSession(id);
    removeSession(id);
    emit('sessions:refresh-request');
    toast(live ? `已终止 ${label}` : `已移除 ${label}`, 'success', 2000);
  } catch (err) {
    toast(`关闭失败：${err.message}`, 'error');
  }
}

// ------------------------------------------------------------ history record
function openSessionOrHistory(id) {
  if (!id) return;
  if (state.sessions.some((s) => s.session_id === id)) {
    setActiveSession(id);
    return;
  }
  openHistoryRecord(id);
}

function openHistoryRecord(id) {
  if (!id) return;
  historyRecordId = id;
  const rec = state.persistedSessions.find((r) => r.session_id === id)
    || state.sessions.find((s) => s.session_id === id)
    || { session_id: id };
  renderHistoryMeta(rec, null);
  setHistoryText('#historyFinal', '加载中…');
  setHistoryText('#historySummary', '加载中…');
  openModal(HISTORY_MODAL);
  loadHistoryRecord(id, rec);
}

async function loadHistoryRecord(id, rec) {
  const [finalRes, summaryRes] = await Promise.allSettled([
    hubApi.sessionFinal(id),
    hubApi.transcriptSummary(id),
  ]);
  if (historyRecordId !== id) return;

  if (finalRes.status === 'fulfilled') {
    const final = finalRes.value || {};
    renderHistoryMeta(rec, final);
    setHistoryText('#historyFinal', String(final.final_response || '').trim() || '（暂无最终回复）');
  } else {
    setHistoryText('#historyFinal', `加载失败：${finalRes.reason?.message || finalRes.reason}`, true);
  }

  if (summaryRes.status === 'fulfilled') {
    const summary = summaryRes.value || {};
    const text = String(summary.digest_markdown || summary.latest_conclusion || '').trim();
    setHistoryText('#historySummary', text || '（暂无过程摘要）');
  } else {
    setHistoryText('#historySummary', `加载失败：${summaryRes.reason?.message || summaryRes.reason}`, true);
  }
}

function renderHistoryMeta(rec, final) {
  const model = [
    rec.effective_model ?? final?.effective_model,
    rec.effective_thinking_level ?? final?.effective_thinking_level,
  ].filter(Boolean).join(' · ');
  const status = rec.status || final?.lifecycle_status;
  const exitCode = rec.exit_code ?? final?.exit_code;
  const start = rec.created_at || final?.created_at;
  const end = rec.ended_at || final?.ended_at;
  const rows = [
    ['Profile', rec.profile_name || rec.profile_id || final?.profile_id || ''],
    ['模型', model],
    ['工作目录', rec.cwd || ''],
    ['开始', formatWhen(start)],
    ['结束', formatWhen(end)],
    ['退出码', exitCode === null || exitCode === undefined ? '' : String(exitCode)],
    ['状态', status ? statusLabel(status) : ''],
  ].filter(([, value]) => value !== '' && value !== null && value !== undefined);
  $('#historyMeta').innerHTML = `<dl class="kv-list">${rows
    .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${key === '工作目录' ? `<code>${escapeHtml(String(value))}</code>` : escapeHtml(String(value))}</dd>`)
    .join('')}</dl>`;
}

function setHistoryText(selector, text, isError = false) {
  const el = $(selector);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('is-error', isError);
}

function onHistoryModalClick(ev) {
  const btn = ev.target.closest('[data-action="copy-final"]');
  if (!btn) return;
  const text = $('#historyFinal')?.textContent || '';
  if (!text || text === '加载中…' || text === '（暂无最终回复）') {
    toast('没有可复制的最终回复', 'info', 2000);
    return;
  }
  copyText(text, '已复制最终回复');
}
