"""本人メモの 3 ツールの処理（DM 本人メモ v1・M5）。

server.dispatch_personal_memory_tool が門（claim・DM・予約 ID・allowlist・入力検証）を
通した後にだけ呼ぶ。ここに届く Principal は署名済み claim と resolver の値だけから作られている。

- observe: 発話を guard で検査し、告知済み・active の人だけ揮発バッファに入れる。
  5 発話（または掃除スレッドが 480 秒）で学習ジョブを起動する。起動できなければ捨てる
- context: 返信前に読む。1.0 秒で諦めて空を返す（メモなしで現行 Aico が返事をする）
- command: 本人の操作（一覧・忘れて・止めて・再開・全部消して・告知済みの記録）

DB と Hermes の処理は専用の小さいスレッドプールで動かす（MCP 既定の executor が
重いツールで詰まっても context の 1.0 秒を守るため）。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import structlog

from teamagent.adapters.personal_memory_store import (
    PersonalMemoryStoreError,
    Principal,
    Snapshot,
    State,
    Target,
)
from teamagent.mcp_gateway.personal_memory import texts
from teamagent.mcp_gateway.personal_memory.buffer import (
    Batch,
    DailyJobQuota,
    JobSlots,
    StaleGenerationError,
    Sweeper,
    ThreadLauncher,
    VolatileUtteranceBuffer,
    launch_daemon_thread,
)
from teamagent.mcp_gateway.personal_memory.learner import LearnClient, run_learn_job
from teamagent.mcp_gateway.personal_memory.schemas import (
    CommandInput,
    ContextInput,
    ObserveInput,
)
from teamagent.personal_memory.guard import check_entry, check_utterance

logger = structlog.get_logger(__name__)

CONTEXT_TIMEOUT_S: Final = 1.0
OBSERVE_TIMEOUT_S: Final = 2.0
COMMAND_TIMEOUT_S: Final = 8.0
CONTEXT_MAX_CHARS: Final = 3600
LISTING_TTL_S: Final = 600.0

OBSERVE_TOOL: Final = "personal_memory_observe"
CONTEXT_TOOL: Final = "personal_memory_context"
COMMAND_TOOL: Final = "personal_memory_command"


class _Store(Protocol):
    def load(self, principal: Principal) -> Snapshot | None: ...

    def mark_noticed(self, principal: Principal) -> None: ...

    def apply_learned(
        self,
        principal: Principal,
        *,
        expected_version: int,
        adds: Sequence[tuple[Target, str]],
        remove_ids: Sequence[str],
    ) -> int: ...

    def forget(
        self, principal: Principal, entry_id: str, *, expected_version: int | None = None
    ) -> int | None: ...

    def set_state(self, principal: Principal, state: State) -> None: ...

    def request_erase(self, principal: Principal) -> None: ...

    def confirm_erase(self, principal: Principal) -> int: ...


class _Directory(Protocol):
    def cached_member_names(self) -> frozenset[str]: ...

    def refresh_if_stale(self) -> frozenset[str]: ...


class ListingCache:
    """「何を覚えてる？」で見せた番号と entry_id の対応（本人ごと・TTL つき）。"""

    def __init__(self, ttl_s: float = LISTING_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, int, tuple[str | None, ...]]] = {}

    def put(self, key: str, version: int, ids: Sequence[str | None]) -> None:
        with self._lock:
            now = self._clock()
            for stale in [k for k, (at, _, _) in self._items.items() if now - at > self._ttl_s]:
                del self._items[stale]
            self._items[key] = (now, version, tuple(ids))

    def get(self, key: str) -> tuple[int, tuple[str | None, ...]] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            at, version, ids = item
            if self._clock() - at > self._ttl_s:
                del self._items[key]
                return None
            return version, ids

    def drop(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


@dataclass
class PersonalMemoryRuntime:
    store: _Store
    client: LearnClient | None  # HermesLearnClient（未設定なら学習しない）
    directory: _Directory | None
    buffer: VolatileUtteranceBuffer = field(default_factory=VolatileUtteranceBuffer)
    quota: DailyJobQuota = field(default_factory=DailyJobQuota)
    slots: JobSlots = field(default_factory=JobSlots)
    listing: ListingCache = field(default_factory=ListingCache)
    thread_launcher: ThreadLauncher = launch_daemon_thread
    clock: Callable[[], float] = time.monotonic
    notice: Callable[[], str | None] = texts.build_notice
    sweeper: Sweeper | None = None

    def __post_init__(self) -> None:
        if self.sweeper is None:
            self.sweeper = Sweeper(self.sweep_tick, thread_launcher=self.thread_launcher)

    # --- 名簿 --------------------------------------------------------------------------------

    def _cached_names(self) -> frozenset[str]:
        if self.directory is None:
            return frozenset()
        try:
            return self.directory.cached_member_names()
        except Exception:
            return frozenset()

    # --- observe -----------------------------------------------------------------------------

    def observe(
        self, principal: Principal, message_id: str, payload: ObserveInput
    ) -> dict[str, Any]:
        verdict = check_utterance(payload.utterance, has_attachment=payload.has_attachment)
        if not verdict.ok:
            return self._observed(principal, "dropped", reason=str(verdict.reasons[0]))
        if self.client is None:
            return self._observed(principal, "dropped", reason="learner_disabled")
        sent_at = _slack_ts(message_id)
        if sent_at is None:
            return self._observed(principal, "dropped", reason="bad_message_id")
        # 読み始めの世代。読んでいる間に凍結・全削除されたら、この発話も入れない
        generation = self.buffer.generation(principal)
        try:
            snapshot = self.store.load(principal)
        except PersonalMemoryStoreError as exc:
            return self._observed(principal, "dropped", reason=f"store_{exc.code}")
        if snapshot is None or not snapshot.noticed:
            return self._observed(principal, "dropped", reason="not_noticed")
        if snapshot.noticed_at is not None and sent_at <= snapshot.noticed_at.timestamp():
            # 告知を記録する前のメッセージ（1 通目）は学習しない。plugin の呼び順に頼らない
            return self._observed(principal, "dropped", reason="before_notice")
        if snapshot.state != "active":
            return self._observed(principal, "dropped", reason="frozen")
        self._ensure_sweeper()
        try:
            batch = self.buffer.add(
                principal, payload.utterance, message_id, self.clock(), generation=generation
            )
        except StaleGenerationError:
            return self._observed(principal, "dropped", reason="discarded")
        if batch is None:
            return self._observed(principal, "buffered")
        status = self._launch(batch)
        return self._observed(
            principal, "queued" if status == "started" else "dropped", reason=status
        )

    @staticmethod
    def _observed(principal: Principal, status: str, *, reason: str = "") -> dict[str, Any]:
        logger.info("personal_memory_observe", sha16=principal.sha16, status=status, reason=reason)
        return {"status": status}

    # --- 学習ジョブの起動 --------------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        if self.sweeper is not None:
            self.sweeper.ensure_started()

    def sweep_tick(self) -> None:
        for batch in self.buffer.sweep(self.clock()):
            self._launch(batch)
        if self.directory is not None:
            self.directory.refresh_if_stale()

    def _launch(self, batch: Batch) -> str:
        """学習ジョブを起動する。起動できなければその束を捨てる（発話は保持しない）。"""
        if self.client is None:
            return "learner_disabled"
        if not self.slots.try_acquire():
            self._dropped(batch, "busy")
            return "busy"
        if not self.quota.try_reserve(batch.principal.key):
            self.slots.release()
            self._dropped(batch, "daily_limit")
            return "daily_limit"
        try:
            self.thread_launcher(lambda: self._job(batch), "personal-memory-learn")
        except Exception as exc:
            self.slots.release()
            self._dropped(batch, f"launch_{type(exc).__name__}"[:40])
            return "launch_failed"
        return "started"

    @staticmethod
    def _dropped(batch: Batch, reason: str) -> None:
        logger.info(
            "personal_memory_batch_dropped",
            sha16=batch.principal.sha16,
            reason=reason,
            utterances=len(batch.utterances),
        )

    def _job(self, batch: Batch) -> None:
        try:
            if self.client is not None:
                run_learn_job(
                    batch.principal,
                    batch.utterances,
                    store=self.store,
                    client=self.client,
                    directory=self.directory,
                )
        except Exception as exc:
            logger.warning(
                "personal_memory_learn_failed",
                sha16=batch.principal.sha16,
                error=type(exc).__name__,
            )
        finally:
            self.slots.release()

    # --- context -----------------------------------------------------------------------------

    def build_context(self, principal: Principal) -> dict[str, Any]:
        snapshot = self.store.load(principal)
        if snapshot is not None and snapshot.state != "active":
            return _empty_context()  # 止めている人には告知も出さない
        if snapshot is None or not snapshot.noticed:
            notice = self.notice()
            return {
                "memo_context": "",
                "notice_required": notice is not None,
                "notice_text": notice or "",
                "items": 0,
            }
        names = self._cached_names()
        sections: list[tuple[str, list[str]]] = []
        for target, heading in (
            ("user", texts.CONTEXT_USER_HEADING),
            ("memory", texts.CONTEXT_MEMORY_HEADING),
        ):
            items = [
                e.content
                for e in snapshot.entries
                if e.target == target and check_entry(e.content, member_names=names).ok
            ]
            if items:
                sections.append((heading, items))
        if not sections:
            return _empty_context()
        text, count = _frame(sections)
        return {"memo_context": text, "notice_required": False, "notice_text": "", "items": count}

    # --- command -----------------------------------------------------------------------------

    def command(self, principal: Principal, payload: CommandInput) -> dict[str, Any]:
        action = payload.action
        try:
            reply = self._command(principal, payload)
            ok = True
        except PersonalMemoryStoreError as exc:
            ok = False
            reply = {
                "erase_not_requested": texts.ERASE_EXPIRED,
                "no_profile": texts.NOT_STARTED,
            }.get(exc.code, texts.UNAVAILABLE)
            logger.info(
                "personal_memory_command", sha16=principal.sha16, action=action, code=exc.code
            )
        else:
            logger.info("personal_memory_command", sha16=principal.sha16, action=action, code="ok")
        return {"action": action, "ok": ok, "reply": reply}

    def _command(self, principal: Principal, payload: CommandInput) -> str:
        key = principal.key
        action = payload.action
        if action == "list":
            snapshot = self.store.load(principal)
            if snapshot is None:
                return texts.NOT_STARTED
            names = self._cached_names()
            shown = [e for e in snapshot.entries if check_entry(e.content, member_names=names).ok]
            # いまの規則に合わず返事に使っていない項目も、本人には見せて番号で消せるようにする
            # （見えない・消せないまま件数と字数の枠だけを使い続けないため）
            hidden = [e for e in snapshot.entries if e not in shown]
            self.listing.put(key, snapshot.version, [e.entry_id for e in [*shown, *hidden]])
            return texts.listing(
                [e.content for e in shown],
                hidden_items=[e.content for e in hidden],
                admin_views=snapshot.admin_view_count,
                frozen=snapshot.state != "active",
            )
        if action == "forget":
            return self._forget(principal, payload.item_no or 0)
        if action == "freeze":
            # DB への書き込みが失敗しても、ためていた発話は先に捨てる（止めたのに学習させない）
            self.buffer.discard(principal)
            self.store.set_state(principal, "frozen")
            self.buffer.discard(principal)
            self.listing.drop(key)
            return texts.FROZEN
        if action == "resume":
            self.store.set_state(principal, "active")
            self.listing.drop(key)
            return texts.RESUMED
        if action == "erase_request":
            self.store.request_erase(principal)
            return texts.ERASE_CONFIRM
        if action == "erase_confirm":
            self.buffer.discard(principal)
            count = self.store.confirm_erase(principal)
            self.buffer.discard(principal)
            self.listing.drop(key)
            return texts.erased(count)
        if action == "notice_ack":
            if self.notice() is None:
                # 告知文が確定していない（送れない）のに告知済みにしない
                raise PersonalMemoryStoreError("notice_unavailable")
            self.store.mark_noticed(principal)
            return texts.NOTICE_RECORDED
        raise PersonalMemoryStoreError("bad_action")  # pragma: no cover（schemas で閉じている）

    def _forget(self, principal: Principal, item_no: int) -> str:
        cached = self.listing.get(principal.key)
        if cached is None:
            return texts.LIST_FIRST
        version, ids = cached
        if not 1 <= item_no <= len(ids) or ids[item_no - 1] is None:
            return texts.NO_SUCH_ITEM
        entry_id = ids[item_no - 1]
        assert entry_id is not None
        try:
            new_version = self.store.forget(principal, entry_id, expected_version=version)
        except PersonalMemoryStoreError as exc:
            if exc.code != "version_conflict":
                raise
            self.listing.drop(principal.key)
            return texts.LIST_STALE
        if new_version is None:
            self.listing.drop(principal.key)
            return texts.NO_SUCH_ITEM
        # 見せた一覧の番号はそのまま使えるようにする（消した番号だけ空ける）
        remaining = list(ids)
        remaining[item_no - 1] = None
        self.listing.put(principal.key, new_version, remaining)
        return texts.forgot(item_no)


_SLACK_TS_RE: Final = re.compile(r"([0-9]{9,11})\.([0-9]{6})")


def _slack_ts(message_id: str) -> float | None:
    """署名済み claim の message（Slack の ts）を秒に直す。形が違えば None。"""
    match = _SLACK_TS_RE.fullmatch(message_id)
    if match is None:
        return None
    return int(match.group(1)) + int(match.group(2)) / 1_000_000


def _empty_context() -> dict[str, Any]:
    return {"memo_context": "", "notice_required": False, "notice_text": "", "items": 0}


def _frame(sections: Sequence[tuple[str, Sequence[str]]]) -> tuple[str, int]:
    """固定の枠＋箇条書き。CONTEXT_MAX_CHARS を項目単位で超えない（途中で切らない）。"""
    lines = [texts.CONTEXT_HEADER]
    footer = texts.CONTEXT_FOOTER
    used = len(texts.CONTEXT_HEADER) + 1 + len(footer)
    count = 0
    for heading, items in sections:
        heading_cost = len(heading) + 1
        first = True
        for item in items:
            line = f"- {texts.display_item(item)}"
            cost = len(line) + 1 + (heading_cost if first else 0)
            if used + cost > CONTEXT_MAX_CHARS:
                break
            if first:
                lines.append(heading)
                first = False
            lines.append(line)
            used += cost
            count += 1
    lines.append(footer)
    return "\n".join(lines), count


# --- プロセス内の singleton ---------------------------------------------------------------------

_EXECUTOR: Final = ThreadPoolExecutor(max_workers=3, thread_name_prefix="personal-memory")
_RUNTIME: PersonalMemoryRuntime | None = None
_RUNTIME_LOCK: Final = threading.Lock()


def _build_default_runtime() -> PersonalMemoryRuntime:
    from teamagent.adapters.hermes_learn_client import HermesLearnClient
    from teamagent.adapters.personal_memory_store import PersonalMemoryStore
    from teamagent.adapters.slack_member_directory import SlackMemberDirectory

    team_id = os.environ.get("SLACK_TEAM_ID", "").strip()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    directory = SlackMemberDirectory(team_id=team_id, token=token) if team_id and token else None
    return PersonalMemoryRuntime(
        store=PersonalMemoryStore(),
        client=HermesLearnClient.from_env(),
        directory=directory,
    )


def get_runtime() -> PersonalMemoryRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = _build_default_runtime()
        return _RUNTIME


def reset_runtime_for_tests(runtime: PersonalMemoryRuntime | None = None) -> None:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None and _RUNTIME.sweeper is not None:
            _RUNTIME.sweeper.stop()
        _RUNTIME = runtime


async def _in_executor(fn: Callable[[], dict[str, Any]], timeout_s: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, fn), timeout=timeout_s)


async def handle_personal_memory(
    tool: str,
    principal: Principal,
    message_id: str,
    payload: ObserveInput | ContextInput | CommandInput,
) -> dict[str, Any]:
    """門を通った呼び出しを処理する。例外は呼び出し側（server）で固定コードにする。"""
    if tool == CONTEXT_TOOL:
        try:
            runtime = get_runtime()
            return await _in_executor(lambda: runtime.build_context(principal), CONTEXT_TIMEOUT_S)
        except Exception as exc:  # 時間切れ・DB 障害でもメモなしで返事をさせる
            logger.info(
                "personal_memory_context_empty", sha16=principal.sha16, error=type(exc).__name__
            )
            return _empty_context()
    runtime = get_runtime()
    if tool == OBSERVE_TOOL and isinstance(payload, ObserveInput):
        try:
            return await _in_executor(
                lambda: runtime.observe(principal, message_id, payload), OBSERVE_TIMEOUT_S
            )
        except Exception as exc:
            logger.info(
                "personal_memory_observe_failed", sha16=principal.sha16, error=type(exc).__name__
            )
            return {"status": "dropped"}
    if tool == COMMAND_TOOL and isinstance(payload, CommandInput):
        try:
            return await _in_executor(
                lambda: runtime.command(principal, payload), COMMAND_TIMEOUT_S
            )
        except TimeoutError:
            # 処理は裏で続いていて、あとで成功しうる（結果は personal_memory_command のログに残る）
            logger.warning("personal_memory_command_slow", sha16=principal.sha16)
            return {"action": payload.action, "ok": False, "reply": texts.PENDING}
        except Exception as exc:
            logger.warning(
                "personal_memory_command_failed", sha16=principal.sha16, error=type(exc).__name__
            )
            return {"action": payload.action, "ok": False, "reply": texts.UNAVAILABLE}
    raise ValueError("unsupported personal memory call")


__all__ = [
    "COMMAND_TOOL",
    "CONTEXT_MAX_CHARS",
    "CONTEXT_TIMEOUT_S",
    "CONTEXT_TOOL",
    "OBSERVE_TOOL",
    "ListingCache",
    "PersonalMemoryRuntime",
    "get_runtime",
    "handle_personal_memory",
    "reset_runtime_for_tests",
]
