// The bundled splash / error view. It only talks to the desktop shell (commands splash_state,
// retry_hub and recover_hub); once the shell reports the hub ready, this page sends the window to
// the console itself, so a window navigated back to the splash never stays on it.
(function () {
  'use strict';
  const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args || {});
  const $ = (id) => document.getElementById(id);
  const REDIRECT_INTERVAL_MS = 2000;
  let retrying = false;
  let lastRedirectAt = 0;
  // An IPC rejection (e.g. the page lacks a capability) is otherwise invisible: the periodic
  // refresh would overwrite it. Keep it in front of the state message until the next attempt.
  let ipcError = '';

  const errorText = (e) => (e && e.message ? e.message : String(e));

  function render(view) {
    const isError = view.phase === 'error';
    $('starting').hidden = isError;
    $('error').hidden = !isError;
    if (isError) {
      $('error-message').textContent = ipcError || view.message || '';
      $('log-path').textContent = view.log_path || '';
      const tail = view.log_tail || '';
      $('log-section').hidden = tail.length === 0;
      if ($('error-log').textContent !== tail) {
        $('error-log').textContent = tail;
        $('error-log').scrollTop = $('error-log').scrollHeight;
      }
      $('retry').disabled = retrying;
      $('recover').hidden = !view.can_recover;
      $('recover').disabled = retrying;
      $('recovery-detail').hidden = !view.recovery_detail;
      $('recovery-detail').textContent = view.recovery_detail || '';
    } else {
      retrying = false;
      ipcError = '';
      $('starting-message').textContent = view.message || '正在启动 Hub…';
      $('starting-detail').textContent = view.detail || '';
    }
    if (view.phase === 'ready' && view.console_url) {
      // Throttle so the 700 ms poll cannot abort a navigation in flight, but a failed one
      // (still on the splash 2 s later) retries on its own.
      const now = Date.now();
      if (now - lastRedirectAt >= REDIRECT_INTERVAL_MS) {
        lastRedirectAt = now;
        window.location.replace(view.console_url);
      }
    } else {
      lastRedirectAt = 0;
    }
  }

  async function refresh() {
    try {
      render(await invoke('splash_state'));
    } catch (e) {
      // Not inside the app (or the shell is busy): keep the starting view.
    }
  }

  $('retry').addEventListener('click', async () => {
    retrying = true;
    ipcError = '';
    $('retry').disabled = true;
    try {
      await invoke('retry_hub');
    } catch (e) {
      ipcError = errorText(e);
      $('error-message').textContent = ipcError;
    } finally {
      setTimeout(() => { retrying = false; refresh(); }, 300);
    }
  });

  $('recover').addEventListener('click', async () => {
    retrying = true;
    ipcError = '';
    $('recover').disabled = true;
    $('retry').disabled = true;
    try {
      await invoke('recover_hub');
    } catch (e) {
      ipcError = errorText(e);
      $('error-message').textContent = ipcError;
    } finally {
      setTimeout(() => { retrying = false; refresh(); }, 300);
    }
  });

  refresh();
  setInterval(refresh, 700);
})();
