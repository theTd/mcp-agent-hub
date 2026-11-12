#!/usr/bin/env node
import { readFileSync } from 'node:fs';

const port = Number(process.env.ROUTE_TEST_PORT ?? 8789);
const repeats = Math.max(1, Number(process.env.ROUTE_TEST_REPEATS ?? 3));
const batchSize = Math.max(1, Number(process.env.ROUTE_TEST_BATCH ?? 5));
const prompt = readFileSync(new URL('../docs/route-prompt-current.txt', import.meta.url), 'utf8');
const scenarios = [
  ['fact', '简单事实问答', 'sol-low', 'null'],
  ['phone', '手机单步安装并确认', 'terra-low', 'null'],
  ['game', '游戏单步操作并回报', 'terra-low', 'null'],
  ['probe', '执行一次真实审批检查调用并返回结果（不是握手或占位）', 'terra-low', 'null'],
  ['shots', '多步骤远程脚本加一组 UI 截图，只采集证据', 'terra-medium', 'null'],
  ['shotcapture', '只采集一张本地截图并返回文件，不做解释或诊断', 'terra-low', 'null'],
  ['shotdiag', '检查截图中的布局故障并解释原因，必要时提出修复', 'terra-medium', 'null'],
  ['search', '跨未知 worktree 查找状态', 'terra-medium', 'null'],
  ['livediag', '远程主机上服务健康降级，查日志定位根因、修复并验证恢复', 'terra-medium', 'null'],
  ['liveprobe', '在运行中的游戏里反复试验多轮，摸清一个未知的渲染卡顿现象，没有明确终点', 'terra-high', 'null'],
  ['retry', 'terra medium 做多步真机操作失败了，重试这项任务', 'terra-high', 'null'],
  ['mcpres', '只读调研，需要引擎知识库 MCP 工具查一个引擎接口的行为', 'terra-medium', 'null'],
  ['offline', '离线分析手机录像指标', 'dsh-high', 'null'],
  ['survey', '指定仓库事实调查，不审 commit', 'dsh-high', 'null'],
  ['impl', '按规格实现代码并补测试', 'dsh-high', 'null'],
  ['gui', '只改 GUI 代码不运行', 'dsh-high', 'null'],
  ['review', '单仓 commit 代码/设计审查', 'dsh-max', 'kimi-high'],
  ['merge', '语义冲突合并并交付，查跨文件遗漏', 'dsh-max', 'kimi-max'],
  ['ui', '多轮截图 UI/UX 精修并查遗漏', 'opus-xhigh', 'kimi-max'],
  ['balance', '设计数值平衡并查边界', 'opus-xhigh', 'kimi-max'],
  ['copy', '玩家公告和商店文案', 'agy-medium', 'null'],
  ['art', '玩家可见艺术方向', 'agy-medium', 'null'],
  ['model', '3D 模型与实体比例', 'agy-high', 'null'],
  ['refactor', '普通跨系统重构', 'dsh-high', 'kimi-max'],
  ['hardalgo', '高风险复杂算法，主代理处理', 'root', 'null'],
  ['astra', '用户明确要求 Astra 做 CAD', 'astra-high', 'null'],
  ['gap', '大型跨系统设计完整性审计', 'dsh-max', 'kimi-max'],
  ['ready', '只回复 ready，真实任务稍后', 'defer', 'null'],
  ['noise', '提到 Minecraft/GUI 但只改纯代码函数', 'dsh-high', 'null'],
  ['cad', 'CAD 空间几何但未点名 Astra', 'agy-high', 'null'],
  ['taskfail', 'DSH 已接手但任务执行失败，请选择任务失败 fallback', 'sol-xhigh', 'null'],
  ['infraoff', 'DSH 启动前不可用，请选择基础设施不可用 fallback', 'agy-high', 'null'],
  ['infraref', 'DSH 不可用的跨系统重构并查遗漏，请给第一跳 fallback', 'agy-high', 'kimi-max'],
];
const expected = Object.fromEntries(scenarios.map(([id, , primary, companion]) => [id, primary + '|' + companion]));
const nl = String.fromCharCode(10);

