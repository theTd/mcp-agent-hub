// Shared console state, persisted user preferences and the event bus between console modules.
import { hubApi } from './hub-api.js';

export const state = {
  profiles: {},
  enginesCatalog: {},
  globalRoutingInstructions: '',
  routingGuideMarkdown: '',

  sessions: [],
  sessionsLoaded: false,
  activeSessionId: null,
  recentEvents: [],
  globalSeq: 0,
  // Bumped on every local change to the session list, so a poll that started earlier is discarded.
  localMutationSeq: 0,

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
