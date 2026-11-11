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
}
