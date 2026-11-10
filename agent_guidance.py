"""English instructions authored by the Hub for parent agents."""

BACKGROUND_POLL_HINT = (
    "Run background_poll_command using the host shell's background mode "
    "(for example, run_in_background=true or WaitMsBeforeAsync=500), then yield "
    "for its completion notification or continue other work. Do not run it in "
    "the foreground or repeatedly call wait_subagent_events. For several sessions, "
    "use one hub_poll.py command with --session id1,id2,id3; do not write a polling loop. "
    "If the host has no background shell support, use wait_subagent_events as a fallback."
)

ROUTING_RULES = (
    "Provide a short, readable title summarizing the task, such as 'Fix build failure' "
    "or 'Repair session resume'; do not use the full task as the title.",
    "Choose an enabled profile, model and thinking_level from the catalog and its routing notes.",
    'Use env={"KEY": "VALUE"} for per-session environment overrides. These override profile '
    "values and are preserved on resume.",
    "Use background_poll_command from spawn_subagent, send_to_subagent or resume_subagent "
    "to wait for further events. " + BACKGROUND_POLL_HINT,
    "Questions and approval requests set waiting_parent. Answer the native interaction with "
    "reply_to_subagent or respond_to_subagent_approval_or_choice, using its request_id.",
    "Subagents use their native questions and responses. The Hub observes native records; "
    "do not ask subagents to call Hub reporting tools. Intermediate text does not confirm "
    "completion. Check the native turn completion and from_previous_turn flag before using a final answer.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. Use spawn_subagent to start work, "
    "send_to_subagent to steer it, and resume_subagent to restore an ended session. "
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
