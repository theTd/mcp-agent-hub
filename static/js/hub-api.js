// REST / WebSocket client for the hub's console API (server.py, section 4).

async function request(method, url, body, { timeoutMs } = {}) {
  const init = { method, headers: {} };
  if (timeoutMs) init.signal = AbortSignal.timeout(timeoutMs);
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!res.ok) {
    // FastAPI puts the actionable text in `detail`; surface it as the Error message.
    const detail = data?.detail ?? data?.error ?? `${res.status} ${res.statusText}`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return data ?? {};
}

const enc = encodeURIComponent;

export const hubApi = {
  listProfiles: (refresh = false) => request('GET', refresh ? '/api/profiles?refresh=true' : '/api/profiles'),
  saveProfile: (profile) => request('POST', '/api/profiles', profile),
  deleteProfile: (id) => request('DELETE', `/api/profiles/${enc(id)}`),
  toggleEngineModel: (engine, model, enabled) =>
    request('POST', `/api/engines/${enc(engine)}/models/toggle`, { model, enabled }),
  setModelNote: (engine, model, enabled, note) =>
    request('POST', `/api/engines/${enc(engine)}/models/toggle`, { model, enabled, note }),
  saveRoutingGuide: (text, timeoutMinutes) => request('POST', '/api/routing-guide', {
    global_routing_instructions: text,
    subagent_idle_timeout_minutes: timeoutMinutes,
  }),

  listSessions: () => request('GET', '/api/sessions', undefined, { timeoutMs: 10000 }),
  // Archive spec §6: the 归档 list, one record, its raw scrollback, revive and delete.
  listArchive: ({ limit = 50, offset = 0, q = '' } = {}) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (q) params.set('q', q);
    return request('GET', `/api/archive?${params.toString()}`);
  },
  archiveRecord: (id) => request('GET', `/api/archive/${enc(id)}`),
  archiveScrollback: async (id) => {
    const res = await fetch(`/api/archive/${enc(id)}/scrollback`, { headers: { Accept: 'text/plain' } });
    const text = await res.text();
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { detail = JSON.parse(text).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    return text;
  },
  resumeSession: (id, text) =>
    request('POST', `/api/sessions/${enc(id)}/resume`, text === undefined ? {} : { text }),
  deleteArchive: (id) => request('DELETE', `/api/archive/${enc(id)}`),
  createSession: (body) => request('POST', '/api/sessions', body),
  launchPreview: (body) => request('POST', '/api/launch-preview', body),
  sendInput: (id, text) => request('POST', `/api/sessions/${enc(id)}/input`, { text }),
  // The transcript's message box: delivered to a live session, or resumes an ended one with it.
  sendMessage: (id, text) => request('POST', `/api/sessions/${enc(id)}/message`, { text }),
  respondInteraction: (id, body) => request('POST', `/api/sessions/${enc(id)}/respond`, body),
  terminateSession: (id) => request('DELETE', `/api/sessions/${enc(id)}`),
  sessionFinal: (id) => request('GET', `/api/sessions/${enc(id)}/final`),
  // Bounded like listSessions: a hung request would otherwise stop this pane's refresh for good.
  transcript: (id, limit = 80) => request('GET', `/api/sessions/${enc(id)}/transcript?limit=${encodeURIComponent(limit)}`, undefined, { timeoutMs: 10000 }),

  listWin32Windows: () => request('GET', '/api/win32/windows'),
  windowFrame: (id) => request('GET', `/api/sessions/${enc(id)}/frame`),
  tuckWindow: (id, tuck) => request('POST', `/api/sessions/${enc(id)}/tuck`, { tuck }),
  clickWindow: (id, x, y) => request('POST', `/api/sessions/${enc(id)}/click`, { x, y }),

  ptySocketUrl: (id) => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/pty/${enc(id)}`,
  mcpEndpointUrl: () => `${location.origin}/mcp`,
};
