// Profile & model settings modal: tab switching plus the profile list and editor.
import { hubApi } from './hub-api.js';
import { state, on, reloadProfiles, engineName, sortEngines } from './console-state.js';
import { $, $$, escapeHtml, icon, toast, confirmDialog, setBusy, openModal, registerModal, isModalOpen } from './ui-kit.js';
import { renderEngineCatalog } from './engine-catalog.js';
import { initRoutingGuide, refreshRoutingGuide, saveRoutingGuide, isRoutingDirty, discardRoutingChanges } from './routing-guide.js';

const MODAL_ID = 'profileModal';
const CUSTOM_MODEL = '__custom__';
// Last-resort launch commands for a new profile when no existing profile of that engine exists yet.
const FALLBACK_COMMANDS = {
  omp: 'C:\\Users\\cui\\AppData\\Local\\omp\\omp.exe',
  kimi: 'C:\\Users\\cui\\.kimi-code\\bin\\kimi.exe',
  agy: 'C:\\Users\\cui\\AppData\\Local\\agy\\bin\\agy.EXE',
  codex: 'C:\\Users\\cui\\AppData\\Roaming\\npm\\codex.cmd',
  dsh: 'pwsh.exe',
  pwsh: 'pwsh.exe',
};

let selectedId = null; // id of the saved profile being edited; null while editing a new draft
let dirty = false;
let activeTab = 'profiles';
let filterText = '';

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
  $('#editModelCustom').addEventListener('input', updateModelWarning);
  $('#editArgs').addEventListener('input', validateArgs);
  $('#editId').addEventListener('input', () => showFieldError('editId', ''));
  $('#editName').addEventListener('input', () => {
    if (!selectedId) $('#editorTitle').textContent = $('#editName').value.trim() || '新建 Profile';
  });

  const pane = $('#profileEditorPane');
  pane.addEventListener('input', () => setDirty(true));
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
  const order = sortEngines(new Set(Object.values(state.profiles).map((p) => p.engine || 'other')));
  return Object.entries(state.profiles).sort(([, a], [, b]) =>
    order.indexOf(a.engine || 'other') - order.indexOf(b.engine || 'other'));
}

// ------------------------------------------------------------- profile list
function matchesFilter(id, p) {
  if (!filterText) return true;
  return [id, p.name, p.engine, p.default_model, p.default_thinking_level, p.when_to_use]
    .some((v) => String(v || '').toLowerCase().includes(filterText));
}

function renderProfileList() {
  const groups = new Map();
  let total = 0;
  let shown = 0;
  for (const [id, p] of sortedProfileEntries()) {
    total += 1;
    if (!matchesFilter(id, p)) continue;
    shown += 1;
    const engine = p.engine || 'other';
    if (!groups.has(engine)) groups.set(engine, []);
    groups.get(engine).push([id, p]);
  }
  $('#profileListCount').textContent = filterText ? `${shown} / ${total}` : String(total);

  const list = $('#profileList');
  if (!shown) {
    list.innerHTML = `<div class="list-empty">${filterText ? '没有匹配的 Profile' : '还没有 Profile，点击「新建」创建一个。'}</div>`;
    return;
  }
  list.innerHTML = [...groups.keys()].map((engine) => `
    <div class="pm-group">
      <div class="pm-group-head">
        <span class="engine-dot" data-engine="${escapeHtml(engine)}"></span>
        <span>${escapeHtml(engineName(engine))}</span>
        <span class="pm-group-count">${groups.get(engine).length}</span>
      </div>
      ${groups.get(engine).map(([id, p]) => profileItemHtml(id, p)).join('')}
    </div>`).join('');
}

