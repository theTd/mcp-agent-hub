"""Ordered, bounded background persistence, independent of HTTP and agent engines.

Only serialized value snapshots cross the writer boundary. Acceptance is deliberately
different from durability; callers that need durability await a ticket barrier.
"""
import asyncio
import contextvars
import functools
import logging
import pickle
import sqlite3
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from hub_database import HubDatabase

logger = logging.getLogger(__name__)


class PersistenceUnavailable(RuntimeError):
    """A bounded admission/barrier failed, or storage needs operator attention."""


@dataclass
class _Unit:
    owner: object
    operations: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    committed: list = field(default_factory=list)
    event_seq: int = 0
    ticket: int = 0
    size: int = 0
    created: float = field(default_factory=time.monotonic)


_current_unit = contextvars.ContextVar("hub_persistence_unit", default=None)
_internal_producer = contextvars.ContextVar("hub_internal_persistence_producer", default=False)


def logical_write(function):
    """Reserve before changing live telemetry, and enqueue its related rows atomically."""
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        if self.db.in_unit:
            return await function(self, *args, **kwargs)
        candidate = args[0] if args else kwargs.get("session", kwargs.get("session_id"))
        session = self.sessions.get(candidate) if isinstance(candidate, str) else candidate
        snapshot = session.to_dict() if session is not None else {}
        # Reserve before mutation, including archive JSON that can expand control
        # characters and duplicate messages. The fixed allowance covers raw scrollback.
        values = tuple(value.to_dict() if hasattr(value, "to_dict") else value for value in args)
        named_values = {key: value.to_dict() if hasattr(value, "to_dict") else value
                        for key, value in kwargs.items()}
        metadata = ()
        if session is not None:
            metadata = (session.profile_snapshot, session.conversation_ref, session.last_screen)
            if function.__name__ in {"_archive_session", "deliver_parent_message", "respond_to_interaction"} or kwargs.get("final"):
                metadata += (self.parent_messages.get(session.session_id, []),
                             self._worker_message_logs.get(session.session_id, []))
        estimate = 1024 * 1024 + 8 * len(pickle.dumps((values, named_values, snapshot, metadata)))
        async with self.db.unit(estimate):
            return await function(self, *args, **kwargs)
    return wrapped


