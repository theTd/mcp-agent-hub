// "引擎与模型" tab: per-engine models with enable switches, thinking-level pills and notes.
//
// Aggregator engines (`omp` with `google/…`, `ollama-cloud/…`, `kimi` with `kimi-code/…`) render as
// collapsible model groups with one tri-state switch per group, so a 60-model channel folds into one
// row and toggles in a single request. Engines whose ids carry no `vendor/` prefix keep the flat
// `.model-rows` list exactly as before.
import { hubApi } from './hub-api.js';
import {
  state, reloadProfiles, sortEngines, engineDefaultModel, defaultModelSource,
} from './console-state.js';
import { $, escapeHtml, icon, toast, confirmDialog } from './ui-kit.js';

let wired = false;

/** Model-search text per engine, kept across re-renders (typing must never lose focus). */
const searchQueries = new Map();
/** Manual collapse choice per `${engine}:${group}`; absent = the default rule decides. */
const groupCollapse = new Map();

export function renderEngineCatalog() {
  const wrap = $('#engineCatalog');
  if (!wired) {
    wrap.addEventListener('click', onCatalogClick);
    wrap.addEventListener('change', onCatalogChange);
    wrap.addEventListener('input', onCatalogInput);
    wired = true;
  }
  // A full re-render (after every reloadProfiles) would otherwise drop the caret out of the filter box.
  const focus = captureFilterFocus(wrap);
  const engines = sortEngines(Object.keys(state.enginesCatalog));
  wrap.innerHTML = `
    <div class="note">${icon('info')}<span>用开关启用或停用引擎与模型。停用引擎后不能再新建或恢复该引擎的会话，正在运行的会话不受影响；停用的模型会从启动面板与 MCP 可用模型列表中移除；备注是写给主 Agent 看的模型说明。聚合渠道的引擎（模型名形如 <code>google/xxx</code>）按渠道分组，分组开关一次启用或停用整组，新发现的同组模型跟随分组策略。</span></div>
    ${engines.length ? engines.map(engineCardHtml).join('') : '<div class="list-empty">没有发现任何引擎，试试右上角的「重新扫描模型」。</div>'}`;
  wrap.querySelectorAll('[data-engine-card]').forEach((card) => {
    const engine = card.dataset.engineCard;
    card.querySelectorAll('input[data-indeterminate="true"]').forEach((el) => { el.indeterminate = true; });
    if (card.querySelector('.mg-toolbar')) applyModelFilter(card, engine);
  });
  restoreFilterFocus(wrap, focus);
}

function captureFilterFocus(wrap) {
  const active = document.activeElement;
  if (!active || !wrap.contains(active)) return null;
  if (!active.matches?.('[data-action="filter-models"]')) return null;
  return { engine: active.closest('[data-engine-card]')?.dataset.engineCard || '', caret: active.selectionStart };
}

function restoreFilterFocus(wrap, focus) {
  wrap.querySelectorAll('[data-action="filter-models"]').forEach((input) => {
    const engine = input.closest('[data-engine-card]')?.dataset.engineCard || '';
    input.value = searchQueries.get(engine) || '';
  });
  if (!focus) return;
  const input = Array.from(wrap.querySelectorAll('[data-action="filter-models"]'))
    .find((el) => (el.closest('[data-engine-card]')?.dataset.engineCard || '') === focus.engine);
  if (!input) return;
  input.focus();
  const caret = Math.min(input.value.length, Math.max(0, Number(focus.caret) || 0));
  try { input.setSelectionRange(caret, caret); } catch { /* type=search quirks are harmless */ }
}

// ---------------------------------------------------------------- group model
/** Aggregator prefix of a model id: only the FIRST `/` splits (matches the hub's extract_model_group). */
export function extractModelGroup(id) {
  const text = String(id ?? '');
  const slash = text.indexOf('/');
  if (slash < 0) return '';
  const prefix = text.slice(0, slash).trim();
  const rest = text.slice(slash + 1).trim();
  return prefix && rest ? prefix : '';
}

