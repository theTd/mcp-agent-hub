//! The few hub REST calls the shell needs. Always 127.0.0.1, never through a proxy.

use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;

pub const SERVICE_NAME: &str = "mcp-agent-hub";
pub const LIVE_STATUSES: [&str; 3] = ["starting", "running", "waiting_parent"];

#[derive(Debug, Clone, Deserialize)]
pub struct HubInfo {
    pub service: String,
    #[serde(default)]
    pub pid: u32,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub repo: String,
    #[serde(default)]
    pub started_at: String,
    #[serde(default)]
    pub shutdown_enabled: bool,
}

impl HubInfo {
    pub fn is_agent_hub(&self) -> bool {
        self.service == SERVICE_NAME
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct SessionSummary {
    pub session_id: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub profile_id: Option<String>,
}

impl SessionSummary {
    pub fn is_live(&self) -> bool {
        LIVE_STATUSES.contains(&self.status.as_str())
    }

    pub fn is_waiting(&self) -> bool {
        self.status == "waiting_parent"
    }

    /// What a toast calls this session.
    pub fn display_title(&self) -> String {
        if let Some(t) = self
            .title
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
        {
            return t.to_string();
        }
        let short: String = self.session_id.chars().take(8).collect();
        match self.profile_id.as_deref().filter(|p| !p.is_empty()) {
            Some(p) => format!("{p} · {short}"),
            None => format!("会话 {short}"),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct SessionCounts {
    pub live: usize,
    pub waiting: usize,
}

pub fn count_sessions(sessions: &[SessionSummary]) -> SessionCounts {
    SessionCounts {
        live: sessions.iter().filter(|s| s.is_live()).count(),
        waiting: sessions.iter().filter(|s| s.is_waiting()).count(),
    }
}

#[derive(Debug, Deserialize)]
struct SessionsReply {
    #[serde(default)]
    sessions: Vec<SessionSummary>,
}

#[derive(Debug, Deserialize)]
pub struct TelemetryEvents {
    #[serde(default)]
    pub global_seq: u64,
    #[serde(default)]
    pub events: Vec<Value>,
}

/// The hub's restart progress: `idle` → `stopping` → `resuming` → `ready` (or `failed`).
#[derive(Debug, Clone, Default, Deserialize, serde::Serialize)]
pub struct RestartStatus {
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub restart_id: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub target_count: u32,
    #[serde(default)]
    pub resumed_count: u32,
    #[serde(default)]
    pub skipped_count: u32,
    #[serde(default)]
    pub failed_count: u32,
}

impl RestartStatus {
    /// The shell stops polling here: nothing more will change until the next request.
    pub fn is_terminal(&self) -> bool {
        matches!(self.phase.as_str(), "ready" | "failed" | "idle")
    }
}

/// One `HTTP <code> …` line with the hub's own `detail`, so the error view says why the hub refused
/// instead of only naming a status code.
fn http_error_text(what: &str, code: u16, body: &str) -> String {
    let detail = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .get("detail")
                .or_else(|| value.get("error"))
                .or_else(|| value.get("message"))
                .map(|d| match d {
                    Value::String(s) => s.trim().to_string(),
                    other => other.to_string(),
                })
        })
        .unwrap_or_else(|| body.trim().to_string());
    let hint = match code {
        404 => "（Hub 可能是旧版本，不支持优雅重启）",
        403 => "（Hub 拒绝了关闭令牌）",
        409 => "（Hub 当前状态不允许重启）",
        _ => "",
    };
    if detail.is_empty() {
        format!("HTTP {code} {what}{hint}")
    } else {
        format!("HTTP {code} {what}: {detail}{hint}")
    }
}

#[derive(Clone)]
pub struct HubApi {
    client: reqwest::Client,
    base: String,
}

impl HubApi {
    pub fn new(port: u16) -> Self {
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(2))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        HubApi {
            client,
            base: format!("http://127.0.0.1:{port}"),
        }
    }

    async fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        timeout: Duration,
    ) -> Result<T, String> {
        let resp = self
            .client
            .get(format!("{}{}", self.base, path))
            .timeout(timeout)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status()));
        }
        resp.json::<T>().await.map_err(|e| e.to_string())
    }

    pub async fn info(&self, timeout: Duration) -> Result<HubInfo, String> {
        self.get_json("/api/hub/info", timeout).await
    }

    pub async fn sessions(&self) -> Result<Vec<SessionSummary>, String> {
        let reply: SessionsReply = self
            .get_json("/api/sessions", Duration::from_secs(5))
            .await?;
        Ok(reply.sessions)
    }

    /// Ask an owned hub to stop the way Ctrl+C does. The token goes only into the header.
    pub async fn shutdown(&self, token: &str) -> Result<(), String> {
        let resp = self
            .client
            .post(format!("{}/api/hub/shutdown", self.base))
            .header("X-Hub-Shutdown-Token", token)
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if resp.status().is_success() {
            Ok(())
        } else {
            Err(format!("HTTP {}", resp.status()))
        }
    }

    /// Ask an owned hub to stop *and plan a restart*: it archives the live sessions, remembers them
    /// in `resume_on_start.json`, and stops. `202 Accepted` (and a plain `200`) both mean accepted;
    /// a `404` means the running hub predates this endpoint.
    pub async fn graceful_restart(
        &self,
        token: &str,
        mode: &str,
        restart_id: Option<&str>,
    ) -> Result<Value, String> {
        let body = serde_json::json!({
            "mode": mode,
            "restart_id": restart_id,
            "continue_unfinished_turns": true,
        });
        let resp = self
            .client
            .post(format!("{}/api/hub/graceful-restart", self.base))
            .header("X-Hub-Shutdown-Token", token)
            .json(&body)
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let code = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        if (200..300).contains(&code) {
            if text.trim().is_empty() {
                return Ok(Value::Null);
            }
            return serde_json::from_str(&text)
                .map_err(|e| format!("Hub 返回的不是 JSON：{e}（{text}）"));
        }
        Err(http_error_text("graceful-restart", code, &text))
    }

    /// `GET /api/hub/restart-status`: how far the hub got with resuming after a planned restart.
    pub async fn restart_status(&self, timeout: Duration) -> Result<RestartStatus, String> {
        let resp = self
            .client
            .get(format!("{}/api/hub/restart-status", self.base))
            .timeout(timeout)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let code = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        if !(200..300).contains(&code) {
            return Err(http_error_text("restart-status", code, &text));
        }
        serde_json::from_str(&text)
            .map_err(|e| format!("restart-status 不是 JSON：{e}（{text}）"))
    }

    /// Console-only telemetry. This cursor is independent from parent wait handles.
    pub async fn telemetry(&self, after_seq: u64) -> Result<TelemetryEvents, String> {
        let reply: TelemetryEvents = self
            .get_json(
                &format!("/api/telemetry/events?session_id=all&after_seq={after_seq}"),
                Duration::from_secs(10),
            )
            .await?;
        Ok(reply)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(status: &str) -> SessionSummary {
        SessionSummary {
            session_id: "abcdef123456".into(),
            title: None,
            status: status.into(),
            profile_id: None,
        }
    }

    #[test]
    fn counts_live_and_waiting() {
        let list: Vec<SessionSummary> = [
            "starting",
            "running",
            "waiting_parent",
            "completed",
            "exited",
            "terminated",
        ]
        .iter()
        .map(|s| session(s))
        .collect();
        let c = count_sessions(&list);
        assert_eq!(c.live, 3);
        assert_eq!(c.waiting, 1);
    }

    #[test]
    fn display_title_falls_back() {
        let mut s = session("running");
        assert_eq!(s.display_title(), "会话 abcdef12");
        s.profile_id = Some("pwsh".into());
        assert_eq!(s.display_title(), "pwsh · abcdef12");
        s.title = Some("  修复登录  ".into());
        assert_eq!(s.display_title(), "修复登录");
    }

    #[test]
    fn info_parses_the_hub_answer() {
        let info: HubInfo = serde_json::from_str(
            r#"{"service":"mcp-agent-hub","pid":12,"port":4465,"repo":"E:\\r","started_at":"x","shutdown_enabled":true}"#,
        )
        .unwrap();
        assert!(info.is_agent_hub());
        assert_eq!(
            (info.pid, info.port, info.shutdown_enabled),
            (12, 4465, true)
        );
        let other: HubInfo = serde_json::from_str(r#"{"service":"something-else"}"#).unwrap();
        assert!(!other.is_agent_hub());
    }

    #[test]
    fn restart_status_parses_a_full_and_a_sparse_answer() {
        let full: RestartStatus = serde_json::from_str(
            r#"{"ok":true,"phase":"resuming","restart_id":"r-1","mode":"hub","target_count":3,"resumed_count":1,"skipped_count":1,"failed_count":1}"#,
        )
        .unwrap();
        assert_eq!(full.phase, "resuming");
        assert_eq!(full.restart_id.as_deref(), Some("r-1"));
        assert_eq!(
            (
                full.target_count,
                full.resumed_count,
                full.skipped_count,
                full.failed_count
            ),
            (3, 1, 1, 1)
        );
        assert!(!full.is_terminal());

        // A hub that has no restart in mind answers with idle and nulls instead of omitting keys.
        let idle: RestartStatus = serde_json::from_str(
            r#"{"ok":true,"phase":"idle","restart_id":null,"mode":null,"target_count":0,"resumed_count":0,"skipped_count":0,"failed_count":0}"#,
        )
        .unwrap();
        assert!(idle.is_terminal());
        assert_eq!(idle.restart_id, None);
    }

    #[test]
    fn error_text_keeps_the_hub_detail_and_names_the_code() {
        assert_eq!(
            http_error_text(
                "graceful-restart",
                404,
                r#"{"detail":"Not Found"}"#
            ),
            "HTTP 404 graceful-restart: Not Found（Hub 可能是旧版本，不支持优雅重启）"
        );
        assert_eq!(
            http_error_text("graceful-restart", 409, r#"{"detail":"Hub 正在恢复会话"}"#),
            "HTTP 409 graceful-restart: Hub 正在恢复会话（Hub 当前状态不允许重启）"
        );
        assert_eq!(
            http_error_text("restart-status", 500, ""),
            "HTTP 500 restart-status"
        );
    }
}
