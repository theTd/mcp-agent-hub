"""English instructions authored by the Hub for parent agents."""

CLEANUP_HINT = (
    "After collecting a result, terminate_subagent when the subagent is no longer needed; "
    "completion does not close it. Before the parent finishes, close started subagents unless "
    "the user requested handoff/cancellation. Results remain readable and resumable."
)

CONTINUATION_HINT = (
    "Handle pending interactions first; collect completed results and inspect failed/unknown IDs. "
    "If unfinished_session_ids remains, use the returned HTTP poll arguments or background_poll_command; "
    "Progress, delivery and timeout are not completion. "
    + CLEANUP_HINT
)

BACKGROUND_POLL_HINT = (
    CONTINUATION_HINT + " MCP calls are non-blocking: never use wait_subagent_events to wait. "
    "Run hub_poll.py/background_poll_command as an HTTP long poll; it returns after one batch and "
    "must be rearmed with next_wait_arguments/next_poll_command if work remains. Keep one waiter per session. "
    "For multiple sessions, use explicit comma-separated IDs and a shared cursor; merge cursors "
    "from the lowest unconsumed seq and deduplicate by event seq. Never advance a cursor from "
    "status or an unrelated session."
)

ROUTING_RULES = (
    "Use a short task title; do not use the full task.",
    "Choose an enabled profile, model and thinking_level from the catalog and routing guide.",
    'env={"KEY":"VALUE"} overrides profile env and survives resume.',
    'extra_args=["--flag","value"] appends literal per-session TUI CLI tokens to profile args and survives resume. '
    "Use model, thinking_level and bypass_approvals fields for those controls; DSH/GUI sessions reject CLI args.",
    'Codex/Claude mcp_servers accept HTTP {"url","headers"} or stdio {"command","args","env"}; '
    'disabled_mcp_servers disables names. Claude only: strict_mcp_config=true loads only supplied servers, including an empty set. '
    "Settings survive resume; do not mix them with native MCP overrides; config does not prove connection/authentication.",
    "bypass_approvals=null inherits the profile; false requires approval; true bypasses it for this session only.",
    "Windows DSH reports sandbox_preparation. A blocked check prevents dispatch; skipped means unverified. Permission readiness does not prove sandbox/access; inspect the reason and do not blindly retry.",
    BACKGROUND_POLL_HINT,
    "Questions and approvals set waiting_parent; answer with reply_to_subagent or "
    "respond_to_subagent_approval_or_choice using request_id.",
    "For repeated low-risk read-only, workspace-local test or build approvals, prefer a short-lived, "
    "session-scoped auto_approval_rules policy. Never use it for deletion, privilege escalation, "
    "credentials, network upload, process/service control or workspace-outside changes.",
    "delivered=true means input was sent, not accepted; response_state=unconfirmed → wait, never resend.",
    "Native records are authoritative; intermediate text is not completion. Check native turn completion "
    "and from_previous_turn before reporting done.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. Use spawn_subagent to start work, "
    "send_to_subagent to steer it, and resume_subagent to restore an ended session. "
    "MCP control calls return immediately: accepted/pending is not delivery confirmation. "
    "Use the HTTP poll stream and matching submit_id to observe delivery and completion. "
    "Never blindly resend a failed/unconfirmed Codex submission: inspect it, then clear an idle "
    "nonempty draft with send_to_subagent(text='', clear_input=true) before sending new text. "
    "Inspect progress with the HTTP poll stream; retrieve persisted answers with "
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
