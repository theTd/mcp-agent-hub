//! Toasts for sessions that wait on the parent: long-polls the hub's mailbox and shows one toast per
//! approval or question (per request_id) from user-launched sessions, unless the window is in front.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tauri::{AppHandle, Manager};

use crate::desktop_toast::{self, ToastClick};
use crate::hub_supervisor::HubShell;

pub const PROMPT_MAX_CHARS: usize = 120;
const POLL_TIMEOUT_SECS: u64 = 25;
const DEDUP_CAPACITY: usize = 2000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WaitKind {
    Approval,
    Question,
}

impl WaitKind {
    pub fn toast_title(self) -> &'static str {
        match self {
            WaitKind::Approval => "子 Agent 等待审批",
            WaitKind::Question => "子 Agent 在提问",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaitingToast {
    /// De-dup key: the request_id, or `<session>#<seq>` when the event has none.
    pub key: String,
    pub session_id: String,
    pub kind: WaitKind,
    pub prompt_line: String,
}

fn str_field<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
}

/// The first non-empty line, at most `max` characters (an ellipsis marks a cut).
pub fn first_line_capped(text: &str, max: usize) -> String {
    let line = text
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("");
    if line.chars().count() <= max {
        return line.to_string();
    }
    let mut out: String = line.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Which mailbox events notify: `approval_waiting_reply` and `question_waiting_reply`, nothing else.
pub fn waiting_toast_for(event: &Value) -> Option<WaitingToast> {
    // The parent agent handles its children's questions and approvals through the mailbox.
    // Missing provenance keeps older hubs compatible with the existing notification behavior.
    if str_field(event, "launch_source") == Some("agent") {
        return None;
    }
    let kind = match str_field(event, "category")? {
        "approval_waiting_reply" => WaitKind::Approval,
        "question_waiting_reply" => WaitKind::Question,
        _ => return None,
    };
    let session_id = str_field(event, "session_id")?.to_string();
    let key = match str_field(event, "request_id") {
        Some(rid) => rid.to_string(),
        None => format!(
            "{}#{}",
            session_id,
            event.get("seq").and_then(Value::as_u64).unwrap_or(0)
        ),
    };
    // Native structured interactions carry the raw prompt; historical events may only have content.
    let prompt = event
        .get("interaction")
        .and_then(|i| str_field(i, "prompt"))
        .or_else(|| str_field(event, "content"))
        .unwrap_or("");
    Some(WaitingToast {
        key,
        session_id,
        kind,
        prompt_line: first_line_capped(prompt, PROMPT_MAX_CHARS),
    })
}

/// Remembers which keys already produced a toast (bounded).
pub struct ToastDeduper {
    seen: HashSet<String>,
    order: VecDeque<String>,
    capacity: usize,
}

impl ToastDeduper {
    pub fn new(capacity: usize) -> Self {
        ToastDeduper {
            seen: HashSet::new(),
            order: VecDeque::new(),
            capacity: capacity.max(1),
        }
    }

    /// True the first time a key is seen.
    pub fn first_time(&mut self, key: &str) -> bool {
        if self.seen.contains(key) {
            return false;
        }
        if self.order.len() >= self.capacity {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            }
        }
        self.seen.insert(key.to_string());
        self.order.push_back(key.to_string());
        true
    }
}

/// The toasts a batch of events produces, in order, after de-dup.
pub fn toasts_for_events(events: &[Value], dedup: &mut ToastDeduper) -> Vec<WaitingToast> {
    events
        .iter()
        .filter_map(waiting_toast_for)
        .filter(|t| dedup.first_time(&t.key))
        .collect()
}

fn main_window_in_front(app: &AppHandle) -> bool {
    match app.get_webview_window("main") {
        Some(w) => w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false),
        None => false,
    }
}

async fn pause(gen_rx: &mut tokio::sync::watch::Receiver<u64>, d: Duration) {
    tokio::select! {
        _ = tokio::time::sleep(d) => {}
        _ = gen_rx.changed() => {}
    }
}

