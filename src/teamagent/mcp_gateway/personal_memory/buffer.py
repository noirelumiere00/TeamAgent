"""本人メモの揮発状態（発話のバッファ・1 日の回数枠・同時実行の枠・掃除スレッド）。

mcp は uvicorn の単一プロセス・ECS desired_count=1 なので、プロセス内の状態がそのまま系全体の
状態になる（skills/clip_proposal/limits.py と同じ判断）。Skill のインスタンスは呼び出しごとに
作り直されるので、状態はモジュールの singleton に置く。

発話は MCP プロセスのメモリにだけ置き（設計 §10b.4 の限定例外）、永続化もログ出力もしない。
1 人 5 発話で取り出し、最初の発話から FLUSH_S 秒たったら掃除スレッドが取り出す。
取り出した後にジョブを起動できなければ、その場で捨てる（呼び出し側の責任）。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

import structlog

from teamagent.adapters.personal_memory_store import Principal

logger = structlog.get_logger(__name__)

FLUSH_AFTER_UTTERANCES: Final = 5
FLUSH_S: Final = 480.0
DAILY_JOBS_PER_USER: Final = 6
MAX_PENDING_PRINCIPALS: Final = 64
RECENT_MESSAGE_IDS: Final = 32
SWEEP_INTERVAL_S: Final = 15.0
MAX_JOBS_ENV: Final = "PERSONAL_MEMORY_MAX_JOBS"
JST: Final = timezone(timedelta(hours=9))


class StaleGenerationError(Exception):
    """読み始めた後に凍結・全削除でバッファが捨てられた（その発話も捨てる）。"""


@dataclass(frozen=True, slots=True)
class Batch:
    """取り出した発話の束（学習ジョブ 1 本ぶん）。repr に中身を出さない。"""

    principal: Principal
    utterances: tuple[str, ...] = field(repr=False)


@dataclass(slots=True)
class _Pending:
    principal: Principal
    first_at: float
    utterances: list[str] = field(default_factory=list, repr=False)


class VolatileUtteranceBuffer:
    """本人ごとの発話バッファ（最大 5 発話・first_at から FLUSH_S 秒）。"""

    def __init__(
        self,
        *,
        flush_after: int = FLUSH_AFTER_UTTERANCES,
        flush_s: float = FLUSH_S,
        max_principals: int = MAX_PENDING_PRINCIPALS,
    ) -> None:
        self._flush_after = flush_after
        self._flush_s = flush_s
        self._max_principals = max_principals
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        # 同じ Slack メッセージの再送（plugin の再試行）を二重に数えない。値は Slack の ts だけ
        self._recent: dict[str, deque[str]] = {}
        # discard のたびに進める。observe は読み始めの世代を渡し、途中で進んでいたら入れない
        self._generation: dict[str, int] = {}

    def generation(self, principal: Principal) -> int:
        with self._lock:
            return self._generation.get(principal.key, 0)

    def add(
        self,
        principal: Principal,
        utterance: str,
        message_id: str,
        now: float,
        *,
        generation: int | None = None,
    ) -> Batch | None:
        """発話を 1 件入れる。5 件目でちょうど取り出して返す。入れなかった場合も None。

        ``generation`` を渡すと、その後に discard されていたら ``StaleGenerationError`` を投げる。
        """
        key = principal.key
        with self._lock:
            if generation is not None and self._generation.get(key, 0) != generation:
                raise StaleGenerationError
            recent = self._recent.setdefault(key, deque(maxlen=RECENT_MESSAGE_IDS))
            if message_id in recent:
                return None
            pending = self._pending.get(key)
            if pending is None:
                if len(self._pending) >= self._max_principals:
                    return None  # 上限（対象者は allowlist で絞るので通常は届かない）
                pending = _Pending(principal=principal, first_at=now)
                self._pending[key] = pending
            recent.append(message_id)
            pending.utterances.append(utterance)
            if len(pending.utterances) < self._flush_after:
                return None
            del self._pending[key]
            return Batch(principal=pending.principal, utterances=tuple(pending.utterances))

    def sweep(self, now: float) -> list[Batch]:
        """first_at から flush_s 秒以上たったものを取り出す。"""
        out: list[Batch] = []
        with self._lock:
            for key in [k for k, p in self._pending.items() if now - p.first_at >= self._flush_s]:
                pending = self._pending.pop(key)
                out.append(Batch(principal=pending.principal, utterances=tuple(pending.utterances)))
        return out

    def discard(self, principal: Principal) -> None:
        """凍結・全削除のときに、その人の未処理の発話を捨てる（世代も進める）。"""
        with self._lock:
            self._pending.pop(principal.key, None)
            self._generation[principal.key] = self._generation.get(principal.key, 0) + 1

    def pending_principals(self) -> int:
        with self._lock:
            return len(self._pending)

    def pending_utterances(self, principal: Principal) -> int:
        with self._lock:
            pending = self._pending.get(principal.key)
            return 0 if pending is None else len(pending.utterances)


def jst_date_key(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(JST).strftime("%Y%m%d")


class DailyJobQuota:
    """JST の暦日で区切る 1 人 N 本の学習ジョブ枠。受付時に加算し、戻さない。"""

    def __init__(
        self,
        limit: int = DAILY_JOBS_PER_USER,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._limit = limit
        self._now = now
        self._lock = threading.Lock()
        self._date_key = ""
        self._used: dict[str, int] = {}

    def try_reserve(self, key: str) -> bool:
        date_key = jst_date_key(self._now())
        with self._lock:
            if date_key != self._date_key:
                self._date_key = date_key
                self._used = {}
            used = self._used.get(key, 0)
            if used >= self._limit:
                return False
            self._used[key] = used + 1
            return True

    def used(self, key: str) -> int:
        date_key = jst_date_key(self._now())
        with self._lock:
            return self._used.get(key, 0) if date_key == self._date_key else 0


def configured_max_jobs() -> int:
    """PERSONAL_MEMORY_MAX_JOBS（既定 1・1〜2 に丸める。Hermes の同時実行は 2 まで）。"""
    raw = os.environ.get(MAX_JOBS_ENV, "").strip()
    try:
        value = int(raw) if raw else 1
    except ValueError:
        return 1
    return min(2, max(1, value))


class JobSlots:
    """全体の同時実行の枠。待たない（取れなければその束は捨てる）。"""

    def __init__(self, limit: int | None = None) -> None:
        self._limit = configured_max_jobs() if limit is None else max(1, limit)
        # 二重 release をその場で ValueError にする（上限が静かに緩むのを防ぐ）
        self._semaphore = threading.BoundedSemaphore(self._limit)
        self._lock = threading.Lock()
        self._in_use = 0

    @property
    def limit(self) -> int:
        return self._limit

    def try_acquire(self) -> bool:
        if not self._semaphore.acquire(blocking=False):
            return False
        with self._lock:
            self._in_use += 1
        return True

    def release(self) -> None:
        with self._lock:
            self._in_use -= 1
        self._semaphore.release()

    def in_use(self) -> int:
        with self._lock:
            return self._in_use


ThreadLauncher = Callable[[Callable[[], None], str], None]


def launch_daemon_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


class Sweeper:
    """一定間隔で tick を呼ぶ daemon スレッド（1 プロセスに 1 本）。"""

    def __init__(
        self,
        tick: Callable[[], None],
        *,
        interval_s: float = SWEEP_INTERVAL_S,
        thread_launcher: ThreadLauncher = launch_daemon_thread,
    ) -> None:
        self._tick = tick
        self._interval_s = interval_s
        self._launcher = thread_launcher
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started = False

    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            self._launcher(self._loop, "personal-memory-sweeper")
        except Exception as exc:
            with self._lock:
                self._started = False
            logger.warning("personal_memory_sweeper_start_failed", error=type(exc).__name__)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        try:
            self._tick()
        except Exception as exc:
            logger.warning("personal_memory_sweep_failed", error=type(exc).__name__)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.run_once()


__all__ = [
    "DAILY_JOBS_PER_USER",
    "FLUSH_AFTER_UTTERANCES",
    "FLUSH_S",
    "Batch",
    "DailyJobQuota",
    "JobSlots",
    "StaleGenerationError",
    "Sweeper",
    "ThreadLauncher",
    "VolatileUtteranceBuffer",
    "configured_max_jobs",
    "jst_date_key",
    "launch_daemon_thread",
]
