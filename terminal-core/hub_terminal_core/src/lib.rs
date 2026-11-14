//! PyO3 binding of the hub's terminal core: Python types in, `conpty_session` calls out, nothing else.
//!
//! Rules this layer keeps (§5.2, §6.5):
//! * every call that can block releases the GIL (`Python::detach`) and passes its §5.2 deadline;
//! * `notify` and `log` are the only callables the core holds, and the core calls them with no core
//!   lock held (the plain crate guarantees that);
//! * an unknown session id is a `KeyError`, a spawn failure a `RuntimeError` carrying the Win32 message,
//!   a refused `write` a `TimeoutError` with nothing queued, a refused size a `ValueError`;
//! * a panic inside a call becomes a `RuntimeError`, never an abort (the workspace release profile
//!   unwinds, and the `release_profile` test below pins that);
//! * no core thread ever waits for the GIL while another thread holds it and waits on that thread (B2):
//!   the callbacks `try_attach` and skip their call, `shutdown` detaches, and `Drop` skips the join
//!   when it cannot attach.

use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::sync::Arc;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule};

use conpty_session::{CoreError, CoreOptions, SessionFlags, SpawnRequest, TextRequest};
use terminal_stream::strip_terminal_replies;

const DEFAULT_WRITE_DEADLINE_MS: u64 = 50;
const DEFAULT_KILL_GRACE_MS: u64 = 2000;
/// `_capture_submit_diagnostic`'s `[-65536:]`: a character count, not a byte count.
const DEFAULT_RAW_TAIL_CHARS: usize = 65536;
/// `get_clean_screen_text`'s default (doc §4).
const DEFAULT_SCREEN_LINES: usize = 80;
/// `persist_session`/`snapshot_all` read 250 rows and at most the 512 KiB scrollback cap.
const DEFAULT_SCROLLBACK_LINES: usize = 250;
const DEFAULT_SCROLLBACK_BYTES: usize = 512 * 1024;
/// §5.2's spawn bound: the whole call, process creation included.
const SPAWN_DEADLINE: Duration = Duration::from_millis(2000);
/// §5.2's `stats` bound; the table read it needs is contended only by a spawn insert.
const STATS_DEADLINE: Duration = Duration::from_millis(5);

/// §6.4's ticket lifetime: a console opens the URL within a second of fetching it, so 30 s is slack
/// for a slow tab and short enough that a leaked URL is worthless.
const DEFAULT_TICKET_TTL_MS: u64 = 30_000;

/// `KeyError` for an unknown id, `TimeoutError` for a deadline that left nothing done, `ValueError` for a
/// refused argument, `RuntimeError` for everything a spawn or a thread could fail with (§5.1).
fn core_error(error: CoreError) -> PyErr {
    match error {
        CoreError::UnknownSession(id) => PyKeyError::new_err(id),
        CoreError::Invalid(message) => PyValueError::new_err(message),
        CoreError::Deadline(message) => PyTimeoutError::new_err(message),
        CoreError::Spawn(message) | CoreError::Internal(message) => PyRuntimeError::new_err(message),
    }
}

fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        "unknown panic payload".to_string()
    }
}

/// `catch_unwind` around a method body: a panic is one failed call, and the session state stays valid
/// (§6.3: only a thread-body panic marks a session crashed).
fn guarded<T>(body: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    match std::panic::catch_unwind(AssertUnwindSafe(body)) {
        Ok(result) => result,
        Err(payload) => Err(PyRuntimeError::new_err(format!(
            "terminal core panic: {}",
            panic_message(payload)
        ))),
    }
}