function modelGroup(model) {
  return typeof model.group === 'string' ? model.group : extractModelGroup(model.id);
}

function collectGroups(info, models) {
  const byGroup = new Map();
  const ungrouped = [];
  for (const model of models) {
    const group = modelGroup(model);
    if (!group) { ungrouped.push(model); continue; }
    if (!byGroup.has(group)) byGroup.set(group, []);
    byGroup.get(group).push(model);
  }
  const serverOrder = Array.isArray(info.model_groups) ? info.model_groups.map((g) => g.name) : [];
  const known = serverOrder.filter((name) => byGroup.has(name));
  let names = [...known, ...Array.from(byGroup.keys()).filter((name) => !known.includes(name))];
  // Enabled-containing groups first, discovery order preserved inside each half.
  const withEnabled = names.filter((name) => byGroup.get(name).some((m) => m.enabled !== false));
  names = [...withEnabled, ...names.filter((name) => !withEnabled.includes(name))];

  const serverByName = new Map(
    (Array.isArray(info.model_groups) ? info.model_groups : []).map((g) => [g.name, g]));
  const disabledGroups = new Set(info.disabled_model_groups || []);
  const groups = names.map((name) => {
    const list = byGroup.get(name);
    const server = serverByName.get(name);
    return {
      name,
      models: list,
      total: list.length,
      enabledCount: list.filter((m) => m.enabled !== false).length,
      groupDisabled: server ? Boolean(server.group_disabled) : disabledGroups.has(name),
    };
  });
  return { groups, ungrouped };
}

/** Default collapsed state: one group opens unless big; several groups fold everything chunky. */
function defaultCollapsed(group, groupCount) {
  if (groupCount <= 1) return group.total > 20;
  return !(group.enabledCount > 0 && group.total <= 30);
}

function groupKeyOf(engine, group) {
  return `${engine}:${group}`;
}

function isCollapsedNow(engine, group, groupCount, queryActive) {
  if (queryActive) return false; // a search always opens what it matched, without touching the saved choice
  const key = groupKeyOf(engine, group.name);
  if (groupCollapse.has(key)) return groupCollapse.get(key);
  return defaultCollapsed(group, groupCount);
}

// --------------------------------------------------------------------- cards
function engineCardHtml(engine) {
  const info = state.enginesCatalog[engine] || {};
  if (Array.isArray(info.models)) return modelsCardHtml(engine, info);
  return legacyCardHtml(engine, info); // older hub without B3's normalised model list
}

