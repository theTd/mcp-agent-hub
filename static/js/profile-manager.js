// Profile & model settings modal: tab switching plus the profile list and editor.
import { hubApi } from './hub-api.js';
import {
  state, on, reloadProfiles, engineName, sortProfiles, engineModels, engineDefaultModel,
  defaultModelSource, defaultThinkingSource,
} from './console-state.js';
import {
  $, $$, escapeHtml, icon, toast, confirmDialog, setBusy, openModal, registerModal, isModalOpen,
} from './ui-kit.js';
import { renderEngineCatalog } from './engine-catalog.js';
import { initRoutingGuide, refreshRoutingGuide, saveRoutingGuide, isRoutingDirty, discardRoutingChanges } from './routing-guide.js';

const MODAL_ID = 'profileModal';

// Last-resort launch commands for a new profile when no existing profile or catalog binary is available.
const FALLBACK_COMMANDS = {
  omp: 'omp.exe',
  kimi: 'kimi.exe',
  agy: 'agy.exe',
  codex: 'codex.cmd',
  claude: 'claude.exe',
  dsh: 'pwsh.exe',
  pwsh: 'pwsh.exe',
};

const ENGINE_IDS = ['omp', 'kimi', 'agy', 'codex', 'claude', 'dsh', 'pwsh'];

// Keys the editor renders itself, so the read-only "高级" box shows only what is left.
const SHOWN_KEYS = new Set([
  'id', 'name', 'engine', 'kind', 'command', 'config_dir', 'args', 'env', 'default_model', 'default_thinking_level',
  'task_mode', 'startup_delay_sec', 'enabled', 'when_to_use', 'description', 'dsh_tier',
  'bypass_approvals',
]);
// Engines whose launch the hub injects approval flags into; bypass_approvals does nothing elsewhere.
const APPROVAL_ENGINES = new Set(['omp', 'kimi', 'codex', 'claude', 'agy']);
const BYPASS_HELP = '打开后递归删除、强推、发布等高风险操作会直接执行，不再交给主 Agent 审批；已归档会话恢复时也按此开关的当前值启动。';
// Values the hub computes on the way out; they are not stored keys.
const COMPUTED_KEYS = new Set([
  'all_discovered_models', 'available_models', 'available_thinking_levels', 'model_details',
]);

let selectedId = null; // id of the saved profile being edited; null while editing a new draft
let dirty = false;
let activeTab = 'profiles';
let filterText = '';
let argsError = '';
let envError = '';
let serverError = '';
let saveBusy = false;

export function initProfileManager() {
  registerModal(MODAL_ID, { beforeClose: confirmDiscardAll });

  $$(`#${MODAL_ID} [role="tab"]`).forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));
  $('#profileSearch').addEventListener('input', (ev) => {
    filterText = ev.target.value.trim().toLowerCase();
    renderProfileList();
  });
  $('#profileList').addEventListener('click', onProfileListClick);
  $('#btnNewProfile').addEventListener('click', startNewProfile);
  $('#btnDuplicateProfile').addEventListener('click', duplicateProfile);
  $('#btnDeleteProfile').addEventListener('click', deleteProfile);
  $('#btnSaveProfile').addEventListener('click', saveProfile);
  $('#btnRescan').addEventListener('click', rescanModels);
  $('#editEngine').addEventListener('change', onEngineChange);
  $('#editModelSelect').addEventListener('change', onModelSelectChange);
  $('#editArgs').addEventListener('input', updateArgsValidation);
  if ($('#editEnv')) $('#editEnv').addEventListener('input', updateEnvValidation);
  $('#editId').addEventListener('input', () => showFieldError('editId', ''));
  $('#editName').addEventListener('input', () => {
    if (!selectedId) $('#editorTitle').textContent = $('#editName').value.trim() || '新建 Profile';
  });

  const pane = $('#profileEditorPane');
  pane.addEventListener('input', () => {
    serverError = '';
    refreshEditorError();
    setDirty(true);
  });
  pane.addEventListener('change', () => setDirty(true));
  pane.addEventListener('click', (ev) => {
    const link = ev.target.closest('[data-goto-tab]');
    if (link) {
      ev.preventDefault();
      switchTab(link.dataset.gotoTab);
    }
  });
  $(`#${MODAL_ID}`).addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault();
      if (activeTab === 'profiles') saveProfile();
      else if (activeTab === 'routing') saveRoutingGuide();
    }
  });

  initRoutingGuide();
  on('profiles:changed', () => {
    if (!isModalOpen(MODAL_ID)) return;
    renderProfileList();
    renderEngineCatalog();
    if (dirty) renderEngineInfo($('#editEngine').value);
    else if (selectedId && state.profiles[selectedId]) fillEditorFromProfile(selectedId);
  });
}

