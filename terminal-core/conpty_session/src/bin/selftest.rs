//! Headless self test for the ConPTY core: spawn, echo, resize, tree kill, exit reporting and the
//! nested-job experiment of §12.8.
//!
//! Every check prints one `PASS <name>` or `FAIL <name>: reason` line and the exit code is the number of
//! failures, so the release gate can read the output as-is. Nothing here opens a window: ConPTY children
//! are headless by construction, and no shell is started with `start`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use conpty_session::job_tree;
use conpty_session::{Core, CoreOptions, SpawnRequest};

use windows::Win32::System::Threading::GetCurrentProcess;

const ECHO_DEADLINE: Duration = Duration::from_secs(10);
const TREE_DEADLINE: Duration = Duration::from_secs(10);

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--nested-outer") {
        std::process::exit(nested_outer());
    }
    if args.iter().any(|arg| arg == "--selftest") {
        let failures = std::panic::catch_unwind(run_selftest).unwrap_or_else(|_| {
            println!("FAIL selftest: panicked");
            1
        });
        std::process::exit(i32::try_from(failures.min(125)).unwrap_or(125));
    }
    eprintln!("usage: selftest --selftest");
    std::process::exit(2);
}

fn logs() -> Arc<Mutex<Vec<String>>> {
    Arc::new(Mutex::new(Vec::new()))
}

fn start_core(logs: Arc<Mutex<Vec<String>>>) -> Arc<Core> {
    let log = move |message: &str| {
        if let Ok(mut lines) = logs.lock() {
            lines.push(message.to_string());
        }
    };
    let notify = || {};
    Core::new(
        Box::new(notify),
        Box::new(log),
        CoreOptions {
            drain_interval: Duration::from_millis(50),
            tick_interval: Duration::from_millis(5),
            ..CoreOptions::default()
        },
    )
}

fn environment() -> HashMap<String, String> {
    std::env::vars().collect()
}

