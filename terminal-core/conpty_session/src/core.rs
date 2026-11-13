//! The core: one session table, one ticker thread, the calls Python makes (§5.2) and the drain record the
//! ticker wakes the hub for (§5.3).
//!
//! Nothing here parses, renders or batches: step 1 owns the PTYs and the counters. What it does own is
//! the two rules that make the rest of the design work: the core takes no lock while calling `notify`
//! or `log`, and every blocking call has a deadline.

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock, RwLockReadGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::conpty::{self, SpawnSpec};
use crate::session::{DrainRecord, Session, SessionOptions, TickFlags};

/// Calls this step's Python layer makes. `drain_interval` is the 50 ms notify window; `tick_interval`
/// is how often the ticker looks (§5.3).
#[derive(Clone, Copy, Debug)]
pub struct CoreOptions {
    pub raw_cap_bytes: usize,
    pub write_queue_bytes: usize,
    pub drain_interval: Duration,
    pub tick_interval: Duration,
}

impl Default for CoreOptions {
    fn default() -> Self {
        Self {
            raw_cap_bytes: 512 * 1024,
            write_queue_bytes: 1024 * 1024,
            drain_interval: Duration::from_millis(50),
            tick_interval: Duration::from_millis(5),
        }
    }
}

/// What `spawn` needs. `argv[0]` is resolved inside; `env` is the whole session environment.
pub struct SpawnRequest {
    pub session_id: String,
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: HashMap<String, String>,
    pub cols: u16,
    pub rows: u16,
    /// Archived scrollback of a resumed session (A1): put in the raw buffer before the first byte.
    pub seed_history: Option<Vec<u8>>,
}

#[derive(Clone, Copy, Debug)]
pub struct SpawnReply {
    pub pid: u32,
    pub cols: u16,
    pub rows: u16,
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub sessions: usize,
    pub alive: usize,
    pub crashed: usize,
    pub bytes_total: u64,
    pub chunks_total: u64,
    pub dropped_bytes: u64,
    pub notify_calls: u64,
    pub panics: u64,
}

/// The plain-Rust error surface; the binding maps it onto `KeyError`/`RuntimeError`/`ValueError`.
#[derive(Debug, Clone)]
pub enum CoreError {
    UnknownSession(String),
    Spawn(String),
    Invalid(String),
    Deadline(String),
    Internal(String),
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CoreError::UnknownSession(id) => write!(f, "unknown session: {id}"),
            CoreError::Spawn(message)
            | CoreError::Invalid(message)
            | CoreError::Deadline(message)
            | CoreError::Internal(message) => write!(f, "{message}"),
        }
    }
}

/// Bounded wait for a read lock, so a writer (a spawn inserting a session) cannot hold the hub's loop.
fn read_deadline<'a, T>(lock: &'a RwLock<T>, deadline: Duration) -> Option<RwLockReadGuard<'a, T>> {
    let end = Instant::now() + deadline;
    loop {
        match lock.try_read() {
            Ok(guard) => return Some(guard),
            Err(std::sync::TryLockError::Poisoned(poisoned)) => return Some(poisoned.into_inner()),
            Err(std::sync::TryLockError::WouldBlock) => {
                if Instant::now() >= end {
                    return None;
                }
                thread::sleep(Duration::from_micros(200));
            }
        }
    }
}

pub struct Core {
    sessions: RwLock<HashMap<String, Arc<Session>>>,
    notify: Box<dyn Fn() + Send + Sync>,
    log: Arc<dyn Fn(&str) + Send + Sync>,
    opts: CoreOptions,
    tick: Arc<TickFlags>,
    /// Ticker wake: set by `shutdown`, waited on with the tick interval.
    stop: Mutex<bool>,
    wake: Condvar,
    stopping: AtomicBool,
    notify_calls: AtomicU64,
    panics: AtomicU64,
    ticker: Mutex<Option<JoinHandle<()>>>,
}

