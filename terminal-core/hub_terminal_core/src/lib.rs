//! PyO3 binding of the hub's terminal core: Python types in, `conpty_session` calls out, nothing else.
//!
//! Rules this layer keeps (§5.2, §6.5):
//! * every call that can block releases the GIL (`Python::detach`) and passes its §5.2 deadline;
//! * `notify` and `log` are the only callables the core holds, and the core calls them with no core
//!   lock held (the plain crate guarantees that);
//! * an unknown session id is a `KeyError`, a spawn failure a `RuntimeError` carrying the Win32 message;
//! * a panic inside a call becomes a `RuntimeError`, never an abort (the workspace release profile
//!   unwinds, and the `release_profile` test below pins that).

use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::sync::Arc;
use std::time::Duration;

use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

use conpty_session::{CoreError, CoreOptions, SpawnRequest};

const DEFAULT_WRITE_DEADLINE_MS: u64 = 50;
const DEFAULT_KILL_GRACE_MS: u64 = 2000;
const DEFAULT_RAW_TAIL_BYTES: usize = 64 * 1024;

/// `KeyError` for an unknown id, `ValueError` for a refused argument, `RuntimeError` for everything a
/// spawn or a thread could fail with (§5.1).
fn core_error(error: CoreError) -> PyErr {
    match error {
        CoreError::UnknownSession(id) => PyKeyError::new_err(id),
        CoreError::Invalid(message) => PyValueError::new_err(message),
        CoreError::Spawn(message)
        | CoreError::Deadline(message)
        | CoreError::Internal(message) => PyRuntimeError::new_err(message),
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
            options.raw_cap_bytes = *value as usize;
        }
        if let Some(value) = opts.get("write_queue_bytes") {
            options.write_queue_bytes = *value as usize;
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

/// One object per hub (`Core(notify, log, opts)`), holding the session table for the process lifetime.
#[pyclass(module = "hub_terminal_core", name = "Core")]
pub struct Core {
    inner: Arc<conpty_session::Core>,
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
        let notify_fn: Box<dyn Fn() + Send + Sync> = match notify {
            Some(callback) => Box::new(move || {
                Python::attach(|py| {
                    if let Err(error) = callback.call0(py) {
                        error.write_unraisable(py, None);
                    }
                });
            }),
            None => Box::new(|| {}),
        };
        let log_fn: Box<dyn Fn(&str) + Send + Sync> = match log {
            Some(callback) => Box::new(move |message: &str| {
                Python::attach(|py| {
                    if let Err(error) = callback.call1(py, (message,)) {
                        error.write_unraisable(py, None);
                    }
                });
            }),
            None => Box::new(|_: &str| {}),
        };
        Ok(Self {
            inner: conpty_session::Core::new(notify_fn, log_fn, options_from_dict(opts)),
        })
    }

    /// Starts one console session. `flags`/`seed_history` are the §5.2 shape: `seed_history` seeds the
    /// raw buffer now; the strip flags arrive with the parser in step 2 and are accepted unused.
    #[pyo3(signature = (session_id, argv, cwd=None, env=None, cols=80, rows=24, flags=None, seed_history=None))]
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
    ) -> PyResult<Bound<'py, PyDict>> {
        let _ = flags;
        guarded(|| {
            let request = SpawnRequest {
                session_id,
                argv,
                cwd,
                env: env.unwrap_or_else(|| std::env::vars().collect()),
                cols,
                rows,
                seed_history: seed_history.map(|seed| seed.into_bytes()),
            };
            // The spawn itself is bounded by the OS (CreateProcessW); the 2 s §5.2 deadline covers the
            // session-table write that follows it.
            let reply = py
                .detach(|| self.inner.spawn(request))
                .map_err(core_error)?;
            let dict = PyDict::new(py);
            dict.set_item("pid", reply.pid)?;
            dict.set_item("cols", reply.cols)?;
            dict.set_item("rows", reply.rows)?;
            Ok(dict)
        })
    }

    /// Enqueues one write as a whole: returns the bytes accepted, 0 when the deadline expired with the
    /// session's writer queue full.
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

    /// Terminates the session's tree and returns its exit code (`None` when it was still running after
    /// the grace period).
    #[pyo3(signature = (session_id, grace_ms=None))]
    fn kill(&self, py: Python<'_>, session_id: String, grace_ms: Option<u64>) -> PyResult<Option<i32>> {
        guarded(|| {
            let grace = Duration::from_millis(grace_ms.unwrap_or(DEFAULT_KILL_GRACE_MS));
            py.detach(|| self.inner.kill(&session_id, grace))
                .map_err(core_error)
        })
    }

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
                out.push(dict);
            }
            Ok(out)
        })
    }

    /// The last `max_bytes` of the raw byte stream, decoded lossily (step 2 owns the UTF-8 carry).
    #[pyo3(signature = (session_id, max_bytes=None, deadline_ms=None))]
    fn raw_tail(
        &self,
        py: Python<'_>,
        session_id: String,
        max_bytes: Option<usize>,
        deadline_ms: Option<u64>,
    ) -> PyResult<String> {
        guarded(|| {
            let deadline = Duration::from_millis(deadline_ms.unwrap_or(DEFAULT_WRITE_DEADLINE_MS));
            let bytes = py
                .detach(|| {
                    self.inner
                        .raw_tail(&session_id, max_bytes.unwrap_or(DEFAULT_RAW_TAIL_BYTES), deadline)
                })
                .map_err(core_error)?;
            Ok(String::from_utf8_lossy(&bytes).into_owned())
        })
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let stats = self.inner.stats();
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
    fn shutdown(&self) {
        self.inner.shutdown();
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        // The hub keeps one Core for its lifetime; a dropped one must not leave the ticker calling into
        // a finalising interpreter.
        self.inner.shutdown();
    }
}

#[pymodule]
fn hub_terminal_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Core>()?;
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
