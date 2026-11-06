// "引擎与模型" tab: per-engine models with enable switches, thinking-level pills and notes.
import { hubApi } from './hub-api.js';
import {
  state, reloadProfiles, sortEngines, engineDefaultModel, defaultModelSource,
} from './console-state.js';
import { $, escapeHtml, icon, toast, confirmDialog } from './ui-kit.js';

let wired = false;

export function renderEngineCatalog() {
  const wrap = $('#engineCatalog');
  if (!wired) {
    wrap.addEventListener('click', onCatalogClick);
    wrap.addEventListener('change', onCatalogChange);
    wired = true;
  }
  const engines = sortEngines(Object.keys(state.enginesCatalog));
  wrap.innerHTML = `
    <div class="note">${icon('info')}<span>用开关启用或停用模型。停用的模型会从启动面板与 MCP 可用模型列表中移除；备注是写给主 Agent 看的模型说明。</span></div>
    ${engines.length ? engines.map(engineCardHtml).join('') : '<div class="list-empty">没有发现任何引擎，试试右上角的「重新扫描模型」。</div>'}`;
}

function engineCardHtml(engine) {
  const info = state.enginesCatalog[engine] || {};
  if (Array.isArray(info.models)) return modelsCardHtml(engine, info);
  return legacyCardHtml(engine, info); // older hub without B3's normalised model list
}

function cardHeadHtml(engine, info, enabledCount, total, bulkLabel, bulkDisabled) {
  const e = escapeHtml(engine);
  return `
    <header class="ec-head">
      <span class="engine-badge" data-engine="${e}">${e}</span>
      <h4 class="ec-name">${escapeHtml(info.name || engine)}</h4>
      <span class="state-badge ${info.installed ? 'ok' : 'off'}">${info.installed ? `${icon('check', 'icon-xs')}已安装` : '未检测到本地程序'}</span>
      <span class="ec-bin" title="${escapeHtml(info.binary || '')}">${escapeHtml(info.binary || '')}</span>
      <div class="ec-actions">
        <button type="button" class="btn btn-sm" data-action="enable-all" ${total ? '' : 'disabled'}>全部启用</button>
        <button type="button" class="btn btn-sm" data-action="only-default" ${bulkDisabled ? 'disabled' : ''}
          title="${bulkLabel}">${bulkLabel}</button>
      </div>
    </header>`;
}

function modelsCardHtml(engine, info) {
  const models = info.models;
  const enabledCount = models.filter((m) => m.enabled !== false).length;
  const flags = info.cli_flags_template || {};
  const rows = models.length
    ? models.map((m) => modelRowHtml(engine, m)).join('')
    : '<div class="list-empty">未检测到模型</div>';
  return `
    <section class="engine-card" data-engine-card="${escapeHtml(engine)}">
      ${cardHeadHtml(engine, info, enabledCount, models.length, '只保留各 Profile 的默认模型', !models.length)}
      <div class="ec-meta">
        <span>模型参数 <code>${escapeHtml(flags.model || 'N/A')}</code></span>
        <span>思考等级参数 <code>${escapeHtml(flags.thinking_level || 'N/A')}</code></span>
        <span class="ec-count">${enabledCount} / ${models.length} 已启用</span>
      </div>
      <div class="model-rows">${rows}</div>
    </section>`;
}

function modelRowHtml(engine, model) {
  const enabled = model.enabled !== false;
  const isDefault = model.id === engineDefaultModel(engine);
  const levels = (model.thinking_levels || []).filter(Boolean);
  const pills = levels.map((level) =>
    `<span class="pill pill-effort${level === model.default_thinking_level ? ' is-default' : ''}" title="${level === model.default_thinking_level ? '模型默认思考等级' : ''}">${escapeHtml(level)}</span>`).join('');
  const note = model.note || '';
  return `
    <div class="model-row${enabled ? '' : ' is-off'}" data-model="${escapeHtml(model.id)}" data-enabled="${enabled}">
      <label class="switch" title="${enabled ? '停用' : '启用'} ${escapeHtml(model.id)}">
        <input type="checkbox" data-action="toggle-model" ${enabled ? 'checked' : ''} />
        <span class="switch-track"></span>
      </label>
      <div class="mr-main">
        <div class="mr-id" title="${escapeHtml(model.label || model.id)}">${escapeHtml(model.id)}${isDefault ? '<span class="def-tag">引擎默认</span>' : ''}</div>
        <div class="mr-levels">${pills || '<span class="muted">无思考等级</span>'}</div>
      </div>
      <div class="mr-note" data-note="${escapeHtml(note)}">
        <span class="mr-note-text${note ? '' : ' is-empty'}">${escapeHtml(note || '（无备注）')}</span>
        <button type="button" class="icon-btn sm" data-action="edit-note" title="编辑模型备注">${icon('pencil')}</button>
      </div>
    </div>`;
}

