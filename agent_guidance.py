"""English instructions authored by the Hub for parent agents."""

CLEANUP_HINT = (
    "After collecting a result, terminate_subagent when no longer needed; completion leaves the "
    "session open. Close started sessions before finishing unless handed off or cancelled. "
    "Results remain readable and resumable."
)

WAIT_HANDLE_HINT = (
    "After spawn, resume, or send, GET the returned wait_url; retry the same URL after timeout. "
    "States are waiting, ask, done, failed. For an ask (approval or question), immediately call "
    "respond_to_subagent with request_id; responses are idempotent. "
    "If a task is unfinished, or a response is unconfirmed, GET wait_url next. "
    + CLEANUP_HINT
)

ROUTING_RULES = (
    "Use a short title and choose enabled profile/model/thinking_level from the catalog.",
    'env={"KEY":"VALUE"} overrides profile env across resume; extra_args=["--flag","value"] '
    "are literal per-session TUI argv (DSH/GUI reject them).",
    'Codex/Claude mcp_servers use HTTP {"url","headers"} or stdio {"command","args","env"}; '
    "disabled_mcp_servers and Claude strict_mcp_config are session-only. Do not mix these with native overrides; config does not prove connection/authentication.",
    "bypass_approvals=null inherits profile, false requires approval, true bypasses it for this session.",
    "A blocked Windows DSH sandbox_preparation prevents dispatch; skipped is unverified. Permission readiness does not prove access; inspect the reason.",
    WAIT_HANDLE_HINT,
    "For every engine, create the task's own worktree, scratch, logs and results under cwd before spawning: sandboxes and auto-accept cover only cwd (Codex workspace-write, Claude acceptEdits, DSH workspace root), so work outside it turns into approvals; for DSH every call, reads included, and the hub cannot add roots. "
    "Only what the task creates belongs there: leave files it did not create (sources, shared or others' data, credentials) in place, and never widen cwd to a parent, home or drive root to cover them.",
    "Repeated approvals outside cwd mean a wrong layout: answer the pending one, then move only files the task created under cwd, or respawn from a task folder holding them. "
    "Never set auto_approval_rules to stop them: rules match only structured-argv approvals inside cwd, which no engine reports yet.",
    "On a DSH full-access request never pick Allow for this session (approve_session) unless the user said so, even when more will follow: it grants full shell access, deletes included, until the session ends. Pick Allow once (approve).",
    "delivered=true means sent, not accepted; response_state=unconfirmed means GET wait_url and never resend. Native records are authoritative; intermediate text is not completion—wait for done.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. Use spawn_subagent to start work, "
    "send_to_subagent to steer it, and resume_subagent to restore an ended session. "
    "Control calls return immediately with opaque wait_handle and wait_url; GET that URL and retry it after timeout. "
    "No cursor or session parameters are needed. "
    "After any approval response, response_state=unconfirmed has priority: GET wait_url, never inspect or resend. "
    "Never blindly resend a failed/unconfirmed Codex submission: inspect it, then clear an idle "
    "nonempty draft with send_to_subagent(text='', clear_input=true) before sending new text. "
    "Inspect progress with get_subagent_status_and_trace only when needed; retrieve persisted answers with "
    "get_subagent_final_response only when an HTTP result is unavailable.\n"
    + "\n".join(f"- {rule}" for rule in ROUTING_RULES)
)

# Console descriptions stay localized. Only built-in descriptions use these English equivalents;
# user-authored profile descriptions and routing notes are passed through verbatim.
PROFILE_DESCRIPTIONS = {
    "omp": "Oh-My-Pi terminal agent; model and thinking level are selected at launch (--model / --thinking).",
    "kimi": "Kimi Code CLI; model uses -m and per-session thinking uses KIMI_MODEL_THINKING_EFFORT. The status bar still shows the global setting.",
    "agy": "Antigravity CLI; the Hub appends the selected thinking level to the model name.",
    "codex": "Codex CLI; model uses -m and thinking uses -c model_reasoning_effort.",
    "claude": "Claude Code CLI; model and thinking use --model / --effort. High-risk commands require approval by default.",
    "dsh": "Native session created through the DSH Hub API, with live events in the console.",
    "pwsh": "Interactive PowerShell 7 terminal.",
}
