// Shared console state, persisted user preferences and the event bus between console modules.
import { hubApi } from './hub-api.js';

export const state = {
  profiles: {},
  profilesLoaded: false,
  enginesCatalog: {},
  globalRoutingInstructions: '',
  routingGuideMarkdown: '',
  defaultCwd: '',
  hubPort: null,

  sessions: [],
  persistedSessions: [],
  // Archive spec §7: the 归档 tab's count comes from /api/sessions (archive_total), so it stays fresh
  // with the normal poll without a second request.
  archiveTotal: 0,
  sessionsLoaded: false,
  activeSessionId: null,
  recentEvents: [],
  globalSeq: 0,
  // Bumped on every local change to the session list, so a poll that started earlier is discarded.
  localMutationSeq: 0,
  // null = no poll answered yet, true = connected, false = the last poll failed.
  hubConnected: null,

  layoutMode: 'single', // 'single' | 'matrix'
};

// ------------------------------------------------------------------ event bus
const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event)?.delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try { fn(payload); } catch (err) { console.error(`[console-state] listener for "${event}" failed`, err); }
  }
}

// ---------------------------------------------------------------- preferences
const PREFS_KEY = 'subAgentHub.console.prefs.v1';
const PREF_DEFAULTS = {
  layoutMode: 'single',
  sidebarCollapsed: false,
  launcherCollapsed: true,
  eventsCollapsed: false,
  lastProfileId: '',
  recentCwds: [],
};

export const prefs = (() => {
  try {
    return { ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') };
  } catch {
    return { ...PREF_DEFAULTS };
  }
})();

export function savePrefs(patch) {
  Object.assign(prefs, patch);
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* storage unavailable */ }
}

// ------------------------------------------------------------------- profiles
export async function reloadProfiles(refreshDiscovery = false) {
  const data = await hubApi.listProfiles(refreshDiscovery);
  state.profiles = data.profiles || {};
  state.enginesCatalog = data.engines_catalog || {};
  state.globalRoutingInstructions = data.global_routing_instructions || '';
  state.routingGuideMarkdown = data.routing_guide_markdown || '';
  state.defaultCwd = data.default_cwd || '';
  state.hubPort = Number.isFinite(data.hub_port) ? data.hub_port : null;
  state.profilesLoaded = true;
  emit('profiles:changed');
}

const ENGINE_ORDER = ['omp', 'kimi', 'agy', 'codex', 'dsh', 'pwsh'];

export function engineName(engine) {
  return state.enginesCatalog[engine]?.name || engine || '其他';
}

export function sortEngines(engines) {
  const rank = (e) => {
    const i = ENGINE_ORDER.indexOf(e);
    return i === -1 ? ENGINE_ORDER.length : i;
  };
  return [...engines].sort((a, b) => rank(a) - rank(b) || String(a).localeCompare(String(b)));
}

export function sortProfiles(entries) {
  const order = sortEngines(new Set(entries.map(([, p]) => p.engine || 'other')));
  return [...entries].sort(([, a], [, b]) => {
    const byEngine = order.indexOf(a.engine || 'other') - order.indexOf(b.engine || 'other');
    if (byEngine) return byEngine;
    return String(a.name || a.id || '').localeCompare(String(b.name || b.id || ''), 'zh-CN');
  });
}

/** The engine's default model id, as reported by the catalog (`""` when unknown). */
export function engineDefaultModel(engine) {
  return state.enginesCatalog[engine]?.default_model || '';
}

/**
 * Models for one engine, normalised to B3's shape. Older hubs only expose
 * `available_models` / `disabled_models`, so the missing `models` list is derived from those:
 * the console then degrades to engine-level thinking levels instead of hiding everything.
 */
export function engineModels(engine) {
  const cat = state.enginesCatalog[engine] || {};
  if (Array.isArray(cat.models)) return cat.models;
  const all = cat.all_discovered_models || cat.available_models || [];
  const disabled = new Set(cat.disabled_models || []);
  const levels = cat.available_thinking_levels || [];
  return all.map((id) => ({
    id,
    label: id,
    thinking_levels: levels,
    default_thinking_level: cat.default_thinking_level || null,
    enabled: !disabled.has(id),
    note: '',
  }));
}

/** Where the profile's default model came from: 'profile' or 'engine' (B4's `*_source`). */
export function defaultModelSource(profile) {
  if (profile?.default_model_source) return profile.default_model_source;
  const cat = state.enginesCatalog[profile?.engine] || {};
  if (!profile?.default_model) return 'engine';
  return profile.default_model === cat.default_model ? 'engine' : 'profile';
}

/** Where the profile's default thinking level came from: 'profile' or 'engine' (B4). */
export function defaultThinkingSource(profile) {
  if (profile?.default_thinking_source) return profile.default_thinking_source;
  const cat = state.enginesCatalog[profile?.engine] || {};
  if (!profile?.default_thinking_level) return 'engine';
  return profile.default_thinking_level === cat.default_thinking_level ? 'engine' : 'profile';
}

// ------------------------------------------------------------------- sessions
export function markLocalMutation() {
  state.localMutationSeq += 1;
}

export function setActiveSession(id) {
  if (state.activeSessionId === id) return;
  state.activeSessionId = id;
  emit('session:activated', id);
}

export function upsertSession(session) {
  markLocalMutation();
  const i = state.sessions.findIndex((s) => s.session_id === session.session_id);
  if (i >= 0) state.sessions[i] = session;
  else state.sessions.push(session);
  state.sessionsLoaded = true;
  emit('sessions:changed');
}

export function removeSession(id) {
  markLocalMutation();
  state.sessions = state.sessions.filter((s) => s.session_id !== id);
  if (state.activeSessionId === id) state.activeSessionId = state.sessions.at(-1)?.session_id ?? null;
  emit('sessions:changed');
}

export function setHubConnected(ok) {
  if (state.hubConnected === ok) return;
  state.hubConnected = ok;
  emit('hub:connection', ok);
}
