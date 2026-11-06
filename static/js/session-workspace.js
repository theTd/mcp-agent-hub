// Session list in the sidebar plus the terminal / web GUI / desktop panes in the workspace.
import { hubApi } from './hub-api.js';
import { state, on, emit, setActiveSession, removeSession } from './console-state.js';
import { $, escapeHtml, icon, formatDuration, shortPath, toast, confirmDialog, setBusy } from './ui-kit.js';

// ------------------------------------------------------------- status model
const STATUS = {
  starting: { label: '启动中', tone: 'info' },
  running: { label: '运行中', tone: 'success' },
  waiting_parent: { label: '等待主 Agent', tone: 'warning' },
  completed: { label: '已完成', tone: 'violet' },
  exited: { label: '已退出', tone: 'muted' },
  terminated: { label: '已终止', tone: 'danger' },
};
const LIVE_STATUSES = new Set(['starting', 'running', 'waiting_parent']);

const statusMeta = (status) => STATUS[status] || { label: status || '未知', tone: 'muted' };
const isLive = (status) => LIVE_STATUSES.has(status);
const engineKey = (s) => (s.kind === 'desktop_gui' ? 'desktop' : s.engine || s.kind || 'tui');

const QUICK_KEYS = {
  enter: { text: '\r', label: '⏎', title: '发送 Enter' },
  yes: { text: 'y\r', label: 'y⏎', title: '发送 y + Enter（确认）' },
  esc: { text: '\x1b', label: 'Esc', title: '发送 Esc' },
  interrupt: { text: '\x03', label: '^C', title: '发送 Ctrl+C（中断）' },
};

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
const panes = new Map(); // session_id -> { el, head, body, refs, headSig, mounted }
const terminals = new Map(); // session_id -> { term, fit, ws, host, ro, opened, disposed }
const streams = new Map(); // session_id -> { stopped, timer }

export function initSessionWorkspace() {
  $('#sessionList').addEventListener('click', onSessionListClick);
  $('#sessionList').addEventListener('keydown', onSessionListKeydown);
  $('#gridContainer').addEventListener('click', onWorkspaceClick);
  $('#gridContainer').addEventListener('dblclick', onWorkspaceDblClick);

  on('sessions:changed', renderAll);
  on('session:activated', () => {
    renderAll();
    focusActiveTerminal();
  });
  on('layout:changed', renderWorkspace);
  window.addEventListener('resize', scheduleFitAll);
  renderAll();
}

function renderAll() {
  disposeGoneSessions();
  renderSessionList();
  renderWorkspace();
}

