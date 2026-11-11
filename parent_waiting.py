"""Small helpers shared by console telemetry and the durable wait inbox."""

def session_targets(session_id):
    """Return explicit IDs, or None for the legacy all-sessions scope."""
    if session_id == "all":
        return None
    ids = list(dict.fromkeys(part.strip() for part in session_id.split(",") if part.strip()))
    if not ids or "all" in ids:
        raise ValueError("Use explicit comma-separated session IDs, or 'all' alone.")
    return ids
