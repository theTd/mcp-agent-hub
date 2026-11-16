// 会话总览: the live session list as a grid of tiles over the workspace area.
// It has its own switch (state.sessionListView, saved in prefs) and never changes the workspace
// layout: the 单窗口 / 平铺 panes stay mounted underneath while the board is open.
import { state, on, emit, prefs, savePrefs, setActiveSession } from './console-state.js';
import { $, $$, escapeHtml, icon } from './ui-kit.js';
import { statusLabel, statusTone } from './session-status-text.js';
import {
  isLive, engineKey, sessionTitle, formatRuntimeHms, cwdBase, modelLine, bypassBadgeHtml,
  activityText, idleText, pendingQuestionText,
} from './session-card-fields.js';

const GROUPS = [
  { key: 'waiting', label: '待回复' },
  { key: 'running', label: '运行中' },
  { key: 'ended', label: '已结束' },
];
const FILTERS = [{ key: 'all', label: '全部' }, ...GROUPS];
const TASK_MAX = 400;
const ANSWER_MAX = 600;

let filter = 'all';
let query = '';
let chipsSig = '';
const tiles = new Map(); // session_id -> { el, sig }
const sections = new Map(); // group key -> { el, title, grid }

export function isBoardOpen() {
  return state.sessionListView === 'board';
}

export function initSessionBoard() {
  state.sessionListView = prefs.sessionListView === 'board' ? 'board' : 'list';
  $('#sessionBoardChips').addEventListener('click', onChipClick);
  $('#sessionBoardFilter').addEventListener('input', (ev) => {
    query = ev.target.value.trim().toLowerCase();
    render();
  });
  $('#sessionBoardBody').addEventListener('click', onBodyClick);
  $('#sessionBoardBody').addEventListener('keydown', onBodyKeydown);
  document.addEventListener('keydown', onEscape);
  on('sessions:changed', () => { if (isBoardOpen()) render(); });
  on('session:activated', () => { if (isBoardOpen()) markActive(); });
  // Opening a session from elsewhere (event feed, a link) means the user wants to see it.
  on('session:open', () => setBoardOpen(false));
  setInterval(tickClocks, 1000);
  apply({ moveFocus: false });
}

export function setBoardOpen(open) {
  const view = open ? 'board' : 'list';
  if (state.sessionListView === view) return;
  state.sessionListView = view;
  savePrefs({ sessionListView: view });
  apply({ moveFocus: open });
  emit('board:changed', open);
}

function apply({ moveFocus }) {
  const open = isBoardOpen();
  document.body.classList.toggle('board-open', open);
  $('#sessionBoard').hidden = !open;
  $$('[data-cmd="toggle-session-board"]').forEach((b) => b.setAttribute('aria-pressed', String(open)));
  if (!open) return;
  render();
  // Keys must not keep going to a terminal that is now hidden under the board.
  if (moveFocus) $('#sessionBoard').focus({ preventScroll: true });
}

// ------------------------------------------------------------------ grouping
function groupOf(s) {
  if (s.status === 'waiting_parent') return 'waiting';
  return isLive(s) ? 'running' : 'ended';
}

function matches(s) {
  if (!query) return true;
  return [sessionTitle(s), s.engine, s.profile_name, s.profile_id, s.effective_model, s.cwd, s.session_id, s.last_task]
    .filter(Boolean).join(' ').toLowerCase().includes(query);
}

// -------------------------------------------------------------------- render
function render() {
  const all = [...state.sessions].reverse(); // newest first, like the sidebar list
  const counts = { all: all.length, waiting: 0, running: 0, ended: 0 };
  for (const s of all) counts[groupOf(s)] += 1;
  renderChips(counts);

  const alive = new Set(all.map((s) => s.session_id));
  for (const [id, tile] of tiles) {
    if (alive.has(id)) continue;
    tile.el.remove();
    tiles.delete(id);
  }

  const body = $('#sessionBoardBody');
  const shown = all.filter((s) => (filter === 'all' || groupOf(s) === filter) && matches(s));
  if (!shown.length) {
    for (const sec of sections.values()) sec.el.remove();
    renderEmpty(body, all.length);
    return;
  }
  body.querySelector('.board-empty')?.remove();

  let slot = 0;
  for (const g of GROUPS) {
    const items = shown.filter((s) => groupOf(s) === g.key);
    const sec = sectionFor(g.key);
    if (!items.length) {
      sec.el.remove();
      continue;
    }
    sec.title.textContent = `${g.label}（${items.length}）`;
    if (body.children[slot] !== sec.el) body.insertBefore(sec.el, body.children[slot] || null);
    slot += 1;
    items.forEach((s, i) => {
      const tile = tileFor(s);
      if (sec.grid.children[i] !== tile.el) sec.grid.insertBefore(tile.el, sec.grid.children[i] || null);
    });
    // Whatever is left past the end belongs to another group now or is filtered out.
    while (sec.grid.children.length > items.length) sec.grid.lastElementChild.remove();
  }
}