// ------------------------------------------------------------- session list
function renderSessionList() {
  const list = $('#sessionList');
  const ordered = [...state.sessions].reverse();
  $('#sessionCount').textContent = String(state.sessions.length);
  renderSessionSummary();

  if (!ordered.length) {
    list.innerHTML = state.sessionsLoaded
      ? '<div class="list-empty">暂无会话</div>'
      : '';
    cards.clear();
    return;
  }
  list.querySelector('.list-empty')?.remove();

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
    const sig = [s.title, s.status, s.engine, s.kind, s.effective_model, s.effective_thinking_level].join('|');
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

function renderSessionSummary() {
  const live = state.sessions.filter((s) => isLive(s.status)).length;
  const waiting = state.sessions.filter((s) => s.status === 'waiting_parent').length;
  const el = $('#sessionSummary');
  if (!state.sessions.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `${live} 个运行中${waiting ? ` · <span class="warn">${waiting} 个待回复</span>` : ''}`;
}

function pillsHtml(s) {
  const model = s.effective_model ? `<span class="pill pill-model" title="模型">${escapeHtml(s.effective_model)}</span>` : '';
  const effort = s.effective_thinking_level
    ? `<span class="pill pill-effort" title="思考等级">${icon('gauge')}${escapeHtml(s.effective_thinking_level)}</span>`
    : '';
  return model + effort;
}

function sessionCardHtml(s) {
  const st = statusMeta(s.status);
  const pills = pillsHtml(s);
  return `
    <div class="sc-row">
      <span class="status-dot" data-status="${escapeHtml(s.status)}" title="${escapeHtml(st.label)}"></span>
      <span class="sc-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
      <button type="button" class="icon-btn sm danger sc-close" data-action="close"
        title="${isLive(s.status) ? '终止会话' : '移除会话'}" aria-label="${isLive(s.status) ? '终止会话' : '移除会话'}">${icon('x')}</button>
    </div>
    ${pills ? `<div class="sc-pills">${pills}</div>` : ''}
    <div class="sc-activity" data-ref="activity"></div>
    <div class="sc-meta">
      <span class="engine-badge" data-engine="${escapeHtml(engineKey(s))}">${escapeHtml(engineKey(s))}</span>
      <span class="status-text" data-tone="${st.tone}">${escapeHtml(st.label)}</span>
      <span class="dot-sep"></span>
      <span data-ref="idle"></span>
    </div>`;
}

function activityText(s) {
  if (s.current_activity) return s.current_activity;
  return s.last_task ? `任务：${s.last_task}` : '';
}

function idleText(s) {
  if (isLive(s.status)) return `空闲 ${formatDuration(s.idle_seconds)}`;
  return s.exit_code !== null && s.exit_code !== undefined ? `退出码 ${s.exit_code}` : s.session_id;
}

async function onSessionListClick(ev) {
  const card = ev.target.closest('.session-card');
  if (!card) return;
  if (ev.target.closest('[data-action="close"]')) {
    ev.stopPropagation();
    await closeSession(card.dataset.id);
    return;
  }
  if (state.activeSessionId === card.dataset.id) focusActiveTerminal();
  else setActiveSession(card.dataset.id);
}

function onSessionListKeydown(ev) {
  const card = ev.target.closest('.session-card');
  if (!card || ev.target !== card) return;
  if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    setActiveSession(card.dataset.id);
    focusActiveTerminal();
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

  state.sessions.forEach((s, index) => {
    const isVisible = visibleIds.has(s.session_id);
    let pane = panes.get(s.session_id);
    if (!pane) {
      if (!isVisible) return; // panes (and their terminals) are created lazily on first view
      pane = createPane(s);
      panes.set(s.session_id, pane);
    }
    pane.el.hidden = !isVisible;
    updatePane(pane, s);
    // Insert new panes only; existing ones are never moved (moving an iframe reloads it).
    if (!pane.el.isConnected) grid.insertBefore(pane.el, grid.children[index] || null);
    if (isVisible && !pane.mounted) {
      mountPaneBody(pane, s);
      pane.mounted = true;
    }
    if (isVisible && s.kind === 'desktop_gui') ensureStream(s.session_id);
  });
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
      <p class="empty-desc">在左侧选择 Profile 后点击「拉起 Agent」，或让任意 MCP 客户端调用 <code>spawn_subagent</code>。所有终端与 GUI 窗口都会集中显示在这里。</p>
      <div class="empty-actions">
        <button type="button" class="btn btn-primary" data-cmd="focus-launcher">${icon('play')}拉起 Agent</button>
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
    refs: { dot: ref('dot'), status: ref('status'), step: ref('step'), activity: ref('activity'), cwd: ref('cwd'), idle: ref('idle') },
    headSig: '',
    mounted: false,
  };
}

function paneHeadHtml(s) {
  const live = isLive(s.status);
  const isDesktop = s.kind === 'desktop_gui';
  const keys = Object.entries(QUICK_KEYS).map(([key, k]) =>
    `<button type="button" data-action="key" data-key="${key}" title="${k.title}" ${live ? '' : 'disabled'}>${k.label}</button>`).join('');
  const tuckButton = s.tucked_offscreen
    ? `<button type="button" class="btn btn-sm" data-action="tuck" data-tuck="false" title="把原窗口移回桌面">${icon('eye', 'icon-sm')}<span class="btn-label">恢复桌面显示</span></button>`
    : `<button type="button" class="btn btn-sm" data-action="tuck" data-tuck="true" title="把原窗口收纳到屏幕外，后台继续渲染">${icon('archive', 'icon-sm')}<span class="btn-label">收纳原窗口</span></button>`;
  return `
    <div class="pane-title">
      <span class="engine-badge" data-engine="${escapeHtml(engineKey(s))}">${escapeHtml(engineKey(s))}</span>
      <strong title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</strong>
      ${pillsHtml(s)}
    </div>
    <div class="pane-actions">
      ${isDesktop ? tuckButton : `<div class="key-group" role="group" aria-label="快捷按键">${keys}</div>`}
      ${state.layoutMode === 'matrix'
        ? `<button type="button" class="icon-btn sm" data-action="focus-single" title="在单窗口视图中打开（或双击标题栏）" aria-label="在单窗口视图中打开">${icon('maximize')}</button>`
        : ''}
      <button type="button" class="icon-btn sm danger" data-action="close" title="${live ? '终止会话' : '移除会话'}" aria-label="${live ? '终止会话' : '移除会话'}">${icon('x')}</button>
    </div>`;
}

function updatePane(pane, s) {
  const sig = [state.layoutMode, s.title, s.status, s.kind, s.engine, s.effective_model, s.effective_thinking_level, s.tucked_offscreen].join('|');
  if (sig !== pane.headSig) {
    pane.head.innerHTML = paneHeadHtml(s);
    pane.headSig = sig;
  }
  const st = statusMeta(s.status);
  pane.el.dataset.status = s.status;
  pane.el.classList.toggle('focused', s.session_id === state.activeSessionId);
  pane.refs.dot.dataset.status = s.status;
  pane.refs.status.textContent = st.label;
  pane.refs.status.dataset.tone = st.tone;
  pane.refs.step.textContent = s.step_count ? `第 ${s.step_count} 步` : '';
  pane.refs.activity.textContent = activityText(s);
  pane.refs.activity.title = activityText(s);
  pane.refs.cwd.hidden = !s.cwd;
  pane.refs.cwd.title = s.cwd || '';
  pane.refs.cwd.querySelector('span').textContent = shortPath(s.cwd);
  pane.refs.idle.textContent = idleText(s);
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
        <iframe src="${escapeHtml(guiUrl)}" title="${escapeHtml(s.title)} Web GUI"></iframe>
        <div class="web-gui-term"></div>
      </div>`;
    attachTerminal(s.session_id, pane.body.querySelector('.web-gui-term'));
  } else if (s.kind === 'desktop_gui') {
    pane.body.innerHTML = `
      <div class="desktop-stream">
        <img alt="${escapeHtml(s.title)} 窗口画面" draggable="false" hidden />
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

  const t = { term, fit, host, ws: null, ro: null, opened: false, disposed: false, retries: 0, retryTimer: 0 };
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
      sendResize();
    };
    ws.onmessage = (ev) => term.write(ev.data);
    ws.onclose = () => {
      if (t.disposed) return;
      const session = state.sessions.find((x) => x.session_id === sessionId);
      if (!session || !isLive(session.status)) {
        term.write('\r\n\x1b[90m── 终端连接已关闭 ──\x1b[0m\r\n');
        return;
      }
      if (t.retries === 0) term.write('\r\n\x1b[90m── 终端连接已断开，正在重连… ──\x1b[0m\r\n');
      t.retries += 1;
      t.retryTimer = setTimeout(() => { if (!t.disposed) connect(); }, Math.min(10000, 1000 * 2 ** (t.retries - 1)));
    };
  };
  term.onData((data) => {
    if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({ type: 'input', data }));
  });
  term.onResize(sendResize);
  connect();

  t.ro = new ResizeObserver(() => scheduleFit(sessionId));
  t.ro.observe(host);
  terminals.set(sessionId, t);
  return t;
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

function focusActiveTerminal() {
  requestAnimationFrame(() => terminals.get(state.activeSessionId)?.term.focus());
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
      await sendKey(id, action.dataset.key);
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
  focusActiveTerminal();
}

async function sendKey(id, key) {
  const k = QUICK_KEYS[key];
  if (!k) return;
  try {
    await hubApi.sendInput(id, k.text);
  } catch (err) {
    toast(`发送按键失败：${err.message}`, 'error');
  }
  terminals.get(id)?.term.focus();
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
  const live = isLive(s.status);
  if (live) {
    const ok = await confirmDialog({
      title: '终止这个会话？',
      message: `「${s.title}」仍在运行。终止后进程会被结束，未完成的任务将中断。`,
      confirmText: '终止会话',
      danger: true,
    });
    if (!ok) return;
  }
  try {
    await hubApi.terminateSession(id);
    removeSession(id);
    emit('sessions:refresh-request');
    toast(live ? `已终止 ${s.title}` : `已移除 ${s.title}`, 'success', 2000);
  } catch (err) {
    toast(`关闭失败：${err.message}`, 'error');
  }
}
