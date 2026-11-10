"""Transport-independent continuation contract for parent agents."""

from agent_guidance import CONTINUATION_HINT


def session_targets(session_id):
    """Return explicit IDs, or None for the legacy all-sessions scope."""
    if session_id == "all":
        return None
    ids = list(dict.fromkeys(part.strip() for part in session_id.split(",") if part.strip()))
    if not ids or "all" in ids:
        raise ValueError("Use explicit comma-separated session IDs, or 'all' alone.")
    return ids


def build_wait_contract(sessions, pending_interactions, next_seq):
    """Classify observed state without mistaking an old answer for completion.

    The caller supplies compact snapshots, including unknown IDs. Terminal sessions are
    excluded from the next wait so a completed member cannot repeatedly wake a group.
    """
    pending_ids = {item["session_id"] for item in pending_interactions}
    unfinished, completed, inspect = [], [], []
    for sid, state in sessions.items():
        status = state.get("status")
        if sid in pending_ids or status in ("starting", "running", "waiting_parent"):
            unfinished.append(sid)
        elif status == "completed" or (status == "exited" and state.get("turn_completed")):
            completed.append(sid)
        else:
            inspect.append(sid)
    action = (
        "reply" if pending_interactions else
        "inspect" if inspect else
        "collect_results" if completed else
        "wait" if unfinished else "done"
    )
    return {
        "next_action": action,
        "wait_required": bool(unfinished),
        "unfinished_session_ids": unfinished,
        "completed_session_ids": completed,
        "inspect_session_ids": inspect,
        "session_states": sessions,
        "next_wait_arguments": {
            "session_id": ",".join(unfinished),
            "after_seq": next_seq,
            "timeout_seconds": 60.0,
        } if unfinished else None,
        "continuation_hint": CONTINUATION_HINT,
    }
