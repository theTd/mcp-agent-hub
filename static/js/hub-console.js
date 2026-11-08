// Console entry point: top bar, layout, session polling and wiring of all panels.
import { hubApi } from './hub-api.js';
import { state, prefs, savePrefs, on, emit, setHubConnected, reloadProfiles } from './console-state.js';
import { $, $$, toast, copyText, openModal, initModalSystem, icon } from './ui-kit.js';
import { initSessionLauncher, expandLauncher } from './session-launcher.js';
import { initSessionWorkspace } from './session-workspace.js';
import { initEventFeed } from './event-feed.js';
import { initProfileManager, openProfileManager } from './profile-manager.js';
import { initDesktopWindows, openDesktopWindows } from './desktop-windows.js';

const POLL_MS = 2500;
const POLL_HIDDEN_MS = 10000;

let pollTimer = 0;
let polling = false;
let refreshQueued = false;
let deepLinkHandled = false;
let pendingOpenSessionId = null;

// ------------------------------------------------------------------- polling
async function refreshSessions() {
  clearTimeout(pollTimer);
  if (polling) {
    refreshQueued = true;
    return;
  }
  polling = true;
  const mutationSeq = state.localMutationSeq;
  try {
    const data = await hubApi.listSessions();
    if (mutationSeq !== state.localMutationSeq) {
      // The list changed locally while this request was in flight; its answer may be stale.
      refreshQueued = true;
      return;
    }
    state.sessions = data.sessions || [];
    state.persistedSessions = data.persisted_sessions || [];
    state.archiveTotal = Number.isFinite(data.archive_total) ? data.archive_total : state.archiveTotal;
    state.recentEvents = data.recent_events || [];
    state.globalSeq = data.global_seq || 0;
    state.sessionsLoaded = true;
    if (!state.sessions.some((s) => s.session_id === state.activeSessionId)) {
      state.activeSessionId = state.sessions.at(-1)?.session_id ?? null;
    }
    setConnected(true);
    emit('sessions:changed');
    handleDeepLink();
  } catch (err) {
    console.error('[hub-console] session poll failed', err);
    setConnected(false);
  } finally {
    polling = false;
    const delay = refreshQueued ? 0 : document.hidden ? POLL_HIDDEN_MS : POLL_MS;
    refreshQueued = false;
    pollTimer = setTimeout(refreshSessions, delay);
  }
}

function requestRefresh() {
  if (polling) refreshQueued = true;
  else refreshSessions();
}

function setConnected(ok) {
  const dot = $('#connDot');
  dot.dataset.state = ok ? 'ok' : 'error';
  $('#endpointChip').title = ok ? '已连接 Hub · 点击复制 MCP 接入地址' : '与 Hub 的连接已断开，正在重试…';
  updateConnBanner(ok);
  setHubConnected(ok);
}

/** F6: one persistent banner in the workspace instead of toasts for every failed poll. */
function updateConnBanner(ok) {
  const banner = $('#connBanner');
  if (!banner) return;
  if (ok === true) {
    banner.hidden = true;
    banner.textContent = '';
    return;
  }
  banner.hidden = false;
  banner.dataset.tone = ok === false ? 'danger' : 'pending';
  banner.innerHTML = ok === false
    ? `${icon('alert')}<span>与 Hub 的连接已断开，正在重试…</span>`
    : `${icon('info')}<span>正在连接 Hub…</span>`;
}

/** F3: ?session=<id> and window.openHubSession(id) activate that session and keep the URL in sync. */
function handleDeepLink() {
  if (!state.sessionsLoaded) return;
  if (!deepLinkHandled) {
    deepLinkHandled = true;
    const id = new URL(location.href).searchParams.get('session');
    if (id) pendingOpenSessionId = id;
  }
  if (pendingOpenSessionId) {
    const targetId = pendingOpenSessionId;
    pendingOpenSessionId = null;
    emit('session:open', targetId);
  }
}

function openHubSession(id) {
  const sessionId = String(id || '').trim();
  if (!sessionId) return;
  try {
    const nextUrl = new URL(location.href);
    nextUrl.searchParams.set('session', sessionId);
    history.replaceState(null, '', nextUrl);
  } catch { /* ignore history errors */ }
  if (state.sessionsLoaded && state.sessions.some((s) => s.session_id === sessionId)) {
    pendingOpenSessionId = null;
    emit('session:open', sessionId);
    requestRefresh();
  } else {
    pendingOpenSessionId = sessionId;
    requestRefresh();
  }
}

window.openHubSession = openHubSession;

