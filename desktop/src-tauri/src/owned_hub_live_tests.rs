//! Tests for owned_hub. The fast test always runs. The `live_*` tests start real hubs from this
//! checkout, headless (no console window, no app window), and run only on request:
//! `AGENT_HUB_PYTHON=<venv python> cargo test -- --ignored --test-threads=1 --nocapture`
//! Port: AGENT_HUB_PORT, default 4465, never 4444.

use super::*;
use crate::hub_api::count_sessions;
use crate::hub_config;
use crate::waiting_notifier::{toasts_for_events, ToastDeduper, WaitKind};
use crate::windows_process::{
    kill_process_tree, list_processes, listening_pid, live_process_tree, ProcessEntry,
};
use serde_json::{json, Value};
use std::os::windows::process::CommandExt;
use std::sync::Mutex;

static LIVE_LOCK: Mutex<()> = Mutex::new(());
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn block_on<F: std::future::Future>(f: F) -> F::Output {
    tauri::async_runtime::block_on(f)
}

#[test]
fn a_foreign_listener_is_reported_busy_with_its_pid() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    match block_on(probe_port(&HubApi::new(port), port)) {
        PortProbe::Busy { pid, .. } => assert_eq!(pid, std::process::id()),
        other => panic!("expected Busy, got {other:?}"),
    }
    drop(listener);
    assert!(matches!(
        block_on(probe_port(&HubApi::new(port), port)),
        PortProbe::Free
    ));
}

fn live_config() -> Option<HubConfig> {
    let env = |n: &str| std::env::var(n).ok();
    let mut cfg =
        hub_config::resolve(&env, &Default::default(), &hub_config::built_from_repo()).ok()?;
    if std::env::var("AGENT_HUB_PORT").is_err() {
        cfg.port = 4465;
    }
    assert_ne!(cfg.port, 4444, "live tests never use the live hub's port");
    if let Err(e) = hub_config::validate(&cfg) {
        eprintln!("SKIPPED (set AGENT_HUB_PYTHON): {e}");
        return None;
    }
    Some(cfg)
}

fn alive(pid: u32) -> bool {
    list_processes().unwrap().iter().any(|p| p.pid == pid)
}

