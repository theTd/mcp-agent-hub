// What a session looks like in a list: its title, live state and the short texts shown on the
// sidebar card and the session board tile. Shared so the two views never drift apart.
import { formatDuration, firstLine } from './ui-kit.js';
import { activityLabel } from './session-status-text.js';

const LIVE_STATUSES = new Set(['starting', 'running', 'waiting_parent']);

export const isLive = (s) => (typeof s.process_alive === 'boolean' ? s.process_alive : LIVE_STATUSES.has(s.status));

export const engineKey = (s) => s.engine || s.kind || 'tui';

export const profileLabel = (s) => s.profile_name || s.profile_id || '会话';

const LEGACY_TITLE_ID_SUFFIX_RE = /\s*\([a-z0-9_-]+-[0-9a-f]{6,}\)\s*$/i;

function summarizeTaskForTitle(taskText, maxLen = 24) {
  if (!taskText) return '';
  let text = String(taskText).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  text = text.replace(/\[Sub-Agent Bridge Info[\s\S]*$/i, '').trim();
  text = text.replace(/\s*\[Hub Session: session_id=[^\]]+\]/g, '').trim();
  const readExec = text.match(/^Read\s+(?:["'`]?[A-Za-z]:[\\/][^\s"'`]+["'`]?|["'`]?[^\s"'`:]+["'`]?)(?:\s+whole)?\s+and\s+(?:execute|run|implement|verify|fix|complete)\b\s*[:：]?\s*([\s\S]*)$/i);
  if (readExec) {
    const rest = (readExec[1] || '').trim();
    const quoted = rest.match(/^["'“”‘’《》]([^"'“”‘’《》\n]{2,36})["'“”‘’《》]/);
    if (quoted) {
      const q = quoted[1].trim();
      return q.length > maxLen ? `${q.slice(0, maxLen - 1).trimEnd()}…` : q;
    }
    if (rest) text = rest;
    else {
      const m = text.match(/([^\s\\/]+)\.(?:md|txt|json|py|js|ts)/i);
      text = m ? `执行 ${m[1]}` : '执行任务文档';
    }
  }
  const candidates = [];
  for (const rawLine of text.split('\n')) {
    let line = rawLine.trim().replace(/^[#*>\-\d.)\s`]+/, '').trim();
    if (!line || line.startsWith('```')) continue;
    line = line.replace(/["'`]?(?:[A-Za-z]:[\\/]|\/[A-Za-z0-9._-]+\/)[^\s"'`，,。；;)]+["'`]?/g, '').trim();
    line = line.replace(/^(?:请你|请帮我|帮我|麻烦你|麻烦|请先|现在请|请|你需要|需要你|任务[:：]|目标[:：]|背景[:：])\s*/g, '').trim();
    if (line) candidates.push(line);
  }
  if (!candidates.length) return '';
  const first = candidates[0];
  const clauses = first.split(/[。！？!?；;\n]|，|,\s+/).map((c) => c.trim()).filter(Boolean);
  let chosen = clauses[0] || first;
  if (chosen.length < 4 && clauses.length > 1) {
    chosen = `${chosen} ${clauses[1]}`.trim();
  }
  chosen = chosen.replace(/\s+/g, ' ').replace(/^[:：,，.。;；\-—\s]+|[:：,，.。;；\-—\s]+$/g, '');
  if (!chosen) return '';
  return chosen.length > maxLen ? `${chosen.slice(0, maxLen - 1).trimEnd()}…` : chosen;
}

export function sessionTitle(s) {
  const label = profileLabel(s);
  const rawTitle = firstLine(s.title);
  if (rawTitle && !LEGACY_TITLE_ID_SUFFIX_RE.test(rawTitle) && rawTitle !== s.session_id) {
    if (rawTitle === label) {
      const taskSummary = summarizeTaskForTitle(s.last_task);
      return `${label} · ${taskSummary || '交互会话'}`;
    }
    if (rawTitle.startsWith(`${label} · `) || rawTitle.startsWith(`${label} - `) || rawTitle.startsWith(`${label}:`)) {
      return rawTitle;
    }
    return `${label} · ${rawTitle}`;
  }
  const taskSummary = summarizeTaskForTitle(s.last_task);
  return `${label} · ${taskSummary || '交互会话'}`;
}

export function formatRuntimeHms(s) {
  const created = Number(s?.created_at);
  if (!Number.isFinite(created) || created <= 0) return '00:00:00';
  const live = isLive(s);
  const endTs = live
    ? Date.now() / 1000
    : Number(s?.ended_at || s?.last_output_at || s?.archived_at || Date.now() / 1000);
  const elapsed = Math.max(0, Math.floor(endTs - created));
  const hh = Math.floor(elapsed / 3600);
  const mm = Math.floor((elapsed % 3600) / 60);
  const ss = elapsed % 60;
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
}

export function cwdBase(cwd) {
  const parts = String(cwd || '').split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

export function modelLine(s) {
  return [s.effective_model, s.effective_thinking_level].filter(Boolean).join(' · ');
}

/** Round 2: a session launched with the profile's bypass_approvals on (no high-risk approval). */
export function bypassBadgeHtml(s) {
  return s.high_risk_approval === false
    ? '<span class="approval-off-badge" title="该 Profile 打开了 bypass_approvals：高风险操作不会暂停等待审批">全自动</span>'
    : '';
}

export function activityText(s) {
  return activityLabel(s.current_activity);
}

export function idleText(s) {
  if (isLive(s)) return `空闲 ${formatDuration(s.idle_seconds)}`;
  return s.exit_code !== null && s.exit_code !== undefined ? `退出码 ${s.exit_code}` : '';
}

export function pendingQuestionText(s) {
  const interaction = s.pending_interaction;
  let text = '';
  if (typeof interaction === 'string') text = interaction;
  else if (interaction && typeof interaction === 'object') {
    text = interaction.question || interaction.text || interaction.prompt || interaction.title || '';
    const options = Array.isArray(interaction.options) ? interaction.options.filter(Boolean) : [];
    if (options.length) text += `\n选项：${options.map((opt, i) => `${i + 1}) ${opt}`).join('  ')}`;
  }
  text = String(text || '').trim();
  return text || activityText(s);
}
