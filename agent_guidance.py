"""English instructions authored by the Hub for parent agents."""
from pathlib import Path

# The waiter parents run as one background command (stdlib only, shipped in the portable payload).
HUB_WAIT_CLI = Path(__file__).resolve().parent / "tools" / "hub_wait.py"

CLEANUP_HINT = (
    "After collecting a result, close the session: collect_and_terminate(wait_handle) collects and closes every "
    "finished session in one call, terminate_subagent one. Completion leaves the session open; close started "
    "sessions before finishing unless handed off or cancelled. Results remain readable and resumable."
)

WAIT_HANDLE_HINT = (
    "Waiting: spawn, send, resume and respond return at once with wait_url, one URL per session or spawn group "
    "for all its turns, with no cursor or session parameters; a timeout GET returns state=waiting (states: waiting, "
    "ask, undelivered, done, failed). "
    "Default: any host that can run a background command, including a shell that auto-backgrounds after 10 seconds "
    "(AGY) and notifies you on exit, runs the hub's waiter as ONE background shell command: "
    f'python "{HUB_WAIT_CLI}" <wait_url> [--max-seconds N] (Python 3.8+) exits 0 with the JSON on ask, undelivered, '
    "done or failed, 2 after N seconds (default 3000) still waiting (run it again; that cap is a heartbeat), and 3 on "
    "an HTTP error or a hub unreachable for 2 minutes. Do not call wait_for_hub instead. "
    "Fallback only, for a host with no background shell or async tasks at all: call "
    "wait_for_hub(wait_handle, timeout_seconds=50), shorter if the MCP call itself is capped, repeating on each "
    "state=waiting. "
    "undelivered lists every sent message the worker never read (the hub could not submit or confirm it); the worker "
    "still works: follow each entry's recovery_hint, then GET wait_url again; a done result lists them under "
    "undelivered_messages. "
    "Every ask is an open approval or question: answer it at once with respond_to_subagent and its request_id "
    "(idempotent), or respond_to_subagents for several. An ask answer lists every open request in requests: answer "
    "every entry. After answering, and while any task is unfinished, wait again; it returns the next ask or the "
    "result. After a response with response_state=unconfirmed, GET wait_url; never inspect or resend. "
    "An answered request never comes back: never make a waiter skip, filter or dedupe asks by request_id (that hides "
    "the next request); if wait_url repeats one, call get_subagent_status_and_trace and report a hub fault. "
    "A done result whose outcome is error (also in engine_errors) means the engine stopped on an error, not an "
    "answer: quota and auth need the user, rate_limit, network and server retry by sending a message, request needs "
    "a smaller or changed request. "
    "Wait only on wait_url or wait_for_hub; polling /api/sessions is never waiting. Call "
    "get_subagent_status_and_trace only when you need to see progress, not to wait. "
    + CLEANUP_HINT
)

# list_active_sessions is intentionally compact for routine orchestration; request the legacy payload only
# when a diagnostic caller needs it.
SESSION_LIST_HINT = (
    "list_active_sessions returns a compact view by default; view=\"full\" gives the legacy payload. Each entry "
    "names spawned_by (the spawning hub session's id and title, or an outside parent's process); mine=true lists "
    "only the sessions you spawned."
)

# Prepended to a spawn's task when the parent passes leaf_worker=true. Advice only: nothing blocks the
# worker's agent-hub tools (for that, Codex and Claude take disabled_mcp_servers).
LEAF_WORKER_NOTE = (
    "[agent-hub] You are a leaf worker: do this task yourself and never spawn subagents. Do not call "
    "spawn_subagent or spawn_subagents, and do not hand any part of the task to another agent. If the task "
    "is too big to finish alone, say so in your reply instead."
)


def with_leaf_worker_note(task: str) -> str:
    return f"{LEAF_WORKER_NOTE}\n\n{task}"