export function openProfileManager(tab = 'profiles') {
  openModal(MODAL_ID);
  switchTab(tab);
  renderProfileList();
  renderEngineCatalog();
  refreshRoutingGuide();
  if (!dirty) {
    const id = selectedId && state.profiles[selectedId] ? selectedId : firstProfileId();
    if (id) selectProfile(id);
    else startNewProfile();
  }
}

function switchTab(tab) {
  activeTab = tab;
  $$(`#${MODAL_ID} [role="tab"]`).forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));
  $$(`#${MODAL_ID} [role="tabpanel"]`).forEach((p) => { p.hidden = p.dataset.panel !== tab; });
}

function firstProfileId() {
  return sortedProfileEntries()[0]?.[0] || null;
}

function sortedProfileEntries() {
  return sortProfiles(Object.entries(state.profiles));
}

// ------------------------------------------------------------- profile list
function matchesFilter(id, p) {
  if (!filterText) return true;
  return [id, p.name, p.engine, p.default_model, p.default_thinking_level, p.when_to_use, p.description]
    .some((value) => String(value || '').toLowerCase().includes(filterText));
}

function renderProfileList() {
  const all = sortedProfileEntries();
  const shown = all.filter(([id, p]) => matchesFilter(id, p));
  $('#profileListCount').textContent = filterText ? `${shown.length} / ${all.length}` : String(all.length);

  const list = $('#profileList');
  if (!shown.length) {
    list.innerHTML = `<div class="list-empty">${filterText ? '没有匹配的 Profile' : '还没有 Profile，点击「新建」创建一个。'}</div>`;
    return;
  }
  list.innerHTML = shown.map(([id, p]) => profileItemHtml(id, p)).join('');
}

function profileItemHtml(id, p) {
  const enabled = p.enabled !== false;
  const engine = p.engine || 'other';
  const model = p.default_model || engineDefaultModel(engine) || '未设置模型';
  const level = p.default_thinking_level || '未设置思考等级';
  return `
    <button type="button" class="pm-item${enabled ? '' : ' is-disabled'}" role="option" data-id="${escapeHtml(id)}" aria-selected="${id === selectedId}">
      <span class="pm-item-row">
        <span class="pm-item-name">${escapeHtml(p.name || id)}</span>
        <span class="engine-badge" data-engine="${escapeHtml(engine)}">${escapeHtml(engine)}</span>
      </span>
      <span class="pm-item-id">${escapeHtml(id)}</span>
      <span class="pm-item-defaults">${escapeHtml(model)} · ${escapeHtml(level)}${enabled ? '' : ' · 已停用'}</span>
    </button>`;
}

async function onProfileListClick(ev) {
  const item = ev.target.closest('.pm-item');
  if (!item || item.dataset.id === selectedId) return;
  if (!(await confirmDiscardProfile())) return;
  selectProfile(item.dataset.id);
}

function selectProfile(id) {
  selectedId = id;
  fillEditorFromProfile(id);
  renderProfileList();
}

// ------------------------------------------------------------------- editor
function fillEditorFromProfile(id) {
  const p = state.profiles[id];
  if (!p) return;
  writeDraft(profileDraft(id, p));
}

