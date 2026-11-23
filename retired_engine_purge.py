"""One-off purge of a retired engine's data at hub start (decision `dsh-purge-at-start`).

DSH left the hub on 2026-11-15 (`dsh-removed`); the owner then decided on page
`hub-dsh-removal-1115` that its old data goes too (`dsh-data-purged`): the DSH sessions' rows in
the hub database and a saved `dsh` profile, each after a backup. `HubSessionManager.start` calls
`purge_retired_engine_data` once, in a worker thread, before restart-resume reads a row and before
the console is served.

Rules that shape this module:

- The database and the profiles file are independent halves; one failing never stops the other.
- Nothing is deleted unless its backup opened and holds the same retired-engine sessions.
- No `VACUUM`: on a large database it would stall the start and needs twice the space, so the
  database file keeps its size.
- A start with nothing to purge writes nothing (no backup, no database write, no file write).
- Session rows use the `engine` column, then the launch record's engine, then `profile_id` when
  both are empty; orphan child rows use the legacy `dsh-` id prefix because no session row remains.
- It imports neither `session_manager` nor `server` (they import it); the caller hands in the
  profile writer so the file is rewritten exactly the way the hub writes it.
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("mcp-agent-hub.retired_engine_purge")

# Engines that no longer exist in the hub. One tuple, so a later removal is one entry.
RETIRED_ENGINES = ("dsh",)
# Every table in hub_database.py that holds per-session rows, by its `session_id` column. A
# session's rows in all of them go in one transaction; rows of other sessions that merely mention
# a retired id (a parent id, a mailbox entry) are not touched.
SESSION_TABLES = ("sessions", "tool_calls", "intermediate_messages", "worker_events")
# The writer holds the database only for one commit batch, so an immediate transaction waits at
# most this long for it.
BUSY_TIMEOUT_SEC = 5.0


def _profile_stamp(stamp: Optional[str] = None) -> str:
    return stamp or time.strftime("%Y%m%d-%H%M%S")


def _retired_session_ids(conn: sqlite3.Connection) -> List[str]:
    """Session ids of the retired engines, by the rule the hub uses to decide a row's engine.

    Use the `engine` column, then the launch record's `engine`, then `profile_id` for old rows with
    neither value. Values are stripped and lower-cased. The old launch path forms ids as
    ``<profile_id>-<uuid>``; orphan child rows therefore use the legacy ``dsh-`` prefix.
    """
    ids: List[str] = []
    for row in conn.execute("SELECT session_id, engine, profile_snapshot, profile_id FROM sessions").fetchall():
        session_id, engine, snapshot = row[0], row[1], row[2]
        eng = str(engine or "").strip().lower()
        if not eng:
            try:
                launch = json.loads(snapshot) if snapshot else None
            except (TypeError, ValueError):
                launch = None
            eng = str((launch or {}).get("engine") or "").strip().lower() if isinstance(launch, dict) else ""
        if not eng:
            eng = str(row[3] or "").strip().lower()
        if eng in RETIRED_ENGINES and session_id is not None:
            ids.append(str(session_id))
    return ids


def _retired_rows_by_table(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    session_ids = set(_retired_session_ids(conn))
    result: Dict[str, Set[str]] = {"sessions": session_ids}
    for table in SESSION_TABLES[1:]:
        # A range, not `LIKE 'dsh-%'`: LIKE ignores case, so SQLite cannot use the session_id
        # index for it and every start would scan the whole table. Ids are `<profile_id>-<uuid>`
        # with the lower-case `dsh` profile, and '.' is the character after '-'.
        orphaned = {
            str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT c.session_id FROM {table} c "
                "LEFT JOIN sessions s ON s.session_id = c.session_id "
                "WHERE c.session_id >= 'dsh-' AND c.session_id < 'dsh.' AND s.session_id IS NULL"
            ).fetchall() if row[0] is not None
        }
        result[table] = session_ids | orphaned
    return result


def _copy_database(db_path: Path, backup_path: Path, expected_rows: Dict[str, Set[str]]) -> None:
    """SQLite's online backup, then prove the copy opens and holds the same retired sessions.

    Raises on any failure; the caller then deletes nothing. A copy that failed its check is
    removed, so an earlier backup is never replaced by a broken one.
    """
    temporary_path = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        source = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_SEC)
        try:
            destination = sqlite3.connect(str(temporary_path))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        check = sqlite3.connect(str(temporary_path))
        try:
            copied_rows = _retired_rows_by_table(check)
        finally:
            check.close()
        for table, expected in expected_rows.items():
            if copied_rows.get(table, set()) != expected:
                raise RuntimeError(f"the backup {table} id set does not match the source")
        if backup_path.exists():
            raise FileExistsError(f"{backup_path} already exists; refusing to replace a backup")
        os.rename(temporary_path, backup_path)
    except BaseException:
        # A copy that failed its check must not be mistaken for a good backup on the next start.
        # Only this call's temporary copy and its sidecars go; an existing backup and its files
        # are never touched.
        for candidate in (temporary_path, Path(str(temporary_path) + "-wal"),
                          Path(str(temporary_path) + "-shm")):
            try:
                candidate.unlink()
            except OSError:
                pass
        raise


def purge_retired_engine_rows(db_path: Path, *, stamp: Optional[str] = None) -> Dict[str, Any]:
    """Delete the retired engines' rows after a verified backup; never raises.

    Returns `{"sessions", "rows", "backup"}`: what went and where the copy is (`None` when there
    was nothing to purge or the backup failed).
    """
    result: Dict[str, Any] = {"sessions": 0, "rows": 0, "backup": None}
    db_path = Path(db_path)
    try:
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_SEC)
    except sqlite3.Error as exc:
        logger.warning("Retired-engine purge: the database %s did not open (%s); nothing deleted",
                       db_path, exc)
        return result
    try:
        try:
            rows_to_delete = _retired_rows_by_table(conn)
            ids = sorted(rows_to_delete["sessions"])
        except sqlite3.Error as exc:
            logger.warning("Retired-engine purge: the database %s did not answer (%s); nothing deleted",
                           db_path, exc)
            return result
        if not any(rows_to_delete.values()):
            return result
        backup_path = db_path.parent / f"hub_state.pre-dsh-purge-{_profile_stamp(stamp)}.db"
        try:
            logger.info("Retired-engine purge: database size before backup is %d bytes", db_path.stat().st_size)
            _copy_database(db_path, backup_path, rows_to_delete)
        except Exception as exc:
            logger.warning(
                "Retired-engine purge: backup of %s to %s failed (%s); nothing deleted",
                db_path, backup_path, exc,
            )
            return result
        counts: Dict[str, int] = {}
        try:
            with conn:  # one transaction over every table: all of it or none of it
                for table in SESSION_TABLES:
                    table_ids = sorted(rows_to_delete[table])
                    if not table_ids:
                        counts[table] = 0
                        continue
                    placeholders = ", ".join("?" for _ in table_ids)
                    cursor = conn.execute(f"DELETE FROM {table} WHERE session_id IN ({placeholders})", table_ids)
                    counts[table] = max(int(cursor.rowcount or 0), 0)
        except sqlite3.Error as exc:
            logger.warning(
                "Retired-engine purge: deleting the rows failed (%s); nothing deleted, the backup is at %s",
                exc, backup_path,
            )
            result["backup"] = str(backup_path)
            return result
        result.update(sessions=len(ids), rows=sum(counts.values()), backup=str(backup_path))
        logger.info(
            "Retired-engine purge: deleted %d %s session(s) and %d row(s) (%s); backup %s; "
            "no VACUUM, so the database file keeps its size",
            len(ids), "/".join(RETIRED_ENGINES), sum(counts.values()),
            ", ".join(f"{table} {count}" for table, count in counts.items()), backup_path,
        )
        return result
    finally:
        conn.close()


def _is_retired_profile(profile_id: Any, entry: Any) -> bool:
    if isinstance(entry, dict):
        engine = str(entry.get("engine") or "").strip().lower()
        if engine:
            return engine in RETIRED_ENGINES
    return str(profile_id or "").strip().lower() in RETIRED_ENGINES


def purge_retired_engine_profiles(
    profiles_path: Path,
    write_profiles: Callable[[Dict[str, Any]], None],
    *,
    stamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove the retired engines' saved profiles after a byte-for-byte backup; never raises.

    `write_profiles` is the hub's own writer (`HubSessionManager._write_profiles_file`), so the
    file keeps its format and every other key, value and order. Returns `{"removed", "backup"}`;
    `removed` is 0 when nothing changed.
    """
    result: Dict[str, Any] = {"removed": 0, "backup": None}
    path = Path(profiles_path)
    if not path.exists():
        logger.info("Retired-engine purge: no profiles file at %s; nothing to remove", path)
        return result
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Retired-engine purge: the profiles file %s did not open (%s); nothing changed",
                       path, exc)
        return result
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("Retired-engine purge: the profiles file %s did not parse (%s); nothing changed",
                       path, exc)
        return result
    if not isinstance(document, dict):
        logger.warning("Retired-engine purge: the profiles file %s is not a JSON object; nothing changed",
                       path)
        return result
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        return result
    retired = [pid for pid, entry in profiles.items() if _is_retired_profile(pid, entry)]
    if not retired:
        return result
    backup_path = path.with_name(f"agent_profiles.pre-dsh-purge-{_profile_stamp(stamp)}.json")
    temporary_path = path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary_path.read_bytes() != raw:
            raise IOError("the backup read-back did not match the source")
        if backup_path.exists():
            raise FileExistsError(f"{backup_path} already exists; refusing to replace a backup")
        os.rename(temporary_path, backup_path)
    except OSError as exc:
        if temporary_path.is_file():
            try:
                temporary_path.unlink()
            except OSError:
                pass
        logger.warning(
            "Retired-engine purge: backup of %s to %s failed (%s); nothing changed",
            path, backup_path, exc,
        )
        return result
    for pid in retired:
        profiles.pop(pid, None)
    try:
        write_profiles(document)
    except Exception as exc:
        logger.warning(
            "Retired-engine purge: writing %s failed (%s); the file still holds the retired profile(s), "
            "the backup is at %s", path, exc, backup_path,
        )
        return result
    result.update(removed=len(retired), backup=str(backup_path))
    logger.info("Retired-engine purge: removed %d %s profile(s) from %s; backup %s",
                len(retired), "/".join(RETIRED_ENGINES), path, backup_path)
    return result


def purge_retired_engine_data(
    db_path: Path,
    profiles_path: Path,
    write_profiles: Callable[[Dict[str, Any]], None],
    *,
    stamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Both halves, each inside its own guard: the hub's one call at start (never raises)."""
    database: Dict[str, Any] = {"sessions": 0, "rows": 0, "backup": None}
    try:
        database = purge_retired_engine_rows(db_path, stamp=stamp)
    except Exception as exc:  # a surprise in one half costs that half only
        logger.warning("Retired-engine purge: the database half failed (%s); nothing deleted there", exc)
    profiles: Dict[str, Any] = {"removed": 0, "backup": None}
    try:
        profiles = purge_retired_engine_profiles(profiles_path, write_profiles, stamp=stamp)
    except Exception as exc:
        logger.warning("Retired-engine purge: the profiles half failed (%s); the file is unchanged", exc)
    return {"database": database, "profiles": profiles}