fn options_from_dict(opts: Option<HashMap<String, u64>>) -> CoreOptions {
    let mut options = CoreOptions::default();
    if let Some(opts) = opts {
        if let Some(value) = opts.get("raw_cap_bytes") {
            options.flags.raw_cap_bytes = *value as usize;
        }
        if let Some(value) = opts.get("raw_slack_bytes") {
            options.flags.raw_slack_bytes = *value as usize;
        }
        if let Some(value) = opts.get("history_rows") {
            options.flags.history_rows = *value as usize;
        }
        if let Some(value) = opts.get("write_queue_bytes") {
            options.write_queue_bytes = *value as usize;
        }
        if let Some(value) = opts.get("sink_queue_bytes") {
            options.sink_queue_bytes = *value as usize;
        }
        if let Some(value) = opts.get("drain_interval_ms") {
            options.drain_interval = Duration::from_millis(*value);
        }
        if let Some(value) = opts.get("tick_interval_ms") {
            options.tick_interval = Duration::from_millis(*value);
        }
    }
    options
}

/// §5.2's `flags` dict, over the core's defaults: what the dict names wins, missing keys keep the default.
fn flags_from_dict<'py>(
    base: SessionFlags,
    flags: Option<&Bound<'py, PyDict>>,
) -> PyResult<SessionFlags> {
    let mut out = base;
    let Some(flags) = flags else {
        return Ok(out);
    };
    if let Some(value) = flags.get_item("strip_esc3j_on_output")? {
        out.strip_esc3j_on_output = value.extract()?;
    }
    if let Some(value) = flags.get_item("strip_osc_color_queries")? {
        out.strip_osc_color_queries = value.extract()?;
    }
    if let Some(value) = flags.get_item("history_rows")? {
        out.history_rows = value.extract()?;
    }
    if let Some(value) = flags.get_item("raw_cap_bytes")? {
        out.raw_cap_bytes = value.extract()?;
    }
    if let Some(value) = flags.get_item("raw_slack_bytes")? {
        out.raw_slack_bytes = value.extract()?;
    }
    Ok(out)
}

/// One object per hub (`Core(notify, log, opts)`), holding the session table for the process lifetime.
#[pyclass(module = "hub_terminal_core", name = "Core")]
pub struct Core {
    inner: Arc<conpty_session::Core>,
    /// The hub's `log`, kept on this side too: the spawn's log lines are queued while the GIL is
    /// released and delivered here after it is back (§5.2, SF3).
    log: Option<Arc<Py<PyAny>>>,
}

#[pymethods]
impl Core {
    #[new]
    #[pyo3(signature = (notify=None, log=None, opts=None))]
    fn new(
        notify: Option<Py<PyAny>>,
        log: Option<Py<PyAny>>,
        opts: Option<HashMap<String, u64>>,
    ) -> PyResult<Self> {
        // `try_attach`, never `attach`: a Python thread that holds the GIL while calling into the core
        // must not be able to make the ticker wait for it forever (B2). A skipped callback only means
        // the hub sees the next dirty record on the following tick — and it is not counted as a wakeup,
        // which is why the closure reports whether it ran (nit 7).
        let notify_fn: Box<dyn Fn() -> bool + Send + Sync> = match notify {
            Some(callback) => Box::new(move || {
                Python::try_attach(|py| {
                    if let Err(error) = callback.call0(py) {
                        error.write_unraisable(py, None);
                    }
                })
                .is_some()
            }),
            None => Box::new(|| false),
        };
        let log_shared = log.map(Arc::new);
        let log_fn: Box<dyn Fn(&str) + Send + Sync> = match log_shared.clone() {
            Some(shared) => Box::new(move |message: &str| {
                Python::try_attach(|py| {
                    if let Err(error) = shared.call1(py, (message,)) {
                        error.write_unraisable(py, None);
                    }
                });
            }),
            None => Box::new(|_: &str| {}),
        };
        Ok(Self {
            inner: conpty_session::Core::new(notify_fn, log_fn, options_from_dict(opts))
                .map_err(core_error)?,
            log: log_shared,
        })
    }

    /// The port the console's terminal sockets are served on (`127.0.0.1:<ws_port>`, §6.4). The hub
    /// reports it; the console only ever sees it inside a ticket URL.
    #[getter]
    fn ws_port(&self) -> u16 {
        self.inner.ws_port()
    }

    /// `ws_listening()`: false once the accept thread has ended, which the hub's health check reports
    /// (§6.4, `ws-listening-health`).
    fn ws_listening(&self) -> bool {
        self.inner.ws_listening()
    }

