"""Doubao CLI automatic recovery: settings, retry budget, the hub-wide service gate, message text.

The Doubao CLI stops its turn when its shared service drops:

    ❯ auth_unavailable: 运行时与共享服务断开连接，任务已停止。请重试

and exits with code 3. `doubao_session_record.detect_doubao_service_link_loss` reads that row off
the screen (never `◆` prose and never a `▍` echo). The hub then resumes the same turn instead of
letting the parent see a failed worker:

- `RecoveryBudget` bounds the attempts: a few per turn plus a rolling cap per session, so a service
  that stays down cannot make one worker respawn forever;
- `DoubaoServiceGate` serializes the spawns of N sessions that crashed together and lets them wait
  for the shared service instead of each burning an attempt on their own;
- the message builders keep every parent-visible sentence in one place (the continuation prompt an
  interrupted turn is resumed with, and the failure text once the budget is spent).

Nothing here touches a session: the manager owns the session lifecycle and calls this module for the
policy, the pacing and the wording.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# The CLI's own failure code for "运行时与共享服务断开连接，任务已停止。请重试".
DOUBAO_LINK_LOSS_CODE = "auth_unavailable"
# A session the hub is bringing back after that link loss carries this status, which is neither a
# live process nor an end state: `_restart_live_snapshot` and the shutdown archive treat it as a cut
# off session, and `to_dict` keeps it instead of rewriting it to `exited`.
RECOVERING_STATUS = "recovering"

ENV_ENABLED = "HUB_DOUBAO_AUTO_RECOVERY"
ENV_MAX_ATTEMPTS = "HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS"
ENV_ROLLING_MAX = "HUB_DOUBAO_AUTO_RECOVERY_ROLLING_MAX"
ENV_ROLLING_WINDOW_SEC = "HUB_DOUBAO_AUTO_RECOVERY_ROLLING_WINDOW_SEC"
# Test/ops affordances: the pacing is overridable without editing code (a zero schedule runs the
# recovery without a real wait). Both keep their documented defaults when unset or unparsable.
ENV_BACKOFF_SEC = "HUB_DOUBAO_AUTO_RECOVERY_BACKOFF_SEC"
ENV_SPAWN_GAP_SEC = "HUB_DOUBAO_AUTO_RECOVERY_SPAWN_GAP_SEC"
ENV_SERVICE_READY_WAIT_SEC = "HUB_DOUBAO_AUTO_RECOVERY_SERVICE_READY_WAIT_SEC"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_ROLLING_MAX = 6
DEFAULT_ROLLING_WINDOW_SEC = 1800.0
DEFAULT_BACKOFF_SEC: Tuple[float, ...] = (1.5, 5.0, 15.0)
DEFAULT_JITTER_RATIO = 0.2
DEFAULT_SPAWN_GAP_SEC = 2.0
DEFAULT_SERVICE_READY_WAIT_SEC = 12.0

_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disabled"})

# The text an interrupted submitted turn is resumed with. It never asks for a redo: the turn already
# delivered part of its work, and the recovered process reads the same conversation.
RECOVERY_CONTINUATION_PROMPT = (
    "上一次运行时与共享服务断开连接，任务中途中断。"
    "请从断开处继续完成原任务；已完成的部分不要重做，完成后按原要求汇报。"
)


def _env_value(environ: Optional[Mapping[str, str]], name: str) -> str:
    """`os.environ[name]` stripped, so both a test mapping and the real environment read alike."""
    source = os.environ if environ is None else environ
    try:
        return str(source.get(name) or "").strip()
    except Exception:
        return ""


def _env_float(environ: Optional[Mapping[str, str]], name: str, default: float) -> float:
    raw = _env_value(environ, name)
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < 0.0:
        return float(default)
    return value


def _env_int(environ: Optional[Mapping[str, str]], name: str, default: int,
             *, minimum: int = 1) -> int:
    raw = _env_value(environ, name)
    if not raw:
        return int(default)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    return int(default) if value < minimum else value


def doubao_auto_recovery_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """`HUB_DOUBAO_AUTO_RECOVERY`: on unless the value is one of 0/false/off/no/disabled."""
    raw = _env_value(environ, ENV_ENABLED)
    if not raw:
        return True
    return raw.lower() not in _DISABLED_VALUES


def doubao_auto_recovery_max_attempts(environ: Optional[Mapping[str, str]] = None) -> int:
    """`HUB_DOUBAO_AUTO_RECOVERY_MAX_ATTEMPTS`: recovery attempts per turn (default 3)."""
    return _env_int(environ, ENV_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)


def doubao_auto_recovery_rolling_max(environ: Optional[Mapping[str, str]] = None) -> int:
    """`HUB_DOUBAO_AUTO_RECOVERY_ROLLING_MAX`: attempts per rolling window (default 6)."""
    return _env_int(environ, ENV_ROLLING_MAX, DEFAULT_ROLLING_MAX)


def doubao_auto_recovery_rolling_window_sec(
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """`HUB_DOUBAO_AUTO_RECOVERY_ROLLING_WINDOW_SEC`: the rolling window (default 1800 s)."""
    value = _env_float(environ, ENV_ROLLING_WINDOW_SEC, DEFAULT_ROLLING_WINDOW_SEC)
    return value if value > 0.0 else DEFAULT_ROLLING_WINDOW_SEC


def doubao_auto_recovery_jitter_ratio(environ: Optional[Mapping[str, str]] = None) -> float:
    """`HUB_DOUBAO_AUTO_RECOVERY_JITTER`: the ± share of the backoff (default 0.2)."""
    value = _env_float(environ, "HUB_DOUBAO_AUTO_RECOVERY_JITTER", DEFAULT_JITTER_RATIO)
    return value if value <= 1.0 else DEFAULT_JITTER_RATIO


def doubao_auto_recovery_backoff(environ: Optional[Mapping[str, str]] = None) -> Tuple[float, ...]:
    """The backoff schedule (`1.5, 5, 15` seconds by default), optionally CSV-overridden."""
    raw = _env_value(environ, ENV_BACKOFF_SEC)
    if not raw:
        return DEFAULT_BACKOFF_SEC
    values: List[float] = []
    for part in raw.replace(";", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            return DEFAULT_BACKOFF_SEC
        if value < 0.0 or not math.isfinite(value):
            return DEFAULT_BACKOFF_SEC
        values.append(value)
    return tuple(values) if values else DEFAULT_BACKOFF_SEC


def doubao_auto_recovery_spawn_gap_sec(environ: Optional[Mapping[str, str]] = None) -> float:
    """The minimum gap between two Doubao spawns the gate allows (default 2.0 s)."""
    return _env_float(environ, ENV_SPAWN_GAP_SEC, DEFAULT_SPAWN_GAP_SEC)


def doubao_auto_recovery_service_ready_wait_sec(
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """How long a waiting recovery watches for the shared service (default 12.0 s)."""
    return _env_float(environ, ENV_SERVICE_READY_WAIT_SEC, DEFAULT_SERVICE_READY_WAIT_SEC)


# ---------------------------------------------------------------------------------------------
# Retry budget
# ---------------------------------------------------------------------------------------------


@dataclass
class RecoveryBudget:
    """How many recovery attempts one session may still spend.

    Two caps: `max_attempts` per turn (a crashed turn may not respawn a worker forever) and
    `rolling_max` inside `rolling_window_sec` across turns (a service that keeps dropping must
    stop costing restarts). `attempts` is the per-turn counter; the rolling timestamps outlive a
    successful turn on purpose -- `note_turn_end(completion_reported=True)` resets only the
    per-turn counter.

    `clock`, `rng` and `sleep` are injectable, so a unit test runs the window and the jitter
    deterministically instead of sleeping seconds.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    rolling_max: int = DEFAULT_ROLLING_MAX
    rolling_window_sec: float = DEFAULT_ROLLING_WINDOW_SEC
    delays: Tuple[float, ...] = DEFAULT_BACKOFF_SEC
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    attempts: int = 0
    rolling: List[float] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    rng: Callable[[], float] = random.random
    sleep: Optional[Callable[[float], Any]] = None

    # ---- construction -----------------------------------------------------------------------

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, **overrides: Any) -> "RecoveryBudget":
        """The budget the environment asks for; `overrides` win (a test's clock/rng/delays)."""
        built: Dict[str, Any] = {
            "max_attempts": doubao_auto_recovery_max_attempts(environ),
            "rolling_max": doubao_auto_recovery_rolling_max(environ),
            "rolling_window_sec": doubao_auto_recovery_rolling_window_sec(environ),
            "delays": doubao_auto_recovery_backoff(environ),
            "jitter_ratio": doubao_auto_recovery_jitter_ratio(environ),
        }
        built.update(overrides)
        return cls(**built)

    # ---- windows ----------------------------------------------------------------------------

    def prune(self, now: Optional[float] = None) -> int:
        """Drops rolling timestamps that left the window; returns how many went."""
        current = float(self.clock() if now is None else now)
        window = max(0.0, float(self.rolling_window_sec))
        keep_from = current - window
        if not self.rolling:
            return 0
        kept = [stamp for stamp in self.rolling if float(stamp) > keep_from]
        dropped = len(self.rolling) - len(kept)
        self.rolling = kept
        return dropped

    def rolling_attempts(self, now: Optional[float] = None) -> int:
        """Attempts recorded inside the rolling window right now."""
        self.prune(now)
        return len(self.rolling)

    def exhausted_reason(self, now: Optional[float] = None) -> str:
        """"" while an attempt is allowed, else the Chinese reason the failure text names."""
        if int(self.attempts) >= int(self.max_attempts):
            return f"本轮已自动恢复 {int(self.attempts)} 次，达到单轮上限 {int(self.max_attempts)} 次"
        if self.rolling_attempts(now) >= int(self.rolling_max):
            minutes = float(self.rolling_window_sec) / 60.0
            return (f"{minutes:g} 分钟内已自动恢复 {self.rolling_attempts(now)} 次，"
                    f"达到滚动上限 {int(self.rolling_max)} 次")
        return ""

    def can_attempt(self, now: Optional[float] = None) -> bool:
        return not self.exhausted_reason(now)

    def begin_attempt(self, now: Optional[float] = None) -> int:
        """Consumes one attempt and returns its 1-based number (raises when none is left)."""
        reason = self.exhausted_reason(now)
        if reason:
            raise RuntimeError(reason)
        current = float(self.clock() if now is None else now)
        self.attempts = int(self.attempts) + 1
        self.rolling.append(current)
        self.prune(current)
        return int(self.attempts)

    # ---- pacing -----------------------------------------------------------------------------

    def delay_for(self, attempt: int) -> float:
        """The backoff before attempt N (the last delay repeats past the schedule), ± jitter."""
        index = max(0, int(attempt) - 1)
        if not self.delays:
            return 0.0
        base = float(self.delays[min(index, len(self.delays) - 1)])
        if base <= 0.0:
            return 0.0
        try:
            sample = float(self.rng())
        except Exception:
            sample = 0.5
        factor = 1.0 + float(self.jitter_ratio) * (2.0 * sample - 1.0)
        return max(0.0, base * factor)

    def next_delay(self, now: Optional[float] = None) -> float:
        """The backoff the attempt after this one would wait."""
        return self.delay_for(int(self.attempts) + 1)

    # ---- turn boundaries --------------------------------------------------------------------

    def note_turn_end(self, completion_reported: bool) -> None:
        """A turn that really finished clears the per-turn cap; the rolling window is kept."""
        if completion_reported:
            self.attempts = 0
        self.prune()

    def snapshot(self) -> Dict[str, Any]:
        return {"attempts": int(self.attempts), "rolling": [float(v) for v in self.rolling]}

    def restore(self, state: Optional[Mapping[str, Any]]) -> None:
        """Copies a snapshot onto this budget (a recovery hands its counters to the new object)."""
        if not isinstance(state, Mapping):
            return
        try:
            self.attempts = int(state.get("attempts") or 0)
        except (TypeError, ValueError):
            self.attempts = 0
        values = state.get("rolling")
        self.rolling = []
        for stamp in values if isinstance(values, (list, tuple)) else ():
            try:
                self.rolling.append(float(stamp))
            except (TypeError, ValueError):
                continue

    def remaining_turn_attempts(self) -> int:
        return max(0, int(self.max_attempts) - int(self.attempts))