class HubPersistence:
    READS = frozenset({
        "get_max_seq", "get_session_record", "get_recent_tool_calls",
        "get_recent_intermediate_messages", "get_recent_worker_events", "list_persisted_sessions",
        "list_archived", "count_archived", "get_scrollback_blob", "list_session_ids",
    })
    WRITES = frozenset({
        "upsert_session", "add_tool_call", "complete_latest_tool_call",
        "add_intermediate_message", "insert_worker_event", "delete_session_record",
        "update_archive_fields", "update_archive_fields_bulk", "reset_session_after_resume",
    })

    def __init__(self, db_path, *, max_units=4096, max_bytes=32 * 1024 * 1024,
                 admission_timeout=2.0, batch_size=128, batch_delay=0.025):
        self.db_path = db_path
        self.max_units = max_units
        self.max_bytes = max_bytes
        self.admission_timeout = admission_timeout
        self.batch_size = batch_size
        self.batch_delay = batch_delay
        self._condition = asyncio.Condition()
        self._queue = deque()
        self._active = []
        self._units = 0
        self._bytes = 0
        self._accepted_ticket = 0
        self._committed_ticket = 0
        self._next_seq = 0
        self._committed_seq = 0
        self._fatal = None
        self._last_error = None
        self._closing = False
        self._accepting = True
        self._closed = False
        self._task = None
        self._backend = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-database")

    async def start(self):
        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        def initialize():
            backend = HubDatabase(self.db_path)
            try:
                backend.open_writer()
                return backend, backend.get_max_seq()
            except BaseException:
                backend.close_writer()
                raise
        try:
            self._backend, self._next_seq = await loop.run_in_executor(self._executor, initialize)
        except BaseException:
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise
        self._committed_seq = self._next_seq
        self._task = asyncio.create_task(self._run(), name="hub-persistence-writer")

    @property
    def in_unit(self):
        unit = _current_unit.get()
        return unit is not None and unit.owner == (self, asyncio.current_task())

    @property
    def accepted_ticket(self):
        return self._accepted_ticket

    async def run_internal(self, coroutine):
        token = _internal_producer.set(True)
        try:
            return await coroutine
        finally:
            _internal_producer.reset(token)

    def status(self):
        pending = self._active + list(self._queue)
        oldest = min((unit.created for unit in pending), default=time.monotonic())
        return {
            "state": ("failed" if self._fatal else "closed" if self._closed else
                      "draining" if not self._accepting or self._closing else
                      "backlogged" if self._units else "healthy"),
            "pending_writes": self._units,
            "pending_bytes": self._bytes,
            "oldest_pending_age_ms": round((time.monotonic() - oldest) * 1000) if pending else 0,
            "last_committed_seq": self._committed_seq,
            "last_error": self._last_error,
        }

    def _check_open(self):
        if self._fatal:
            raise PersistenceUnavailable("数据库写入失败；待写数据已保留，需检查服务日志") from self._fatal
        if self._closing or self._closed or self._task is None:
            raise PersistenceUnavailable("数据库持久化服务尚未启动或正在关闭")
        if not self._accepting and not _internal_producer.get():
            raise PersistenceUnavailable("Hub 正在关闭，不再接收新写入")

    def stop_accepting(self):
        """Reject external producers while final internal snapshots are collected."""
        self._accepting = False

    async def _reserve(self, size):
        if size > self.max_bytes:
            raise PersistenceUnavailable("单次持久化数据超过队列容量")
        async def wait_capacity():
            async with self._condition:
                while True:
                    self._check_open()
                    if self._units < self.max_units and self._bytes + size <= self.max_bytes:
                        self._units += 1
                        self._bytes += size
                        return
                    await self._condition.wait()
        try:
            timeout = None if _internal_producer.get() else self.admission_timeout
            await asyncio.wait_for(wait_capacity(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise PersistenceUnavailable("数据库后台队列已满，请稍后重试") from exc

    @asynccontextmanager
    async def unit(self, reserve_bytes):
        if self.in_unit:
            yield _current_unit.get()
            return
        await self._reserve(reserve_bytes)
        unit = _Unit(owner=(self, asyncio.current_task()), size=reserve_bytes)
        token = _current_unit.set(unit)
        queued = False
        try:
            yield unit
            actual = sum(len(operation) for operation in unit.operations)
            if actual > reserve_bytes:
                raise PersistenceUnavailable("持久化快照超过预留容量，拒绝不完整写入")
            if unit.operations:
                async with self._condition:
                    self._check_open()
                    self._bytes += actual - unit.size
                    unit.size = actual
                    self._accepted_ticket += 1
                    unit.ticket = self._accepted_ticket
                    self._queue.append(unit)
                    queued = True
                    # No await between acceptance and event publication.
                    try:
                        for callback in unit.accepted:
                            callback()
                    finally:
                        self._condition.notify_all()
        finally:
            _current_unit.reset(token)
            unit.owner = None  # Do not retain a completed HTTP/watch task with queued snapshots.
            if not queued:
                async with self._condition:
                    self._units -= 1
                    self._bytes -= unit.size
                    self._condition.notify_all()

    def on_accept(self, callback):
        if not self.in_unit:
            raise RuntimeError("accept callback requires a logical write unit")
        _current_unit.get().accepted.append(callback)

    def on_commit(self, callback):
        if not self.in_unit:
            raise RuntimeError("commit callback requires a logical write unit")
        _current_unit.get().committed.append(callback)

    async def write(self, name, *args, **kwargs):
        if name not in self.WRITES:
            raise ValueError("Unknown persistence operation")
        if name in {"upsert_session", "add_tool_call", "add_intermediate_message"}:
            kwargs.setdefault("timestamp", time.time())
        operation = pickle.dumps((name, args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        if self.in_unit:
            _current_unit.get().operations.append(operation)
        else:
            async with self.unit(len(operation)) as unit:
                unit.operations.append(operation)

    async def insert_worker_event(self, record):
        if not self.in_unit:
            raise RuntimeError("mailbox events require a logical write unit")
        self._next_seq += 1
        record = dict(record, seq=self._next_seq)
        unit = _current_unit.get()
        unit.event_seq = record["seq"]
        await self.write("insert_worker_event", record)
        return record["seq"]

    async def delete_session_record(self, session_id):
        exists = await self.read("get_session_record", session_id)
        if exists is None:
            return False
        await self.write("delete_session_record", session_id)
        await self.flush()
        return True

    async def read(self, name, *args, consistent=True, **kwargs):
        if name not in self.READS:
            raise ValueError("Unknown persistence query")
        if self._backend is None or self._closed:
            raise PersistenceUnavailable("数据库持久化服务不可用")
        deadline = time.monotonic() + 2.0
        if consistent:
            if self.in_unit and _current_unit.get().operations:
                raise RuntimeError("A logical write must not read its own unsubmitted rows")
            await self.flush()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(getattr(self._backend, name), *args, **kwargs),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except (asyncio.TimeoutError, sqlite3.Error) as exc:
            raise PersistenceUnavailable("数据库查询暂时不可用，请稍后重试") from exc

    async def flush(self, ticket=None, timeout=2.0):
        target = self._accepted_ticket if ticket is None else ticket
        async def wait_committed():
            async with self._condition:
                while self._committed_ticket < target:
                    if self._fatal:
                        raise PersistenceUnavailable("数据库提交失败，尚未完成落盘") from self._fatal
                    if self._closed:
                        raise PersistenceUnavailable("数据库已关闭，仍有未提交数据")
                    await self._condition.wait()
        try:
            await asyncio.wait_for(wait_committed(), timeout=max(0.001, timeout))
        except asyncio.TimeoutError as exc:
            raise PersistenceUnavailable("等待数据库落盘超时，请稍后重试") from exc

    async def _run(self):
        try:
            while True:
                async with self._condition:
                    await self._condition.wait_for(lambda: self._queue or self._closing)
                    if not self._queue and self._closing:
                        return
                if not self._closing:
                    await asyncio.sleep(self.batch_delay)
                async with self._condition:
                    self._active = [self._queue.popleft() for _ in range(min(self.batch_size, len(self._queue)))]
                backoff = 0.05
                while True:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            self._executor, self._backend.apply_batch,
                            [tuple(unit.operations) for unit in self._active],
                        )
                        break
                    except sqlite3.OperationalError as exc:
                        if getattr(exc, "sqlite_errorcode", 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            raise
                        self._last_error = "数据库锁冲突，后台重试中"
                        await asyncio.sleep(backoff)
                        backoff = min(1.0, backoff * 2)
                async with self._condition:
                    for unit in self._active:
                        self._committed_ticket = unit.ticket
                        self._committed_seq = max(self._committed_seq, unit.event_seq)
                        self._units -= 1
                        self._bytes -= unit.size
                        for callback in unit.committed:
                            try:
                                callback()
                            except Exception:
                                # SQL has already committed; callback failure must never retry it.
                                logger.exception("Post-commit callback failed for ticket %d", unit.ticket)
                    self._active = []
                    self._last_error = None
                    self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fatal = exc
            self._last_error = f"数据库写入失败 ({type(exc).__name__})"
            logger.exception("Persistence stopped; %d logical writes retained", self._units)
            async with self._condition:
                self._condition.notify_all()

    async def close(self, timeout=6.0):
        if self._closed:
            return
        deadline = time.monotonic() + timeout
        self._closing = True
        async with self._condition:
            self._condition.notify_all()
        try:
            await self.flush(timeout=max(0.001, timeout - 0.2))
        finally:
            if self._units:
                logger.error("Persistence shutdown has %d uncommitted writes (%d bytes)", self._units, self._bytes)
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._closed = True
            if self._backend is not None:
                # Ordered behind any in-flight SQLite operation on the same writer thread.
                closing = asyncio.wrap_future(self._executor.submit(self._backend.close_writer))
                try:
                    await asyncio.wait_for(asyncio.shield(closing), timeout=max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    logger.error("Writer connection cleanup is still in progress")
            self._executor.shutdown(wait=False)


def _read_method(name):
    async def method(self, *args, **kwargs):
        return await self.read(name, *args, **kwargs)
    method.__name__ = name
    return method


def _write_method(name):
    async def method(self, *args, **kwargs):
        return await self.write(name, *args, **kwargs)
    method.__name__ = name
    return method


# Keep the storage vocabulary identical at both boundaries, without endpoint-specific wrappers.
for _name in HubPersistence.READS:
    setattr(HubPersistence, _name, _read_method(_name))
for _name in HubPersistence.WRITES - {"insert_worker_event", "delete_session_record"}:
    setattr(HubPersistence, _name, _write_method(_name))