/// Waits until the session's raw tail contains `needle`.
fn wait_for_output(core: &Core, id: &str, needle: &str, deadline: Duration) -> Result<String, String> {
    let end = Instant::now() + deadline;
    let mut last_len = 0usize;
    while Instant::now() < end {
        let tail = core
            .raw_tail(id, 64 * 1024, Duration::from_millis(50))
            .map_err(|error| error.to_string())?;
        let text = String::from_utf8_lossy(&tail).to_string();
        last_len = text.len();
        if text.contains(needle) {
            return Ok(text);
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    Err(format!(
        "no {needle:?} within {deadline:?} (tail was {last_len} bytes)"
    ))
}

/// Waits for the drain flags to settle after a kill: the console can flush its last bytes after the
/// process is gone, which dirties the session once more. Once quiet, a drain must return nothing.
fn drain_settles(core: &Core) -> Result<(), String> {
    let settle = Instant::now() + Duration::from_secs(2);
    while !core.drain().is_empty() {
        if Instant::now() >= settle {
            return Err("the session never went quiet after kill".to_string());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    if core.drain().is_empty() {
        Ok(())
    } else {
        Err("a second drain still returned records".to_string())
    }
}

fn check(name: &str, result: Result<(), String>) -> u32 {
    match result {
        Ok(()) => {
            println!("PASS {name}");
            0
        }
        Err(reason) => {
            println!("FAIL {name}: {reason}");
            1
        }
    }
}

fn run_selftest() -> u32 {
    let log_lines = logs();
    let core = start_core(Arc::clone(&log_lines));
    let mut failures = 0;

    let session = "selftest";
    let spawned = core.spawn(SpawnRequest {
        session_id: session.to_string(),
        argv: vec!["cmd.exe".to_string()],
        cwd: None,
        env: environment(),
        cols: 100,
        rows: 30,
        seed_history: None,
    });
    let (pid, spawn_size) = match spawned {
        Ok(reply) => (reply.pid, (reply.cols, reply.rows)),
        Err(error) => {
            println!("FAIL spawn-cmd: {error}");
            core.shutdown();
            return 1;
        }
    };
    failures += check(
        "spawn-cmd",
        if pid != 0 && spawn_size == (100, 30) {
            Ok(())
        } else {
            Err(format!("pid {pid}, size {spawn_size:?}"))
        },
    );

    // A new console prints its banner; wait for the prompt before typing so the echo check is not a race.
    let _ = wait_for_output(&core, session, ">", ECHO_DEADLINE);
    let wrote = core.write(session, b"echo hub-core-ok\r\n", Duration::from_millis(50));
    failures += check(
        "write-echo",
        match wrote {
            Ok(bytes) if bytes > 0 => wait_for_output(&core, session, "hub-core-ok", ECHO_DEADLINE)
                .map(|_| ()),
            Ok(_) => Err(format!("write accepted 0 of {} bytes", b"echo hub-core-ok\r\n".len())),
            Err(error) => Err(error.to_string()),
        },
    );

    failures += check(
        "resize",
        match core.resize(session, 120, 40, Duration::from_millis(50)) {
            Ok(size) if size == (120, 40) => Ok(()),
            Ok(size) => Err(format!("resized to {size:?}")),
            Err(error) => Err(error.to_string()),
        },
    );

    // A grandchild that would outlive its parent if only the root pid were killed. `ping` never reads
    // input, so the console input pipe stays backed up — harmless here, and it keeps the tree simple.
    let ping_written = core.write(session, b"cmd /c ping -n 60 127.0.0.1\r\n", Duration::from_millis(50));
    let ping_ok = matches!(&ping_written, Ok(bytes) if *bytes > 0);
    let mut grandchild: Option<u32> = None;
    if ping_ok {
        let end = Instant::now() + TREE_DEADLINE;
        while Instant::now() < end {
            if let Some(child) = job_tree::children_of(pid).first().copied() {
                grandchild = Some(child);
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    failures += check(
        "grandchild-started",
        match (&ping_written, grandchild) {
            (Ok(bytes), Some(child)) if *bytes > 0 => {
                if job_tree::process_alive(child) {
                    Ok(())
                } else {
                    Err(format!("grandchild {child} is not alive"))
                }
            }
            (Ok(bytes), None) => Err(format!("write accepted {bytes} bytes, no child appeared")),
            (Err(error), _) => Err(error.to_string()),
            (Ok(_), Some(child)) => Err(format!("grandchild {child} vanished")),
        },
    );

    let killed = core.kill(session, Duration::from_millis(2000));
    let (tree_dead, tree_detail) = match grandchild {
        Some(child) => {
            let end = Instant::now() + Duration::from_secs(3);
            let mut alive = job_tree::process_alive(child);
            while alive && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(50));
                alive = job_tree::process_alive(child);
            }
            let root_alive = job_tree::process_alive(pid);
            (
                !alive && !root_alive,
                format!("cmd {pid} alive={root_alive} ping {child} alive={alive}"),
            )
        }
        None => (
            !job_tree::process_alive(pid),
            format!("cmd {pid} alive={}", job_tree::process_alive(pid)),
        ),
    };
    failures += check(
        "kill-tree",
        if killed.is_ok() && tree_dead {
            Ok(())
        } else {
            Err(format!("kill={killed:?}, {tree_detail}"))
        },
    );

    let drained = core.drain();
    let record = drained.iter().find(|record| record.session_id == session);
    failures += check(
        "drain-exit",
        match record {
            Some(record) if !record.alive && record.exit_code.is_some() && record.bytes_total > 0 => Ok(()),
            Some(record) => Err(format!(
                "alive={} exit={:?} bytes={}",
                record.alive, record.exit_code, record.bytes_total
            )),
            None => Err("the session was not in the drain record".to_string()),
        },
    );
    failures += check(
        "drain-clears-flags",
        drain_settles(&core),
    );

    let stats = core.stats();
    failures += check(
        "stats",
        if stats.sessions >= 1 && stats.bytes_total > 0 && stats.notify_calls > 0 && stats.panics == 0 {
            Ok(())
        } else {
            Err(format!("{stats:?}"))
        },
    );

    let _ = core.kill_all(Duration::from_millis(2000));
    core.shutdown();
    failures += check(
        "no-leftovers",
        if !job_tree::process_alive(pid) {
            Ok(())
        } else {
            Err(format!("cmd {pid} survived the selftest"))
        },
    );

    // §12.8: the hub runs inside the desktop app's kill-on-close job, so a per-session job is nested.
    // The child re-executes this binary with the outer job in place and reports its own checks.
    failures += check("nested-job", run_nested_child());
    if failures > 0 {
        let lines = log_lines.lock().map(|lines| lines.clone()).unwrap_or_default();
        for line in lines {
            println!("log: {line}");
        }
    }
    failures
}

/// Re-executes this binary inside an outer kill-on-close job (the desktop app's shape) and checks that
/// the inner per-session job still assigns and still kills the tree.
fn run_nested_child() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    let output = std::process::Command::new(exe)
        .arg("--nested-outer")
        .output()
        .map_err(|error| error.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if output.status.success() && stdout.contains("PASS nested-assign") && stdout.contains("PASS nested-tree-kill") {
        Ok(())
    } else {
        Err(format!(
            "exit {:?}: {}",
            output.status.code(),
            stdout.trim().replace('\n', " | ")
        ))
    }
}

fn nested_outer() -> i32 {
    use conpty_session::job::JobObject;

    let mut failures: u32 = 0;
    let log_lines = logs();

    let outer = JobObject::new_kill_on_close();
    let outer = match outer {
        Ok(job) => job,
        Err(error) => {
            println!("FAIL nested-outer-job: {error}");
            return 1;
        }
    };
    // SAFETY: GetCurrentProcess returns a pseudo handle for this process; it is borrowed, never closed.
    let self_process = unsafe { GetCurrentProcess() };
    if let Err(error) = outer.assign(self_process) {
        println!("FAIL nested-outer-job: {error}");
        return 1;
    }
    failures += check(
        "nested-outer-job",
        if outer.contains(self_process) {
            Ok(())
        } else {
            Err("the outer job does not contain this process".to_string())
        },
    );

    let core = start_core(Arc::clone(&log_lines));
    let spawned = core.spawn(SpawnRequest {
        session_id: "nested".to_string(),
        argv: vec!["cmd.exe".to_string()],
        cwd: None,
        env: environment(),
        cols: 100,
        rows: 30,
        seed_history: None,
    });
    let pid = match spawned {
        Ok(reply) => reply.pid,
        Err(error) => {
            println!("FAIL nested-spawn: {error}");
            let _ = core.kill_all(Duration::from_millis(2000));
            core.shutdown();
            return i32::try_from(failures + 1).unwrap_or(125);
        }
    };
    let _ = wait_for_output(&core, "nested", ">", ECHO_DEADLINE);
    let _ = core.write("nested", b"cmd /c ping -n 60 127.0.0.1\r\n", Duration::from_millis(50));

    let mut grandchild = None;
    let end = Instant::now() + TREE_DEADLINE;
    while Instant::now() < end {
        if let Some(child) = job_tree::children_of(pid).first().copied() {
            grandchild = Some(child);
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }

    let log = log_lines.lock().map(|lines| lines.clone()).unwrap_or_default();
    let assignment_refused = log
        .iter()
        .any(|line| line.contains("job assignment failed"));
    failures += check(
        "nested-assign",
        if assignment_refused {
            Err("AssignProcessToJobObject was refused inside the outer job".to_string())
        } else {
            Ok(())
        },
    );

    let killed = core.kill("nested", Duration::from_millis(2000));
    let mut tree_dead = !job_tree::process_alive(pid);
    if let Some(child) = grandchild {
        let end = Instant::now() + Duration::from_secs(3);
        while job_tree::process_alive(child) && Instant::now() < end {
            std::thread::sleep(Duration::from_millis(50));
        }
        tree_dead = tree_dead && !job_tree::process_alive(child);
    }
    failures += check(
        "nested-tree-kill",
        if killed.is_ok() && tree_dead {
            Ok(())
        } else {
            Err(format!(
                "kill={killed:?} pid alive={} grandchild {grandchild:?}",
                job_tree::process_alive(pid)
            ))
        },
    );

    let _ = core.kill_all(Duration::from_millis(2000));
    core.shutdown();
    i32::try_from(failures).unwrap_or(125)
}
