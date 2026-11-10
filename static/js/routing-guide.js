// "路由总则" tab: edit the global routing instructions and preview what the parent agent receives.
import { hubApi } from './hub-api.js';
import { state, on, reloadProfiles } from './console-state.js';
import { $, toast, setBusy, copyText } from './ui-kit.js';

let dirty = false;

export function initRoutingGuide() {
  $('#routingInput').addEventListener('input', () => setDirty(true));
  $('#subagentIdleTimeout').addEventListener('input', () => setDirty(true));
  $('#btnSaveRouting').addEventListener('click', saveRoutingGuide);
  $('#btnCopyRoutingPreview').addEventListener('click', () =>
    copyText($('#routingPreview').textContent, '已复制路由指南'));
  on('profiles:changed', refreshRoutingGuide);
}

export function refreshRoutingGuide() {
  if (!dirty) {
    $('#routingInput').value = state.globalRoutingInstructions;
    $('#subagentIdleTimeout').value = state.subagentIdleTimeoutMinutes;
  }
  $('#routingPreview').textContent = state.routingGuideMarkdown || '（暂无内容）';
}

export async function saveRoutingGuide() {
  const input = $('#subagentIdleTimeout');
  const timeout = Number(input.value);
  if (!input.value.trim() || !Number.isSafeInteger(timeout) || timeout < 0) {
    toast('自动关闭时间必须是非负整数（分钟，0 表示禁用）', 'error');
    input.focus();
    return;
  }
  const btn = $('#btnSaveRouting');
  setBusy(btn, true);
  try {
    await hubApi.saveRoutingGuide($('#routingInput').value, timeout);
    setDirty(false);
    await reloadProfiles(false);
    toast('调度总则与自动关闭设置已保存', 'success');
  } catch (err) {
    toast(`保存失败：${err.message}`, 'error');
  } finally {
    setBusy(btn, false);
  }
}

export function isRoutingDirty() {
  return dirty;
}

export function discardRoutingChanges() {
  setDirty(false);
  refreshRoutingGuide();
}

function setDirty(value) {
  dirty = value;
  $('#routingDirty').hidden = !value;
}