function normalizePrimary(value, id) {
  const v = value.toLowerCase().replace(/\s+/g, ' ').trim();
  if (v === 'defer' || (id === 'ready' && v === '')) return 'defer';
  if (v.includes('gpt-6-astra')) return 'astra-high';
  if (v.includes('opus-5-5') || v.includes('claude-opus-5-5')) return 'opus-xhigh';
  if (v.includes('deepseek') && v.includes('max')) return 'dsh-max';
  if (v.includes('deepseek') && v.includes('high')) return 'dsh-high';
  if (v.includes('terra') && v.includes('medium')) return 'terra-medium';
  if (v.includes('terra') && /(^|[^a-z])low/.test(v)) return 'terra-low';
  if (v.includes('terra') && v.includes('high')) return 'terra-high';
  if (v.includes('gemini-4-argon') && v.includes('medium')) return 'agy-medium';
  if (v.includes('gemini-4-argon') && v.includes('high')) return 'agy-high';
  if (v.includes('gpt-6.1-sol') && v.includes('xhigh')) return id === 'hardalgo' ? 'root' : 'sol-xhigh';
  if (v.includes('gpt-6.1-sol') && v.includes('low')) return 'sol-low';
  if (v.includes('gpt-6.1-sol') && v.includes('high')) return 'sol-high';
  if (v === 'root' || v.includes('primary codex')) return 'root';
  return v;
}

function normalizeCompanion(value) {
  const v = value.toLowerCase().replace(/\s+/g, ' ').trim();
  if (!v || v === 'null' || v === 'none' || v === '-') return 'null';
  if (v.includes('kimi') && v.includes('max')) return 'kimi-max';
  if (v.includes('kimi')) return 'kimi-high';
  return v;
}

async function ask(batch) {
  const input = [
    '你是路由评测器。',
    prompt,
    '输出每个输入一行，格式为 id|primary|companion；id 必须从输入中原样复制，大小写和字符都不能改。primary 必须使用 sol-low、terra-low、terra-medium、terra-high、dsh-high、dsh-max、opus-xhigh、agy-medium、agy-high、root、astra-high、sol-high、sol-xhigh 或 defer；companion 只能使用 kimi-high、kimi-max 或 null。不要 JSON、代码围栏或解释。',
    batch.map(([id, text]) => id + ': ' + text).join(nl),
  ].join(nl);
  const res = await fetch(`http://127.0.0.1:${port}/v1/responses`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: 'Bearer eval-dummy-key' },
    body: JSON.stringify({ model: `model-${port}`, max_output_tokens: 1400, input: [{ type: 'message', role: 'user', content: input }] }),
  });
  const body = await res.json();
  const output = (body.output ?? []).flatMap((item) => item.content ?? []).map((part) => part.text ?? '').join('');
  const got = [];
  const protocolErrors = [];
  for (const line of output.split(nl)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const fields = trimmed.split('|');
    if (fields.length !== 3 || !expected[fields[0]]) {
      protocolErrors.push({ line: trimmed });
      continue;
    }
    const primary = normalizePrimary(fields[1], fields[0]);
    const companion = normalizeCompanion(fields[2]);
    const validPrimary = new Set(['sol-low', 'terra-low', 'terra-medium', 'terra-high', 'dsh-high', 'dsh-max', 'opus-xhigh', 'agy-medium', 'agy-high', 'root', 'astra-high', 'sol-high', 'sol-xhigh', 'defer']);
    const validCompanion = new Set(['kimi-high', 'kimi-max', 'null']);
    if (!validPrimary.has(primary) || !validCompanion.has(companion)) {
      protocolErrors.push({ line: trimmed, reason: 'unknown token' });
      continue;
    }
    got.push({ id: fields[0], primary, companion });
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
  port, repeats, batchSize, calls: runs.length, expected: scenarios.length * repeats,
  seen: 0, passed: 0, missing: 0,
  primaryErrors: [],
  companionErrors: [],
  protocolErrors: [],
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
    const [wantPrimary, wantCompanion] = expected[item.id].split('|');
    if (item.primary !== wantPrimary) stats.primaryErrors.push({ id: item.id, actual: item.primary, expected: wantPrimary });
    if (item.companion !== wantCompanion) stats.companionErrors.push({ id: item.id, actual: item.companion, expected: wantCompanion });
    if (item.primary === wantPrimary && item.companion === wantCompanion) stats.passed += 1;
  }
  stats.missing += [...run.expectedIds].filter((id) => !seen.has(id)).length;
  stats.protocolErrors.push(...run.protocolErrors);
}
console.log(JSON.stringify(stats, null, 2));
