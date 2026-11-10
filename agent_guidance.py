"""English instructions authored by the Hub for parent agents."""

CLEANUP_HINT = (
    "After collecting a subagent's result, call terminate_subagent as soon as it is no longer "
    "needed. Completion does not close its process. Before finishing the parent task, close "
    "all subagents you started that no longer need to remain open. Saved results remain "
    "readable after closing; use resume_subagent if you need the session again. Do not rely "
    "on automatic cleanup as a substitute for prompt closure."
)

CONTINUATION_HINT = (
    "Handle pending interactions first. Collect completed results and inspect failed/unknown IDs. "
    "While unfinished_session_ids is nonempty, call wait_subagent_events with next_wait_arguments "
    "after processing this result. Progress, delivery and timeout do not mean completion. "
    + CLEANUP_HINT
)

BACKGROUND_POLL_HINT = (
    CONTINUATION_HINT + " Do not finish the parent task with delegated work outstanding "
    "unless the user explicitly requested a handoff or cancellation. "
    "Use background_poll_command only when the host provides reliable background completion "
    "notifications. A process/session ID alone is not a notification; otherwise use the MCP wait. "
    "The CLI exits after one event batch or timeout: handle its output and rearm using "
    "next_wait_arguments or next_poll_command if work remains. Keep at most one waiter per "
    "session. For multiple sessions, wait on explicit comma-separated IDs with a shared cursor; "
    "when combining separate cursors, start from the lowest unconsumed cursor and deduplicate "
    "by event seq. Never advance a cursor using a status lookup or an unrelated session."
)

ROUTING_RULES = (
    CLEANUP_HINT,
    "Provide a short, readable title summarizing the task, such as 'Fix build failure' "
    "or 'Repair session resume'; do not use the full task as the title.",
    "Choose an enabled profile, model and thinking_level from the catalog and its routing notes.",
    'Use env={"KEY": "VALUE"} for per-session environment overrides. These override profile '
    "values and are preserved on resume.",
    'Use extra_args=["--flag", "value"] for per-session TUI CLI arguments appended to profile '
    "args and preserved on resume. Tokens are passed literally, without shell parsing. "
    "Use the dedicated model, thinking_level and bypass_approvals fields for those controls; "
    "DSH and GUI sessions do not accept CLI arguments.",
    'Codex/Claude accept mcp_servers={"docs":{"url":"https://example.com/mcp"}} or '
    'mcp_servers={"local":{"command":"node","args":["server.mjs"],"env":{"MODE":"readonly"}}}. '
    'HTTP supports headers. Use disabled_mcp_servers=["unused"] to disable named servers; '
    'Codex names refer to ordinary mcp_servers, not plugin MCP. Claude alone supports '
    'strict_mcp_config=true to load only the supplied set (an empty set is allowed). '
    'These session settings survive resume. Do not combine with native MCP overrides in '
    'profile args/extra_args/settings. Configuration does not confirm connection or authentication.',
    "Use bypass_approvals=null to inherit the profile, false to require approvals, or true to bypass "
    "them for this session. Explicit overrides survive resume and never change the profile.",
    "Local Windows DSH launches report sandbox_preparation. The Hub can add a root-only "
    "WRITE_OWNER grant to the matching host account on an owned workspace. A blocked check "
    "prevents dispatch; skipped means the host could not be verified. Permission readiness "
    "does not confirm native sandbox startup or access to existing descendants. Inspect the "
    "reason instead of repeatedly retrying.",
    BACKGROUND_POLL_HINT,
    "Questions and approval requests set waiting_parent. Answer the native interaction with "
    "reply_to_subagent or respond_to_subagent_approval_or_choice, using its request_id.",
    "An approval response's delivered flag means input was sent, not accepted by the CLI. "
    "If response_state is unconfirmed, keep waiting; never resend automatically.",
    "Subagents use their native questions and responses. The Hub observes native records; "
    "do not ask subagents to call Hub reporting tools. Intermediate text does not confirm "
    "completion. Check the native turn completion and from_previous_turn flag before using a final answer.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. Use spawn_subagent to start work, "
    "send_to_subagent to steer it, and resume_subagent to restore an ended session. "
    "Check delivery.delivered and the matching last_injection; null means unconfirmed, not success. "
    "Never blindly resend a failed/unconfirmed Codex submission: inspect it, then clear an idle "
    "nonempty draft with send_to_subagent(text='', clear_input=true) before sending new text. "
    "Inspect progress with get_subagent_status_and_trace; retrieve persisted answers with "
    "get_subagent_final_response even after a process exits.\n"
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