function cardHeadHtml(engine, info, enabledCount, total, bulkLabel, bulkDisabled) {
  const e = escapeHtml(engine);
  const engineOff = info.enabled === false;
  return `
    <header class="ec-head">
      <span class="engine-badge" data-engine="${e}">${e}</span>
      <h4 class="ec-name">${escapeHtml(info.name || engine)}</h4>
      <span class="state-badge ${info.installed ? 'ok' : 'off'}">${info.installed ? `${icon('check', 'icon-xs')}已安装` : '未检测到本地程序'}</span>
      <span class="ec-bin" title="${escapeHtml(info.binary || '')}">${escapeHtml(info.binary || '')}</span>
      <label class="switch ec-engine-switch" title="${engineOff ? '启用' : '停用'}引擎 ${e}">
        <input type="checkbox" data-action="toggle-engine" ${engineOff ? '' : 'checked'} />
        <span class="switch-track"></span>
      </label>
      <span class="ec-engine-state${engineOff ? ' is-off' : ''}">${engineOff ? '引擎已停用' : '引擎已启用'}</span>
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
  const { groups, ungrouped } = collectGroups(info, models);
  let body;
  if (groups.length) {
    body = groupedBodyHtml(engine, info, groups, ungrouped);
  } else {
    // No aggregator prefix anywhere: the familiar flat list, unchanged.
    const rows = models.length
      ? models.map((m) => modelRowHtml(engine, m)).join('')
      : '<div class="list-empty">未检测到模型</div>';
    body = `<div class="model-rows">${rows}</div>`;
  }
  return `
    <section class="engine-card${info.enabled === false ? ' is-off' : ''}" data-engine-card="${escapeHtml(engine)}">
      ${cardHeadHtml(engine, info, enabledCount, models.length, '只保留各 Profile 的默认模型', !models.length)}
      <div class="ec-meta">
        <span>模型参数 <code>${escapeHtml(flags.model || 'N/A')}</code></span>
        <span>思考等级参数 <code>${escapeHtml(flags.thinking_level || flags.thinking || 'N/A')}</code></span>
        <span class="ec-count">${enabledCount} / ${models.length} 已启用</span>
      </div>
      ${body}
    </section>`;
}

function groupedBodyHtml(engine, info, groups, ungrouped) {
  const query = (searchQueries.get(engine) || '').trim();
  const sections = groups.map((group) => groupSectionHtml(engine, info, group, groups.length, query));
  if (ungrouped.length) {
    // Trailing section for ids without a prefix, only when they coexist with grouped ones. It has no
    // group switch: a switch over "everything else" would mean nothing to the aggregator user.
    sections.push(groupSectionHtml(engine, info, {
      name: '',
      label: '其他模型',
      models: ungrouped,
      total: ungrouped.length,
      enabledCount: ungrouped.filter((m) => m.enabled !== false).length,
      groupDisabled: false,
      switchless: true,
    }, groups.length, query));
  }
  return `
    ${toolbarHtml(engine, groups.length, query)}
    <div class="model-groups">${sections.join('')}</div>
    <div class="list-empty mg-empty" hidden>没有匹配的分组或模型</div>`;
}

function toolbarHtml(engine, groupCount, query) {
  const e = escapeHtml(engine);
  return `
    <div class="mg-toolbar">
      <label class="mg-search">
        ${icon('search')}
        <input type="search" data-action="filter-models" data-engine="${e}" value="${escapeHtml(query)}"
          placeholder="筛选分组、模型或备注" autocomplete="off" spellcheck="false" aria-label="筛选模型" />
      </label>
      ${groupCount >= 2 ? `<div class="mg-toolbar-actions">
        <button type="button" class="btn btn-sm" data-action="expand-all-groups">展开全部</button>
        <button type="button" class="btn btn-sm" data-action="collapse-all-groups">全部折叠</button>
      </div>` : ''}
    </div>`;
}

function groupSectionHtml(engine, info, group, groupCount, query) {
  const key = groupKeyOf(engine, group.name);
  const collapsed = isCollapsedNow(engine, group, groupCount, Boolean(query));
  const fallbackCollapsed = groupCollapse.has(key) ? groupCollapse.get(key) : defaultCollapsed(group, groupCount);
  const total = group.total;
  const enabledCount = group.enabledCount;
  const partial = enabledCount > 0 && enabledCount < total;
  const allEnabled = total > 0 && enabledCount === total;
  const label = group.label || group.name;
  const g = escapeHtml(group.name);
  const rows = group.models.map((m) => modelRowHtml(engine, m)).join('');
  const stale = group.groupDisabled && enabledCount > 0
    ? '<span class="mg-stale" title="分组已停用，只有显式启用的模型保持开启；新发现的模型默认停用">新模型默认停用</span>'
    : '';
  const switchHtml = group.switchless ? '' : `
        <label class="switch mg-switch" title="${allEnabled ? '停用' : '启用'}分组 ${g}">
          <input type="checkbox" data-action="toggle-group" data-group="${g}"
            ${allEnabled ? 'checked' : ''} ${partial ? 'data-indeterminate="true"' : ''} />
          <span class="switch-track"></span>
        </label>`;
  return `
    <div class="model-group${group.switchless ? ' is-ungrouped' : ''}" data-group="${g}"
      data-collapsed="${collapsed ? 'true' : 'false'}" data-default-collapsed="${fallbackCollapsed ? 'true' : 'false'}"
      data-group-key="${escapeHtml(key)}">
      <div class="mg-head">
        <button type="button" class="mg-collapse-btn" data-action="toggle-group-collapse"
          aria-expanded="${collapsed ? 'false' : 'true'}">
          <span class="mg-chevron">${icon('chevron-down')}</span>
          <span class="mg-name">${escapeHtml(label)}</span>
          <span class="mg-count">${enabledCount} / ${total} 已启用</span>
        </button>
        ${stale}
        ${switchHtml}
      </div>
      <div class="mg-body model-rows">${rows}</div>
    </div>`;
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
        <div class="mr-id" title="${escapeHtml(model.label || model.id)}">${modelIdHtml(model)}${isDefault ? '<span class="def-tag">引擎默认</span>' : ''}</div>
        <div class="mr-levels">${pills || '<span class="muted">无思考等级</span>'}</div>
      </div>
      <div class="mr-note" data-note="${escapeHtml(note)}">
        <span class="mr-note-text${note ? '' : ' is-empty'}">${escapeHtml(note || '（无备注）')}</span>
        <button type="button" class="icon-btn sm" data-action="edit-note" title="编辑模型备注">${icon('pencil')}</button>
      </div>
    </div>`;
}

