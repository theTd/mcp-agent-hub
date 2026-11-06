// Sidebar launcher (启动 Sub-Agent): pick a Profile, model and thinking level, preview the exact
// command the hub will run, then start the session in the console (spec F1).
import { hubApi } from './hub-api.js';
import {
  state, prefs, savePrefs, on, emit, engineModels, engineDefaultModel, setActiveSession, upsertSession,
} from './console-state.js';
import { $, $$, escapeHtml, setBusy, toast } from './ui-kit.js';

const MAX_RECENT_CWDS = 8;
const PREVIEW_DEBOUNCE_MS = 250;

// idle = nothing to show, ok = preview answered, error = server said ok:false, failed = request failed
let preview = { status: 'idle', data: null };
let previewSeq = 0;
let previewTimer = 0;
let currentLevel = '';

export function initSessionLauncher() {
  applyCollapsed(prefs.launcherCollapsed !== false);
  $('#launcherToggle').addEventListener('click', () => {
    const collapsed = !$('#launcherSection').classList.contains('collapsed');
    applyCollapsed(collapsed);
    savePrefs({ launcherCollapsed: collapsed });
  });
  $('#profileSelect').addEventListener('change', () => {
    savePrefs({ lastProfileId: $('#profileSelect').value });
    renderProfileDetails({ reset: true });
  });
  $('#modelSelect').addEventListener('change', () => {
    renderThinkingPills({ keepLevel: true });
    schedulePreview();
  });

  const pills = $('#thinkingPills');
  pills.addEventListener('click', (ev) => {
    const pill = ev.target.closest('[data-level]');
    if (!pill) return;
    selectLevel(pill.dataset.level);
    schedulePreview();
  });
  pills.addEventListener('keydown', onPillsKeydown);

  $('#cwdInput').addEventListener('input', schedulePreview);
  $('#launchBtn').addEventListener('click', launchSession);
  // Ctrl+Enter starts from any field inside the launcher section.
  $('#launcherSection').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      launchSession();
    }
  });
  $('#whenToUseHint').addEventListener('click', (ev) => ev.currentTarget.classList.toggle('expanded'));

  on('profiles:changed', () => {
    renderProfileOptions({ reset: false });
    renderCwdPlaceholder();
  });
  on('hub:connection', (ok) => {
    syncLaunchButton();
    // A lost connection hides the preview line; refresh it once the hub is back.
    if (ok === true) schedulePreview();
  });

  renderCwdHistory();
  renderCwdPlaceholder();
  syncLaunchButton();
}

/** Opens the launcher for this visit only; the saved collapsed/expanded choice is left alone. */
export function expandLauncher() {
  applyCollapsed(false);
}

function applyCollapsed(collapsed) {
  $('#launcherSection').classList.toggle('collapsed', collapsed);
  $('#launcherToggle').setAttribute('aria-expanded', String(!collapsed));
}

// ------------------------------------------------------------------ profiles
function renderProfileOptions({ reset = true } = {}) {
  const sel = $('#profileSelect');
  const previous = sel.value || prefs.lastProfileId;
  const entries = Object.entries(state.profiles).filter(([, p]) => p.enabled !== false);
  sel.innerHTML = '';
  for (const [id, p] of entries) {
    const opt = new Option(p.name || id, id);
    opt.title = id;
    sel.appendChild(opt);
  }
  if (!entries.length) sel.appendChild(new Option('没有已启用的 Profile', ''));
  sel.disabled = entries.length === 0;
  const keep = previous && state.profiles[previous]?.enabled !== false ? previous : entries[0]?.[0] || '';
  sel.value = keep;
  renderProfileDetails({ reset });
  syncLaunchButton();
}

function currentProfile() {
  return state.profiles[$('#profileSelect').value] || {};
}