function profileItemHtml(id, p) {
  const enabled = p.enabled !== false;
  const when = (p.when_to_use || '').trim();
  const pills = [
    p.default_model ? `<span class="pill pill-model">${escapeHtml(p.default_model)}</span>` : '',
    p.default_thinking_level ? `<span class="pill pill-effort">${icon('gauge')}${escapeHtml(p.default_thinking_level)}</span>` : '',
  ].join('');
  return `
    <button type="button" class="pm-item${enabled ? '' : ' is-disabled'}" role="option" data-id="${escapeHtml(id)}" aria-selected="${id === selectedId}">
      <span class="pm-item-row">
        <span class="pm-item-name">${escapeHtml(p.name || id)}</span>
        ${enabled ? '' : '<span class="tag-off">已停用</span>'}
      </span>
      <span class="pm-item-id">${escapeHtml(id)}</span>
      ${pills ? `<span class="pm-item-pills">${pills}</span>` : ''}
      ${when ? `<span class="pm-item-when" title="${escapeHtml(when)}">${escapeHtml(when)}</span>` : ''}
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
  writeEditor({
    isNew: false,
    id,
    enabled: p.enabled !== false,
    when_to_use: p.when_to_use || '',
    name: p.name || '',
    engine: p.engine || 'omp',
    kind: p.kind || 'tui',
    default_model: p.default_model || '',
    default_thinking_level: p.default_thinking_level || '',
    command: p.command || '',
    task_mode: p.task_mode || '',
    startup_delay_sec: p.startup_delay_sec ?? 2.5,
    argsText: JSON.stringify(p.args || []),
    description: p.description || '',
  });
}

function ensureOption(select, value) {
  if (value && ![...select.options].some((o) => o.value === value)) select.appendChild(new Option(value, value));
}

function writeEditor(d) {
  $('#editorTitle').textContent = d.isNew ? (d.name || '新建 Profile') : (d.name || d.id);
  $('#editorSubtitle').textContent = d.isNew ? '新 Profile · 保存后即可通过 MCP 调用' : d.id;
  $('#editorSubtitle').classList.toggle('is-id', !d.isNew);
  $('#editEnabled').checked = d.enabled;
  $('#editWhenToUse').value = d.when_to_use;

  const idInput = $('#editId');
  idInput.value = d.id;
  idInput.readOnly = !d.isNew;
  $('#editIdHelp').textContent = d.isNew
    ? '主 Agent 调用时使用的 agent_type，保存后不可修改。'
    : 'ID 创建后不可修改；需要改名时请用「复制为新 Profile」。';
  showFieldError('editId', '');

  $('#editName').value = d.name;
  for (const [sel, value] of [['#editEngine', d.engine], ['#editKind', d.kind], ['#editTaskMode', d.task_mode]]) {
    ensureOption($(sel), value);
    $(sel).value = value;
  }
  $('#editCommand').value = d.command;
  $('#editDelay').value = d.startup_delay_sec;
  $('#editArgs').value = d.argsText;
  validateArgs();
  $('#editDesc').value = d.description;
  populateEngineFields(d.engine, d.default_model, d.default_thinking_level);

  $('#btnDeleteProfile').hidden = d.isNew;
  $('#btnDuplicateProfile').hidden = d.isNew;
  setDirty(false);
}

function readEditor() {
  const modelChoice = $('#editModelSelect').value;
  return {
    id: $('#editId').value.trim(),
    enabled: $('#editEnabled').checked,
    when_to_use: $('#editWhenToUse').value.trim(),
    name: $('#editName').value.trim(),
    engine: $('#editEngine').value,
    kind: $('#editKind').value,
    default_model: modelChoice === CUSTOM_MODEL ? $('#editModelCustom').value.trim() : modelChoice,
    default_thinking_level: $('#editThinking').value,
    command: $('#editCommand').value.trim(),
    task_mode: $('#editTaskMode').value,
    startup_delay_sec: $('#editDelay').value,
    argsText: $('#editArgs').value,
    description: $('#editDesc').value.trim(),
  };
}

function populateEngineFields(engine, currentModel, currentThinking) {
  const cat = state.enginesCatalog[engine] || {};
  const all = cat.all_discovered_models || cat.available_models || [];
  const disabled = new Set(cat.disabled_models || []);
  const levels = cat.available_thinking_levels || [];

  const modelSel = $('#editModelSelect');
  modelSel.innerHTML = '';
  modelSel.appendChild(new Option(`引擎默认（${cat.default_model || '自动'}）`, ''));
  for (const m of all) modelSel.appendChild(new Option(disabled.has(m) ? `${m}（已停用）` : m, m));
  modelSel.appendChild(new Option('自定义模型 ID…', CUSTOM_MODEL));

  const custom = $('#editModelCustom');
  if (!currentModel || all.includes(currentModel)) {
    modelSel.value = currentModel || '';
    custom.value = '';
    custom.hidden = true;
  } else {
    modelSel.value = CUSTOM_MODEL;
    custom.value = currentModel;
    custom.hidden = false;
  }
  updateModelWarning();

  const thinkSel = $('#editThinking');
  thinkSel.innerHTML = '';
  thinkSel.appendChild(new Option(`引擎默认（${cat.default_thinking_level || '自动'}）`, ''));
  for (const level of levels) thinkSel.appendChild(new Option(level, level));
  ensureOption(thinkSel, currentThinking);
  thinkSel.value = currentThinking || '';

  renderEngineInfo(engine);
}

function renderEngineInfo(engine) {
  const cat = state.enginesCatalog[engine] || {};
  const flags = cat.cli_flags_template || {};
  const enabled = cat.available_models || [];
  const all = cat.all_discovered_models || enabled;
  const levels = cat.available_thinking_levels || [];
  $('#engineInfo').innerHTML = `
    <div class="info-card-head">
      ${icon('cpu')}<strong>${escapeHtml(cat.name || engine)}</strong>
      <span class="state-badge ${cat.installed ? 'ok' : 'off'}">${cat.installed ? '已安装' : '未检测到本地程序'}</span>
      <span class="muted" style="margin-left:auto;font-size:11.5px">引擎自动识别信息</span>
    </div>
    <dl class="kv-list">
      <dt>可执行文件</dt><dd><code>${escapeHtml(cat.binary || 'N/A')}</code></dd>
      <dt>模型参数</dt><dd><code>${escapeHtml(flags.model || '自动')}</code></dd>
      <dt>思考等级参数</dt><dd><code>${escapeHtml(flags.thinking_level || '自动')}</code></dd>
      <dt>已启用模型</dt><dd>${enabled.length} / ${all.length}<a href="#" data-goto-tab="catalog" style="margin-left:8px">管理模型开关</a></dd>
      <dt>思考等级</dt><dd>${levels.length ? levels.map((l) => `<span class="pill pill-muted">${escapeHtml(l)}</span>`).join('') : '无'}</dd>
    </dl>`;
}

function onEngineChange() {
  const engine = $('#editEngine').value;
  if (!selectedId) {
    const defaults = engineDefaults(engine);
    $('#editCommand').value = defaults.command;
    ensureOption($('#editTaskMode'), defaults.task_mode);
    $('#editTaskMode').value = defaults.task_mode;
    $('#editArgs').value = defaults.argsText;
    validateArgs();
  }
  populateEngineFields(engine, '', '');
}

/** Defaults for a new profile, learned from the existing profiles of the same engine. */
function engineDefaults(engine) {
  const mostCommon = (pick) => {
    const counts = new Map();
    for (const p of Object.values(state.profiles)) {
      if (p.engine !== engine) continue;
      const v = pick(p);
      if (v) counts.set(v, (counts.get(v) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  };
  return {
    command: mostCommon((p) => p.command) || FALLBACK_COMMANDS[engine] || '',
    task_mode: mostCommon((p) => p.task_mode) || '',
    argsText: mostCommon((p) => JSON.stringify(p.args || [])) || '[]',
  };
}

function onModelSelectChange() {
  const custom = $('#editModelCustom');
  const isCustom = $('#editModelSelect').value === CUSTOM_MODEL;
  custom.hidden = !isCustom;
  if (isCustom) custom.focus();
  updateModelWarning();
}

function updateModelWarning() {
  const cat = state.enginesCatalog[$('#editEngine').value] || {};
  const choice = $('#editModelSelect').value;
  const model = choice === CUSTOM_MODEL ? $('#editModelCustom').value.trim() : choice;
  const warn = $('#editModelWarn');
  const isDisabled = model && (cat.disabled_models || []).includes(model);
  warn.hidden = !isDisabled;
  warn.textContent = isDisabled ? '该模型已在「模型开关」中停用，主 Agent 将无法使用它。' : '';
}

function validateArgs() {
  const input = $('#editArgs');
  let value = [];
  let message = '';
  try {
    value = JSON.parse(input.value.trim() || '[]');
    if (!Array.isArray(value)) message = '必须是 JSON 数组，例如 [] 或 ["--auto-approve"]';
  } catch {
    message = 'JSON 格式不正确，例如 [] 或 ["--auto-approve"]';
  }
  input.classList.toggle('is-invalid', !!message);
  $('#editArgsError').textContent = message;
  $('#editArgsError').hidden = !message;
  return { ok: !message, value };
}

function showFieldError(fieldId, message) {
  $(`#${fieldId}`).classList.toggle('is-invalid', !!message);
  const err = $(`#${fieldId}Error`);
  err.textContent = message;
  err.hidden = !message;
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
  const engine = 'omp';
  const defaults = engineDefaults(engine);
  writeEditor({
    isNew: true,
    id: '',
    enabled: true,
    when_to_use: '',
    name: '',
    engine,
    kind: 'tui',
    default_model: '',
    default_thinking_level: '',
    command: defaults.command,
    task_mode: defaults.task_mode,
    startup_delay_sec: 2.5,
    argsText: defaults.argsText,
    description: '',
  });
  renderProfileList();
  $('#editId').focus();
}

