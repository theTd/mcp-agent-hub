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
];
const expected = Object.fromEntries(scenarios.map(([id, , action]) => [id, action]));
const valid = new Set(['terminate', 'respond', 'wait', 'title', 'catalog', 'env', 'args', 'mcp', 'inspect', 'defer']);
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
    `Output exactly ${batch.length} lines in input order, one for every case; never omit a case. Format id|action and copy ids exactly. action must be one of terminate, respond, wait, title, catalog, env, args, mcp, inspect, defer. For title, output the literal token title, never a generated title. No JSON or explanation.`,
    batch.map(([id, text]) => `${id}: ${text}`).join(nl),
  ].join(nl);
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
    if (item.action === expected[item.id]) stats.passed += 1;
    else stats.errors.push({ id: item.id, actual: item.action, expected: expected[item.id] });
  }
  stats.missing += [...run.expectedIds].filter((id) => !seen.has(id)).length;
  stats.protocolErrors.push(...run.protocolErrors);
}
console.log(JSON.stringify(stats, null, 2));