function renderProfileDetails({ reset }) {
  const p = currentProfile();
  const models = engineModels(p.engine || '');
  // Engines without a model list (pwsh) get no model / thinking rows at all.
  const showModel = models.length > 0;
  $('#modelField').hidden = !showModel;
  $('#thinkingField').hidden = !showModel;
  renderModelOptions({ keepSelection: !reset });
  renderThinkingPills({ keepLevel: !reset });

  const hint = $('#whenToUseHint');
  const text = (p.when_to_use || p.description || '').trim();
  hint.classList.remove('expanded');
  hint.innerHTML = text
    ? `<div class="hint-text"><span class="hint-label">何时调用</span>${escapeHtml(text)}</div>`
    : '<div class="hint-text hint-empty">尚未填写调用说明，可在「Profile 与模型」中补充。</div>';
  schedulePreview();
}

function renderModelOptions({ keepSelection }) {
  const sel = $('#modelSelect');
  const p = currentProfile();
  const engine = p.engine || '';
  const models = engineModels(engine);
  const previous = keepSelection ? sel.value : '';
  sel.innerHTML = '';
  if (!models.length) return;

  const def = p.default_model || engineDefaultModel(engine);
  const defEntry = def ? models.find((m) => m.id === def) : null;
  const firstEnabled = models.find((m) => m.enabled !== false);
  let label = 'Profile 默认';
  if (def) {
    label = defEntry && defEntry.enabled === false
      ? `Profile 默认（${def} 已停用 → ${firstEnabled?.id || '无可用模型'}）`
      : `Profile 默认（${def}）`;
  }
  sel.appendChild(new Option(label, ''));
  for (const m of models) {
    if (m.enabled === false) continue;
    const opt = new Option(m.id, m.id);
    if (m.note) opt.title = m.note;
    sel.appendChild(opt);
  }
  sel.value = previous && [...sel.options].some((o) => o.value === previous) ? previous : '';
}

// ------------------------------------------------------------ thinking pills
function renderThinkingPills({ keepLevel }) {
  const wrap = $('#thinkingPills');
  const p = currentProfile();
  const models = engineModels(p.engine || '');
  if (!models.length) {
    wrap.innerHTML = '';
    currentLevel = '';
    return;
  }
  const modelId = $('#modelSelect').value;
  const model = modelId
    ? models.find((m) => m.id === modelId)
    : defaultModelEntry(models);
  const levels = (model?.thinking_levels || []).filter(Boolean);
  // Changing the model resets thinking to 默认 when the current level is not supported.
  if (!keepLevel || !levels.includes(currentLevel)) currentLevel = '';

  // V-1: the label is the level a launch with 思考等级 = 默认 would really use, not the selected one.
  const defaultLevel = defaultThinkingLevel();
  const defaultLabel = `默认${defaultLevel ? ` · ${escapeHtml(defaultLevel)}` : ''}`;
  wrap.innerHTML = [`<button type="button" class="level-pill" role="radio" data-level="" aria-checked="${currentLevel === ''}">${defaultLabel}</button>`]
    .concat(levels.map((level) =>
      `<button type="button" class="level-pill" role="radio" data-level="${escapeHtml(level)}" aria-checked="${currentLevel === level}">${escapeHtml(level)}</button>`))
    .join('');
  updatePillTabIndex();
}

function selectLevel(level) {
  currentLevel = level;
  for (const pill of $$('#thinkingPills [data-level]')) {
    pill.setAttribute('aria-checked', String(pill.dataset.level === level));
  }
  updatePillTabIndex();
}

function updatePillTabIndex() {
  for (const pill of $$('#thinkingPills [data-level]')) {
    pill.tabIndex = pill.getAttribute('aria-checked') === 'true' ? 0 : -1;
  }
}

