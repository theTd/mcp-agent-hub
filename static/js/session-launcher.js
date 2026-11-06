// Sidebar launcher: pick a profile, model and effort, then spawn a session in the console.
import { hubApi } from './hub-api.js';
import { state, prefs, savePrefs, on, emit, engineName, sortEngines, setActiveSession, upsertSession } from './console-state.js';
import { $, escapeHtml, setBusy, toast } from './ui-kit.js';

const MAX_RECENT_CWDS = 8;

export function initSessionLauncher() {
  applyCollapsed(prefs.launcherCollapsed !== false);
  $('#launcherToggle').addEventListener('click', () => {
    const collapsed = !$('#launcherSection').classList.contains('collapsed');
    applyCollapsed(collapsed);
    savePrefs({ launcherCollapsed: collapsed });
  });
  $('#profileSelect').addEventListener('change', () => {
    savePrefs({ lastProfileId: $('#profileSelect').value });
    renderProfileDetails({ keepSelection: false });
  });
  $('#launchBtn').addEventListener('click', launchSession);
  $('#taskInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      launchSession();
    }
  });
  $('#whenToUseHint').addEventListener('click', (ev) => ev.currentTarget.classList.toggle('expanded'));
  on('profiles:changed', renderProfileOptions);
  renderCwdHistory();
}

/** Opens the launcher for this visit only; the saved collapsed/expanded choice is left alone. */
export function expandLauncher() {
  applyCollapsed(false);
}

function applyCollapsed(collapsed) {
  $('#launcherSection').classList.toggle('collapsed', collapsed);
  $('#launcherToggle').setAttribute('aria-expanded', String(!collapsed));
}

function renderProfileOptions() {
  const sel = $('#profileSelect');
  const previous = sel.value || prefs.lastProfileId;
  const groups = new Map();
  for (const [id, p] of Object.entries(state.profiles)) {
    if (p.enabled === false) continue;
    const engine = p.engine || 'other';
    if (!groups.has(engine)) groups.set(engine, []);
    groups.get(engine).push([id, p]);
  }

  sel.innerHTML = '';
  for (const engine of sortEngines(groups.keys())) {
    const group = document.createElement('optgroup');
    group.label = engineName(engine);
    for (const [id, p] of groups.get(engine)) {
      const opt = new Option(p.name || id, id);
      opt.title = id;
      group.appendChild(opt);
    }
    sel.appendChild(group);
  }

  const hasProfiles = sel.options.length > 0;
  sel.disabled = !hasProfiles;
  $('#launchBtn').disabled = !hasProfiles;
  if (!hasProfiles) sel.appendChild(new Option('没有已启用的 Profile', ''));
  if (previous && state.profiles[previous] && state.profiles[previous].enabled !== false) sel.value = previous;
  renderProfileDetails({ keepSelection: true });
}

function fillOverrideSelect(sel, defaultValue, values, keepSelection) {
  const previous = keepSelection ? sel.value : '';
  sel.innerHTML = '';
  sel.appendChild(new Option(defaultValue ? `默认 · ${defaultValue}` : '默认（不覆盖）', ''));
  for (const v of values) sel.appendChild(new Option(v === defaultValue ? `${v}  ★` : v, v));
  if (previous && values.includes(previous)) sel.value = previous;
  sel.disabled = values.length === 0;
}

function renderProfileDetails({ keepSelection }) {
  const p = state.profiles[$('#profileSelect').value] || {};
  fillOverrideSelect($('#modelSelect'), p.default_model || '', p.available_models || [], keepSelection);
  fillOverrideSelect($('#thinkingSelect'), p.default_thinking_level || '', p.available_thinking_levels || [], keepSelection);

  const hint = $('#whenToUseHint');
  const text = (p.when_to_use || p.description || '').trim();
  hint.classList.remove('expanded');
  hint.innerHTML = text
    ? `<div class="hint-text"><span class="hint-label">何时调用</span>${escapeHtml(text)}</div>`
    : '<div class="hint-text hint-empty">尚未填写调用说明，可在「Profile 与模型」中补充。</div>';
}

async function launchSession() {
  const btn = $('#launchBtn');
  if (btn.disabled) return;
  const profileId = $('#profileSelect').value;
  if (!profileId) {
    toast('请先在「Profile 与模型」中启用至少一个 Profile', 'error');
    return;
  }
  const task = $('#taskInput').value.trim();
  const cwd = $('#cwdInput').value.trim();
  setBusy(btn, true);
  try {
    const data = await hubApi.createSession({
      profile_id: profileId,
      model: $('#modelSelect').value || null,
      thinking_level: $('#thinkingSelect').value || null,
      task: task || null,
      cwd: cwd || null,
    });
    if (!data.session) throw new Error('服务端没有返回会话信息');
    $('#taskInput').value = '';
    if (cwd) rememberCwd(cwd);
    upsertSession(data.session);
    setActiveSession(data.session.session_id);
    emit('sessions:refresh-request');
    toast(`已拉起 ${data.session.title || profileId}`, 'success');
  } catch (err) {
    toast(`拉起失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
  }
}

function rememberCwd(cwd) {
  const recent = [cwd, ...prefs.recentCwds.filter((c) => c !== cwd)].slice(0, MAX_RECENT_CWDS);
  savePrefs({ recentCwds: recent });
  renderCwdHistory();
}

function renderCwdHistory() {
  $('#cwdHistory').innerHTML = prefs.recentCwds.map((c) => `<option value="${escapeHtml(c)}"></option>`).join('');
}