function profileDraft(id, p) {
  return {
    isNew: false,
    id,
    name: p.name || '',
    engine: p.engine || '',
    kind: p.kind === 'web_gui' ? 'web_gui' : 'tui',
    enabled: p.enabled !== false,
    when_to_use: p.when_to_use || '',
    description: p.description || '',
    default_model: p.default_model || '',
    default_thinking_level: p.default_thinking_level || '',
    model_source: defaultModelSource(p),
    thinking_source: defaultThinkingSource(p),
    command: p.command || '',
    config_dir: p.config_dir || '',
    argsText: (Array.isArray(p.args) ? p.args : []).join('\n'),
    envText: formatEnvText(p.env),
    task_mode: p.task_mode || '',
    delayText: p.startup_delay_sec ?? '',
    dsh_tier: p.dsh_tier === 'pro' ? 'pro' : 'flash',
    bypass_approvals: p.bypass_approvals === true,
    extra: p,
  };
}

function readDraft() {
  const engine = $('#editEngine').value;
  const isDsh = engine === 'dsh';
  const modelChoice = $('#editModelSelect').value;
  const thinkingChoice = $('#editThinking').value;
  return {
    isNew: !selectedId,
    id: $('#editId').value.trim(),
    name: $('#editName').value.trim(),
    engine,
    kind: isDsh && $('#editKind').value === 'web_gui' ? 'web_gui' : 'tui',
    enabled: $('#editEnabled').checked,
    when_to_use: $('#editWhenToUse').value.trim(),
    description: $('#editDesc').value.trim(),
    default_model: modelChoice,
    default_thinking_level: thinkingChoice,
    model_source: modelChoice ? 'profile' : 'engine',
    thinking_source: thinkingChoice ? 'profile' : 'engine',
    command: $('#editCommand').value.trim(),
    config_dir: ($('#editConfigDir')?.value || '').trim(),
    argsText: $('#editArgs').value,
    envText: $('#editEnv') ? $('#editEnv').value : '',
    task_mode: $('#editTaskMode').value,
    delayText: $('#editDelay').value,
    dsh_tier: $('#editDshTier').value,
    bypass_approvals: $('#editBypassApprovals').checked,
    extra: selectedId ? state.profiles[selectedId] : null,
  };
}

function writeDraft(d) {
  $('#editorTitle').textContent = d.isNew ? (d.name || '新建 Profile') : (d.name || d.id || '编辑 Profile');
  $('#editorSubtitle').textContent = d.isNew ? '新 Profile · 保存后即可通过 MCP 调用' : d.id;
  $('#editorSubtitle').classList.toggle('is-id', !d.isNew);
  $('#editEnabled').checked = d.enabled;
  $('#editWhenToUse').value = d.when_to_use;
  $('#editName').value = d.name;
  $('#editDesc').value = d.description;

  const idInput = $('#editId');
  idInput.value = d.id;
  idInput.readOnly = !d.isNew;
  $('#editIdHelp').textContent = d.isNew
    ? '主 Agent 调用时使用的 agent_type，保存后不可修改。'
    : 'ID 创建后不可修改；需要改名时请用「复制为新 Profile」。';
  showFieldError('editId', '');

  setEngineSelect(d.engine);
  $('#editKind').value = d.kind === 'web_gui' ? 'web_gui' : 'tui';
  $('#editCommand').value = d.command;
  if ($('#editConfigDir')) $('#editConfigDir').value = d.config_dir || '';
  $('#editArgs').value = d.argsText;
  if ($('#editEnv')) $('#editEnv').value = d.envText || '';
  ensureOption($('#editTaskMode'), d.task_mode);
  $('#editTaskMode').value = d.task_mode || '';
  $('#editDelay').value = d.delayText;
  $('#editDshTier').value = d.dsh_tier;
  $('#editBypassApprovals').checked = d.bypass_approvals === true;

  renderEngineGroups(d.engine);
  populateDefaults(d.engine, d.model_source === 'engine' ? '' : d.default_model,
    d.thinking_source === 'engine' ? '' : d.default_thinking_level);
  renderEngineInfo(d.engine);
  renderAdvanced(d.extra, d.isNew);

  argsError = '';
  envError = '';
  serverError = '';
  refreshEditorError();
  updateArgsValidation();
  updateEnvValidation();
  setDirty(false);
  $('#btnDeleteProfile').hidden = d.isNew;
  $('#btnDuplicateProfile').hidden = d.isNew;
  syncSaveButton();
}

