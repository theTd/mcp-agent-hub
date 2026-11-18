#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Each scenario: [id, situation, primary, companion, notice]. A list accepts any of its tokens.
// notice: tune = the parent must remind the user to adjust the routing rule for that job type.
export const scenarios = [
  ['fact', '简单事实问答', 'sol-low', 'null', 'none'],
  ['phone', '手机单步安装并确认', 'terra-low', 'null', 'none'],
  ['game', '游戏单步操作并回报', 'terra-low', 'null', 'none'],
  ['probe', '执行一次真实审批检查调用并返回结果（不是握手或占位）', 'terra-low', 'null', 'none'],
  ['shots', '多步骤远程脚本加一组 UI 截图，只采集证据', 'terra-medium', 'null', 'none'],
  ['shotcapture', '只采集一张本地截图并返回文件，不做解释或诊断', 'terra-low', 'null', 'none'],
  ['shotdiag', '检查截图中的布局故障并解释原因，必要时提出修复', 'terra-medium', 'null', 'none'],
  ['search', '跨未知 worktree 查找状态', 'terra-medium', 'null', 'none'],
  ['livediag', '远程主机上服务健康降级，查日志定位根因、修复并验证恢复', 'terra-medium', 'null', 'none'],
  ['liveprobe', '在运行中的游戏里反复试验多轮，摸清一个未知的渲染卡顿现象，没有明确终点', 'terra-high', 'null', 'none'],
  // Cause not stated: either notice answer is defensible.
  ['retry', 'terra medium 做多步真机操作失败了，重试这项任务', 'terra-high', 'null', ['tune', 'none']],
  ['mcpres', '只读调研，需要引擎知识库 MCP 工具查一个引擎接口的行为', 'omp-max', 'null', 'none'],
  ['offline', '离线分析手机录像指标', 'omp-high', 'null', 'none'],
  ['survey', '指定仓库事实调查，不审 commit', 'omp-max', 'null', 'none'],
  // Research over-application guards: static research never stays on omp-high.
  ['researcharch', '调研几种消息队列的架构取舍，给出技术方案对比，不写代码', 'omp-max', 'null', 'none'],
  ['researchlib', '离线调研两个开源库的实现差异并做来源对比，不启动任何服务', 'omp-max', 'null', 'none'],
  ['researchplain', '研究某个技术方案在几个项目里的落地方式，先查资料再汇报', 'omp-max', 'null', 'none'],
  ['impl', '按规格实现代码并补测试', 'omp-high', 'null', 'none'],
  ['gui', '只改 GUI 代码不运行', 'omp-high', 'null', 'none'],
  ['review', '单仓 commit 代码/设计审查', 'omp-max', 'kimi-high', 'none'],
  ['merge', '语义冲突合并并交付，查跨文件遗漏', 'omp-max', 'kimi-max', 'none'],
  ['ui', '多轮截图 UI/UX 精修并查遗漏', 'opus-xhigh', 'kimi-max', 'none'],
  ['balance', '设计数值平衡并查边界', 'opus-xhigh', 'kimi-max', 'none'],
  ['copy', '玩家公告和商店文案', 'agy-medium', 'null', 'none'],
  ['art', '玩家可见艺术方向', 'agy-medium', 'null', 'none'],
  ['model', '3D 模型与实体比例', 'carbon-medium', 'null', 'none'],
  ['modelinfra', 'AGY 在启动前不可用，需要继续完成 3D 建模', 'sol-xhigh', 'null', 'none'],
  ['modelfail', 'AGY 做 3D 建模交回的模型比例全错、漏做一半部件；brief 完整、环境正常。给出下一跳', 'sol-xhigh', 'null', 'tune'],
  ['refactor', '普通跨系统重构', 'omp-high', 'kimi-max', 'none'],
  ['hardalgo', '高风险复杂算法，主代理处理', 'root', 'null', 'none'],
  ['astra', '用户明确要求 Astra 做 CAD', 'astra-high', 'null', 'none'],
  ['gap', '大型跨系统设计完整性审计', 'omp-max', 'kimi-max', 'none'],
  ['ready', '只回复 ready，真实任务稍后', 'defer', 'null', 'none'],
  ['noise', '提到 Minecraft/GUI 但只改纯代码函数', 'omp-high', 'null', 'none'],
  ['cad', 'CAD 空间几何但未点名 Astra', 'carbon-medium', 'null', 'none'],
  ['localcheck', '在本机用一两条命令查看某个配置文件里的端口号', 'root', 'null', 'none'],
  // Cause not stated: either fallback level and either notice answer is defensible.
  ['taskfail', 'OMP 已接手但任务执行失败，请选择任务失败 fallback', ['sol-xhigh', 'sol-medium'], 'null', ['tune', 'none']],
  ['infraoff', 'OMP 启动前不可用，请选择基础设施不可用 fallback', 'agy-high', 'null', 'none'],
  ['infraref', 'OMP 不可用的跨系统重构并查遗漏，请给第一跳 fallback', 'agy-high', 'kimi-max', 'none'],
  // Capability shortfalls: the brief and the environment were fine, the model fell short.
  ['halfimpl', 'OMP high 已接手按规格实现代码，交回时只完成一半函数、没写测试；brief 完整、环境正常。给出下一跳', 'sol-xhigh', 'null', 'tune'],
  ['wrongdiag', 'terra medium 做远程诊断，交回的根因与日志里的明确证据相反；brief 清楚、主机可访问。给出重试路由', 'terra-high', 'null', 'tune'],
  // The fallback route for a review has no companion rule of its own: kimi-high or null.
  ['missreview', 'OMP max 做单仓 commit 审查，结论是无问题，却漏掉 diff 里一个明显的越界 bug。给出下一跳', 'sol-xhigh', ['kimi-high', 'null'], 'tune'],
  ['agyfail', 'OMP 启动前不可用，转 AGY high 做的跨系统重构交回时一半没改完、编译不过；brief 完整。给出下一跳', 'sol-xhigh', ['kimi-max', 'null'], 'tune'],
  // Routes with no fallback rule: the next route is open, the reminder is not.
  ['opusui', 'Opus xhigh 做 UI 精修，交回的版本有一半改动违背 brief 里写明的需求；brief 完整、环境正常。给出下一步', ['opus-xhigh', 'root', 'sol-xhigh', 'defer'], ['kimi-max', 'null'], 'tune'],
  ['lowfail', 'terra low 单步手机安装，回报已装好，实际没装上；brief 清楚、手机正常。给出重试路由', ['terra-low', 'terra-medium', 'terra-high'], 'null', 'tune'],
  // Failures with another cause: sol medium, never sol xhigh, and no routing reminder.
  ['briefgap', 'OMP high 交回的实现不对，原因是 brief 漏写了接口规格文件路径，worker 按 brief 做的部分都正确。给出补全 brief 后的派发', 'sol-medium', 'null', 'none'],
  ['envfail', 'OMP high 已接手实现，测试依赖的本地数据库没启动，测试跑不起来而失败；代码按规格写对了。环境修好后给出下一跳', 'sol-medium', 'null', 'none'],
  ['agyinfra', 'OMP 启动前不可用，转 AGY high 做的实现因网络中断中途失败，它此前的改动都正确。给出下一跳', 'sol-medium', 'null', 'none'],
  ['noaccess', 'OMP high 实现任务卡住：仓库目录没有写权限，worker 报告后停下。权限补上后给出下一跳', 'sol-medium', 'null', 'none'],
  // Other stops: nothing to escalate. Their next route is secondary here.
  ['blocked', 'terra medium 按 brief 停下报告：另一位测试者的在线世界锁挡住了操作，它没有强制解锁。给出下一步', ['defer', 'terra-medium'], 'null', 'none'],
  ['hubdrop', 'terra medium 会话因 Hub 进程重启中途断开，任务未完成，它此前的操作都正确。给出重新派发', ['terra-medium', 'terra-high'], 'null', 'none'],
  ['cancelled', '用户中途取消了 OMP 正在做的实现任务，说以后再做', 'defer', 'null', 'none'],
  ['success', 'OMP high 按规格完成实现并补了测试，主代理复核全部正确。给出后续路由', ['defer', 'omp-high'], 'null', 'none'],
];