function onPillsKeydown(ev) {
  const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
  if (!keys.includes(ev.key)) return;
  const pills = $$('#thinkingPills [data-level]');
  if (!pills.length) return;
  ev.preventDefault();
  const current = Math.max(0, pills.findIndex((pill) => pill.getAttribute('aria-checked') === 'true'));
  let next = current;
  if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') next = Math.max(0, current - 1);
  else if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') next = Math.min(pills.length - 1, current + 1);
  else if (ev.key === 'Home') next = 0;
  else if (ev.key === 'End') next = pills.length - 1;
  const target = pills[next];
  if (!target) return;
  selectLevel(target.dataset.level);
  target.focus();
  schedulePreview();
}

function updateDefaultPillLabel() {
  const pill = $('#thinkingPills [data-level=""]');
  if (!pill) return;
  pill.textContent = `默认${defaultThinkingLevel() ? ` · ${defaultThinkingLevel()}` : ''}`;
}

/** The model a launch with 模型 = Profile 默认 really runs (a disabled default is substituted). */
function defaultModelEntry(models) {
  const p = currentProfile();
  const def = p.default_model || engineDefaultModel(p.engine || '');
  const entry = def ? models.find((m) => m.id === def) : null;
  if (entry && entry.enabled !== false) return entry;
  return models.find((m) => m.enabled !== false) || null;
}

/**
 * V-1: the level a launch with 思考等级 = 默认 would use for the current model choice — the profile
 * default when that model supports it, else the model's default, else the middle level.
 */
function defaultThinkingLevel() {
  const p = currentProfile();
  const models = engineModels(p.engine || '');
  const selectedId = $('#modelSelect').value;
  const entry = selectedId ? models.find((m) => m.id === selectedId) : defaultModelEntry(models);
  if (!entry) return '';
  const levels = (entry.thinking_levels || []).filter(Boolean);
  const profileLevel = p.default_thinking_level || '';
  if (profileLevel && levels.includes(profileLevel)) return profileLevel;
  const modelDefault = entry.default_thinking_level || '';
  if (modelDefault && levels.includes(modelDefault)) return modelDefault;
  return levels.length ? levels[Math.floor(levels.length / 2)] : '';
}

// ----------------------------------------------------------------- preview
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, PREVIEW_DEBOUNCE_MS);
}

async function runPreview() {
  const profileId = $('#profileSelect').value;
  const seq = ++previewSeq;
  if (!profileId) {
    preview = { status: 'idle', data: null };
    renderPreview();
    syncLaunchButton();
    return;
  }
  const body = {
    profile_id: profileId,
    model: $('#modelSelect').value || null,
    thinking_level: currentLevel || null,
    cwd: $('#cwdInput').value.trim() || null,
  };
  try {
    const data = await hubApi.launchPreview(body);
    if (seq !== previewSeq) return;
    if (data && data.ok === false) preview = { status: 'error', data };
    else if (data && data.ok === true) preview = { status: 'ok', data };
    else preview = { status: 'failed', data: null };
  } catch {
    // The hub may not have /api/launch-preview yet; hide the line but keep launching.
    if (seq !== previewSeq) return;
    preview = { status: 'failed', data: null };
  }
  updateDefaultPillLabel();
  renderPreview();
  syncLaunchButton();
}

