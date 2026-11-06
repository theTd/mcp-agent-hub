// "模型开关" tab: per-engine discovered models with enable / disable toggles.
import { hubApi } from './hub-api.js';
import { state, reloadProfiles, sortEngines } from './console-state.js';
import { $, escapeHtml, icon, toast } from './ui-kit.js';

let wired = false;

export function renderEngineCatalog() {
  const wrap = $('#engineCatalog');
  if (!wired) {
    wrap.addEventListener('click', onCatalogClick);
    wired = true;
  }
  const engines = sortEngines(Object.keys(state.enginesCatalog));
  wrap.innerHTML = `
    <div class="note">${icon('info')}<span>点击模型即可启用或停用。停用的模型会从启动面板和 MCP 可用模型列表中移除，主 Agent 也无法再指定它。</span></div>
    ${engines.length ? engines.map(engineCardHtml).join('') : '<div class="list-empty">没有发现任何引擎，试试右上角的「重新扫描模型」。</div>'}`;
}

function modelsOf(info) {
  const all = info.all_discovered_models || info.available_models || [];
  const disabled = new Set(info.disabled_models || []);
  return { all, disabled };
}

function engineCardHtml(engine) {
  const info = state.enginesCatalog[engine] || {};
  const { all, disabled } = modelsOf(info);
  const enabledCount = all.filter((m) => !disabled.has(m)).length;
  const levels = info.available_thinking_levels || [];
  const flags = info.cli_flags_template || {};
  const e = escapeHtml(engine);

  const chips = all.map((m) => {
    const enabled = !disabled.has(m);
    return `<button type="button" class="model-chip" data-action="toggle-model" data-model="${escapeHtml(m)}" aria-pressed="${enabled}"
        title="点击${enabled ? '停用' : '启用'} ${escapeHtml(m)}">
        <span class="chk">${icon('check')}</span>${escapeHtml(m)}${m === info.default_model ? '<span class="def-tag">默认</span>' : ''}
      </button>`;
  }).join('');
  const levelPills = levels.map((l) =>
    `<span class="pill pill-effort${l === info.default_thinking_level ? ' is-default' : ''}" title="${l === info.default_thinking_level ? '默认思考等级' : ''}">${escapeHtml(l)}</span>`).join('');

  return `
    <section class="engine-card" data-engine-card="${e}">
      <header class="ec-head">
        <span class="engine-badge" data-engine="${e}">${e}</span>
        <h4 class="ec-name">${escapeHtml(info.name || engine)}</h4>
        <span class="state-badge ${info.installed ? 'ok' : 'off'}">${info.installed ? `${icon('check', 'icon-xs')}已安装` : '未检测到本地程序'}</span>
        <span class="ec-bin" title="${escapeHtml(info.binary || '')}">${escapeHtml(info.binary || '')}</span>
        <div class="ec-actions">
          <button type="button" class="btn btn-sm" data-action="enable-all" ${all.length ? '' : 'disabled'}>全部启用</button>
          <button type="button" class="btn btn-sm" data-action="only-default" ${info.default_model ? '' : 'disabled'}
            title="只保留默认模型 ${escapeHtml(info.default_model || '')}">仅保留默认</button>
        </div>
      </header>
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

async function onCatalogClick(ev) {
  const btn = ev.target.closest('[data-action]');
  const card = btn?.closest('[data-engine-card]');
  if (!card) return;
  const engine = card.dataset.engineCard;

  if (btn.dataset.action === 'toggle-model') {
    const enable = btn.getAttribute('aria-pressed') !== 'true';
    btn.classList.add('is-busy');
    try {
      await hubApi.toggleEngineModel(engine, btn.dataset.model, enable);
      await reloadProfiles(false);
    } catch (err) {
      toast(`切换失败：${err.message}`, 'error');
      btn.classList.remove('is-busy');
    }
    return;
  }

  const info = state.enginesCatalog[engine] || {};
  const { all, disabled } = modelsOf(info);
  const keep = info.default_model || all[0];
  const enableAll = btn.dataset.action === 'enable-all';
  // Only send the toggles whose state actually changes.
  const changes = all
    .map((m) => [m, enableAll || m === keep])
    .filter(([m, wantEnabled]) => wantEnabled === disabled.has(m));
  if (!changes.length) {
    toast('已经是目标状态，无需调整', 'info', 1800);
    return;
  }
  card.setAttribute('aria-busy', 'true');
  try {
    for (const [model, enabled] of changes) await hubApi.toggleEngineModel(engine, model, enabled);
    toast(enableAll ? `已启用 ${info.name || engine} 的全部模型` : `${info.name || engine} 仅保留默认模型 ${keep}`, 'success');
  } catch (err) {
    toast(`批量切换失败：${err.message}`, 'error');
  } finally {
    try { await reloadProfiles(false); } catch { /* surfaced by the next action */ }
    card.removeAttribute('aria-busy');
  }
}