function renderChips(counts) {
  const sig = `${filter}|${counts.all}|${counts.waiting}|${counts.running}|${counts.ended}`;
  if (sig === chipsSig) return;
  chipsSig = sig;
  $('#sessionBoardChips').innerHTML = FILTERS.map((f) => `
    <button type="button" data-filter="${f.key}" aria-pressed="${filter === f.key}">
      ${f.label}<span class="count-badge">${counts[f.key]}</span>
    </button>`).join('');
}

function renderEmpty(body, total) {
  let text;
  if (!state.sessionsLoaded) text = '正在连接 Hub…';
  else if (!total) text = '还没有会话。展开左侧「启动 Sub-Agent」，或让 MCP 客户端调用 spawn_subagent。';
  else text = '没有符合筛选条件的会话';
  let el = body.querySelector('.board-empty');
  if (!el) {
    el = document.createElement('div');
    el.className = 'board-empty';
    body.appendChild(el);
  }
  el.textContent = text;
}

function sectionFor(key) {
  let sec = sections.get(key);
  if (!sec) {
    const el = document.createElement('section');
    el.className = 'board-group';
    el.dataset.group = key;
    el.innerHTML = '<h3 class="board-group-title"></h3><div class="board-grid" role="list"></div>';
    sec = { el, title: el.querySelector('.board-group-title'), grid: el.querySelector('.board-grid') };
    sections.set(key, sec);
  }
  return sec;
}

function tileFor(s) {
  let tile = tiles.get(s.session_id);
  if (!tile) {
    const el = document.createElement('article');
    el.className = 'board-tile';
    el.dataset.id = s.session_id;
    el.tabIndex = 0;
    el.setAttribute('role', 'listitem');
    tile = { el, sig: '' };
    tiles.set(s.session_id, tile);
  }
  const group = groupOf(s);
  // Fields that change every poll (activity, step, idle, run time) are written in place below,
  // so a tile is only rebuilt when its shape changes and keyboard focus inside it survives polls.
  const sig = [
    group, sessionTitle(s), s.status, s.process_alive, engineKey(s), s.high_risk_approval,
    s.last_task, s.pending_question_id, group === 'waiting' ? pendingQuestionText(s) : '',
    s.final_response, s.cwd,
  ].join('|');
  if (sig !== tile.sig) {
    tile.el.innerHTML = tileHtml(s, group);
    tile.sig = sig;
  }
  tile.el.dataset.group = group;
  tile.el.classList.toggle('active', s.session_id === state.activeSessionId);
  fillLiveFields(tile.el, s);
  return tile;
}

function clip(text, max) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function contextPercent(s) {
  const win = Number(s.context_window?.window_tokens);
  const used = Number(s.context_window?.used_tokens);
  if (!Number.isFinite(win) || win <= 0 || !Number.isFinite(used)) return null;
  return Math.min(100, Math.max(0, Math.round((used / win) * 100)));
}

