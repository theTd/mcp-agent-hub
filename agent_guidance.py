"""English instructions authored by the Hub for parent agents."""

CLEANUP_HINT = (
    "After collecting a result, terminate_subagent when no longer needed; completion leaves the "
    "session open. Close started sessions before finishing unless handed off or cancelled. "
    "Results remain readable and resumable."
)

WAIT_HANDLE_HINT = (
    "Waiting: spawn, send, resume and respond return at once with wait_url, one URL per session or spawn group "
    "for all its turns, with no cursor or session parameters. GET it, and again after a timeout. "
    "States are waiting, ask, done, failed. Every ask is an open approval or question: answer it at once "
    "with respond_to_subagent and its request_id (idempotent). After answering, and while any task is unfinished, "
    "GET wait_url again; it returns the next ask or the result. "
    "After a response with response_state=unconfirmed, GET wait_url; never inspect or resend. "
    "An answered request never comes back: if wait_url repeats one, call get_subagent_status_and_trace and report a hub fault. "
    "Never make a waiter skip, filter or dedupe asks by request_id; that hides the next request. "
    "Wait on wait_url only, never by polling session status; call get_subagent_status_and_trace only to inspect progress. "
    + CLEANUP_HINT
)

ROUTING_RULES = (
    "Use a short title and choose enabled profile/model/thinking_level from the catalog.",
    'env={"KEY":"VALUE"} overrides profile env across resume; extra_args=["--flag","value"] '
    "are literal per-session TUI argv (DSH/GUI reject them).",
    'Codex/Claude mcp_servers use HTTP {"url","headers"} or stdio {"command","args","env"}; '
    "disabled_mcp_servers and Claude strict_mcp_config are session-only. Do not mix these with native overrides; config does not prove connection/authentication.",
    "Approvals: leave approval_mode unset, even to be explicit or careful: the default, auto, is the safe choice. Codex and Claude then run their own auto-approval reviewer (Codex --approve-for-me, Claude auto mode) and only what it escalates comes to you; other engines bring you their high-risk actions. "
    'Set approval_mode="ask" only when you or the user want to approve every high-risk action yourselves, "bypass" (no checks) only when the user asked for it or the task needs it. '
    "bypass_approvals is the legacy switch (true = bypass, false = the default); never pass both.",
    "A blocked Windows DSH sandbox_preparation prevents dispatch; skipped is unverified. Permission readiness does not prove access; inspect the reason.",
    WAIT_HANDLE_HINT,
    "For every engine, create the task's own worktree, scratch, logs and results under cwd before spawning: sandboxes and auto-accept cover only cwd (Codex workspace-write, Claude acceptEdits in ask mode, DSH workspace root), so work outside it turns into approvals (the engine reviewer's in auto mode, yours in ask mode); for DSH every call, reads included, and the hub cannot add roots. "
    "Only what the task creates belongs there: leave files it did not create (sources, shared or others' data, credentials) in place, and never widen cwd to a parent, home or drive root to cover them.",
    "Repeated approvals outside cwd mean a wrong layout: answer the pending one, then move only files the task created under cwd, or respawn from a task folder holding them. "
    "Never set auto_approval_rules to stop them: rules match only structured-argv approvals inside cwd, which no engine reports yet.",
    "On a DSH full-access request never pick Allow for this session (approve_session) unless the user said so, even when more will follow: it grants full shell access, deletes included, until the session ends. Pick Allow once (approve).",
    "delivered=true means sent, not accepted. Native records are authoritative; intermediate text is not completion—wait for done.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. Use spawn_subagent to start work, "
    "send_to_subagent to steer it, and resume_subagent to restore an ended session. "
    "Never blindly resend a failed/unconfirmed Codex submission: inspect it, then clear an idle "
    "nonempty draft with send_to_subagent(text='', clear_input=true) before sending new text. "
    "Keep sent text short: put a follow-up longer than a few lines in a file under the session cwd "
    "and send a one-line pointer to it; long text is typed into the terminal slowly and can fail to land. "
    "Retrieve persisted answers with get_subagent_final_response only when an HTTP result is unavailable.\n"
    + "\n".join(f"- {rule}" for rule in ROUTING_RULES)
)

# Console descriptions stay localized. Only built-in descriptions use these English equivalents;
# user-authored profile descriptions and routing notes are passed through verbatim.
PROFILE_DESCRIPTIONS = {
    "omp": "Oh-My-Pi terminal agent; model and thinking level are selected at launch (--model / --thinking).",
    "kimi": "Kimi Code CLI; model uses -m and per-session thinking uses KIMI_MODEL_THINKING_EFFORT. The status bar still shows the global setting.",
    "agy": "Antigravity CLI; the Hub appends the selected thinking level to the model name.",
    "codex": "Codex CLI; model uses -m and thinking uses -c model_reasoning_effort.",
    "claude": "Claude Code CLI; model and thinking use --model / --effort. Runs in Claude's auto mode by default: its classifier approves or blocks each tool call.",
    "dsh": "Native session created through the DSH Hub API, with live events in the console.",
    "pwsh": "Interactive PowerShell 7 terminal.",
}