export const primaryTokens = ['sol-low', 'terra-low', 'terra-medium', 'terra-high', 'omp-high', 'omp-max', 'opus-xhigh', 'agy-medium', 'agy-high', 'carbon-medium', 'root', 'astra-high', 'sol-medium', 'sol-high', 'sol-xhigh', 'defer'];
export const companionTokens = ['kimi-high', 'kimi-max', 'null'];
export const noticeTokens = ['tune', 'none'];
export const preamble = '你是路由评测器。';
export const instruction = '输出每个输入一行，格式为 id|primary|companion|notice；id 必须从输入中原样复制，大小写和字符都不能改。'
  + `primary 必须使用 ${primaryTokens.join('、')}，defer 表示现在不启动任何模型；companion 只能使用 ${companionTokens.join('、')}；`
  + 'notice 只能使用 tune（主代理须在汇报中提醒用户调整这类任务的路由规则）或 none（不提醒）。不要 JSON、代码围栏或解释。';

const nl = String.fromCharCode(10);
const accepts = (want) => [want].flat();

function normalizePrimary(value, id) {
  const v = value.toLowerCase().replace(/\s+/g, ' ').trim();
  if (v === 'defer' || (id === 'ready' && v === '')) return 'defer';
  if (v.includes('gpt-6.2-astra')) return 'astra-high';
  if (v.includes('opus-5-5') || v.includes('claude-opus-5-5')) return 'opus-xhigh';
  if (v.includes('deepseek') && v.includes('max')) return 'omp-max';
  if (v.includes('deepseek') && v.includes('high')) return 'omp-high';
  if (v.includes('terra') && v.includes('medium')) return 'terra-medium';
  if (v.includes('terra') && /(^|[^a-z])low/.test(v)) return 'terra-low';
  if (v.includes('terra') && v.includes('high')) return 'terra-high';
  if (v.includes('gemini-4-carbon') && v.includes('medium')) return 'carbon-medium';
  if (v.includes('gemini-4-argon') && v.includes('medium')) return 'agy-medium';
  if (v.includes('gemini-4-argon') && v.includes('high')) return 'agy-high';
  if (v.includes('gpt-6.2-sol') && v.includes('xhigh')) return id === 'hardalgo' ? 'root' : 'sol-xhigh';
  if (v.includes('gpt-6.2-sol') && v.includes('low')) return 'sol-low';
  if (v.includes('gpt-6.2-sol') && v.includes('medium')) return 'sol-medium';
  if (v.includes('gpt-6.2-sol') && v.includes('high')) return 'sol-high';
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

function normalizeNotice(value) {
  const v = value.toLowerCase().replace(/\s+/g, ' ').trim();
  if (v.includes('tune')) return 'tune';
  if (!v || v === 'none' || v === 'null' || v === '-') return 'none';
  return v;
}

async function ask(prompt, port, batch, expected) {
  const input = [
    preamble,
    prompt,
    instruction,
    batch.map(([id, text]) => id + ': ' + text).join(nl),
  ].join(nl + nl); // blank lines between the parts: run together, whole batches went unanswered (2026-11-12)
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
    if (fields.length !== 4 || !expected[fields[0]]) {
      protocolErrors.push({ line: trimmed });
      continue;
    }
    const primary = normalizePrimary(fields[1], fields[0]);
    const companion = normalizeCompanion(fields[2]);
    const notice = normalizeNotice(fields[3]);
    if (!primaryTokens.includes(primary) || !companionTokens.includes(companion) || !noticeTokens.includes(notice)) {
      protocolErrors.push({ line: trimmed, reason: 'unknown token' });
      continue;
    }
    got.push({ id: fields[0], primary, companion, notice });
  }
  return { usage: body.usage ?? {}, got, protocolErrors, expectedIds: new Set(batch.map(([id]) => id)) };
}