function setEngineSelect(engine) {
  const sel = $('#editEngine');
  sel.innerHTML = '';
  if (!engine) sel.appendChild(new Option('选择引擎…', ''));
  for (const value of ENGINE_IDS) {
    sel.appendChild(new Option(`${value} · ${engineName(value)}`, value));
  }
  if (engine && ![...sel.options].some((option) => option.value === engine)) {
    sel.appendChild(new Option(engine, engine));
  }
  sel.value = engine || '';
}

function renderEngineGroups(engine) {
  const isDsh = engine === 'dsh';
  $('#groupLaunch').hidden = isDsh;
  $('#groupDsh').hidden = !isDsh;
  if ($('#fieldConfigDir')) $('#fieldConfigDir').hidden = engine !== 'claude';
  // V-7: pwsh (and any engine the catalog knows no models for) has no model / thinking defaults.
  const hasModels = !!engine && engineModels(engine).length > 0;
  $('#groupDefaults').hidden = !hasModels;
  renderBypassSwitch(engine);
}

/** bypass_approvals applies to omp / kimi / codex / agy only; dsh and pwsh show it disabled (不适用). */
function renderBypassSwitch(engine) {
  const applies = !engine || APPROVAL_ENGINES.has(engine);
  const box = $('#editBypassApprovals');
  box.disabled = !applies;
  $('#editBypassSwitch').classList.toggle('is-disabled', !applies);
  const help = $('#editBypassHelp');
  help.textContent = applies ? BYPASS_HELP : `不适用：${engine} 引擎没有 Hub 注入的审批参数，这个开关对它没有作用。`;
  help.classList.toggle('warn', applies);
}

function ensureOption(select, value) {
  if (value && ![...select.options].some((option) => option.value === value)) {
    select.appendChild(new Option(value, value));
  }
}

/** 默认模型 / 默认思考等级: "" means "let the hub decide" and is saved as null. */
function populateDefaults(engine, modelValue, thinkingValue) {
  const cat = state.enginesCatalog[engine] || {};
  const models = engineModels(engine);
  const modelSel = $('#editModelSelect');
  modelSel.innerHTML = '';
  modelSel.appendChild(new Option(`引擎默认（${cat.default_model || '自动'}）`, ''));
  for (const model of models) {
    const label = model.enabled === false ? `${model.id} (已停用)` : model.id;
    const option = new Option(label, model.id);
    option.disabled = model.enabled === false;
    option.title = model.note || model.label || model.id;
    modelSel.appendChild(option);
  }
  ensureOption(modelSel, modelValue);
  modelSel.value = modelValue || '';
  populateThinkingSelect(engine, modelSel.value, thinkingValue);
}

function populateThinkingSelect(engine, modelValue, thinkingValue) {
  const cat = state.enginesCatalog[engine] || {};
  const models = engineModels(engine);
  const model = modelValue ? models.find((m) => m.id === modelValue) : null;
  const levels = model ? (model.thinking_levels || []) : (cat.available_thinking_levels || []);
  const sel = $('#editThinking');
  sel.innerHTML = '';
  sel.appendChild(new Option('按模型默认', ''));
  for (const level of levels.filter(Boolean)) sel.appendChild(new Option(level, level));
  ensureOption(sel, thinkingValue);
  sel.value = thinkingValue || '';
}

