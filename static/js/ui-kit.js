// Shared UI primitives: DOM helpers, icons, formatting, toasts, modals and the confirm dialog.

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function icon(name, extraClass = '') {
  return `<svg class="icon ${extraClass}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

// ----------------------------------------------------------------- formatting
export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
}

export function formatClock(epochSeconds) {
  if (!epochSeconds) return '';
  return new Date(epochSeconds * 1000).toLocaleTimeString('zh-CN', { hour12: false });
}

/** First non-empty line of a multi-line text, trimmed; "" when there is none. */
export function firstLine(text) {
  for (const line of String(text ?? '').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed) return trimmed;
  }
  return '';
}

/** Clock time today, otherwise date + clock (used by the 历史 list and the record modal). */
export function formatWhen(epochSeconds) {
  if (!epochSeconds) return '';
  const date = new Date(epochSeconds * 1000);
  const clock = date.toLocaleTimeString('zh-CN', { hour12: false });
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  return sameDay ? clock : `${date.toLocaleDateString('zh-CN')} ${clock}`;
}

/** Shortens a long path to its last segments, e.g. "…\\repo\\static". */
export function shortPath(path, max = 42) {
  const p = String(path || '');
  if (p.length <= max) return p;
  const sep = p.includes('\\') ? '\\' : '/';
  const parts = p.split(/[\\/]/).filter(Boolean);
  let tail = parts.pop() || '';
  while (parts.length && tail.length + parts[parts.length - 1].length + 3 <= max) {
    tail = `${parts.pop()}${sep}${tail}`;
  }
  return `…${sep}${tail}`;
}

// --------------------------------------------------------------------- busy
export function setBusy(button, busy) {
  if (!button) return;
  if (busy) {
    button.setAttribute('aria-busy', 'true');
    button.disabled = true;
    if (!button.querySelector('.spinner')) button.insertAdjacentHTML('afterbegin', '<span class="spinner" aria-hidden="true"></span>');
  } else {
    button.removeAttribute('aria-busy');
    button.disabled = false;
    button.querySelector('.spinner')?.remove();
  }
}

// -------------------------------------------------------------------- toasts
const TOAST_ICONS = { success: 'check-circle', error: 'alert', info: 'info' };

export function toast(message, kind = 'info', timeout) {
  const host = document.getElementById('toastHost');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `${icon(TOAST_ICONS[kind] || 'info')}<div class="toast-msg"></div>`;
  el.querySelector('.toast-msg').textContent = message;
  host.appendChild(el);
  while (host.children.length > 4) host.firstElementChild.remove();
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 180);
  };
  el.addEventListener('click', dismiss);
  setTimeout(dismiss, timeout ?? (kind === 'error' ? 6000 : 3000));
}

export async function copyText(text, doneMessage = '已复制到剪贴板') {
  try {
    await navigator.clipboard.writeText(text);
    toast(doneMessage, 'success', 1800);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      toast(doneMessage, 'success', 1800);
    } catch {
      toast('复制失败，请手动选择复制', 'error');
    } finally {
      ta.remove();
    }
  }
}

// -------------------------------------------------------------------- modals
const modalStack = [];
const modalHooks = new Map();

/** hooks: { beforeClose?: () => boolean | Promise<boolean>, afterClose?: () => void } */
export function registerModal(id, hooks) {
  modalHooks.set(id, hooks);
}

export function isModalOpen(id) {
  const el = document.getElementById(id);
  return !!el && !el.hidden;
}

export function openModal(id) {
  const el = document.getElementById(id);
  if (!el || !el.hidden) return;
  el.hidden = false;
  modalStack.push({ id, returnFocus: document.activeElement });
  requestAnimationFrame(() => {
    const target = el.querySelector('[data-autofocus]') || el.querySelector('.modal');
    target?.focus?.();
  });
}

export async function closeModal(id, { force = false } = {}) {
  const el = document.getElementById(id);
  if (!el || el.hidden) return true;
  const hooks = modalHooks.get(id);
  if (!force && hooks?.beforeClose && !(await hooks.beforeClose())) return false;
  el.hidden = true;
  const i = modalStack.findIndex((m) => m.id === id);
  const entry = i >= 0 ? modalStack.splice(i, 1)[0] : null;
  hooks?.afterClose?.();
  if (entry?.returnFocus?.isConnected) entry.returnFocus.focus?.();
  return true;
}

export function initModalSystem() {
  // Esc closes the top-most modal only; terminals keep Esc when no modal is open.
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape' || !modalStack.length) return;
    // An inline editor that handles Esc itself (e.g. the model note input) keeps the modal open.
    if (ev.target instanceof Element && ev.target.closest('[data-esc-local]')) return;
    ev.preventDefault();
    ev.stopPropagation();
    closeModal(modalStack[modalStack.length - 1].id);
  }, true);

  // Keep Tab / Shift+Tab inside the top-most modal (spec F9).
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Tab' || !modalStack.length) return;
    const top = modalStack[modalStack.length - 1];
    const modal = document.getElementById(top.id)?.querySelector('.modal');
    if (!modal) return;
    const focusables = Array.from(modal.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((el) => el.getClientRects().length > 0);
    if (!focusables.length) {
      ev.preventDefault();
      modal.focus();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (!modal.contains(active)) {
      ev.preventDefault();
      (ev.shiftKey ? last : first).focus();
    } else if (ev.shiftKey && active === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && active === last) {
      ev.preventDefault();
      first.focus();
    }
  }, true);

  // Backdrop click closes, but not when a text selection drag ends on the backdrop.
  let downTarget = null;
  document.addEventListener('mousedown', (ev) => { downTarget = ev.target; });
  document.addEventListener('click', (ev) => {
    const closer = ev.target.closest('[data-close-modal]');
    if (closer) {
      closeModal(closer.closest('.modal-backdrop').id);
      return;
    }
    if (ev.target.classList?.contains('modal-backdrop') && downTarget === ev.target) {
      closeModal(ev.target.id);
    }
  });
}

// ------------------------------------------------------------ confirm dialog
let settlePendingConfirm = null;

export function confirmDialog({ title, message, confirmText = '确定', cancelText = '取消', danger = false }) {
  settlePendingConfirm?.(false);
  return new Promise((resolve) => {
    const id = 'confirmDialog';
    let el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      el.className = 'modal-backdrop';
      el.hidden = true;
      document.body.appendChild(el);
    }
    el.innerHTML = `
      <div class="modal modal-sm" role="alertdialog" aria-modal="true" aria-labelledby="confirmDialogTitle" tabindex="-1">
        <header class="modal-head">
          <h3 class="modal-title" id="confirmDialogTitle">${icon(danger ? 'alert' : 'info', danger ? 'tone-danger' : 'tone-info')}<span></span></h3>
        </header>
        <div class="modal-body"><p class="confirm-msg"></p></div>
        <footer class="modal-foot">
          <button type="button" class="btn" data-answer="no" ${danger ? 'data-autofocus' : ''}>${escapeHtml(cancelText)}</button>
          <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-answer="yes" ${danger ? '' : 'data-autofocus'}>${escapeHtml(confirmText)}</button>
        </footer>
      </div>`;
    el.querySelector('.modal-title span').textContent = title;
    el.querySelector('.confirm-msg').textContent = message;

    let settled = false;
    const settle = (value) => {
      if (!settled) {
        settled = true;
        if (settlePendingConfirm === settle) settlePendingConfirm = null;
        resolve(value);
      }
    };
    settlePendingConfirm = settle;
    registerModal(id, { afterClose: () => settle(false) });
    el.querySelector('[data-answer="no"]').addEventListener('click', () => closeModal(id));
    el.querySelector('[data-answer="yes"]').addEventListener('click', () => {
      settle(true);
      closeModal(id);
    });
    openModal(id);
  });
}