function quoteArg(value) {
  const text = String(value ?? '');
  return /[\s"']/.test(text) ? `"${text.replace(/"/g, '\\"')}"` : text;
}

function renderPreview() {
  const box = $('#runPreview');
  if (!box) return;
  if (preview.status === 'failed' || preview.status === 'idle') {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  if (preview.status === 'error') {
    box.innerHTML = `<div class="preview-error">${escapeHtml(preview.data?.error || '无法预览启动参数')}</div>`;
    return;
  }
  const d = preview.data || {};
  const bits = [];
  if (d.model) bits.push(escapeHtml(d.model));
  if (d.thinking_level) bits.push(`思考 ${escapeHtml(d.thinking_level)}`);
  const head = bits.length ? `将运行：${bits.join(' · ')}` : '将运行：该 Profile 不使用模型与思考等级';
  const notes = (Array.isArray(d.notes) ? d.notes : [])
    .map((note) => `<div class="preview-note">${escapeHtml(note)}</div>`).join('');
  const args = Array.isArray(d.args) ? d.args : [];
  const commandLine = [d.command || '', ...args].map(quoteArg).join(' ').trim();
  // V-3b: dsh is launched through the hub API, so the pwsh wrapper says nothing about what runs.
  const spec = d.dsh_spec;
  const dshLine = spec && (spec.provider || spec.model)
    ? `DSH Hub API：${spec.tier || ''} · ${spec.provider || ''}/${spec.model || ''} · 思考 ${spec.effort || ''}`
    : '';
  const env = Object.entries(d.env || {}).map(([key, value]) => `${key}=${value}`).join('\n');
  box.innerHTML = `
    <div class="preview-head">${head}</div>
    ${notes}
    <details class="run-command">
      <summary>完整命令</summary>
      <pre class="run-pre">${escapeHtml(dshLine || commandLine)}</pre>
      ${env ? `<pre class="run-pre run-env">${escapeHtml(env)}</pre>` : ''}
      ${d.cwd ? `<div class="run-cwd">工作目录：<code>${escapeHtml(d.cwd)}</code></div>` : ''}
    </details>`;
}

function syncLaunchButton() {
  const btn = $('#launchBtn');
  if (!btn) return;
  const hasProfile = !!$('#profileSelect').value;
  const connected = state.hubConnected !== false;
  const blocked = preview.status === 'error';
  btn.disabled = !hasProfile || !connected || blocked;
  btn.title = !connected
    ? '与 Hub 的连接已断开，正在重试…'
    : blocked ? '启动参数有误，见上方提示' : '启动 Sub-Agent（Ctrl + Enter）';
}

// ------------------------------------------------------------------ launch
async function launchSession() {
  const btn = $('#launchBtn');
  if (btn.disabled) return;
  const profileId = $('#profileSelect').value;
  if (!profileId) {
    toast('请先在「Profile 与模型」中启用至少一个 Profile', 'error');
    return;
  }
  const p = state.profiles[profileId] || {};
  const task = $('#taskInput').value.trim();
  const cwd = $('#cwdInput').value.trim();
  setBusy(btn, true);
  try {
    const data = await hubApi.createSession({
      profile_id: profileId,
      model: $('#modelSelect').value || null,
      thinking_level: currentLevel || null,
      task: task || null,
      cwd: cwd || null,
    });
    const session = data.session;
    if (!session) throw new Error('服务端没有返回会话信息');
    $('#taskInput').value = '';
    if (cwd) rememberCwd(cwd);
    upsertSession(session);
    setActiveSession(session.session_id);
    emit('sessions:refresh-request');
    const model = session.effective_model || preview.data?.model || '';
    const level = session.effective_thinking_level || preview.data?.thinking_level || '';
    const parts = [p.name || profileId, model, level].filter(Boolean);
    toast(`已启动 ${parts.join(' · ')}`, 'success');
    // Collapse for this visit only: the saved collapsed/expanded preference is untouched (decision 10).
    applyCollapsed(true);
  } catch (err) {
    toast(`启动失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
    syncLaunchButton();
  }
}

function rememberCwd(cwd) {
  const recent = [cwd, ...prefs.recentCwds.filter((c) => c !== cwd)].slice(0, MAX_RECENT_CWDS);
  savePrefs({ recentCwds: recent });
  renderCwdHistory();
}

function renderCwdPlaceholder() {
  $('#cwdInput').placeholder = state.defaultCwd ? `默认：${state.defaultCwd}` : '默认：Hub 当前目录';
}

function renderCwdHistory() {
  $('#cwdHistory').innerHTML = prefs.recentCwds
    .map((cwd) => `<option value="${escapeHtml(cwd)}"></option>`).join('');
}