// ------------------------------------------------------------- old hub shape
function legacyCardHtml(engine, info) {
  const all = info.all_discovered_models || info.available_models || [];
  const disabled = new Set(info.disabled_models || []);
  const enabledCount = all.filter((m) => !disabled.has(m)).length;
  const levels = info.available_thinking_levels || [];
  const flags = info.cli_flags_template || {};
  const chips = all.map((m) => {
    const enabled = !disabled.has(m);
    return `<button type="button" class="model-chip" data-action="toggle-model" data-model="${escapeHtml(m)}" aria-pressed="${enabled}"
        title="点击${enabled ? '停用' : '启用'} ${escapeHtml(m)}">
        <span class="chk">${icon('check')}</span>${escapeHtml(m)}${m === info.default_model ? '<span class="def-tag">默认</span>' : ''}
      </button>`;
  }).join('');
  const levelPills = levels.map((l) =>
    `<span class="pill pill-effort${l === info.default_thinking_level ? ' is-default' : ''}">${escapeHtml(l)}</span>`).join('');
  return `
    <section class="engine-card" data-engine-card="${escapeHtml(engine)}">
      ${cardHeadHtml(engine, info, enabledCount, all.length, '仅保留默认', !info.default_model)}
      <div class="ec-meta">
        <span>模型参数 <code>${escapeHtml(flags.model || 'N/A')}</code></span>
        <span>思考等级参数 <code>${escapeHtml(flags.thinking_level || 'N/A')}</code></span>
      </div>
      <div>
        <div class="ec-label">模型 <span class="ec-count">${enabledCount} / ${all.length} 已启用</span></div>
        <div class="chip-wrap">${chips || '<span class="muted">未检测到模型</span>'}</div>
      </div>
      <div>
        <div class="ec-label">思考等级 <span class="ec-count">${levels.length ? `${levels.length} 档` : ''}</span></div>
        <div class="chip-wrap">${levelPills || '<span class="muted">该引擎没有独立的思考等级</span>'}</div>
      </div>
    </section>`;
}

// ------------------------------------------------------------ profile facts
function profileDefaultsUsing(engine, model) {
  return Object.entries(state.profiles)
    .filter(([, p]) => p.engine === engine && p.enabled !== false
      && defaultModelSource(p) === 'profile' && p.default_model === model)
    .map(([id]) => id);
}

function modelsFor(engine) {
  const info = state.enginesCatalog[engine] || {};
  if (Array.isArray(info.models)) return info.models;
  const all = info.all_discovered_models || info.available_models || [];
  const disabled = new Set(info.disabled_models || []);
  return all.map((id) => ({ id, enabled: !disabled.has(id) }));
}

function firstOtherEnabled(engine, model) {
  return modelsFor(engine).find((m) => m.enabled !== false && m.id !== model)?.id || '';
}

/** The models that must stay enabled for this engine: every Profile default plus the engine default. */
function keptModels(engine) {
  const keep = new Set();
  const engineDefault = engineDefaultModel(engine) || state.enginesCatalog[engine]?.default_model || '';
  if (engineDefault) keep.add(engineDefault);
  for (const [, p] of Object.entries(state.profiles)) {
    if (p.engine !== engine || p.enabled === false) continue;
    if (defaultModelSource(p) === 'profile' && p.default_model) keep.add(p.default_model);
  }
  return keep;
}