async function main() {
  const port = Number(process.env.ROUTE_TEST_PORT ?? 8789);
  const repeats = Math.max(1, Number(process.env.ROUTE_TEST_REPEATS ?? 3));
  const batchSize = Math.max(1, Number(process.env.ROUTE_TEST_BATCH ?? 5));
  const promptFile = process.env.ROUTE_PROMPT_FILE ?? fileURLToPath(new URL('../docs/route-prompt-current.txt', import.meta.url));
  const prompt = readFileSync(promptFile, 'utf8');
  const expected = Object.fromEntries(scenarios.map(([id, , primary, companion, notice]) => [id, { primary, companion, notice }]));
  const runs = [];
  for (let repeat = 0; repeat < repeats; repeat += 1) {
    for (let offset = 0; offset < scenarios.length; offset += batchSize) {
      runs.push(await ask(prompt, port, scenarios.slice(offset, offset + batchSize), expected));
    }
  }
  const stats = {
    port, repeats, batchSize, promptFile, promptBytes: Buffer.byteLength(prompt), calls: runs.length, expected: scenarios.length * repeats,
    seen: 0, passed: 0, missing: 0,
    primaryErrors: [],
    companionErrors: [],
    noticeErrors: [],
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
      let ok = true;
      for (const field of ['primary', 'companion', 'notice']) {
        const want = accepts(expected[item.id][field]);
        if (want.includes(item[field])) continue;
        ok = false;
        stats[`${field}Errors`].push({ id: item.id, actual: item[field], expected: want.join('/') });
      }
      if (ok) stats.passed += 1;
    }
    stats.missing += [...run.expectedIds].filter((id) => !seen.has(id)).length;
    stats.protocolErrors.push(...run.protocolErrors);
  }
  console.log(JSON.stringify(stats, null, 2));
}

