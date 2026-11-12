#!/usr/bin/env node
import { readFileSync } from 'node:fs';

const port = Number(process.env.ROUTE_TEST_PORT ?? 8789);
const repeats = Math.max(1, Number(process.env.DISPATCH_TEST_REPEATS ?? 3));
const batchSize = Math.max(1, Number(process.env.DISPATCH_TEST_BATCH ?? 5));
const promptFile = process.env.DISPATCH_PROMPT_FILE;
if (!promptFile) throw new Error('Set DISPATCH_PROMPT_FILE to a snapshot of the Dispatch and waiting rules');
const prompt = readFileSync(promptFile, 'utf8');
const scenarios = [
  ['cleanup', 'A subagent result is collected and that subagent is no longer needed.', 'terminate'],
  ['pending', 'A subagent has a pending native approval or question that must be handled first.', 'respond'],
  ['unfinished', 'Several delegated sessions remain unfinished after this event batch.', 'wait'],
  ['title', 'You are about to start a subagent for a build failure.', 'title'],
  ['catalog', 'You need to choose an enabled profile, model, and thinking level before dispatch.', 'catalog'],
  ['env', 'The caller wants a per-session environment override that survives resume.', 'env'],
  ['args', 'The caller wants literal extra CLI tokens for a TUI session.', 'args'],
  ['mcp', 'The caller supplies structured Codex MCP servers and disabled server names.', 'mcp'],
  ['approval', 'An approval response says delivered=true but response_state=unconfirmed.', 'wait'],
  ['dsh', 'A Windows DSH sandbox check is blocked before dispatch; inspect the reason before deciding what to do.', 'inspect'],
  ['completion', 'A native turn has produced intermediate text but completion is not confirmed.', 'wait'],
  ['harness', 'You are about to spawn a DSH worker whose task builds a test harness and writes its logs and results; decide where those paths go.', 'layout'],
  ['outside', 'A DSH worker has asked approval ten times so far, once for every edit, run and log read in a scratch folder outside its cwd.', 'layout'],
  ['logreads', 'A DSH worker keeps asking approval to read the logs its own harness writes outside its cwd, and session auto_approval_rules look like a way to stop the prompts.', 'layout'],
  ['grant', 'A DSH approval for one full-access harness run offers Allow once and Allow for this session, and more harness runs will follow; pick the option.', 'once'],
  // Approving once, or leaving the file in place and answering, is right; the case guards against a needless layout change.
  ['oneoff', 'A DSH worker asks once to read a reference file outside its cwd that its task needs.', ['respond', 'once', 'inplace']],
  ['reports', 'You are about to spawn a Codex worker that will write benchmark reports and a scratch database while it works; decide where those paths go.', 'layout'],
  ['sibling', 'A Claude worker has asked approval eight times so far to edit scratch files it created in a sibling folder next to its cwd.', 'layout'],
  // Over-application guards: files the task did not create stay put, and cwd is never widened.
  ['shared', 'A DSH worker keeps asking approval to read a dataset folder outside its cwd that other projects also use; moving the dataset into its cwd would stop the prompts.', 'inplace'],
  ['creds', "A Claude worker keeps asking approval to read the user's cloud credentials file in the home folder; copying it into the worker's cwd would stop the prompts.", 'inplace'],
  ['parentdir', 'A DSH worker keeps asking approval for its log folder, a sibling of its worktree under a parent folder that also holds other projects; making that parent its cwd would stop the prompts.', 'layout'],
  // D13 approval modes: auto is the default, another mode only on a deliberate reason.
  ['modedefault', 'You are about to spawn a Codex worker for an ordinary refactor and the user said nothing about approvals; decide what to pass for approval_mode.', 'omit'],
  ['modesafe', 'You are about to spawn a Claude worker and want to be explicit and safe about approvals, so passing approval_mode or bypass_approvals looks tempting; decide what to pass for approval_mode.', 'omit'],
  ['modeself', 'The user wants to approve every risky command of a Codex worker personally; decide what to pass for approval_mode.', 'ask'],
  ['modenone', 'The user explicitly asked for a throwaway Claude worker with no approval checks at all; decide what to pass for approval_mode.', 'bypass'],
  // Wait-URL lifecycle (2026-11-12 stale-ask report): one URL per session, every ask acted on, no id filters.
  ['nextask', 'You answered an approval with respond_to_subagent and it returned ok; the worker keeps running and may ask again.', 'wait'],
  ['sentok', "You answered an approval with send_to_subagent(text='y') and it returned ok; the worker keeps running.", 'wait'],
  ['askagain', 'After you answered approval int-1 and got ok, the next GET of wait_url returns an ask with request_id int-2.', 'respond'],
  ['staleask', 'wait_url keeps returning an ask for request int-1, which you answered successfully several minutes ago.', 'inspect'],
  ['skipid', 'Your background waiter script keeps waking on an ask you already answered; making the script skip responses that contain that request_id would stop it.', 'inspect'],
  ['pollstatus', 'You are writing a background waiter for a subagent; GET /api/sessions/<id> also shows its status and pending approval, so polling it looks simpler than the wait URL.', 'wait'],
  ['samehandle', 'send_to_subagent returned the same wait_handle as the original spawn, and you wonder which URL to wait on now.', 'wait'],
  // Over-application guard: waiting on wait_url must not stop a deliberate progress check.
  ['progress', 'The user asks what a running subagent is doing right now; its wait_url still says waiting.', 'inspect'],
];
const expected = Object.fromEntries(scenarios.map(([id, , action]) => [id, [action].flat()]));
const valid = new Set(['terminate', 'respond', 'wait', 'title', 'catalog', 'env', 'args', 'mcp', 'inspect', 'defer', 'layout', 'once', 'session', 'rules', 'inplace', 'widen', 'omit', 'ask', 'bypass', 'filter', 'poll']);
const nl = String.fromCharCode(10);

