// Console entry point: top bar, layout, session polling and wiring of all panels.
import { hubApi } from './hub-api.js';
import { state, prefs, savePrefs, on, emit, reloadProfiles } from './console-state.js';
import { $, $$, toast, copyText, openModal, initModalSystem } from './ui-kit.js';
import { initSessionLauncher } from './session-launcher.js';
import { initSessionWorkspace } from './session-workspace.js';
import { initEventFeed } from './event-feed.js';
import { initProfileManager, openProfileManager } from './profile-manager.js';
import { initDesktopWindows, openDesktopWindows } from './desktop-windows.js';

const POLL_MS = 2500;
const POLL_HIDDEN_MS = 10000;

let pollTimer = 0;
let polling = false;
let refreshQueued = false;
let connected = null; // null = never polled, true / false afterwards

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
    state.recentEvents = data.recent_events || [];
    state.globalSeq = data.global_seq || 0;
    state.sessionsLoaded = true;
    if (!state.sessions.some((s) => s.session_id === state.activeSessionId)) {
      state.activeSessionId = state.sessions.at(-1)?.session_id ?? null;
    }
    setConnected(true);
    emit('sessions:changed');
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
  if (connected === true && !ok) toast('与 Hub 的连接已断开，正在自动重试…', 'error');
  if (connected === false && ok) toast('已重新连接到 Hub', 'success');
  connected = ok;
}

// -------------------------------------------------------------------- layout
function setLayout(mode) {
  state.layoutMode = mode === 'matrix' ? 'matrix' : 'single';
  savePrefs({ layoutMode: state.layoutMode });
  $$('[data-layout]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.layout === state.layoutMode)));
  emit('layout:changed');
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  savePrefs({ sidebarCollapsed: collapsed });
}

// ------------------------------------------------------------ global commands
const COMMANDS = {
  'open-profiles': () => openProfileManager(),
  'open-win32': () => openDesktopWindows(),
  'open-mcp-guide': () => openModal('mcpModal'),
  'toggle-sidebar': () => setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed')),
  'focus-launcher': () => {
    setSidebarCollapsed(false);
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
  document.body.classList.toggle('sidebar-collapsed', !!prefs.sidebarCollapsed);

  initSessionLauncher();
  initSessionWorkspace();
  initEventFeed();
  initProfileManager();
  initDesktopWindows();

  on('sessions:refresh-request', requestRefresh);
  on('layout:set', setLayout);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) requestRefresh();
  });

  await Promise.all([
    reloadProfiles(false).catch((err) => toast(`加载 Profile 失败：${err.message}`, 'error')),
    refreshSessions(),
  ]);
}

init();