// ---------------------------------------------------------------- handlers
async function onCatalogChange(ev) {
  const input = ev.target.closest('[data-action="toggle-model"]');
  if (!input) return;
  const card = input.closest('[data-engine-card]');
  const row = input.closest('.model-row');
  if (!card || !row) return;
  const engine = card.dataset.engineCard;
  const model = row.dataset.model;
  const enable = input.checked;
  if (!enable) {
    const affected = profileDefaultsUsing(engine, model);
    if (affected.length) {
      const fallback = firstOtherEnabled(engine, model);
      const ok = await confirmDialog({
        title: '停用这个模型？',
        message: `以下 Profile 默认使用 ${model}：${affected.join('、')}。停用后它们将改用 ${fallback || '（没有其他可用模型）'}。继续停用？`,
        confirmText: '停用',
        danger: true,
      });
      if (!ok) {
        input.checked = true;
        return;
      }
    }
  }
  row.classList.add('is-busy');
  try {
    await hubApi.toggleEngineModel(engine, model, enable);
    await reloadProfiles(false);
  } catch (err) {
    input.checked = !enable;
    toast(`切换失败：${err.message}`, 'error');
  } finally {
    row.classList.remove('is-busy');
  }
}

async function onCatalogClick(ev) {
  const target = ev.target.closest('[data-action]');
  const card = target?.closest('[data-engine-card]');
  if (!card) return;
  const engine = card.dataset.engineCard;
  const button = target.tagName === 'BUTTON' ? target : null;
  if (!button) return; // the switch is handled by the change event

  const action = button.dataset.action;
  if (action === 'edit-note') {
    startNoteEdit(button.closest('.model-row'), engine);
    return;
  }
  if (action === 'toggle-model') {
    await toggleLegacyModel(engine, button);
    return;
  }
  if (action === 'enable-all' || action === 'only-default') {
    await runBulk(engine, action, card);
  }
}

async function toggleLegacyModel(engine, button) {
  const enable = button.getAttribute('aria-pressed') !== 'true';
  button.classList.add('is-busy');
  try {
    await hubApi.toggleEngineModel(engine, button.dataset.model, enable);
    await reloadProfiles(false);
  } catch (err) {
    toast(`切换失败：${err.message}`, 'error');
    button.classList.remove('is-busy');
  }
}

async function runBulk(engine, action, card) {
  const info = state.enginesCatalog[engine] || {};
  const models = modelsFor(engine);
  if (!models.length) return;
  const keep = action === 'enable-all'
    ? new Set(models.map((m) => m.id))
    : keptModels(engine);
  const changes = models.filter((m) => (m.enabled !== false) !== keep.has(m.id));
  if (!changes.length) {
    toast('已经是目标状态，无需调整', 'info', 1800);
    return;
  }
  // Same confirmation rule as a single toggle: never silently disable a Profile default.
  const blocked = changes.filter((m) => !keep.has(m.id) && profileDefaultsUsing(engine, m.id).length);
  if (blocked.length) {
    const ids = blocked.map((m) => m.id).join('、');
    const ok = await confirmDialog({
      title: '停用 Profile 默认模型？',
      message: `以下模型仍是 Profile 的默认模型：${ids}。停用后相关 Profile 将改用其他模型。继续？`,
      confirmText: '继续停用',
      danger: true,
    });
    if (!ok) return;
  }
  card.setAttribute('aria-busy', 'true');
  try {
    for (const model of changes) await hubApi.toggleEngineModel(engine, model.id, keep.has(model.id));
    toast(action === 'enable-all'
      ? `已启用 ${info.name || engine} 的全部模型`
      : `${info.name || engine} 已只保留各 Profile 的默认模型`, 'success');
  } catch (err) {
    toast(`批量切换失败：${err.message}`, 'error');
  } finally {
    try { await reloadProfiles(false); } catch { /* surfaced by the next action */ }
    card.removeAttribute('aria-busy');
  }
}

// ------------------------------------------------------------- inline notes
function startNoteEdit(row, engine) {
  if (!row) return;
  const wrap = row.querySelector('.mr-note');
  if (!wrap || wrap.querySelector('input')) return;
  const model = row.dataset.model;
  const enabled = row.dataset.enabled !== 'false';
  const previous = wrap.dataset.note || '';
  wrap.innerHTML = `<input class="mr-note-input" type="text" value="${escapeHtml(previous)}"
      placeholder="给主 Agent 的模型说明，留空即删除" data-esc-local />`;
  const input = wrap.querySelector('input');
  input.focus();
  input.select();
  let done = false;
  const restore = () => renderEngineCatalog();
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (!save) {
      restore();
      return;
    }
    const note = input.value.trim();
    if (note === previous) {
      restore();
      return;
    }
    try {
      await hubApi.setModelNote(engine, model, enabled, note);
      await reloadProfiles(false);
    } catch (err) {
      toast(`保存备注失败：${err.message}`, 'error');
      restore();
    }
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      finish(true);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}
