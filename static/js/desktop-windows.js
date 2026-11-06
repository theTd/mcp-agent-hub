// "纳管桌面 GUI 窗口" modal: list top-level Win32 windows and attach one as a desktop_gui session.
import { hubApi } from './hub-api.js';
import { emit, setActiveSession, upsertSession } from './console-state.js';
import { $, escapeHtml, icon, toast, setBusy, openModal, closeModal } from './ui-kit.js';

const MODAL_ID = 'win32Modal';
let windows = [];
let query = '';

export function initDesktopWindows() {
  $('#win32Search').addEventListener('input', (ev) => {
    query = ev.target.value.trim().toLowerCase();
    render();
  });
  $('#btnWin32Refresh').addEventListener('click', load);
  $('#win32List').addEventListener('click', onAttachClick);
}

export function openDesktopWindows() {
  $('#win32Search').value = '';
  query = '';
  openModal(MODAL_ID);
  load();
}

async function load() {
  const list = $('#win32List');
  $('#win32Count').textContent = '';
  list.innerHTML = '<div class="list-state"><span class="spinner"></span>正在扫描桌面窗口…</div>';
  try {
    const data = await hubApi.listWin32Windows();
    windows = data.windows || [];
    render();
  } catch (err) {
    list.innerHTML = `<div class="list-state error">${icon('alert', 'icon-lg')}扫描失败：${escapeHtml(err.message)}</div>`;
  }
}

function render() {
  const rows = windows.filter((w) => !query || `${w.title} ${w.pid} ${w.hwnd}`.toLowerCase().includes(query));
  $('#win32Count').textContent = query ? `${rows.length} / ${windows.length} 个窗口` : `${windows.length} 个窗口`;
  $('#win32List').innerHTML = rows.length
    ? rows.map((w) => `
        <div class="win-row">
          <div class="win-icon">${icon('monitor')}</div>
          <div class="win-info">
            <div class="win-title" title="${escapeHtml(w.title)}">${escapeHtml(w.title || '（无标题）')}</div>
            <div class="win-meta">HWND ${escapeHtml(w.hwnd)} · PID ${escapeHtml(w.pid)} · ${escapeHtml(w.width)}×${escapeHtml(w.height)}</div>
          </div>
          <button type="button" class="btn btn-sm btn-primary" data-hwnd="${escapeHtml(w.hwnd)}">纳入总控台</button>
        </div>`).join('')
    : `<div class="list-state">${query ? '没有匹配的窗口' : '没有发现可纳管的窗口'}</div>`;
}

async function onAttachClick(ev) {
  const btn = ev.target.closest('[data-hwnd]');
  if (!btn) return;
  const win = windows.find((w) => String(w.hwnd) === btn.dataset.hwnd);
  if (!win) return;
  setBusy(btn, true);
  try {
    const data = await hubApi.createSession({ profile_id: 'desktop-gui', hwnd: win.hwnd, title: win.title });
    await closeModal(MODAL_ID, { force: true });
    if (data.session) {
      upsertSession(data.session);
      setActiveSession(data.session.session_id);
    }
    emit('sessions:refresh-request');
    toast(`已纳管「${win.title}」`, 'success');
  } catch (err) {
    toast(`纳管失败：${err.message}`, 'error');
    setBusy(btn, false);
  }
}