/// Runs for the app's lifetime. The cursor starts at the hub's current `global_seq` (old events
/// never notify) and is reset whenever the hub generation changes (restart, takeover) or a poll fails.
pub async fn run(app: AppHandle, shell: Arc<HubShell>) {
    let mut dedup = ToastDeduper::new(DEDUP_CAPACITY);
    let mut gen_rx = shell.subscribe_generation();
    let mut cursor: Option<(u64, u64)> = None;
    loop {
        gen_rx.borrow_and_update();
        let Some(target) = shell.ready_target() else {
            cursor = None;
            pause(&mut gen_rx, Duration::from_secs(1)).await;
            continue;
        };
        let after = match cursor {
            Some((generation, seq)) if generation == target.generation => seq,
            _ => match target.api.global_seq().await {
                Ok(seq) => {
                    shell.log(&format!(
                        "notifier: watching events after seq {seq} (generation {})",
                        target.generation
                    ));
                    seq
                }
                Err(_) => {
                    cursor = None;
                    pause(&mut gen_rx, Duration::from_secs(2)).await;
                    continue;
                }
            },
        };
        let polled = tokio::select! {
            r = target.api.poll(after, POLL_TIMEOUT_SECS) => Some(r),
            _ = gen_rx.changed() => None,
        };
        let reply = match polled {
            None => {
                cursor = None;
                continue;
            }
            Some(Err(_)) => {
                cursor = None;
                pause(&mut gen_rx, Duration::from_secs(2)).await;
                continue;
            }
            Some(Ok(reply)) => reply,
        };
        if reply.next_seq < after {
            // The hub's sequence went backwards (a different hub answers now): start over.
            cursor = None;
            continue;
        }
        cursor = Some((target.generation, reply.next_seq));
        if !reply.events.is_empty() {
            shell.refresh_counts_and_tray(&app).await;
        }
        let toasts = toasts_for_events(&reply.events, &mut dedup);
        if toasts.is_empty() {
            continue;
        }
        let titles: HashMap<String, String> = target
            .api
            .sessions()
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|s| (s.session_id.clone(), s.display_title()))
            .collect();
        for toast in toasts {
            if main_window_in_front(&app) {
                shell.log(&format!(
                    "notifier: {} for {} skipped (window in front)",
                    toast.key, toast.session_id
                ));
                continue;
            }
            let session_title = titles.get(&toast.session_id).cloned().unwrap_or_else(|| {
                format!(
                    "会话 {}",
                    toast.session_id.chars().take(8).collect::<String>()
                )
            });
            shell.log(&format!(
                "notifier: toast {} for session {}",
                toast.key, toast.session_id
            ));
            desktop_toast::show(
                &app,
                toast.kind.toast_title(),
                &session_title,
                Some(&toast.prompt_line),
                ToastClick::OpenSession(toast.session_id.clone()),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn approval_and_question_events_notify() {
        let approval = json!({
            "seq": 7, "session_id": "s1", "type": "approval_request", "category": "approval_waiting_reply",
            "request_id": "int-1", "content": "【⚠️ 高风险操作审批请求】\n- 详情: rm -rf build",
            "interaction": {"prompt": "Run `rm -rf build`?\nsecond line", "title": "Shell"}
        });
        let t = waiting_toast_for(&approval).unwrap();
        assert_eq!(t.kind, WaitKind::Approval);
        assert_eq!(t.key, "int-1");
        assert_eq!(t.session_id, "s1");
        assert_eq!(t.prompt_line, "Run `rm -rf build`?");
        assert_eq!(t.kind.toast_title(), "子 Agent 等待审批");

        let ask = json!({
            "seq": 8, "session_id": "s2", "type": "question", "category": "question_waiting_reply",
            "request_id": "ask-1", "content": "Which branch?", "interaction": null
        });
        let t = waiting_toast_for(&ask).unwrap();
        assert_eq!(t.kind, WaitKind::Question);
        assert_eq!(t.prompt_line, "Which branch?");
        assert_eq!(t.kind.toast_title(), "子 Agent 在提问");
    }

    #[test]
    fn agent_launched_sessions_do_not_notify() {
        for category in ["approval_waiting_reply", "question_waiting_reply"] {
            for (source, notifies) in [("agent", false), ("user", true)] {
                let ev = json!({"seq": 1, "session_id": "s1", "category": category,
                    "launch_source": source, "request_id": "r", "content": "Reply?"});
                assert_eq!(
                    waiting_toast_for(&ev).is_some(),
                    notifies,
                    "{source}: {category}"
                );
            }
        }
    }

    #[test]
    fn other_events_do_not_notify() {
        for category in [
            "message",
            "completed",
            "turn_completed",
            "exited",
            "question",
            "approval_request",
            "",
        ] {
            let ev = json!({"seq": 1, "session_id": "s1", "type": "question", "category": category, "request_id": "r"});
            assert!(waiting_toast_for(&ev).is_none(), "{category}");
        }
        assert!(waiting_toast_for(&json!({"seq": 1, "session_id": "s1"})).is_none());
        // No session id → nothing to open.
        assert!(
            waiting_toast_for(&json!({"seq": 1, "category": "question_waiting_reply"})).is_none()
        );
    }

    #[test]
    fn dedup_by_request_id() {
        let mut dedup = ToastDeduper::new(100);
        let ev = |seq: u64, rid: &str| json!({"seq": seq, "session_id": "s1", "category": "approval_waiting_reply", "request_id": rid, "content": "x"});
        let first = toasts_for_events(
            &[ev(1, "int-a"), ev(2, "int-a"), ev(3, "int-b")],
            &mut dedup,
        );
        assert_eq!(
            first.iter().map(|t| t.key.as_str()).collect::<Vec<_>>(),
            vec!["int-a", "int-b"]
        );
        // A later poll that repeats a request (e.g. an adopted interaction) stays quiet.
        assert!(toasts_for_events(&[ev(9, "int-b")], &mut dedup).is_empty());
    }

    #[test]
    fn events_without_request_id_dedup_by_session_and_seq() {
        let mut dedup = ToastDeduper::new(100);
        let ev = |sid: &str, seq: u64| json!({"seq": seq, "session_id": sid, "category": "question_waiting_reply", "request_id": null, "content": "q"});
        let toasts = toasts_for_events(
            &[ev("s1", 4), ev("s1", 4), ev("s1", 5), ev("s2", 4)],
            &mut dedup,
        );
        assert_eq!(
            toasts.iter().map(|t| t.key.as_str()).collect::<Vec<_>>(),
            vec!["s1#4", "s1#5", "s2#4"]
        );
    }

    #[test]
    fn dedup_is_bounded() {
        let mut dedup = ToastDeduper::new(2);
        assert!(dedup.first_time("a"));
        assert!(dedup.first_time("b"));
        assert!(dedup.first_time("c")); // evicts "a"
        assert!(!dedup.first_time("c"));
        assert!(dedup.first_time("a"));
    }

    #[test]
    fn prompt_is_the_first_line_capped_at_120_chars() {
        assert_eq!(first_line_capped("\n\n  hello  \nworld", 120), "hello");
        let long = "审".repeat(130);
        let capped = first_line_capped(&long, 120);
        assert_eq!(capped.chars().count(), 120);
        assert!(capped.ends_with('…'));
        assert_eq!(
            first_line_capped(&"a".repeat(120), 120).chars().count(),
            120
        );
        assert_eq!(first_line_capped("", 120), "");
    }

    #[test]
    fn falls_back_to_content_when_the_interaction_has_no_prompt() {
        let ev = json!({"seq": 3, "session_id": "s1", "category": "approval_waiting_reply", "request_id": "int-9",
                        "content": "line one\nline two", "interaction": {"prompt": "  "}});
        assert_eq!(waiting_toast_for(&ev).unwrap().prompt_line, "line one");
    }
}