async fn wait_all_gone(pids: &[u32], timeout: Duration) -> Vec<u32> {
    let deadline = Instant::now() + timeout;
    loop {
        let left: Vec<u32> = pids.iter().copied().filter(|p| alive(*p)).collect();
        if left.is_empty() || Instant::now() >= deadline {
            return left;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

async fn post_json(
    port: u16,
    path: &str,
    body: Value,
    timeout: Duration,
) -> Result<(u16, Value), String> {
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let resp = client
        .post(format!("http://127.0.0.1:{port}{path}"))
        .json(&body)
        .timeout(timeout)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    Ok((status, resp.json::<Value>().await.unwrap_or(Value::Null)))
}

async fn launch_ready(cfg: &HubConfig, log_dir: &Path, log: LogFn<'_>) -> (OwnedHub, HubInfo) {
    let api = HubApi::new(cfg.port);
    assert!(
        matches!(probe_port(&api, cfg.port).await, PortProbe::Free),
        "port {} must be free before a live test",
        cfg.port
    );
    let mut hub = OwnedHub::launch(cfg, log_dir, log).expect("launch");
    let cancel = AtomicBool::new(false);
    let info = match wait_ready(&api, &cancel, || hub.exit_text()).await {
        Ok(info) => info,
        Err(ReadyError::Failed(m)) => panic!("{m}"),
        Err(ReadyError::Cancelled) => panic!("cancelled"),
    };
    (hub, info)
}

/// A pwsh session through the REST API (ConPTY: no window).
async fn create_pwsh_session(port: u16, cwd: &Path) -> String {
    let (status, body) = post_json(
        port,
        "/api/sessions",
        json!({"profile_id": "pwsh", "title": "headless-live-test", "cwd": cwd.display().to_string()}),
        Duration::from_secs(30),
    )
    .await
    .expect("create session");
    assert_eq!(status, 200, "{body}");
    body["session"]["session_id"]
        .as_str()
        .expect("session id")
        .to_string()
}

fn print_tree(label: &str, tree: &[ProcessEntry]) {
    let list: Vec<String> = tree
        .iter()
        .map(|p| format!("{} {}", p.pid, p.exe))
        .collect();
    println!("{label}: {}", list.join(", "));
}

/// M3 + M6 headless: launch in the job, readiness, a live pwsh session (what 退出 counts before
/// its confirm), graceful /api/hub/shutdown, then hub, launcher, pwsh and port all gone.
#[test]
#[ignore = "starts a real hub on AGENT_HUB_PORT (default 4465)"]
fn live_launch_ready_and_graceful_stop_with_a_pwsh_session() {
    let _g = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some(cfg) = live_config() else { return };
    let logs = tempfile::tempdir().unwrap();
    let log = |l: &str| println!("  [shell] {l}");
    block_on(async {
        let (hub, info) = launch_ready(&cfg, logs.path(), &log).await;
        println!("ready: {info:?}");
        assert!(info.is_agent_hub() && info.shutdown_enabled && info.port == cfg.port);
        assert_eq!(listening_pid(cfg.port), Some(info.pid));
        let api = HubApi::new(cfg.port);
        let sid = create_pwsh_session(cfg.port, logs.path()).await;
        let counts = count_sessions(&api.sessions().await.unwrap());
        println!("session {sid}; live sessions = {}", counts.live);
        assert_eq!(counts.live, 1);
        tokio::time::sleep(Duration::from_secs(2)).await;
        let tree = live_process_tree(hub.pid()).unwrap();
        print_tree("job tree before stop", &tree);
        assert!(
            tree.iter().any(|p| p.pid == info.pid),
            "real python under the launcher"
        );
        assert!(
            tree.iter().any(|p| p.exe.eq_ignore_ascii_case("pwsh.exe")),
            "pwsh session in the tree"
        );
        let pids: Vec<u32> = tree.iter().map(|p| p.pid).collect();
        let token = hub.token.clone();
        let report = hub.stop_gracefully(&log).await;
        println!("stop report: {report:?}");
        assert!(report.graceful, "hub must exit by itself within 10 s");
        assert_eq!(report.exit.as_deref(), Some("0"));
        let left = wait_all_gone(&pids, Duration::from_secs(5)).await;
        assert!(left.is_empty(), "still running: {left:?}");
        assert_eq!(listening_pid(cfg.port), None);
        let bytes = std::fs::read(hub_log::hub_log_path(logs.path())).unwrap();
        let text = String::from_utf8_lossy(&bytes);
        assert!(
            text.contains("Application shutdown complete"),
            "lifespan shutdown ran"
        );
        assert!(!text.contains(&token), "token never logged");
        println!(
            "OK: graceful stop; {} processes gone; port {} free",
            pids.len(),
            cfg.port
        );
    });
}

/// M3 job object headless: the app dies (its job handle closes) → the hub, the launcher and every
/// agent die with it.
#[test]
#[ignore = "starts a real hub on AGENT_HUB_PORT (default 4465)"]
fn live_closing_the_job_kills_the_hub_tree() {
    let _g = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some(cfg) = live_config() else { return };
    let logs = tempfile::tempdir().unwrap();
    let log = |l: &str| println!("  [shell] {l}");
    block_on(async {
        let (hub, info) = launch_ready(&cfg, logs.path(), &log).await;
        create_pwsh_session(cfg.port, logs.path()).await;
        tokio::time::sleep(Duration::from_secs(2)).await;
        let tree = live_process_tree(hub.pid()).unwrap();
        print_tree("job tree", &tree);
        assert!(tree.iter().any(|p| p.pid == info.pid));
        let pids: Vec<u32> = tree.iter().map(|p| p.pid).collect();
        // No shutdown call: exactly what the OS does to the job handle when the app dies.
        drop(hub);
        let left = wait_all_gone(&pids, Duration::from_secs(5)).await;
        assert!(left.is_empty(), "still running: {left:?}");
        assert!(wait_port_free(cfg.port, Duration::from_secs(5)).await);
        println!(
            "OK: job handle closed -> {} processes gone; port {} free",
            pids.len(),
            cfg.port
        );
    });
}

/// M5 headless: a Start-AgentHub.ps1-style hub (no token, not ours) is detected as external; it
/// cannot be stopped through the API (404), so 接管 kills its tree by the info pid.
#[test]
#[ignore = "starts a real hub on AGENT_HUB_PORT (default 4465)"]
fn live_external_hub_is_attached_then_taken_over() {
    let _g = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some(cfg) = live_config() else { return };
    let logs = tempfile::tempdir().unwrap();
    let out = std::fs::File::create(logs.path().join("external-hub.log")).unwrap();
    let err = out.try_clone().unwrap();
    let mut external = Command::new(&cfg.python)
        .arg(cfg.server_py())
        .current_dir(&cfg.repo_dir)
        .env("HUB_PORT", cfg.port.to_string())
        .env_remove("HUB_SHUTDOWN_TOKEN")
        .stdout(out)
        .stderr(err)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .expect("start external hub");
    let launcher = external.id();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        block_on(async {
            let api = HubApi::new(cfg.port);
            let deadline = Instant::now() + READY_TIMEOUT;
            let info = loop {
                if let PortProbe::ExternalHub(info) = probe_port(&api, cfg.port).await {
                    break info;
                }
                assert!(Instant::now() < deadline, "external hub not ready");
                tokio::time::sleep(READY_POLL).await;
            };
            println!("probe: ExternalHub({info:?})");
            assert!(
                !info.shutdown_enabled,
                "an external hub has no token for us"
            );
            assert_ne!(
                info.pid, launcher,
                "info.pid is the real python under the venv launcher"
            );
            assert_eq!(
                api.shutdown("not-our-token").await,
                Err("HTTP 404 Not Found".to_string())
            );
            // Quitting the app in external mode makes no call at all; the hub keeps answering.
            assert!(api.info(Duration::from_secs(2)).await.is_ok());
            let killed = kill_process_tree(info.pid).unwrap();
            println!(
                "takeover: killed {killed} process(es) from pid {}",
                info.pid
            );
            assert!(killed >= 1);
            assert!(
                wait_port_free(cfg.port, PORT_RELEASE_WAIT).await,
                "port released"
            );
            let left = wait_all_gone(&[info.pid, launcher], Duration::from_secs(5)).await;
            assert!(left.is_empty(), "still running: {left:?}");
            println!(
                "OK: external hub detected, not stoppable via API, taken over by pid; port {} free",
                cfg.port
            );
        })
    }));
    let _ = kill_process_tree(launcher);
    let _ = external.wait();
    if let Err(p) = result {
        std::panic::resume_unwind(p);
    }
}