// The same scenarios as a spec for the prompt-stress-test skill (stress.mjs / compare.mjs), printed by --spec.
function skillSpec() {
  const alias = (pairs) => pairs.map(([pattern, token]) => ({ pattern, token }));
  return {
    name: 'hub-routing',
    about: 'mcp-agent-hub routing guide, printed by tools/route-prompt-stress.mjs --spec. Pass the prompt file with --prompt.',
    repeats: 3,
    batch: 5,
    preamble,
    instruction,
    fields: [
      {
        name: 'primary',
        tokens: primaryTokens,
        aliases: alias([
          ['gpt-6.2-astra', 'astra-high'], ['opus-5-5', 'opus-xhigh'], ['deepseek.*max', 'omp-max'], ['deepseek.*high', 'omp-high'],
          ['terra.*medium', 'terra-medium'], ['terra.*(^|[^a-z])low', 'terra-low'], ['terra.*high', 'terra-high'],
          ['gemini-4-carbon.*medium', 'carbon-medium'],
          ['gemini-4-argon.*medium', 'agy-medium'], ['gemini-4-argon.*high', 'agy-high'], ['gpt-6.2-sol.*xhigh', 'sol-xhigh'],
          ['gpt-6.2-sol.*(^|[^a-z])low', 'sol-low'], ['gpt-6.2-sol.*medium', 'sol-medium'], ['gpt-6.2-sol.*high', 'sol-high'], ['primary codex', 'root'],
        ]),
      },
      { name: 'companion', tokens: companionTokens, aliases: alias([['^(none|-|)$', 'null'], ['kimi.*max', 'kimi-max'], ['kimi', 'kimi-high']]) },
      { name: 'notice', tokens: noticeTokens, aliases: alias([['^(null|-|)$', 'none']]) },
    ],
    scenarios: scenarios.map(([id, input, primary, companion, notice]) => ({ id, input, expect: { primary, companion, notice } })),
  };
}

// Run only when started directly; importing the module sends nothing.
const self = fileURLToPath(import.meta.url).toLowerCase();
if (process.argv[1] && resolve(process.argv[1]).toLowerCase() === self) {
  if (process.argv.includes('--spec')) process.stdout.write(JSON.stringify(skillSpec(), null, 2) + nl);
  else await main();
}