ROUTING_RULES = (
    "Use a short title.",
    "leaf_worker is required on every spawn: true (the usual choice for a self-contained job) puts a note before "
    "the task telling it to do the job itself and never spawn subagents; false, only for a worker meant to "
    "dispatch others, sends the task unchanged. The note is advice, not a block: Codex/Claude also take "
    'disabled_mcp_servers=["agent-hub"] to remove the tools.',
    'env={"KEY":"VALUE"} overrides profile env across resume; extra_args=["--flag","value"] '
    "are literal per-session TUI argv (DSH/GUI reject them).",
    'Codex/Claude mcp_servers use HTTP {"url","headers"} or stdio {"command","args","env"}; '
    "disabled_mcp_servers and Claude strict_mcp_config are session-only. Do not mix these with native overrides; config does not prove connection/authentication.",
    "Approvals: leave approval_mode unset, even to be explicit or careful: the default, auto, is the safe choice. Codex and Claude then run their own auto-approval reviewer (Codex --approve-for-me, Claude auto mode) and only what it escalates comes to you; other engines bring you their high-risk actions. "
    'Set approval_mode="ask" only when you or the user want to approve every high-risk action yourselves, "bypass" (no checks) only when the user asked for it or the task needs it. '
    "bypass_approvals is the legacy switch (true = bypass, false = the default); never pass both.",
    "A blocked Windows DSH sandbox_preparation prevents dispatch; skipped is unverified. Permission readiness does not prove access; inspect the reason.",
    WAIT_HANDLE_HINT,
    SESSION_LIST_HINT,
    "For every engine, create the task's own worktree, scratch, logs and results under cwd before spawning: sandboxes and auto-accept cover only cwd (Codex workspace-write, Claude acceptEdits in ask mode, DSH workspace root), so work outside it turns into approvals (the engine reviewer's in auto mode, yours in ask mode); for DSH every call, reads included, and the hub cannot add roots. "
    "Only what the task creates belongs there: leave files it did not create (sources, shared or others' data, credentials) in place, and never widen cwd to a parent, home or drive root to cover them. "
    "When the task must use a folder outside cwd (a shared lock folder, say), pass extra_roots=[absolute folders] (claude, codex, omp, kimi, agy): "
    "the worker may then read AND write there as in cwd, so list each folder the task needs, never a parent that holds them (it grants everything inside); "
    "drive roots, home, credential, system and hub-data folders are refused. DSH takes no extra_roots: leave such a folder in place and answer its approvals.",
    "Repeated approvals outside cwd mean a wrong layout: answer the pending one, then move only files the task created under cwd, or respawn from a task folder holding them. "
    "Never set auto_approval_rules to stop them: rules match only structured-argv approvals inside cwd, which no engine reports yet; to reduce approvals, spawn omp or agy with auto_approve_once_prefixes.",
    "auto_approve_once_prefixes (omp and agy only): the hub answers approve once, never for the session, when a high-risk command starts with one of these prefixes, each starting with the program's absolute path. "
    "A command with ; | & > <, a backtick, $(, ( or ) or a control character is never approved automatically. "
    "At most 20 automatic approvals per session. "
    "Only the prefix is checked, so name a specific program and subcommand, never a shell or interpreter alone (python.exe, cmd.exe /c, env). "
    "Each automatic approval leaves an approval_auto_resolved event.",
    "brief_markdown: for a task description longer than a few lines. The hub writes it to its data folder, starts the task with a one-line pointer to the file, and deletes it when the session closes. Limit: 200 000 characters.",
    "A live measurement brief (numbers read from a running game, device or service under the worker's conditions) must make the worker "
    "read back every controlled variable it set (setting, mode, build, scene, load) on every recorded row, write those values into the row, "
    "and mark a row whose readback differs from the intended value invalid, not keep it.",
    "On a DSH full-access request never pick Allow for this session (approve_session) unless the user said so, even when more will follow: it grants full shell access, deletes included, until the session ends. Pick Allow once (approve).",
    "delivered=true means sent, not accepted. Native records are authoritative; intermediate text is not completion—wait for done.",
)

SERVER_INSTRUCTIONS = (
    "Manage interactive subagents in the Hub console. Call list_agent_profiles for enabled "
    "profiles, models, thinking levels and routing guidance. spawn_subagent starts work, "
    "send_to_subagent steers it, resume_subagent restores an ended session. "
    "Never blindly resend a failed/unconfirmed Codex submission: inspect it, then clear an idle "
    "nonempty draft with send_to_subagent(text='', clear_input=true) before sending new text. "
    "Keep sent text short: put a follow-up longer than a few lines in a file under the session cwd "
    "and send a one-line pointer; long text is typed into the terminal slowly and can fail to land. "
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
