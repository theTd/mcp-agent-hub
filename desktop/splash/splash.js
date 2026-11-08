// The bundled splash / error view. It only talks to the desktop shell (commands splash_state and
// retry_hub); the shell navigates this window to the hub console once the hub answers.
(function () {
  'use strict';
  const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args || {});
  const $ = (id) => document.getElementById(id);
  let retrying = false;

  function render(view) {
    const isError = view.phase === 'error';
    $('starting').hidden = isError;
    $('error').hidden = !isError;
    if (isError) {
      $('error-message').textContent = view.message || '';
      $('log-path').textContent = view.log_path || '';
      const tail = view.log_tail || '';
      $('log-section').hidden = tail.length === 0;
      if ($('error-log').textContent !== tail) {
        $('error-log').textContent = tail;
        $('error-log').scrollTop = $('error-log').scrollHeight;
      }
      $('retry').disabled = retrying;
    } else {
      retrying = false;
      $('starting-message').textContent = view.message || '正在启动 Hub…';
      $('starting-detail').textContent = view.detail || '';
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
    $('retry').disabled = true;
    try {
      await invoke('retry_hub');
    } finally {
      setTimeout(() => { retrying = false; refresh(); }, 300);
    }
  });

  refresh();
  setInterval(refresh, 700);
})();
