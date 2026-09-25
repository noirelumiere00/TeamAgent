"""本人メモのテスト用フェイク（本番の失敗モードを再現する）。

- FakeStore: PersonalMemoryStore と同じ規則（版の照合・active と告知済みの確認・上限・
  全削除の確認期限・profile が無いときの no_profile）で動く。DB 障害は
  ``PersonalMemoryStoreError("store_unavailable")``（本物の _txn が包んだ形）で再現する。
  DB 固有の挙動（RLS・CHECK・SAVEPOINT）は tests/personal_memory/test_store_postgres.py で本物を試す
- hermes_transport: hermes_runtime/aico_hermes/server.py と同じ形の HTTP 応答を返す
  httpx.MockTransport。本物の HermesLearnClient に差して使う（応答の検査も本物が行う）
- ManualLauncher: 学習ジョブ・掃除スレッドを起動せずに溜め、テストから明示的に走らせる
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from teamagent.adapters.hermes_learn_client import HermesLearnClient
from teamagent.adapters.personal_memory_store import (
    EntryRow,
    PersonalMemoryStoreError,
    Principal,
    Snapshot,
    State,
    Target,
    valid_content,
    within_limits,
)

HERMES_URL = "https://hermes.aico.internal:8790"
HERMES_BEARER = "b" * 40


@dataclass
class _Profile:
    email: str
    state: State = "active"
    noticed: bool = False
    version: int = 0
    views: int = 0
    erase_until: float | None = None
    entries: list[EntryRow] = field(default_factory=list)


class FakeStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.profiles: dict[str, _Profile] = {}
        self.audit: list[str] = []
        self.loads = 0
        self.fail = False
        self.load_delay_s = 0.0
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # --- テスト用の準備 --------------------------------------------------------------------

    def create(
        self,
        p: Principal,
        *,
        noticed: bool = True,
        state: State = "active",
        entries: Sequence[tuple[Target, str]] = (),
        views: int = 0,
    ) -> None:
        profile = _Profile(email=p.user_email, state=state, noticed=noticed, views=views)
        profile.entries = [self._row(t, c) for t, c in entries]
        self.profiles[p.key] = profile

    def contents(self, p: Principal, target: Target | None = None) -> list[str]:
        return [
            e.content for e in self.profiles[p.key].entries if target is None or e.target == target
        ]

    def _row(self, target: Target, content: str) -> EntryRow:
        n = next(self._ids)
        return EntryRow(
            entry_id=f"00000000-0000-4000-8000-{n:012d}",
            target=target,
            content=content,
            created_at=datetime(2026, 9, 25, tzinfo=UTC),
        )

    def _check(self) -> None:
        if self.fail:
            raise PersonalMemoryStoreError("store_unavailable")

    def _get(self, p: Principal) -> _Profile:
        profile = self.profiles.get(p.key)
        if profile is None:
            raise PersonalMemoryStoreError("no_profile")
        return profile

    def _ensure(self, p: Principal) -> _Profile:
        return self.profiles.setdefault(p.key, _Profile(email=p.user_email))

    # --- PersonalMemoryStore と同じ API -----------------------------------------------------

    def load(self, p: Principal) -> Snapshot | None:
        self.loads += 1
        if self.load_delay_s:
            time.sleep(self.load_delay_s)
        with self._lock:
            self._check()
            profile = self.profiles.get(p.key)
            if profile is None:
                return None
            return Snapshot(
                state=profile.state,
                noticed=profile.noticed,
                version=profile.version,
                admin_view_count=profile.views,
                erase_confirm_until=None,
                entries=tuple(sorted(profile.entries, key=lambda e: (e.target, e.entry_id))),
            )

    def mark_noticed(self, p: Principal) -> None:
        with self._lock:
            self._check()
            profile = self._ensure(p)
            profile.noticed = True
            profile.version += 1
            self.audit.append("notice_ack")

    def apply_learned(
        self,
        p: Principal,
        *,
        expected_version: int,
        adds: Sequence[tuple[Target, str]],
        remove_ids: Sequence[str],
    ) -> int:
        for target, content in adds:
            if target not in ("user", "memory") or not valid_content(content):
                raise PersonalMemoryStoreError("bad_entry")
        with self._lock:
            self._check()
            profile = self._get(p)
            if profile.state != "active" or not profile.noticed:
                raise PersonalMemoryStoreError("not_active")
            if profile.version != expected_version:
                raise PersonalMemoryStoreError("version_conflict")
            known = {e.entry_id for e in profile.entries}
            if any(r not in known for r in remove_ids):
                raise PersonalMemoryStoreError("unknown_entry")
            remaining = [e for e in profile.entries if e.entry_id not in set(remove_ids)]
            if not within_limits([*[(e.target, e.content) for e in remaining], *adds]):
                raise PersonalMemoryStoreError("over_limit")
            profile.entries = remaining + [self._row(t, c) for t, c in adds]
            profile.version += 1
            self.audit.append("learn_applied")
            return profile.version

    def forget(
        self, p: Principal, entry_id: str, *, expected_version: int | None = None
    ) -> int | None:
        with self._lock:
            self._check()
            profile = self._get(p)
            if expected_version is not None and profile.version != expected_version:
                raise PersonalMemoryStoreError("version_conflict")
            if entry_id not in {e.entry_id for e in profile.entries}:
                return None
            profile.entries = [e for e in profile.entries if e.entry_id != entry_id]
            profile.version += 1
            self.audit.append("forget")
            return profile.version

    def set_state(self, p: Principal, state: State) -> None:
        with self._lock:
            self._check()
            profile = self._ensure(p)
            profile.state = state
            profile.erase_until = None
            profile.version += 1
            self.audit.append("freeze" if state == "frozen" else "resume")

    def request_erase(self, p: Principal) -> None:
        with self._lock:
            self._check()
            profile = self._get(p)
            profile.erase_until = self.clock() + 600

    def confirm_erase(self, p: Principal) -> int:
        with self._lock:
            self._check()
            profile = self._get(p)
            if profile.erase_until is None or profile.erase_until < self.clock():
                raise PersonalMemoryStoreError("erase_not_requested")
            count = len(profile.entries)
            profile.entries = []
            profile.state = "frozen"
            profile.erase_until = None
            profile.version += 1
            self.audit.append("erase_all")
            return count


# --- Hermes（本物のクライアント＋本番と同じ形の HTTP 応答） ---------------------------------------

Responder = Callable[[list[str], list[str], list[str]], tuple[list[str], list[str]]]


def append(user: Sequence[str] = (), memory: Sequence[str] = ()) -> Responder:
    """snapshot に user/memory を足した全量を返す（本家 memory tool の add と同じ結果の形）。"""

    def respond(
        snap_user: list[str], snap_memory: list[str], _utterances: list[str]
    ) -> tuple[list[str], list[str]]:
        return [*snap_user, *user], [*snap_memory, *memory]

    return respond


class HermesFake:
    """hermes_runtime の /v1/learn と同じ応答を返す。mode で失敗の形を選ぶ。"""

    def __init__(self, responder: Responder | None = None) -> None:
        self.responder = responder or append()
        self.mode = "ok"
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append(body)
        job_id = body["job_id"]
        if self.mode == "busy":
            return httpx.Response(503, json={"error": "busy"})
        if self.mode == "timeout":
            return httpx.Response(502, json={"error": "timeout", "job_id": job_id})
        if self.mode == "internal":
            return httpx.Response(500, json={"error": "internal", "job_id": job_id})
        user, memory = self.responder(
            body["snapshot"]["user"], body["snapshot"]["memory"], body["utterances"]
        )
        payload: dict[str, Any] = {
            "job_id": job_id,
            "user": user,
            "memory": memory,
            "dropped": 0,
            "elapsed_s": 1.5,
        }
        if self.mode == "extra_key":
            payload["debug"] = "x"
        if self.mode == "job_mismatch":
            payload["job_id"] = "f" * 32
        if self.mode == "section_sign":
            payload["user"] = [*user, "a§b"]
        return httpx.Response(200, json=payload)

    def client(self) -> HermesLearnClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return HermesLearnClient(service_url=HERMES_URL, bearer=HERMES_BEARER, ca_pem="", http=http)


class FakeDirectory:
    def __init__(self, names: Sequence[str] = ()) -> None:
        self.names = frozenset(names)
        self.refreshes = 0

    def cached_member_names(self) -> frozenset[str]:
        return self.names

    def refresh_if_stale(self) -> frozenset[str]:
        self.refreshes += 1
        return self.names


class ManualLauncher:
    def __init__(self) -> None:
        self.queue: list[tuple[str, Callable[[], None]]] = []

    def __call__(self, target: Callable[[], None], name: str) -> None:
        self.queue.append((name, target))

    @property
    def pending(self) -> int:
        return sum(1 for name, _ in self.queue if name == "personal-memory-learn")

    def run_all(self) -> None:
        jobs = [t for name, t in self.queue if name == "personal-memory-learn"]
        self.queue = [(n, t) for n, t in self.queue if n != "personal-memory-learn"]
        for job in jobs:
            job()


def make_runtime(
    *,
    store: FakeStore | None = None,
    client: HermesFake | None | str = "default",
    directory: FakeDirectory | None = None,
    notice: str | None = "告知文",
) -> tuple[Any, ManualLauncher]:
    from teamagent.mcp_gateway.personal_memory.buffer import (
        DailyJobQuota,
        JobSlots,
    )
    from teamagent.mcp_gateway.personal_memory.service import PersonalMemoryRuntime

    launcher = ManualLauncher()
    fake = HermesFake() if client == "default" else client
    runtime = PersonalMemoryRuntime(
        store=store or FakeStore(),
        client=None if fake is None else fake.client(),  # type: ignore[union-attr]
        directory=directory or FakeDirectory(),
        quota=DailyJobQuota(now=lambda: datetime(2026, 9, 25, 3, 0, tzinfo=UTC)),
        slots=JobSlots(limit=1),
        thread_launcher=launcher,
        notice=lambda: notice,
    )
    runtime.hermes = fake  # type: ignore[attr-defined]
    return runtime, launcher