function renderAdvanced(profile, isNew) {
  const box = $('#advancedBox');
  const list = $('#advancedList');
  const entries = (isNew || !profile) ? [] : Object.entries(profile).filter(([key]) => {
    if (SHOWN_KEYS.has(key) || COMPUTED_KEYS.has(key) || key.endsWith('_source')) return false;
    return true;
  });
  if (!entries.length) {
    box.hidden = true;
    list.innerHTML = '';
    return;
  }
  box.hidden = false;
  list.innerHTML = entries.map(([key, value]) =>
    `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatValue(value))}</dd>`).join('');
}

function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function renderEngineInfo(engine) {
  const card = $('#engineInfo');
  if (!engine) {
    card.innerHTML = `<div class="info-card-head">${icon('cpu')}<strong>引擎识别信息</strong></div>
      <div class="field-help">选择引擎后显示本机的可执行文件、模型与思考等级参数。</div>`;
    return;
  }
  const cat = state.enginesCatalog[engine];
  if (!cat) {
    // V-7: an engine the catalog does not list (pwsh) still has a real launch command. Show it
    // instead of a false "未检测到本地程序" and an N/A binary.
    const command = ($('#editCommand')?.value || '').trim()
      || (selectedId ? (state.profiles[selectedId]?.command || '') : '');
    card.innerHTML = `
      <div class="info-card-head">
        ${icon('cpu')}<strong>${escapeHtml(engine)}</strong>
        <span class="state-badge">无模型探测</span>
        <span class="muted" style="margin-left:auto;font-size:11.5px">引擎自动识别信息</span>
      </div>
      <dl class="kv-list">
        <dt>可执行文件</dt><dd><code>${escapeHtml(command || '未填写')}</code></dd>
        <dt>模型 / 思考等级</dt><dd>该引擎没有模型列表，启动时不会注入模型与思考等级参数。</dd>
      </dl>`;
    return;
  }
  const flags = cat.cli_flags_template || {};
  const enabled = cat.available_models || [];
  const all = cat.all_discovered_models || enabled;
  const levels = cat.available_thinking_levels || [];
  const activeBin = ($('#editCommand')?.value || '').trim() || cat.binary || 'N/A';
  const activeCfgDir = ($('#editConfigDir')?.value || '').trim() || cat.config_dir || '';
  const cfgRow = engine === 'claude' && activeCfgDir
    ? `<dt>配置目录</dt><dd><code>${escapeHtml(activeCfgDir)}</code></dd>`
    : '';
  card.innerHTML = `
    <div class="info-card-head">
      ${icon('cpu')}<strong>${escapeHtml(cat.name || engine)}</strong>
      <span class="state-badge ${cat.installed ? 'ok' : 'off'}">${cat.installed ? '已安装' : '未检测到本地程序'}</span>
      <span class="muted" style="margin-left:auto;font-size:11.5px">引擎自动识别信息</span>
    </div>
    <dl class="kv-list">
      <dt>可执行文件</dt><dd><code>${escapeHtml(activeBin)}</code></dd>
      ${cfgRow}
      <dt>模型参数</dt><dd><code>${escapeHtml(flags.model || '自动')}</code></dd>
      <dt>思考等级参数</dt><dd><code>${escapeHtml(flags.thinking_level || '自动')}</code></dd>
      <dt>已启用模型</dt><dd>${enabled.length} / ${all.length}<a href="#" data-goto-tab="catalog" style="margin-left:8px">管理模型开关</a></dd>
      <dt>思考等级</dt><dd>${levels.length ? levels.map((l) => `<span class="pill pill-muted">${escapeHtml(l)}</span>`).join('') : '无'}</dd>
    </dl>`;
}

async function onEngineChange() {
  const engine = $('#editEngine').value;
  const previousEngine = selectedId && state.profiles[selectedId] ? (state.profiles[selectedId].engine || '') : '';
  serverError = '';
  refreshEditorError();
  if (selectedId && previousEngine && engine && engine !== previousEngine) {
    const ok = await confirmDialog({
      title: '更换引擎？',
      message: '更换引擎会重置命令、参数和任务传入方式。继续？',
      confirmText: '更换引擎',
    });
    if (!ok) {
      $('#editEngine').value = previousEngine;
      return;
    }
  }
  if (!selectedId || engine !== previousEngine) {
    applyEngineDefaults(engine);
    populateDefaults(engine, '', '');
  } else {
    populateDefaults(engine, $('#editModelSelect').value, $('#editThinking').value);
  }
  renderEngineGroups(engine);
  renderEngineInfo(engine);
  renderAdvanced(selectedId ? state.profiles[selectedId] : null, !selectedId);
  updateArgsValidation();
  setDirty(true);
  syncSaveButton();
}