function tileHtml(s, group) {
  const live = isLive(s);
  const title = sessionTitle(s);
  const task = clip(s.last_task, TASK_MAX);
  const answer = group !== 'waiting' ? clip(s.final_response, ANSWER_MAX) : '';
  const folder = cwdBase(s.cwd);
  const model = modelLine(s);
  let middle = '';
  if (group === 'waiting') {
    middle = `
      <div class="bt-question">
        <p title="${escapeHtml(pendingQuestionText(s))}">${escapeHtml(pendingQuestionText(s))}</p>
        <button type="button" class="btn btn-sm btn-primary" data-action="reply">${icon('message', 'icon-sm')}去回复</button>
      </div>`;
  } else if (answer) {
    middle = `<p class="bt-answer" title="${escapeHtml(answer)}"><span class="bt-label">回复</span>${escapeHtml(answer)}</p>`;
  }
  return `
    <header class="bt-head">
      <span class="status-dot" data-status="${escapeHtml(s.status)}" title="${escapeHtml(statusLabel(s.status))}"></span>
      <span class="bt-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
      <span class="engine-badge" data-engine="${escapeHtml(engineKey(s))}">${escapeHtml(engineKey(s))}</span>
      ${bypassBadgeHtml(s)}
      <button type="button" class="icon-btn sm danger bt-close" data-action="close"
        title="${live ? '终止会话' : '移除'}" aria-label="${live ? '终止会话' : '移除'}">${icon('x')}</button>
    </header>
    ${task ? `<p class="bt-task" title="${escapeHtml(task)}"><span class="bt-label">任务</span>${escapeHtml(task)}</p>` : ''}
    ${live ? '<p class="bt-activity"><span class="bt-label">当前</span><span data-ref="activity"></span></p>' : ''}
    ${middle}
    <footer class="bt-foot">
      <div class="bt-meta">
        <span class="status-text" data-tone="${statusTone(s.status)}">${escapeHtml(statusLabel(s.status))}</span>
        ${model ? `<span class="bt-model bt-sep" title="${escapeHtml(model)}">${escapeHtml(model)}</span>` : ''}
        <span data-ref="step"></span>
        <span data-ref="context"></span>
        <span data-ref="files"></span>
      </div>
      <div class="bt-meta">
        <span class="sc-meta-duration" data-ref="duration" title="运行时长"></span>
        <span data-ref="idle"></span>
        ${folder ? `<span class="bt-folder bt-sep" title="${escapeHtml(s.cwd || '')}">${icon('folder', 'icon-xs')}${escapeHtml(folder)}</span>` : ''}
      </div>
    </footer>`;
}

function setRef(el, name, text, sep = true) {
  const node = el.querySelector(`[data-ref="${name}"]`);
  if (!node) return;
  node.textContent = text;
  node.hidden = !text;
  // The meta separators are drawn by each field's own ::before so a hidden field leaves no gap
  // (and a field that wraps to the start of a line has its dot clipped, see .bt-meta in the CSS).
  node.classList.toggle('bt-sep', sep && !!text);
}

function fillLiveFields(el, s) {
  const activity = activityText(s);
  setRef(el, 'activity', activity, false);
  const activityRow = el.querySelector('.bt-activity');
  if (activityRow) activityRow.hidden = !activity;
  setRef(el, 'step', s.step_count ? `第 ${s.step_count} 步` : '');
  const pct = contextPercent(s);
  setRef(el, 'context', pct === null ? '' : `上下文 ${pct}%`);
  const files = Array.isArray(s.files_changed) ? s.files_changed.length : 0;
  setRef(el, 'files', files ? `改动 ${files} 个文件` : '');
  setRef(el, 'duration', formatRuntimeHms(s), false);
  setRef(el, 'idle', idleText(s));
}

function markActive() {
  for (const [id, tile] of tiles) tile.el.classList.toggle('active', id === state.activeSessionId);
}

function tickClocks() {
  if (!isBoardOpen() || document.hidden) return;
  for (const s of state.sessions) {
    const tile = tiles.get(s.session_id);
    if (tile) setRef(tile.el, 'duration', formatRuntimeHms(s), false);
  }
}

// ------------------------------------------------------------------ actions
function onChipClick(ev) {
  const chip = ev.target.closest('[data-filter]');
  if (!chip) return;
  filter = chip.dataset.filter;
  chipsSig = '';
  render();
}

function onBodyClick(ev) {
  const el = ev.target.closest('.board-tile');
  if (!el) return;
  if (ev.target.closest('[data-action="close"]')) {
    ev.stopPropagation();
    emit('session:close-request', el.dataset.id);
    return;
  }
  openFromBoard(el.dataset.id, { reply: !!ev.target.closest('[data-action="reply"]') });
}

function onBodyKeydown(ev) {
  const el = ev.target.closest('.board-tile');
  if (!el || ev.target !== el) return;
  if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    openFromBoard(el.dataset.id);
  }
}

/** A tile opens its session in whichever workspace layout is set; the board closes. */
function openFromBoard(id, { reply = false } = {}) {
  setBoardOpen(false);
  setActiveSession(id);
  emit('session:focus-request', { id, reply });
}

function onEscape(ev) {
  if (ev.key !== 'Escape' || !isBoardOpen()) return;
  if (document.querySelector('.modal-backdrop:not([hidden])')) return;
  const input = $('#sessionBoardFilter');
  if (ev.target === input && input.value) {
    // The first Esc clears the filter, the next one closes the board.
    input.value = '';
    query = '';
    render();
    return;
  }
  ev.preventDefault();
  setBoardOpen(false);
}
