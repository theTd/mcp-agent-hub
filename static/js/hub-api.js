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
  saveRoutingGuide: (text) => request('POST', '/api/routing-guide', { global_routing_instructions: text }),

  listSessions: () => request('GET', '/api/sessions', undefined, { timeoutMs: 10000 }),
  createSession: (body) => request('POST', '/api/sessions', body),
  sendInput: (id, text) => request('POST', `/api/sessions/${enc(id)}/input`, { text }),
  terminateSession: (id) => request('DELETE', `/api/sessions/${enc(id)}`),

  listWin32Windows: () => request('GET', '/api/win32/windows'),
  windowFrame: (id) => request('GET', `/api/sessions/${enc(id)}/frame`),
  tuckWindow: (id, tuck) => request('POST', `/api/sessions/${enc(id)}/tuck`, { tuck }),
  clickWindow: (id, x, y) => request('POST', `/api/sessions/${enc(id)}/click`, { x, y }),

  ptySocketUrl: (id) => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/pty/${enc(id)}`,
  mcpEndpointUrl: () => `${location.origin}/mcp`,
};