function duplicateProfile() {
  const current = readEditor();
  let id = `${current.id}-copy`;
  for (let n = 2; state.profiles[id]; n += 1) id = `${current.id}-copy-${n}`;
  selectedId = null;
  writeEditor({ ...current, isNew: true, id, name: current.name ? `${current.name}（副本）` : '' });
  setDirty(true);
  renderProfileList();
  $('#editId').focus();
  $('#editId').select();
}

async function saveProfile() {
  const d = readEditor();
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
  const args = validateArgs();
  if (!args.ok) {
    $('#editArgs').focus();
    return;
  }
  const delay = parseFloat(d.startup_delay_sec);
  const payload = {
    id: d.id,
    enabled: d.enabled,
    when_to_use: d.when_to_use,
    name: d.name || d.id,
    engine: d.engine,
    kind: d.kind,
    default_model: d.default_model || null,
    default_thinking_level: d.default_thinking_level || null,
    command: d.command,
    args: args.value,
    startup_delay_sec: Number.isFinite(delay) ? delay : 2.5,
    description: d.description,
  };
  // Unset task_mode is omitted, so the server keeps its own default instead of a guessed mode.
  if (d.task_mode) payload.task_mode = d.task_mode;

  const btn = $('#btnSaveProfile');
  setBusy(btn, true);
  try {
    await hubApi.saveProfile(payload);
    selectedId = d.id;
    setDirty(false);
    await reloadProfiles(false);
    toast(`已保存 ${payload.name}`, 'success');
  } catch (err) {
    toast(`保存失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
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