// -------------------------------------------------------------------- layout
function setLayout(mode) {
  state.layoutMode = mode === 'matrix' ? 'matrix' : 'single';
  savePrefs({ layoutMode: state.layoutMode });
  $$('[data-layout]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.layout === state.layoutMode)));
  emit('layout:changed');
}

const SIDEBAR_MIN_W = 220;
const SIDEBAR_DEFAULT_W = 320;
const SIDEBAR_MAX_W = 680;

function clampSidebarWidth(px) {
  const maxByViewport = Math.max(SIDEBAR_MIN_W, Math.floor((window.innerWidth || 1200) * 0.65));
  const upper = Math.min(SIDEBAR_MAX_W, maxByViewport);
  const val = Number(px);
  if (!Number.isFinite(val)) return SIDEBAR_DEFAULT_W;
  return Math.max(SIDEBAR_MIN_W, Math.min(upper, Math.round(val)));
}

function applySidebarWidth(px, { persist = false, notify = true } = {}) {
  const w = clampSidebarWidth(px);
  document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
  if (persist) savePrefs({ sidebarWidth: w });
  if (notify) emit('layout:resized');
  return w;
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  savePrefs({ sidebarCollapsed: collapsed });
  emit('layout:resized');
  setTimeout(() => emit('layout:resized'), 220);
}

function initSidebarResizer() {
  const resizer = $('#sidebarResizer');
  const sidebar = $('#sidebar');
  if (!resizer || !sidebar) return;

  sidebar.addEventListener('transitionend', (ev) => {
    if (ev.propertyName === 'margin-left' || ev.propertyName === 'width') {
      emit('layout:resized');
    }
  });

  let dragging = false;
  let pointerId = null;

  const finishDrag = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('is-resizing-sidebar');
    if (pointerId !== null) {
      try { resizer.releasePointerCapture(pointerId); } catch { /* ignore */ }
      pointerId = null;
    }
    const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10);
    applySidebarWidth(current || prefs.sidebarWidth || SIDEBAR_DEFAULT_W, { persist: true, notify: true });
  };

  resizer.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    dragging = true;
    pointerId = ev.pointerId;
    document.body.classList.add('is-resizing-sidebar');
    try { resizer.setPointerCapture(ev.pointerId); } catch { /* ignore */ }
  });

  resizer.addEventListener('pointermove', (ev) => {
    if (!dragging) return;
    const rect = sidebar.getBoundingClientRect();
    applySidebarWidth(ev.clientX - rect.left, { persist: false, notify: true });
  });

  resizer.addEventListener('pointerup', finishDrag);
  resizer.addEventListener('pointercancel', finishDrag);
  window.addEventListener('blur', finishDrag);

  resizer.addEventListener('dblclick', () => {
    applySidebarWidth(SIDEBAR_DEFAULT_W, { persist: true, notify: true });
  });

  resizer.addEventListener('keydown', (ev) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const step = ev.shiftKey ? 40 : 16;
    const delta = ev.key === 'ArrowRight' ? step : -step;
    const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10)
      || prefs.sidebarWidth
      || SIDEBAR_DEFAULT_W;
    applySidebarWidth(current + delta, { persist: true, notify: true });
  });

  window.addEventListener('resize', () => {
    const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10)
      || prefs.sidebarWidth
      || SIDEBAR_DEFAULT_W;
    applySidebarWidth(current, { persist: false, notify: true });
  });
}

// ------------------------------------------------------------ global commands
const COMMANDS = {
  'open-profiles': () => openProfileManager(),
  'open-win32': () => openDesktopWindows(),
  'open-mcp-guide': () => openModal('mcpModal'),
  'toggle-sidebar': () => setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed')),
  'focus-launcher': () => {
    setSidebarCollapsed(false);
    expandLauncher();
    $('#taskInput').focus();
  },
  'refresh-sessions': () => requestRefresh(),
  'copy-endpoint': () => copyText(hubApi.mcpEndpointUrl(), '已复制 MCP 接入地址'),
  'copy-target': (el) => copyText($(el.dataset.target)?.textContent || ''),
};

function initTopbar() {
  const endpoint = hubApi.mcpEndpointUrl();
  $('#endpointUrl').textContent = endpoint;
  $('#mcpConfigSnippet').textContent = JSON.stringify(
    { mcpServers: { 'subagent-hub': { type: 'http', url: endpoint } } },
    null,
    2,
  );

  document.addEventListener('click', (ev) => {
    const el = ev.target.closest('[data-cmd]');
    if (el) COMMANDS[el.dataset.cmd]?.(el);
  });
  $$('[data-layout]').forEach((b) => b.addEventListener('click', () => setLayout(b.dataset.layout)));
}

// ---------------------------------------------------------------------- init
async function init() {
  initModalSystem();
  initTopbar();

  state.layoutMode = prefs.layoutMode === 'matrix' ? 'matrix' : 'single';
  $$('[data-layout]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.layout === state.layoutMode)));
  applySidebarWidth(prefs.sidebarWidth || SIDEBAR_DEFAULT_W, { persist: false, notify: false });
  document.body.classList.toggle('sidebar-collapsed', !!prefs.sidebarCollapsed);
  initSidebarResizer();

  initSessionLauncher();
  initSessionWorkspace();
  initEventFeed();
  initProfileManager();
  initDesktopWindows();

  on('sessions:refresh-request', requestRefresh);
  on('layout:set', setLayout);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      requestRefresh();
      emit('layout:resized');
    }
  });

  updateConnBanner(null); // 正在连接 Hub…
  await Promise.all([
    reloadProfiles(false).catch((err) => toast(`加载 Profile 失败：${err.message}`, 'error')),
    refreshSessions(),
  ]);
}

init();