impl Core {
    /// Starts the ticker thread. `notify` is called at most every `drain_interval` and only when a
    /// session is dirty (§5.3); `log` is called on spawn, kill and panic only.
    pub fn new(
        notify: Box<dyn Fn() + Send + Sync>,
        log: Box<dyn Fn(&str) + Send + Sync>,
        opts: CoreOptions,
    ) -> Arc<Self> {
        let tick = Arc::new(TickFlags::default());
        let core = Arc::new(Self {
            sessions: RwLock::new(HashMap::new()),
            notify,
            log: Arc::from(log),
            opts,
            tick,
            stop: Mutex::new(false),
            wake: Condvar::new(),
            stopping: AtomicBool::new(false),
            notify_calls: AtomicU64::new(0),
            panics: AtomicU64::new(0),
            ticker: Mutex::new(None),
        });
        let ticker_core = Arc::clone(&core);
        let handle = thread::Builder::new()
            .name("core-ticker".to_string())
            .spawn(move || ticker_core.ticker_body())
            .expect("spawn ticker thread");
        if let Ok(mut slot) = core.ticker.lock() {
            *slot = Some(handle);
        }
        core
    }

    /// 5 ms ticks flush step-2 batches; the Python wakeup is gated to `drain_interval`, and one exit per
    /// session is delivered on the next tick even mid-window.
    fn ticker_body(self: Arc<Self>) {
        let mut last_notify: Option<Instant> = None;
        loop {
            {
                let guard = self.stop.lock().unwrap_or_else(|e| e.into_inner());
                let _ = self
                    .wake
                    .wait_timeout(guard, self.opts.tick_interval)
                    .unwrap_or_else(|e| e.into_inner());
            }
            if self.stopping.load(Ordering::Acquire) {
                return;
            }
            let work = catch_unwind(AssertUnwindSafe(|| {
                let urgent = self.tick.urgent.swap(false, Ordering::AcqRel);
                let pending = self.tick.pending.swap(false, Ordering::AcqRel);
                let due = last_notify
                    .map(|at| at.elapsed() >= self.opts.drain_interval)
                    .unwrap_or(true);
                if urgent || (pending && due) {
                    last_notify = Some(Instant::now());
                    self.notify_calls.fetch_add(1, Ordering::Relaxed);
                    // No core lock is held here: Python may hold one and call back in (§6.5).
                    (self.notify)();
                } else if pending {
                    self.tick.pending.store(true, Ordering::Release);
                }
            }));
            if work.is_err() {
                self.panics.fetch_add(1, Ordering::Relaxed);
                (self.log)("terminal core: notify callback panicked; ticker continues");
            }
        }
    }

    pub fn spawn(&self, request: SpawnRequest) -> Result<SpawnReply, CoreError> {
        let SpawnRequest {
            session_id,
            argv,
            cwd,
            env,
            cols,
            rows,
            seed_history,
        } = request;
        if argv.is_empty() {
            return Err(CoreError::Invalid("spawn needs at least one argv element".into()));
        }
        if cols == 0 || rows == 0 {
            return Err(CoreError::Invalid("spawn needs cols and rows >= 1".into()));
        }
        if let Some(existing) = self
            .sessions
            .read()
            .ok()
            .and_then(|map| map.get(&session_id).cloned())
        {
            if existing.alive() {
                return Err(CoreError::Invalid(format!(
                    "session {session_id} is already running in the core"
                )));
            }
        }

        let spec = SpawnSpec {
            argv: &argv,
            cwd: cwd.as_deref(),
            env: &env,
            cols,
            rows,
        };
        let process = conpty::spawn(&spec).map_err(CoreError::Spawn)?;
        let session = Session::start(
            session_id.clone(),
            process,
            cols,
            rows,
            SessionOptions {
                raw_cap_bytes: self.opts.raw_cap_bytes,
                write_queue_bytes: self.opts.write_queue_bytes,
            },
            Arc::clone(&self.tick),
            Arc::clone(&self.log),
            seed_history,
        );
        let reply = SpawnReply {
            pid: session.pid,
            cols,
            rows,
        };
        // A dead session under the same id is replaced: resume spawns a new process for an id whose
        // previous session has exited, and the hub's screens come from `seed_history` (§8 A1).
        let replaced = self
            .sessions
            .write()
            .map_err(|_| CoreError::Internal("session table lock poisoned".into()))?
            .insert(session_id, session);
        drop(replaced);
        Ok(reply)
    }