/** The full id stays visible and copyable; only the aggregator prefix is muted for scanning. */
function modelIdHtml(model) {
  const id = String(model.id ?? '');
  const group = modelGroup(model);
  if (group && id.startsWith(`${group}/`) && id.length > group.length + 1) {
    return `<span class="mr-prefix">${escapeHtml(group)}/</span>${escapeHtml(id.slice(group.length + 1))}`;
  }
  return escapeHtml(id);
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
    <section class="engine-card${info.enabled === false ? ' is-off' : ''}" data-engine-card="${escapeHtml(engine)}">
      ${cardHeadHtml(engine, info, enabledCount, all.length, '仅保留默认', !info.default_model)}
      <div class="ec-meta">
        <span>模型参数 <code>${escapeHtml(flags.model || 'N/A')}</code></span>
        <span>思考等级参数 <code>${escapeHtml(flags.thinking_level || flags.thinking || 'N/A')}</code></span>
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
  return all.map((id) => ({ id, enabled: !disabled.has(id), group: extractModelGroup(id) }));
}

function firstOtherEnabled(engine, model) {
  return modelsFor(engine).find((m) => m.enabled !== false && m.id !== model)?.id || '';
}

/** Fallback for a group that is about to be switched off: an enabled model OUTSIDE that group. */
function firstEnabledOutsideGroup(engine, group) {
  return modelsFor(engine)
    .find((m) => m.enabled !== false && modelGroup(m) !== group)?.id || '';
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

// ------------------------------------------------------------------ filtering
function applyModelFilter(card, engine) {
  const query = (searchQueries.get(engine) || '').trim().toLowerCase();
  let visible = 0;
  card.querySelectorAll('.model-group').forEach((groupEl) => {
    const name = (groupEl.dataset.group || '').toLowerCase();
    const rows = Array.from(groupEl.querySelectorAll('.model-row'));
    if (!query) {
      groupEl.hidden = false;
      setGroupCollapsed(groupEl, restoreCollapsed(groupEl) === 'true');
      rows.forEach((row) => { row.hidden = false; });
      visible += 1;
      return;
    }
    const nameMatch = Boolean(name) && name.includes(query);
    let anyMatch = false;
    rows.forEach((row) => {
      const modelId = (row.dataset.model || '').toLowerCase();
      const note = (row.querySelector('.mr-note')?.dataset.note || '').toLowerCase();
      const match = nameMatch || modelId.includes(query) || note.includes(query);
      row.hidden = !match;
      anyMatch = anyMatch || match;
    });
    const show = nameMatch || anyMatch;
    groupEl.hidden = !show;
    setGroupCollapsed(groupEl, false);
    if (show) visible += 1;
  });
  const empty = card.querySelector('.mg-empty');
  if (empty) empty.hidden = visible > 0;
}

function restoreCollapsed(groupEl) {
  const key = groupEl.dataset.groupKey || '';
  if (key && groupCollapse.has(key)) return groupCollapse.get(key) ? 'true' : 'false';
  return groupEl.dataset.defaultCollapsed === 'true' ? 'true' : 'false';
}

function setGroupCollapsed(groupEl, collapsed) {
  groupEl.dataset.collapsed = collapsed ? 'true' : 'false';
  const button = groupEl.querySelector('[data-action="toggle-group-collapse"]');
  if (button) button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}

// ------------------------------------------------------------------ handlers
function onCatalogInput(ev) {
  const input = ev.target.closest('[data-action="filter-models"]');
  if (!input) return;
  const card = input.closest('[data-engine-card]');
  if (!card) return;
  const engine = card.dataset.engineCard;
  searchQueries.set(engine, input.value);
  // Filtering touches visibility and collapse only: no innerHTML rebuild, so typing keeps its caret.
  applyModelFilter(card, engine);
}

async function onCatalogChange(ev) {
  const groupInput = ev.target.closest('[data-action="toggle-group"]');
  if (groupInput) {
    await toggleModelGroup(groupInput);
    return;
  }
  const engineInput = ev.target.closest('[data-action="toggle-engine"]');
  if (engineInput) {
    await toggleEngine(engineInput);
    return;
  }
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

async function toggleModelGroup(input) {
  const card = input.closest('[data-engine-card]');
  const engine = card?.dataset.engineCard;
  const group = input.dataset.group || '';
  if (!card || !engine || !group) return;
  const info = state.enginesCatalog[engine] || {};
  const summary = (info.model_groups || []).find((g) => g.name === group);
  const groupModels = modelsFor(engine).filter((m) => modelGroup(m) === group);
  const total = summary?.total ?? groupModels.length;
  // A partial group has no checked state of its own: clicking it means "turn the whole group on".
  const wasPartial = input.dataset.indeterminate === 'true';
  const targetEnable = wasPartial ? true : input.checked;
  const previous = { checked: wasPartial ? false : !input.checked, indeterminate: wasPartial };

  if (!targetEnable) {
    const affected = [];
    for (const model of groupModels) {
      if (model.enabled === false) continue;
      for (const pid of profileDefaultsUsing(engine, model.id)) {
        if (!affected.some((entry) => entry.pid === pid)) affected.push({ pid, model: model.id });
      }
    }
    if (affected.length) {
      const fallback = firstEnabledOutsideGroup(engine, group);
      const ok = await confirmDialog({
        title: `停用分组 ${group}？`,
        message: `以下 Profile 默认使用该分组的模型：${affected.map((entry) => `${entry.pid}（${entry.model}）`).join('、')}。停用后它们将改用 ${fallback || '（没有其他可用模型）'}。继续停用？`,
        confirmText: '停用分组',
        danger: true,
      });
      if (!ok) {
        restoreGroupSwitch(input, previous.checked, previous.indeterminate);
        return;
      }
    }
  }

  card.setAttribute('aria-busy', 'true');
  try {
    try {
      await hubApi.toggleEngineModelGroup(engine, group, targetEnable);
    } catch (err) {
      restoreGroupSwitch(input, previous.checked, previous.indeterminate);
      toast(`${targetEnable ? '启用' : '停用'}分组失败：${err.message}`, 'error');
      return;
    }
    try {
      await reloadProfiles(false);
    } catch (err) {
      toast(`分组状态已切换，但刷新失败：${err.message}`, 'error');
    }
    toast(targetEnable
      ? `已启用分组 ${group}（${total} 个模型）`
      : `已停用分组 ${group}（${total} 个模型）`, 'success');
  } finally {
    card.removeAttribute('aria-busy');
  }
}

function restoreGroupSwitch(input, checked, indeterminate) {
  input.checked = checked;
  input.indeterminate = indeterminate;
  if (indeterminate) input.dataset.indeterminate = 'true';
  else delete input.dataset.indeterminate;
}

async function toggleEngine(input) {
  const card = input.closest('[data-engine-card]');
  const engine = card?.dataset.engineCard;
  if (!engine) return;
  const enable = input.checked;
  if (card) card.setAttribute('aria-busy', 'true');
  try {
    try {
      await hubApi.toggleEngine(engine, enable);
    } catch (err) {
      input.checked = !enable;
      toast(`${enable ? '启用' : '停用'}引擎失败：${err.message}`, 'error');
      return;
    }
    try {
      await reloadProfiles(false);
    } catch (err) {
      toast(`引擎状态已切换，但刷新失败：${err.message}`, 'error');
    }
    if (enable) toast(`已启用引擎 ${engine}`, 'success');
    else toast(`已停用引擎 ${engine}；正在运行的会话不受影响`, 'success');
  } finally {
    if (card) card.removeAttribute('aria-busy');
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
    return;
  }
  if (action === 'toggle-group-collapse') {
    const groupEl = button.closest('.model-group');
    if (!groupEl) return;
    const collapsed = groupEl.dataset.collapsed === 'true';
    setGroupCollapsed(groupEl, !collapsed);
    const key = groupEl.dataset.groupKey;
    if (key) groupCollapse.set(key, !collapsed);
    return;
  }
  if (action === 'expand-all-groups' || action === 'collapse-all-groups') {
    setAllGroups(card, action === 'collapse-all-groups');
  }
}

function setAllGroups(card, collapse) {
  card.querySelectorAll('.model-group').forEach((groupEl) => {
    setGroupCollapsed(groupEl, collapse);
    const key = groupEl.dataset.groupKey;
    if (key) groupCollapse.set(key, collapse);
  });
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

// 全部启用 / 只保留默认: one or two batch requests instead of one request per model.
async function runBulk(engine, action, card) {
  const info = state.enginesCatalog[engine] || {};
  const models = modelsFor(engine);
  if (!models.length) return;
  const staleGroups = Array.isArray(info.disabled_model_groups) ? info.disabled_model_groups : [];
  let enableIds = [];
  let disableIds = [];
  if (action === 'enable-all') {
    enableIds = models.filter((m) => m.enabled === false).map((m) => m.id);
    if (!enableIds.length && !staleGroups.length) {
      toast('已经是目标状态，无需调整', 'info', 1800);
      return;
    }
    // Everything already looks enabled but a group policy still holds new models off: send the
    // whole list so the hub clears the group.
    if (!enableIds.length) enableIds = models.map((m) => m.id);
  } else {
    const keep = keptModels(engine);
    enableIds = models.filter((m) => m.enabled === false && keep.has(m.id)).map((m) => m.id);
    disableIds = models.filter((m) => m.enabled !== false && !keep.has(m.id)).map((m) => m.id);
    if (!enableIds.length && !disableIds.length) {
      toast('已经是目标状态，无需调整', 'info', 1800);
      return;
    }
  }
  // Same confirmation rule as a single toggle: never silently disable a Profile default.
  const blocked = disableIds.filter((id) => profileDefaultsUsing(engine, id).length);
  if (blocked.length) {
    const ok = await confirmDialog({
      title: '停用 Profile 默认模型？',
      message: `以下模型仍是 Profile 的默认模型：${blocked.join('、')}。停用后相关 Profile 将改用其他模型。继续？`,
      confirmText: '继续停用',
      danger: true,
    });
    if (!ok) return;
  }
  card.setAttribute('aria-busy', 'true');
  try {
    // Disable first, enable second: a group fully enabled by this batch then clears its policy.
    if (disableIds.length) await hubApi.toggleEngineModelsBatch(engine, disableIds, false);
    if (enableIds.length) await hubApi.toggleEngineModelsBatch(engine, enableIds, true);
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
