// Maps hub status / activity strings to the console's Simplified Chinese copy (spec F2).
// Used by session cards, panes, the history list and the event feed.

export const STATUS_LABELS = {
  starting: '启动中',
  running: '运行中',
  waiting_parent: '等待主 Agent',
  completed: '已完成',
  exited: '已退出',
  terminated: '已终止',
};

const STATUS_TONES = {
  starting: 'info',
  running: 'success',
  waiting_parent: 'warning',
  completed: 'violet',
  exited: 'muted',
  terminated: 'danger',
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
  [/^Task completed/i, () => '任务已完成'],
  [/^Waiting for parent answer:?\s*(.*)$/i, (m) => (m[1] ? `等待主 Agent 回复：${m[1]}` : '等待主 Agent 回复')],
  [/^Terminated by/i, () => '已终止'],
  [/^Running initial task:\s*(.*)$/i, (m) => (m[1] ? `正在执行初始任务：${m[1]}` : '正在执行初始任务')],
];

export function activityLabel(text) {
  const value = String(text ?? '').trim();
  if (!value) return '';
  for (const [pattern, format] of ACTIVITY_RULES) {
    const match = value.match(pattern);
    if (match) return format(match);
  }
  return value;
}