async def sleep_with_abort(
    delay: float,
    *,
    sleep: Optional[Callable[[float], Any]] = None,
    abort: Optional[Callable[[], bool]] = None,
    poll_sec: float = 0.2,
) -> bool:
    """Sleep `delay`, in steps, and return False as soon as `abort()` asks to stop.

    `sleep` is injectable (a test's immediate sleep); `abort` is checked between steps and once
    before the first one, so a terminate or a shutdown never waits out a 15 s backoff.
    """
    remaining = max(0.0, float(delay))
    if remaining <= 0.0:
        return True
    sleeper = sleep or asyncio.sleep
    step = max(0.01, float(poll_sec))
    while remaining > 0.0:
        if abort is not None and abort():
            return False
        chunk = min(step, remaining)
        result = sleeper(chunk)
        if hasattr(result, "__await__"):
            await result
        remaining -= chunk
    return not (abort is not None and abort())


# ---------------------------------------------------------------------------------------------
# Hub-wide service gate
# ---------------------------------------------------------------------------------------------


class DoubaoServiceGate:
    """One hub-wide gate for the spawned recoveries, so N crashes never stampede the service.

    The sequence a recovery follows is: take `lock` (spawns are serial; the lock is held only for
    the spawn call), respect `min_spawn_gap_sec` since the previous Doubao spawn, and -- when
    another recovery just started spawning -- wait, bounded, for `service_ready` instead of
    starting a second CLI into a shared service that is still coming up. `service_ready` is cleared
    right before the lead spawn and set as soon as the recovered screen shows the idle composer
    (or the lead's attempt fails), and a waiter that runs out of time proceeds anyway: a lost
    attempt is worse than a spawn that may overlap by a moment.

    `clock` and `sleep` are injectable so a test's gap and bounded wait are deterministic.
    """

    def __init__(
        self,
        *,
        min_spawn_gap_sec: float = DEFAULT_SPAWN_GAP_SEC,
        service_ready_wait_sec: float = DEFAULT_SERVICE_READY_WAIT_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.min_spawn_gap_sec = max(0.0, float(min_spawn_gap_sec))
        self.service_ready_wait_sec = max(0.0, float(service_ready_wait_sec))
        self._clock = clock
        self._sleep = sleep
        self._lock: Optional[asyncio.Lock] = None
        self._ready: Optional[asyncio.Event] = None
        self._last_spawn_at: Optional[float] = None
        self._lead_in_flight: bool = False
        # The token of the spawn that is the current lead (None when no lead is in flight). Only a
        # caller holding that token may release the lead slot, so a sibling whose attempt failed
        # before it ever became lead (or was cancelled) can never clear another session's lead.
        self._lead_token: Optional[int] = None
        self._lead_sequence: int = 0
        self.spawn_calls: int = 0

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, **overrides: Any) -> "DoubaoServiceGate":
        built: Dict[str, Any] = {
            "min_spawn_gap_sec": doubao_auto_recovery_spawn_gap_sec(environ),
            "service_ready_wait_sec": doubao_auto_recovery_service_ready_wait_sec(environ),
        }
        built.update(overrides)
        return cls(**built)

    # ---- loop-bound primitives, created on first use -----------------------------------------

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def service_ready(self) -> asyncio.Event:
        if self._ready is None:
            self._ready = asyncio.Event()
        return self._ready

    @property
    def lead_in_flight(self) -> bool:
        return bool(self._lead_in_flight)

    @property
    def lead_token(self) -> Optional[int]:
        """The token of the spawn that is the lead right now (None when none is in flight)."""
        return self._lead_token

    # ---- observations ------------------------------------------------------------------------

    def note_spawn_started(self) -> int:
        """The lead spawn is starting now: the service is not proven ready any more.

        Returns this lead's token: the caller passes it to `note_attempt_settled` (or
        `note_service_ready`) so only the spawn that really owns the lead can release it.
        """
        self._lead_sequence += 1
        token = self._lead_sequence
        self._lead_token = token
        self._lead_in_flight = True
        self._last_spawn_at = float(self._clock())
        self.spawn_calls += 1
        self.service_ready.clear()
        return token

    def note_service_ready(self, lead_token: Optional[int] = None) -> None:
        """The recovered Doubao screen drew its idle composer: the shared service answers.

        Any live Doubao that reaches its composer proves the service, so this is deliberately not
        gated on the current lead's token; it still releases the lead it was watching.
        """
        self._lead_in_flight = False
        self._lead_token = None
        self.service_ready.set()

    def note_spawn_failed(self, lead_token: Optional[int] = None) -> None:
        """The lead's spawn raised: waiting on it would only cost the waiters their own delay.

        Only the holder of the spawn slot calls this, so `None` releases the lead in flight (the
        older caller had no token); an explicit token must match that lead.
        """
        if lead_token is not None and not self._owns_lead(lead_token):
            return
        self._lead_in_flight = False
        self._lead_token = None
        self.service_ready.set()

    def note_attempt_settled(self, lead_token: Optional[int] = None) -> None:
        """The lead's attempt ended without proving readiness: release the waiters.

        Only the caller that really became the lead (and still holds that lead) may release it:
        a sibling whose attempt failed before acquiring the gate, or that was cancelled there,
        passes `None` or a stale token, and that is a no-op -- the active lead keeps the waiters
        held until it settles or its session draws the composer.
        """
        if not self._owns_lead(lead_token):
            return
        self._lead_in_flight = False
        self._lead_token = None
        self.service_ready.set()

    def _owns_lead(self, lead_token: Optional[int]) -> bool:
        """True only for the token of the lead that is in flight right now."""
        if lead_token is None or self._lead_token is None:
            return False
        try:
            return int(lead_token) == int(self._lead_token)
        except (TypeError, ValueError):
            return False

    # ---- waiting ----------------------------------------------------------------------------

    def gap_remaining(self) -> float:
        """Seconds still owed before another spawn may start (0 when the gap is satisfied)."""
        if self._last_spawn_at is None or self.min_spawn_gap_sec <= 0.0:
            return 0.0
        return max(0.0, self.min_spawn_gap_sec - (float(self._clock()) - self._last_spawn_at))

    async def wait_gap(self) -> float:
        remaining = self.gap_remaining()
        if remaining > 0.0:
            sleeper = self._sleep or asyncio.sleep
            result = sleeper(remaining)
            if hasattr(result, "__await__"):
                await result
        return remaining

    async def wait_service_ready(self, timeout: Optional[float] = None) -> bool:
        """True when no spawn is in flight (or the service proved ready in time)."""
        if not self._lead_in_flight:
            return True
        limit = self.service_ready_wait_sec if timeout is None else max(0.0, float(timeout))
        try:
            await asyncio.wait_for(self.service_ready.wait(), limit)
            return True
        except asyncio.TimeoutError:
            return False

    async def acquire_spawn_slot(self, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Take the spawn turn: serializes, honours the gap and waits for the service.

        The reply carries `lead_token`: the caller must hand it to
        `note_attempt_settled(lead_token)` if its attempt ends without proving the service ready,
        and calls `note_service_ready()` once its session draws the composer. It releases `lock`
        with `release_spawn_slot` (`spawn_window` wraps both in one `async with`).
        """
        await self.lock.acquire()
        try:
            waited_gap = await self.wait_gap()
            waited_ready = await self.wait_service_ready(timeout)
            lead_token = self.note_spawn_started()
            return {"gate_gap_sec": waited_gap, "service_ready": waited_ready,
                    "lead_token": lead_token}
        except BaseException:
            self.lock.release()
            raise

    def release_spawn_slot(self) -> None:
        if self.lock.locked():
            self.lock.release()

    def abort_spawn_slot(self, lead_token: Optional[int] = None) -> None:
        """A slot whose spawn raised: mark it settled, then release it.

        `lead_token` is the one `acquire_spawn_slot` handed back, so the abort settles only the
        lead this slot really took (None: this slot's caller kept no token, so the lead in flight
        is its own).
        """
        self.note_spawn_failed(self._lead_token if lead_token is None else lead_token)
        self.release_spawn_slot()

    def finish_spawn_slot(self) -> None:
        """A slot whose spawn returned: the session itself reports readiness later."""
        self.release_spawn_slot()


# ---------------------------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------------------------


def continuation_prompt() -> str:
    """The short prompt a submitted, interrupted turn is resumed with."""
    return RECOVERY_CONTINUATION_PROMPT


def attempt_activity(attempt: int, max_attempts: int) -> str:
    """The session card's one-line activity while a recovery attempt runs."""
    return f"豆包共享服务断开，正在自动恢复（第 {int(attempt)}/{int(max_attempts)} 次）..."


def recovery_event_data(
    attempt: int,
    max_attempts: int,
    *,
    code: str = DOUBAO_LINK_LOSS_CODE,
    detail: str = "",
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """The `auto_recovery` lifecycle event's data (never a wake: see `wait_inbox`)."""
    data: Dict[str, Any] = {
        "attempt": int(attempt),
        "max_attempts": int(max_attempts),
        "code": str(code or DOUBAO_LINK_LOSS_CODE),
    }
    if detail:
        data["detail"] = str(detail)[:400]
    if exit_code is not None:
        data["exit_code"] = int(exit_code)
    return data


def _detail_text(detail: Any) -> str:
    text = str(detail or "").strip().replace("\r", " ")
    return text[:600] if text else "（无更多错误信息）"


def exhaustion_message(
    session_id: str,
    *,
    exit_code: Optional[int],
    attempts: int,
    max_attempts: int,
    error_detail: Any = "",
    resumable: bool = True,
    reason: str = "",
    code: str = DOUBAO_LINK_LOSS_CODE,
) -> str:
    """The `crashed` event's text once the retry budget is spent.

    It names the CLI's own code and exit code, how many attempts were really made, the last error
    the recovery saw, and the standard two-step hint (`get_subagent_final_response`, and
    `resume_subagent` when the session is still resumable).
    """
    exit_text = "None" if exit_code is None else str(int(exit_code))
    lines = [
        f"豆包 CLI 运行时与共享服务断开连接（{code}，exit_code={exit_text}），任务已停止，"
        f"自动恢复失败。",
        f"已尝试自动恢复 {int(attempts)} 次（单轮上限 {int(max_attempts)} 次）"
        + (f"；{reason}" if reason else "") + "，不再重试。",
        f"最后一次错误：{_detail_text(error_detail)}",
    ]
    hint = (f"Call get_subagent_final_response('{session_id}') for the persisted answer and trace.")
    if resumable:
        hint += f" Call resume_subagent('{session_id}') to resume this session manually."
    lines.append(hint)
    return "\n".join(lines)


def exhaustion_event_data(
    attempts: int,
    max_attempts: int,
    *,
    code: str = DOUBAO_LINK_LOSS_CODE,
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """The failed event's data: the parent's wait handle settles on these facts."""
    data: Dict[str, Any] = {
        "auto_recovery_attempts": int(attempts),
        "auto_recovery_exhausted": True,
        "code": str(code or DOUBAO_LINK_LOSS_CODE),
    }
    if exit_code is not None:
        data["exit_code"] = int(exit_code)
    return data


def link_loss_detail(info: Optional[Mapping[str, Any]]) -> str:
    """The one screen row the link loss was read from, for the failure text."""
    if not isinstance(info, Mapping):
        return ""
    return str(info.get("message") or "").strip()