    fn session(&self, id: &str) -> Result<Arc<Session>, CoreError> {
        let map = read_deadline(&self.sessions, Duration::from_millis(50))
            .ok_or_else(|| CoreError::Deadline(format!("session table busy, {id} not read")))?;
        map.get(id)
            .cloned()
            .ok_or_else(|| CoreError::UnknownSession(id.to_string()))
    }

    pub fn write(&self, id: &str, text: &[u8], deadline: Duration) -> Result<usize, CoreError> {
        let session = self.session(id)?;
        session
            .write(text, deadline)
            .map_err(|error| CoreError::Internal(error.to_string()))
    }

    pub fn resize(
        &self,
        id: &str,
        cols: u16,
        rows: u16,
        deadline: Duration,
    ) -> Result<(u16, u16), CoreError> {
        if cols < 20 || rows < 5 {
            return Err(CoreError::Invalid(format!(
                "resize refused: {cols}x{rows} is below the 20x5 floor"
            )));
        }
        let session = self.session(id)?;
        session.resize(cols, rows, deadline).map_err(CoreError::Internal)
    }

    pub fn kill(&self, id: &str, grace: Duration) -> Result<Option<i32>, CoreError> {
        let session = self.session(id)?;
        session.kill(grace).map_err(CoreError::Internal)
    }

    /// `TerminateJobObject` per session. Shutdown calls this after `snapshot_all` in step 5; here it is
    /// also what makes the selftest's "no leftover process" check possible.
    pub fn kill_all(&self, grace: Duration) -> Result<(), CoreError> {
        let sessions: Vec<Arc<Session>> = self
            .sessions
            .read()
            .map_err(|_| CoreError::Internal("session table lock poisoned".into()))?
            .values()
            .cloned()
            .collect();
        for session in sessions {
            if let Err(error) = session.kill(grace) {
                (self.log)(&format!(
                    "terminal core: kill_all failed for {}: {error}",
                    session.id
                ));
            }
        }
        Ok(())
    }

    /// One record per session whose flags were set since the previous call, flags cleared (§5.3).
    pub fn drain(&self) -> Vec<DrainRecord> {
        let sessions: Vec<Arc<Session>> = match read_deadline(&self.sessions, Duration::from_millis(50)) {
            Some(map) => map.values().cloned().collect(),
            None => return Vec::new(),
        };
        sessions
            .into_iter()
            .filter_map(|session| session.drain_record())
            .collect()
    }

    pub fn raw_tail(
        &self,
        id: &str,
        max_bytes: usize,
        deadline: Duration,
    ) -> Result<Vec<u8>, CoreError> {
        let session = self.session(id)?;
        Ok(session.raw_tail(max_bytes, deadline))
    }

    pub fn stats(&self) -> Stats {
        let sessions: Vec<Arc<Session>> = match self.sessions.read() {
            Ok(map) => map.values().cloned().collect(),
            Err(_) => Vec::new(),
        };
        let mut stats = Stats {
            sessions: sessions.len(),
            notify_calls: self.notify_calls.load(Ordering::Relaxed),
            panics: self.panics.load(Ordering::Relaxed),
            ..Stats::default()
        };
        for session in &sessions {
            if session.alive() {
                stats.alive += 1;
            }
            if session.crashed() {
                stats.crashed += 1;
            }
            let (bytes, chunks, dropped) = session.counters();
            stats.bytes_total += bytes;
            stats.chunks_total += chunks;
            stats.dropped_bytes += dropped;
        }
        stats
    }

    pub fn session_ids(&self) -> Vec<String> {
        self.sessions
            .read()
            .map(|map| map.keys().cloned().collect())
            .unwrap_or_default()
    }

    /// Stops the ticker. Sessions keep their own threads; `kill_all` is the documented shutdown path
    /// before this.
    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        if let Ok(mut stop) = self.stop.lock() {
            *stop = true;
        }
        self.wake.notify_all();
        if let Ok(mut slot) = self.ticker.lock() {
            if let Some(handle) = slot.take() {
                let _ = handle.join();
            }
        }
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        self.shutdown();
    }
}