/** Defaults for a new profile, learned from the existing profiles of the same engine. */
function engineDefaults(engine) {
  const mostCommon = (pick) => {
    const counts = new Map();
    for (const p of Object.values(state.profiles)) {
      if (p.engine !== engine) continue;
      const value = pick(p);
      if (value) counts.set(value, (counts.get(value) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  };
  return {
    command: mostCommon((p) => p.command) || state.enginesCatalog[engine]?.binary || FALLBACK_COMMANDS[engine] || '',
    config_dir: mostCommon((p) => p.config_dir) || '',
    task_mode: mostCommon((p) => p.task_mode) || '',
    argsText: mostCommon((p) => (Array.isArray(p.args) ? p.args.join('\n') : '')) || '',
    envText: mostCommon((p) => formatEnvText(p.env)) || '',
    // S3: a new profile starts from the delay an existing profile of the same engine uses.
    delayText: String(mostCommon((p) => p.startup_delay_sec) ?? 2.5),
  };
}

function applyEngineDefaults(engine) {
  const defaults = engineDefaults(engine);
  $('#editCommand').value = defaults.command;
  if ($('#editConfigDir')) $('#editConfigDir').value = defaults.config_dir || '';
  ensureOption($('#editTaskMode'), defaults.task_mode);
  $('#editTaskMode').value = defaults.task_mode;
  $('#editArgs').value = defaults.argsText;
  if ($('#editEnv')) $('#editEnv').value = defaults.envText || '';
  $('#editDelay').value = defaults.delayText;
}

function onModelSelectChange() {
  populateThinkingSelect($('#editEngine').value, $('#editModelSelect').value, $('#editThinking').value);
  setDirty(true);
}

// -------------------------------------------------------- args & env validation
function parseArgsText(text) {
  return String(text || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function formatEnvText(envObj) {
  if (!envObj || typeof envObj !== 'object') return '';
  return Object.entries(envObj)
    .filter(([key]) => Boolean(key))
    .map(([key, value]) => `${key}=${value ?? ''}`)
    .join('\n');
}

function parseEnvText(text) {
  const env = {};
  const lines = String(text || '').split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eqIdx = line.indexOf('=');
    if (eqIdx <= 0) {
      return { env, error: `环境变量格式无效（缺少 '='，应为 KEY=VALUE）：${line}` };
    }
    const key = line.slice(0, eqIdx).trim();
    const val = line.slice(eqIdx + 1).trim();
    if (!key || /[\s=]/.test(key)) {
      return { env, error: `环境变量名「${key}」不能包含空格或等号。` };
    }
    env[key] = val;
  }
  return { env, error: '' };
}

/** Returns the injected flag the user must not put in args, or "" (spec B5 / F7). */
function injectedFlag(engine, args) {
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (engine === 'omp') {
      if (arg.startsWith('--model=')) return '--model=';
      if (arg.startsWith('--thinking=')) return '--thinking=';
    } else if (engine === 'kimi') {
      if (arg === '-m' || arg === '--model') return arg;
      if (arg.startsWith('--model=')) return '--model';
    } else if (engine === 'agy' || engine === 'claude') {
      if (arg === '--model' || arg === '--effort') return arg;
      if (arg.startsWith('--model=')) return '--model';
      if (arg.startsWith('--effort=')) return '--effort';
    } else if (engine === 'codex') {
      if (arg === '-m' || arg === '--model') return arg;
      if (arg.startsWith('--model=')) return '--model';
      if (arg === '-c') {
        const next = String(args[i + 1] || '').trim();
        if (next.startsWith('model_reasoning_effort')) return '-c model_reasoning_effort';
        if (next.startsWith('model=')) return '-c model=';
      }
    }
  }
  return '';
}

function updateArgsValidation() {
  const engine = $('#editEngine').value;
  const args = parseArgsText($('#editArgs').value);
  const flag = engine === 'dsh' ? '' : injectedFlag(engine, args);
  argsError = flag
    ? `参数里不要写模型或思考等级（${flag}）：Hub 会按启动时的选择自动注入。`
    : '';
  $('#editArgs').classList.toggle('is-invalid', !!argsError);
  refreshEditorError();
  syncSaveButton();
}

function updateEnvValidation() {
  const el = $('#editEnv');
  if (!el) return;
  const { error } = parseEnvText(el.value);
  envError = error;
  el.classList.toggle('is-invalid', !!envError);
  refreshEditorError();
  syncSaveButton();
}

// ------------------------------------------------------------ feedback/state
function showFieldError(fieldId, message) {
  $(`#${fieldId}`).classList.toggle('is-invalid', !!message);
  const err = $(`#${fieldId}Error`);
  if (!err) return;
  err.textContent = message;
  err.hidden = !message;
}

function refreshEditorError() {
  const message = argsError || envError || serverError;
  const bar = $('#editorErrors');
  bar.hidden = !message;
  bar.textContent = message;
  bar.dataset.tone = (argsError || envError) ? 'validation' : 'server';
}

function showEditorError(message) {
  serverError = message || '';
  refreshEditorError();
}

function syncSaveButton() {
  const btn = $('#btnSaveProfile');
  if (!btn) return;
  btn.disabled = saveBusy || !!argsError || !!envError;
}

function setDirty(value) {
  dirty = value;
  $('#editorDirty').hidden = !value;
}

/** Close guard for the whole modal: one question covering every tab, flags cleared only if confirmed. */
async function confirmDiscardAll() {
  const parts = [dirty && '当前 Profile ', isRoutingDirty() && '路由总则'].filter(Boolean);
  if (!parts.length) return true;
  const ok = await confirmDialog({
    title: '放弃未保存的修改？',
    message: `${parts.join('和')}有尚未保存的修改，关闭后这些修改会丢失。`,
    confirmText: '放弃修改',
    danger: true,
  });
  if (ok) {
    setDirty(false);
    discardRoutingChanges();
  }
  return ok;
}

async function confirmDiscardProfile() {
  if (!dirty) return true;
  const ok = await confirmDialog({
    title: '放弃未保存的修改？',
    message: '当前 Profile 有尚未保存的修改，继续操作后这些修改会丢失。',
    confirmText: '放弃修改',
    danger: true,
  });
  if (ok) setDirty(false);
  return ok;
}

// ------------------------------------------------------------------ actions
async function startNewProfile() {
  if (!(await confirmDiscardProfile())) return;
  selectedId = null;
  writeDraft({
    isNew: true,
    id: '',
    name: '',
    engine: '',
    kind: 'tui',
    enabled: true,
    when_to_use: '',
    description: '',
    default_model: '',
    default_thinking_level: '',
    model_source: 'engine',
    thinking_source: 'engine',
    command: '',
    config_dir: '',
    argsText: '',
    envText: '',
    task_mode: '',
    delayText: 2.5,
    dsh_tier: 'flash',
    bypass_approvals: false,
    extra: null,
  });
  renderProfileList();
  $('#editEngine').focus();
}

function duplicateProfile() {
  const draft = readDraft();
  const base = draft.id || 'profile';
  let id = `${base}-copy`;
  for (let n = 2; state.profiles[id]; n += 1) id = `${base}-copy-${n}`;
  selectedId = null;
  writeDraft({ ...draft, isNew: true, id, name: draft.name ? `${draft.name}（副本）` : '' });
  setDirty(true);
  renderProfileList();
  $('#editId').focus();
  $('#editId').select();
}

async function saveProfile() {
  if (saveBusy) return;
  serverError = '';
  refreshEditorError();
  const d = readDraft();
  if (!d.engine) {
    showEditorError('请先选择引擎');
    $('#editEngine').focus();
    return;
  }
  if (!d.id) {
    showFieldError('editId', '请填写 Profile ID');
    $('#editId').focus();
    return;
  }
  if (/\s/.test(d.id)) {
    showFieldError('editId', 'ID 不能包含空格');
    $('#editId').focus();
    return;
  }
  if (!selectedId && state.profiles[d.id]) {
    showFieldError('editId', `ID「${d.id}」已存在，请换一个`);
    $('#editId').focus();
    return;
  }
  const args = parseArgsText(d.argsText);
  const flag = d.engine === 'dsh' ? '' : injectedFlag(d.engine, args);
  if (flag) {
    updateArgsValidation();
    $('#editArgs').focus();
    return;
  }
  const { env, error: parsedEnvError } = parseEnvText(d.envText);
  if (parsedEnvError) {
    updateEnvValidation();
    if ($('#editEnv')) $('#editEnv').focus();
    return;
  }
  const isDsh = d.engine === 'dsh';
  const command = isDsh ? (d.command || 'pwsh.exe') : d.command;
  if (!command) {
    showEditorError('请填写命令');
    $('#editCommand').focus();
    return;
  }
  const delay = parseFloat(d.delayText);
  const payload = {
    id: d.id,
    name: d.name || d.id,
    engine: d.engine,
    kind: isDsh ? d.kind : 'tui',
    enabled: d.enabled,
    when_to_use: d.when_to_use || null,
    description: d.description || null,
    default_model: d.default_model || null,
    default_thinking_level: d.default_thinking_level || null,
    command,
    config_dir: d.engine === 'claude' ? (d.config_dir || null) : null,
    args,
    env: !isDsh && Object.keys(env).length ? env : null,
    task_mode: d.task_mode || null,
    startup_delay_sec: Number.isFinite(delay) ? delay : null,
    // The DSH web page is no longer embedded; saving drops the old address from the profile.
    gui_url: null,
    dsh_tier: isDsh ? d.dsh_tier : null,
    // Round 2: always a bool (false clears the key). For dsh / pwsh the box is disabled and keeps
    // the stored value, so a save there never changes it.
    bypass_approvals: d.bypass_approvals === true,
  };

  const btn = $('#btnSaveProfile');
  saveBusy = true;
  setBusy(btn, true);
  try {
    await hubApi.saveProfile(payload);
    selectedId = d.id;
    setDirty(false);
    await reloadProfiles(false);
    toast(`已保存 ${payload.name}`, 'success');
  } catch (err) {
    // FastAPI 400 detail lands here (B8); show it in the same inline error bar.
    showEditorError(err.message);
  } finally {
    saveBusy = false;
    setBusy(btn, false);
    syncSaveButton();
  }
}

async function deleteProfile() {
  if (!selectedId) return;
  const p = state.profiles[selectedId] || {};
  const ok = await confirmDialog({
    title: '删除这个 Profile？',
    message: `将删除「${p.name || selectedId}」（${selectedId}）。主 Agent 将无法再通过这个 agent_type 调用它，此操作无法撤销。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  const btn = $('#btnDeleteProfile');
  setBusy(btn, true);
  try {
    await hubApi.deleteProfile(selectedId);
    toast(`已删除 ${p.name || selectedId}`, 'success');
    setDirty(false);
    selectedId = null;
    await reloadProfiles(false);
    const next = firstProfileId();
    if (next) selectProfile(next);
    else startNewProfile();
  } catch (err) {
    toast(`删除失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
  }
}

async function rescanModels(ev) {
  const btn = ev.currentTarget;
  setBusy(btn, true);
  try {
    await reloadProfiles(true);
    toast('已重新扫描本机 CLI 与模型', 'success');
  } catch (err) {
    toast(`扫描失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
  }
}