    /// `ws_ticket(session_id, ttl_ms=30000) -> {"url", "expires_at"}`: the one-use ticket the console
    /// connects with (§6.4). The key never leaves the core, so the mint is here and not in Python.
    /// `KeyError` for an unknown id (the hub 404s before it asks).
    #[pyo3(signature = (session_id, ttl_ms=None))]
    fn ws_ticket<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        ttl_ms: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let ttl = Duration::from_millis(ttl_ms.unwrap_or(DEFAULT_TICKET_TTL_MS));
            let (url, expires_at) = py
                .detach(|| self.inner.ws_ticket(&session_id, ttl))
                .map_err(core_error)?;
            let dict = PyDict::new(py);
            dict.set_item("url", url)?;
            dict.set_item("expires_at", expires_at)?;
            Ok(dict)
        })
    }

    /// `close_sockets(session_id) -> count`: asks every console socket of the id to close with 1000
    /// (W7's normal close) and reports how many it asked. A retired id answers 0.
    fn close_sockets(&self, py: Python<'_>, session_id: String) -> PyResult<usize> {
        guarded(|| Ok(py.detach(|| self.inner.close_sockets(&session_id))))
    }

    /// How many console sockets the core holds (diagnostics).
    fn socket_count(&self, py: Python<'_>) -> PyResult<usize> {
        guarded(|| Ok(py.detach(|| self.inner.socket_count())))
    }

    /// Starts one console session. `flags`/`seed_history`/`ws_prefix` are the §5.2 shape: the flags set
    /// the strips and the caps, `seed_history` fills the raw ring before the first byte (A1), and
    /// `ws_prefix` is one frame to the sinks already attached (the resume separator).
    ///
    /// The spawn is measured against §5.2's 2 s bound: `CreateProcessW`, the job assignment, the resume
    /// and the three thread starts are all inside it, and a spawn that misses the bound is logged (the
    /// session is kept, because killing a just-started console would be worse than a slow reply).
    #[pyo3(signature = (session_id, argv, cwd=None, env=None, cols=80, rows=24, flags=None, seed_history=None, ws_prefix=None))]
    fn spawn<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<HashMap<String, String>>,
        cols: u16,
        rows: u16,
        flags: Option<&Bound<'py, PyDict>>,
        seed_history: Option<String>,
        ws_prefix: Option<String>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let session_flags = flags_from_dict(self.inner.default_flags(), flags)?;
        let measured = guarded(|| {
            let request = SpawnRequest {
                session_id,
                argv,
                cwd,
                env: env.unwrap_or_else(|| std::env::vars().collect()),
                cols,
                rows,
                flags: session_flags,
                seed_history,
                ws_prefix,
            };
            // No Python callback inside the detached part: the spawn's log lines come back with the
            // reply and are logged below, with the GIL back (§5.2, SF3).
            let started = Instant::now();
            let reply = py
                .detach(|| self.inner.spawn(request))
                .map_err(core_error)?;
            Ok((reply, started.elapsed()))
        })?;
        let (reply, elapsed) = measured;
        for line in &reply.log_lines {
            if let Some(log) = &self.log {
                if let Err(error) = log.call1(py, (line.as_str(),)) {
                    error.write_unraisable(py, None);
                }
            }
        }
        if elapsed > SPAWN_DEADLINE {
            if let Some(log) = &self.log {
                if let Err(error) = log.call1(
                    py,
                    (format!(
                        "terminal core: spawn of pid {} took {elapsed:?}, over the §5.2 bound",
                        reply.pid
                    ),),
                ) {
                    error.write_unraisable(py, None);
                }
            }
        }
        let dict = PyDict::new(py);
        dict.set_item("pid", reply.pid)?;
        dict.set_item("cols", reply.cols)?;
        dict.set_item("rows", reply.rows)?;
        dict.set_item("elapsed_ms", elapsed.as_secs_f64() * 1000.0)?;
        Ok(dict)
    }

    /// Enqueues one write as a whole or not at all: empty text returns 0 with nothing queued, and a queue
    /// still full at the deadline raises `TimeoutError` with nothing queued, so the caller can retry the
    /// same text without duplicating bytes (§5.2, SF4).
    #[pyo3(signature = (session_id, text, deadline_ms=None))]
    fn write(
        &self,
        py: Python<'_>,
        session_id: String,
        text: String,
        deadline_ms: Option<u64>,
    ) -> PyResult<usize> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            py.detach(|| self.inner.write(&session_id, text.as_bytes(), deadline))
                .map_err(core_error)
        })
    }

    /// Applies a size. The 20x5 floor is enforced here (§8); the core never propagates a size.
    #[pyo3(signature = (session_id, cols, rows, deadline_ms=None))]
    fn resize(
        &self,
        py: Python<'_>,
        session_id: String,
        cols: u16,
        rows: u16,
        deadline_ms: Option<u64>,
    ) -> PyResult<(u16, u16)> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            py.detach(|| self.inner.resize(&session_id, cols, rows, deadline))
                .map_err(core_error)
        })
    }

    /// Terminates the session's tree. `grace_ms` bounds the whole call: the wait for the exit and the
    /// console close together (§5.2's 2 s).
    #[pyo3(signature = (session_id, grace_ms=None))]
    fn kill(&self, py: Python<'_>, session_id: String, grace_ms: Option<u64>) -> PyResult<Option<i32>> {
        guarded(|| {
            let grace = Duration::from_millis(grace_ms.unwrap_or(DEFAULT_KILL_GRACE_MS));
            py.detach(|| self.inner.kill(&session_id, grace))
                .map_err(core_error)
        })
    }

    /// Terminates every session in parallel inside one budget (§5.2).
    #[pyo3(signature = (grace_ms=None))]
    fn kill_all(&self, py: Python<'_>, grace_ms: Option<u64>) -> PyResult<()> {
        guarded(|| {
            let grace = Duration::from_millis(grace_ms.unwrap_or(DEFAULT_KILL_GRACE_MS));
            py.detach(|| self.inner.kill_all(grace)).map_err(core_error)
        })
    }

    /// Records for the sessions that changed since the previous call; flags are cleared, so a second
    /// call returns an empty list.
    fn drain<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        guarded(|| {
            let records = py.detach(|| self.inner.drain());
            let mut out = Vec::with_capacity(records.len());
            for record in records {
                let dict = PyDict::new(py);
                dict.set_item("session_id", record.session_id)?;
                dict.set_item("alive", record.alive)?;
                dict.set_item("exit_code", record.exit_code)?;
                dict.set_item("pid", record.pid)?;
                dict.set_item("bytes_total", record.bytes_total)?;
                dict.set_item("chunks_total", record.chunks_total)?;
                dict.set_item("last_output_at", record.last_output_at)?;
                dict.set_item("dropped_bytes", record.dropped_bytes)?;
                dict.set_item("crashed", record.crashed)?;
                dict.set_item("degraded", record.degraded)?;
                dict.set_item("screen_rev", record.screen_rev)?;
                dict.set_item("raw_rev", record.raw_rev)?;
                dict.set_item("screen_text", record.screen_text)?;
                // §6.4: the console's pending size requests, in arrival order (the hub applies its
                // clamp and its propagation to each), the input a text-only session must feed to its
                // own echo, and each socket's own drop count, per handle.
                let resize_requests = PyList::empty(py);
                for request in record.resize_requests {
                    let item = PyDict::new(py);
                    item.set_item("cols", request.cols)?;
                    item.set_item("rows", request.rows)?;
                    item.set_item("propagate", request.propagate)?;
                    resize_requests.append(item)?;
                }
                dict.set_item("resize_requests", resize_requests)?;
                let input_requests = PyList::empty(py);
                for text in record.input_requests {
                    input_requests.append(text)?;
                }
                dict.set_item("input_requests", input_requests)?;
                let socket_drops = PyList::empty(py);
                for (handle, dropped) in record.socket_drops {
                    socket_drops.append((handle, dropped))?;
                }
                dict.set_item("socket_drops", socket_drops)?;
                out.push(dict);
            }
            Ok(out)
        })
    }

    /// `raw_ansi_tail`: the last `max_chars` characters of the decoded ring, cut on a character boundary
    /// (the hub's own tail is `"".join(raw_history)[-65536:]`, a character count).
    #[pyo3(signature = (session_id, max_chars=None, deadline_ms=None))]
    fn raw_tail(
        &self,
        py: Python<'_>,
        session_id: String,
        max_chars: Option<usize>,
        deadline_ms: Option<u64>,
    ) -> PyResult<String> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            py.detach(|| {
                self.inner
                    .raw_tail(&session_id, max_chars.unwrap_or(DEFAULT_RAW_TAIL_CHARS), deadline)
            })
            .map_err(core_error)
        })
    }

    /// `screen` (§5.2): the rendered rows, the cursor, the size and the revision.
    #[pyo3(signature = (session_id, max_lines=None, deadline_ms=None))]
    fn screen<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        max_lines: Option<usize>,
        deadline_ms: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            let reply = py
                .detach(|| {
                    self.inner
                        .screen(&session_id, max_lines.unwrap_or(DEFAULT_SCREEN_LINES), deadline)
                })
                .map_err(core_error)?;
            let dict = PyDict::new(py);
            dict.set_item("text", reply.text)?;
            dict.set_item("cursor_x", reply.cursor_x)?;
            dict.set_item("cursor_y", reply.cursor_y)?;
            dict.set_item("cols", reply.cols)?;
            dict.set_item("rows", reply.rows)?;
            dict.set_item("rev", reply.rev)?;
            Ok(dict)
        })
    }

    /// `scrollback` (§5.2): the history rows above the screen.
    #[pyo3(signature = (session_id, max_lines=None, deadline_ms=None))]
    fn scrollback<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        max_lines: Option<usize>,
        deadline_ms: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            let reply = py
                .detach(|| {
                    self.inner
                        .scrollback(&session_id, max_lines.unwrap_or(DEFAULT_SCROLLBACK_LINES), deadline)
                })
                .map_err(core_error)?;
            let dict = PyDict::new(py);
            dict.set_item("text", reply.text)?;
            dict.set_item("rev", reply.rev)?;
            Ok(dict)
        })
    }

    /// `raw` (§5.2): the history trimmed to the session's cap, its byte count and its revision.
    #[pyo3(signature = (session_id, deadline_ms=None))]
    fn raw<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        deadline_ms: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            let reply = py
                .detach(|| self.inner.raw(&session_id, deadline))
                .map_err(core_error)?;
            let dict = PyDict::new(py);
            dict.set_item("text", reply.text)?;
            dict.set_item("bytes", reply.bytes)?;
            dict.set_item("rev", reply.rev)?;
            Ok(dict)
        })
    }

    /// The replay a console socket gets on connect (`replay-shape`); the socket is step 4.
    #[pyo3(signature = (session_id, deadline_ms=None))]
    fn replay(
        &self,
        py: Python<'_>,
        session_id: String,
        deadline_ms: Option<u64>,
    ) -> PyResult<String> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            py.detach(|| self.inner.replay(&session_id, deadline))
                .map_err(core_error)
        })
    }

    /// `retire(session_id) -> bool`: removes an ended session from the table. `false` for an unknown id;
    /// `ValueError` for a live one and for one whose exit record has not been drained (the next drain
    /// carries it, then the hub retires). Attached queues report `closed`; a later spawn of the id
    /// starts fresh.
    fn retire(&self, py: Python<'_>, session_id: String) -> PyResult<bool> {
        guarded(|| {
            py.detach(|| self.inner.retire(&session_id))
                .map_err(core_error)
        })
    }

    /// `attach_text` (§5.2): a session with a screen and a raw ring but no process. `seed_history` is a
    /// virtual resume's archived scrollback (A1); `seed` is its separator frame.
    #[pyo3(signature = (session_id, cols=80, rows=24, flags=None, seed_history=None, seed=None))]
    fn attach_text<'py>(
        &self,
        py: Python<'py>,
        session_id: String,
        cols: u16,
        rows: u16,
        flags: Option<&Bound<'py, PyDict>>,
        seed_history: Option<String>,
        seed: Option<String>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let session_flags = flags_from_dict(self.inner.default_flags(), flags)?;
        let reply = guarded(|| {
            py.detach(|| {
                self.inner.attach_text(TextRequest {
                    session_id,
                    cols,
                    rows,
                    flags: session_flags,
                    seed_history,
                    seed,
                })
            })
            .map_err(core_error)
        })?;
        for line in &reply.log_lines {
            if let Some(log) = &self.log {
                if let Err(error) = log.call1(py, (line.as_str(),)) {
                    error.write_unraisable(py, None);
                }
            }
        }
        let dict = PyDict::new(py);
        dict.set_item("pid", reply.pid)?;
        dict.set_item("cols", reply.cols)?;
        dict.set_item("rows", reply.rows)?;
        Ok(dict)
    }

    /// `feed` (§5.2): text into a session with no process; returns the new screen revision.
    #[pyo3(signature = (session_id, text, deadline_ms=None))]
    fn feed(
        &self,
        py: Python<'_>,
        session_id: String,
        text: String,
        deadline_ms: Option<u64>,
    ) -> PyResult<u64> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            py.detach(|| self.inner.feed(&session_id, &text, deadline))
                .map_err(core_error)
        })
    }

    /// `watch_screen` (§5.2): at most one render per changed revision and per `interval_ms`, carried by
    /// `drain`'s `screen_text`.
    #[pyo3(signature = (session_id, max_lines=None, interval_ms=250))]
    fn watch_screen(
        &self,
        py: Python<'_>,
        session_id: String,
        max_lines: Option<usize>,
        interval_ms: u64,
    ) -> PyResult<()> {
        guarded(|| {
            py.detach(|| {
                self.inner.watch(
                    &session_id,
                    max_lines.unwrap_or(DEFAULT_SCREEN_LINES),
                    Duration::from_millis(interval_ms),
                )
            })
            .map_err(core_error)
        })
    }

    fn unwatch(&self, py: Python<'_>, session_id: String) -> PyResult<()> {
        guarded(|| {
            py.detach(|| self.inner.unwatch(&session_id))
                .map_err(core_error)
        })
    }

    /// `snapshot_all` (§5.2/§6.6): every session's archive-ready state inside one budget.
    #[pyo3(signature = (max_lines=None, max_bytes=None, budget_ms=8000))]
    fn snapshot_all<'py>(
        &self,
        py: Python<'py>,
        max_lines: Option<usize>,
        max_bytes: Option<usize>,
        budget_ms: u64,
    ) -> PyResult<Vec<Bound<'py, PyDict>>> {
        guarded(|| {
            let snapshots = py.detach(|| {
                self.inner.snapshot_all(
                    max_lines.unwrap_or(DEFAULT_SCROLLBACK_LINES),
                    max_bytes.unwrap_or(DEFAULT_SCROLLBACK_BYTES),
                    Duration::from_millis(budget_ms),
                )
            });
            let mut out = Vec::with_capacity(snapshots.len());
            for snapshot in snapshots {
                let dict = PyDict::new(py);
                dict.set_item("session_id", snapshot.session_id)?;
                dict.set_item("alive", snapshot.alive)?;
                dict.set_item("exit_code", snapshot.exit_code)?;
                dict.set_item("pid", snapshot.pid)?;
                dict.set_item("crashed", snapshot.crashed)?;
                dict.set_item("degraded", snapshot.degraded)?;
                dict.set_item("screen_text", snapshot.screen_text)?;
                dict.set_item("scrollback_text", snapshot.scrollback_text)?;
                dict.set_item("raw_text", snapshot.raw_text)?;
                dict.set_item("raw_rev", snapshot.raw_rev)?;
                dict.set_item("raw_bytes", snapshot.raw_bytes)?;
                out.push(dict);
            }
            Ok(out)
        })
    }

    /// Diagnostics counters. The table read is bounded (§5.2: 5 ms), and the GIL is released for it.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let stats = py.detach(|| self.inner.stats(STATS_DEADLINE));
            let dict = PyDict::new(py);
            dict.set_item("sessions", stats.sessions)?;
            dict.set_item("alive", stats.alive)?;
            dict.set_item("crashed", stats.crashed)?;
            dict.set_item("bytes_total", stats.bytes_total)?;
            dict.set_item("chunks_total", stats.chunks_total)?;
            dict.set_item("dropped_bytes", stats.dropped_bytes)?;
            dict.set_item("notify_calls", stats.notify_calls)?;
            dict.set_item("panics", stats.panics)?;
            Ok(dict)
        })
    }

    /// Stops the ticker. `kill_all` is the shutdown path that ends the sessions first (§6.6).
    ///
    /// The join happens with the GIL released: the ticker may be inside `notify`, which needs the GIL
    /// to return, so waiting for the ticker while holding it deadlocks (B2).
    fn shutdown(&self, py: Python<'_>) {
        py.detach(|| self.inner.shutdown());
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        // The hub keeps one Core for its lifetime; a dropped one must not leave the ticker calling into
        // a finalising interpreter. `try_attach` because this runs on the GC thread and, at interpreter
        // teardown, may not attach at all; the join is skipped then, and the ticker exits by itself at
        // its next tick (the process is going away, which is the `shutdown-ends-cli` path).
        Python::try_attach(|py| py.detach(|| self.inner.shutdown()));
    }
}