/// M10 headless: a worker ask on a real hub reaches the notifier's filter as exactly one
/// 「子 Agent 在提问」 toast for that session (this test shows no toast), de-duplicated.
#[test]
#[ignore = "starts a real hub on AGENT_HUB_PORT (default 4465)"]
fn live_worker_ask_reaches_the_toast_filter_once() {
    let _g = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some(cfg) = live_config() else { return };
    let logs = tempfile::tempdir().unwrap();
    let log = |l: &str| println!("  [shell] {l}");
    block_on(async {
        let (hub, _info) = launch_ready(&cfg, logs.path(), &log).await;
        let api = HubApi::new(cfg.port);
        let start_seq = api.global_seq().await.unwrap();
        let sid = create_pwsh_session(cfg.port, logs.path()).await;
        let port = cfg.port;
        let ask_sid = sid.clone();
        let ask = tauri::async_runtime::spawn(async move {
            post_json(
                port,
                &format!("/api/mailbox/{ask_sid}/worker-ask"),
                json!({"question": "Headless check: which branch?\nsecond line", "timeout_seconds": 3}),
                Duration::from_secs(15),
            )
            .await
        });
        let mut dedup = ToastDeduper::new(100);
        let mut seq = start_seq;
        let mut seen_events = Vec::new();
        let mut toasts = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(20);
        while toasts.is_empty() && Instant::now() < deadline {
            let reply = api.poll(seq, 5).await.unwrap();
            seq = reply.next_seq;
            toasts.extend(toasts_for_events(&reply.events, &mut dedup));
            seen_events.extend(reply.events);
        }
        let categories: Vec<&str> = seen_events
            .iter()
            .filter_map(|e| e["category"].as_str())
            .collect();
        println!("events after seq {start_seq}: {categories:?}");
        println!("toasts: {toasts:?}");
        assert_eq!(toasts.len(), 1, "exactly one toast");
        assert_eq!(toasts[0].session_id, sid);
        assert_eq!(toasts[0].kind, WaitKind::Question);
        assert_eq!(toasts[0].prompt_line, "Headless check: which branch?");
        assert!(
            toasts_for_events(&seen_events, &mut dedup).is_empty(),
            "the same events never toast twice"
        );
        let _ = ask.await;
        let report = hub.stop_gracefully(&log).await;
        assert!(report.graceful);
        println!("OK: one toast for {sid}, de-duplicated; hub stopped");
    });
}