function normalizeAction(value) {
  const v = value.toLowerCase().replace(/\s+/g, ' ').trim();
  for (const token of valid) if (v.includes(token)) return token;
  if (v.includes('terminate_subagent') || v.includes('close')) return 'terminate';
  if (v.includes('wait_url') || v.includes('/api/wait/')) return 'wait';
  if (v.includes('respond_to_subagent')) return 'respond';
  if (v.includes('inspect') || v.includes('check')) return 'inspect';
  return v;
}

async function ask(batch) {
  const input = [
    'You are a protocol evaluator. Read the dispatch/waiting rules below.',
    prompt,
    `Output exactly ${batch.length} lines in input order, one for every case; never omit a case. Format id|action and copy ids exactly. action must be one of terminate, respond, wait, title, catalog, env, args, mcp, inspect, defer, layout, once, session, rules, inplace, widen, omit, ask, bypass, filter, poll. wait means GET the wait_url; inspect means call get_subagent_status_and_trace; filter means make your waiter skip or ignore that request; poll means poll the session status endpoint instead of the wait_url. layout means put the paths under cwd, moving files there if needed, or respawn from a folder holding them; inplace means leave the files where they are and keep answering their approvals; widen means set cwd to a common parent of the paths; rules means set auto_approval_rules; once and session are the options Allow once and Allow for this session, output for a case that asks you to pick an approval option; omit means leave approval_mode unset, and ask or bypass means pass that approval_mode value. For title, output the literal token title, never a generated title. No JSON or explanation.`,
    batch.map(([id, text]) => `${id}: ${text}`).join(nl),
  ].join(nl + nl); // blank lines between the parts: run together, whole batches went unanswered (2026-11-12)
  const res = await fetch(`http://127.0.0.1:${port}/v1/responses`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: 'Bearer eval-dummy-key' },
    body: JSON.stringify({ model: `model-${port}`, max_output_tokens: 1400, input: [{ type: 'message', role: 'user', content: input }] }),
  });
  const body = await res.json();
  const output = (body.output ?? []).flatMap((item) => item.content ?? []).map((part) => part.text ?? '').join('');
  if (process.env.DISPATCH_TEST_DEBUG === '1') console.error(output);
  const got = [];
  const protocolErrors = [];
  for (const raw of output.split(nl)) {
    const line = raw.trim();
    if (!line) continue;
    const fields = line.split('|');
    if (fields.length !== 2 || !expected[fields[0]]) {
      protocolErrors.push({ line });
      continue;
    }
    const action = normalizeAction(fields[1]);
    if (!valid.has(action)) {
      protocolErrors.push({ line, reason: 'unknown action' });
      continue;
    }
    got.push({ id: fields[0], action });
  }
  return { usage: body.usage ?? {}, got, protocolErrors, expectedIds: new Set(batch.map(([id]) => id)) };
}

const runs = [];
for (let repeat = 0; repeat < repeats; repeat += 1) {
  for (let offset = 0; offset < scenarios.length; offset += batchSize) {
    runs.push(await ask(scenarios.slice(offset, offset + batchSize)));
  }
}
const stats = {
  promptFile, port, repeats, batchSize, calls: runs.length, expected: scenarios.length * repeats,
  seen: 0, passed: 0, missing: 0, errors: [], protocolErrors: [],
  inputTokens: runs.reduce((sum, run) => sum + (run.usage.input_tokens ?? 0), 0),
  outputTokens: runs.reduce((sum, run) => sum + (run.usage.output_tokens ?? 0), 0),
};
for (const run of runs) {
  const seen = new Set();
  for (const item of run.got) {
    if (seen.has(item.id)) {
      stats.protocolErrors.push({ id: item.id, reason: 'duplicate id' });
      continue;
    }
    seen.add(item.id);
    stats.seen += 1;
    if (expected[item.id].includes(item.action)) stats.passed += 1;
    else stats.errors.push({ id: item.id, actual: item.action, expected: expected[item.id].join('/') });
  }
  stats.missing += [...run.expectedIds].filter((id) => !seen.has(id)).length;
  stats.protocolErrors.push(...run.protocolErrors);
}
console.log(JSON.stringify(stats, null, 2));