/// `strip_osc_terminal_replies` (§6.4/§8): the xterm.js colour, clipboard and `1[01]\d` replies are
/// removed from one browser message before it can reach a PTY. The socket's input path calls it
/// (`ws/connection.rs`), and a function with its tests is why it is also exported.
#[pyfunction]
fn strip_osc_terminal_replies(text: &str) -> String {
    strip_terminal_replies(text).into_owned()
}

#[pymodule]
fn hub_terminal_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Core>()?;
    module.add_function(wrap_pyfunction!(strip_osc_terminal_replies, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("ABI", "abi3-py311")?;
    Ok(())
}

#[cfg(test)]
mod release_profile {
    //! §10 "build config": a stray `panic = "abort"` silently turns every caught panic back into a hub
    //! kill, so the release profile is asserted, not assumed.
    //!
    //! The check reads the workspace manifest: `cargo metadata` on cargo 1.99.1 reports `profile: null`
    //! (verified with `--format-version 1`, with and without `--no-deps`), so the manifest is the only
    //! place the setting is still observable without building. `cargo build --release -v` and grepping
    //! the rustc flags would be the alternative and costs a release build in every `cargo test`.

    fn section_body(manifest: &str, header: &str) -> Option<String> {
        let mut inside = false;
        let mut body = String::new();
        for line in manifest.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with('[') {
                inside = trimmed == header;
                continue;
            }
            if inside && !trimmed.is_empty() && !trimmed.starts_with('#') {
                body.push_str(&trimmed.replace(' ', ""));
                body.push('\n');
            }
        }
        (!body.is_empty()).then_some(body)
    }

    #[test]
    fn release_profile_unwinds() {
        let workspace = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("binding crate sits inside the workspace");
        let manifest = std::fs::read_to_string(workspace.join("Cargo.toml"))
            .expect("the workspace manifest is readable");
        let release =
            section_body(&manifest, "[profile.release]").expect("the workspace sets [profile.release]");
        assert!(
            release.contains("panic=\"unwind\""),
            "the core's release profile must unwind, found: {release}"
        );
        assert!(
            !release.contains("panic=\"abort\""),
            "a release profile that aborts turns every caught panic into a hub kill: {release}"
        );
        assert!(
            manifest.contains("[workspace]"),
            "terminal-core must stay its own workspace: as a member of desktop/src-tauri it would \
             inherit panic = \"abort\""
        );
    }
}
