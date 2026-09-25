"""学習ジョブ 1 本（DM 本人メモ v1・M5）。learner の daemon スレッドで動く。

1. 本人メモを読む。active でない・告知前・profile が無いなら捨てる
2. 保存済みの項目を guard で再検査し、合格したものだけを Hermes へ送る
   （送らなかった項目は削除候補にしない＝Hermes の結果に無くても消さない）
3. Hermes の結果（全量）と送った snapshot の差分を完全一致で取る
   - 結果にあって snapshot に無い → 追加候補、snapshot にあって結果に無い → 削除
   - 削除が 1 ジョブ MAX_REMOVALS 件を超えたらジョブごと捨てる（注入による一括削除を防ぐ）
4. 追加候補を ``check_entry(utterances=…, member_names=…)`` にかけ、合格分だけ残す
5. 件数・合計字数の上限に収まらない追加は捨てる
6. ``apply_learned(expected_version=読んだ版)``。版が進んでいたら（凍結・削除・忘れて）捨てる

ログは ``personal_memory_learn_result`` 1 行だけ（件数・理由コード・sha16）。本文は出さない。
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

import structlog

from teamagent.adapters.hermes_learn_client import HermesLearnError, LearnResponse
from teamagent.adapters.personal_memory_store import (
    EntryRow,
    PersonalMemoryStoreError,
    Principal,
    Snapshot,
    Target,
    valid_content,
    within_limits,
)
from teamagent.personal_memory.guard import check_entry

logger = structlog.get_logger(__name__)

MAX_REMOVALS: Final = 5
TARGETS: Final[tuple[Target, ...]] = ("user", "memory")


class _Store(Protocol):
    def load(self, principal: Principal) -> Snapshot | None: ...

    def apply_learned(
        self,
        principal: Principal,
        *,
        expected_version: int,
        adds: Sequence[tuple[Target, str]],
        remove_ids: Sequence[str],
    ) -> int: ...


class LearnClient(Protocol):
    def learn(
        self,
        *,
        job_id: str,
        user_entries: Sequence[str],
        memory_entries: Sequence[str],
        utterances: Sequence[str],
    ) -> LearnResponse: ...


class _Directory(Protocol):
    def refresh_if_stale(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class LearnOutcome:
    outcome: str
    added: int = 0
    removed: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    dropped: int = 0


def _passes(entry: EntryRow, member_names: Collection[str]) -> bool:
    return check_entry(entry.content, member_names=member_names).ok


def _diff(sent: Sequence[EntryRow], result: Sequence[str]) -> tuple[list[str], list[EntryRow]]:
    """(追加する文字列, 削除する行)。同じ文言が複数あっても個数で突き合わせる。"""
    result_counts = Counter(result)
    removed: list[EntryRow] = []
    for row in sent:
        if result_counts[row.content] > 0:
            result_counts[row.content] -= 1
        else:
            removed.append(row)
    sent_counts = Counter(row.content for row in sent)
    added: list[str] = []
    for content in result:
        if sent_counts[content] > 0:
            sent_counts[content] -= 1
        else:
            added.append(content)
    return added, removed


def run_learn_job(
    principal: Principal,
    utterances: Sequence[str],
    *,
    store: _Store,
    client: LearnClient,
    directory: _Directory | None,
    clock: Callable[[], float] = time.monotonic,
) -> LearnOutcome:
    started = clock()
    outcome = _run(principal, utterances, store=store, client=client, directory=directory)
    logger.info(
        "personal_memory_learn_result",
        sha16=principal.sha16,
        outcome=outcome.outcome,
        added=outcome.added,
        removed=outcome.removed,
        rejected=outcome.rejected,
        reasons=dict(sorted(outcome.reasons.items())),
        dropped=outcome.dropped,
        elapsed_ms=int((clock() - started) * 1000),
    )
    return outcome


def _run(
    principal: Principal,
    utterances: Sequence[str],
    *,
    store: _Store,
    client: LearnClient,
    directory: _Directory | None,
) -> LearnOutcome:
    try:
        snapshot = store.load(principal)
    except PersonalMemoryStoreError as exc:
        return LearnOutcome(outcome=f"store_{exc.code}")
    if snapshot is None or snapshot.state != "active" or not snapshot.noticed:
        return LearnOutcome(outcome="not_active")

    # 名簿の取得に失敗しても空集合（guard が敬称付きの人名をすべて落とす側）で続ける
    member_names: frozenset[str] = frozenset()
    if directory is not None:
        try:
            member_names = directory.refresh_if_stale()
        except Exception as exc:
            logger.warning("personal_memory_directory_failed", error=type(exc).__name__)

    sent: dict[Target, list[EntryRow]] = {
        target: [e for e in snapshot.entries if e.target == target and _passes(e, member_names)]
        for target in TARGETS
    }
    try:
        response = client.learn(
            job_id=uuid.uuid4().hex,
            user_entries=[e.content for e in sent["user"]],
            memory_entries=[e.content for e in sent["memory"]],
            utterances=list(utterances),
        )
    except HermesLearnError as exc:
        return LearnOutcome(outcome=f"hermes_{exc.code}")

    results: dict[Target, tuple[str, ...]] = {
        "user": response.user_entries,
        "memory": response.memory_entries,
    }
    candidates: list[tuple[Target, str]] = []
    removed_rows: list[EntryRow] = []
    for target in TARGETS:
        added, removed = _diff(sent[target], results[target])
        candidates.extend((target, content) for content in added)
        removed_rows.extend(removed)
    if len(removed_rows) > MAX_REMOVALS:
        return LearnOutcome(
            outcome="too_many_removals", removed=len(removed_rows), dropped=response.dropped
        )

    reasons: Counter[str] = Counter()
    accepted: list[tuple[Target, str]] = []
    for target, raw_content in candidates:
        # Hermes の項目は複数行でありうる。1 行に畳んでから検査する（改行は保存しない）
        content = " ".join(raw_content.split())
        if not valid_content(content):
            reasons["bad_entry"] += 1
            continue
        verdict = check_entry(content, utterances=utterances, member_names=member_names)
        if not verdict.ok:
            reasons.update(str(r) for r in verdict.reasons)
            continue
        accepted.append((target, content))

    # 送らなかった項目も DB の枠を使っているので、保存済みの全項目で上限を測る
    removed_ids = {row.entry_id for row in removed_rows}
    remaining = [(e.target, e.content) for e in snapshot.entries if e.entry_id not in removed_ids]
    while accepted and not within_limits([*remaining, *accepted]):
        accepted.pop()
        reasons["over_limit"] += 1
    rejected = len(candidates) - len(accepted)
    if not within_limits([*remaining, *accepted]):
        return LearnOutcome(outcome="over_limit", rejected=rejected, reasons=dict(reasons))
    if not accepted and not removed_rows:
        return LearnOutcome(
            outcome="no_change", rejected=rejected, reasons=dict(reasons), dropped=response.dropped
        )
    try:
        store.apply_learned(
            principal,
            expected_version=snapshot.version,
            adds=accepted,
            remove_ids=[row.entry_id for row in removed_rows],
        )
    except PersonalMemoryStoreError as exc:
        return LearnOutcome(outcome=f"store_{exc.code}", rejected=rejected, reasons=dict(reasons))
    return LearnOutcome(
        outcome="applied",
        added=len(accepted),
        removed=len(removed_rows),
        rejected=rejected,
        reasons=dict(reasons),
        dropped=response.dropped,
    )


__all__ = ["MAX_REMOVALS", "LearnClient", "LearnOutcome", "run_learn_job"]
