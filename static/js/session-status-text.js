// Maps hub status / activity strings to the console's Simplified Chinese copy (spec F2).
// Used by session cards, panes, the history list and the event feed.

export const STATUS_LABELS = {
  starting: '启动中',
  running: '运行中',
  waiting_parent: '等待主 Agent',
  completed: '已完成',
  exited: '已退出',
  terminated: '已终止',
  // Archive spec §7: end reasons the archive list and the record modal show.
  crashed: '异常退出',
  interrupted: '已中断',
  resumed: '已恢复',
};

/** Archive end-reason label: 已退出 / 异常退出 / 已终止 / 已中断. */
export function endReasonLabel(reason) {
  return STATUS_LABELS[reason] || (reason ? String(reason) : '');
}

const STATUS_TONES = {
  starting: 'info',
  running: 'success',
  waiting_parent: 'warning',
  completed: 'violet',
  exited: 'muted',
  terminated: 'danger',
  crashed: 'danger',
  interrupted: 'warning',
  resumed: 'success',
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || (status ? String(status) : '未知');
}

export function statusTone(status) {
  return STATUS_TONES[status] || 'muted';
}

// Event feed types (spec F5); unknown types pass through unchanged.
export const EVENT_TYPE_LABELS = {
  completed: '完成',
  turn_completed: '完成',
  question: '提问',
  message: '消息',
  message_to_parent: '消息',
  exited: '退出',
  terminated: '终止',
  error: '错误',
  crashed: '异常退出',
  resumed: '恢复',
};

const EVENT_TYPE_TONES = {
  completed: 'success',
  turn_completed: 'success',
  question: 'warning',
  message: 'info',
  message_to_parent: 'info',
  exited: 'muted',
  terminated: 'danger',
  error: 'danger',
  crashed: 'danger',
  resumed: 'success',
};

export function eventTypeLabel(type) {
  return EVENT_TYPE_LABELS[type] || type || '事件';
}

export function eventTypeTone(type) {
  return EVENT_TYPE_TONES[type] || 'muted';
}

// The server keeps writing English activity strings; translate the ones we know and pass
// anything else through unchanged.
const ACTIVITY_RULES = [
  [/^Interactive session running/i, () => '交互会话运行中'],
  [/^Session exited \(exit_code=(-?\d+)\)/i, (m) => `进程已退出（退出码 ${m[1]}）`],
  // F2 addition: the worker's exit message carries the parent-call hint after the code.
  [/^(?:Sub-Agent\s*)?进程已退出\s*[（(]\s*exit_code=(-?\d+)\s*[)）][\s\S]*$/i, (m) => `进程已退出（退出码 ${m[1]}）`],
  [/^Task completed/i, () => '任务已完成'],
  [/^Waiting for parent answer:?\s*(.*)$/i, (m) => (m[1] ? `等待主 Agent 回复：${m[1]}` : '等待主 Agent 回复')],
  [/^Terminated by/i, () => '已终止'],
  [/^Running initial task:\s*(.*)$/i, (m) => (m[1] ? `正在执行初始任务：${m[1]}` : '正在执行初始任务')],
];

// Q10: the event feed translates only a WHOLE lifecycle sentence. A sub-agent message that merely
// starts with "Task completed" is a body, not a status line, and must be shown as written.
const WHOLE_LIFECYCLE_RULES = [
  /^Interactive session running$/i,
  /^Session exited \(exit_code=-?\d+\)$/i,
  /^(?:Sub-Agent\s*)?进程已退出\s*[（(]\s*exit_code=-?\d+\s*[)）]\s*。?$/i,
  /^Task completed(?:\s*\([^()\n]*\))?$/i,
  /^Terminated by[^\n]{0,40}$/i,
  /^Waiting for parent answer:?[\s\S]{0,200}$/i,
  /^Running initial task:[\s\S]{0,200}$/i,
];

/** True when the whole text is one of the hub's lifecycle sentences (not a message body). */
export function isLifecycleSentence(text) {
  const value = String(text ?? '').trim();
  if (!value) return false;
  return WHOLE_LIFECYCLE_RULES.some((pattern) => pattern.test(value));
}

export function activityLabel(text) {
  const value = String(text ?? '').trim();
  if (!value) return '';
  for (const [pattern, format] of ACTIVITY_RULES) {
    const match = value.match(pattern);
    if (match) return format(match);
  }
  // F2 addition: any remaining `exit_code=N` text (older activity strings, event text) reads the same.
  const exit = value.match(/exit_code=(-?\d+)/i);
  if (exit) return `进程已退出（退出码 ${exit[1]}）`;
  return value;
}
