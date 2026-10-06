"""submit の二重受付をはじく（10-06 本番: 1 秒差の 2 回呼びで同じジョブが 2 本走った）。

固定する不変量:
  1. 同じ利用者・同じ内容を続けて 2 回 → ジョブは 1 本・2 回目は既存 job_id と deduplicated
  2. 並び順・全角半角・空白・大文字小文字の差は同じ内容とみなす
  3. 別ブランド・別利用者は別ジョブ
  4. N 分を過ぎて終わったジョブは再利用しない（2 本目を作る）。まだ走っていれば再利用する
  5. 同時に 2 本来ても 1 本（DynamoDB の条件付き書込で勝つのは 1 本だけ）
  6. 失敗・心拍切れのジョブ、順番待ち（busy）で作らなかった依頼は次の依頼を止めない
  7. 錠の読み書きが壊れても受付は止めない（重複判定だけあきらめる）

DynamoDB のフェイクは本番の失敗モードを再現する: 条件に負けた書込は
``ConditionalCheckFailedException``（botocore の ClientError と同じ ``response`` 形）で落ち、
同じキーへの条件付き書込は 1 本ずつ評価される（内部ロック）。
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import TikTokScrapeError
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.dedup import normalize_text
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import JobAdmission, OmiyageReportSubmitSkill

_T0 = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)


class _ConditionalCheckFailedError(Exception):
    """botocore ClientError と同じ ``response`` を持つ条件失敗。"""

    def __init__(self) -> None:
        super().__init__("The conditional request failed")
        self.response = {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "The conditional request failed",
            }
        }


class _ThrottledError(Exception):
    def __init__(self) -> None:
        super().__init__("Rate exceeded")
        self.response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}


class _FakeDynamo:
    """ProposalJobStore が出す式だけを評価する DynamoDB（同じ table に job 行と錠が同居）。"""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, dict[str, str]]] = {}
        self._lock = threading.RLock()
        # 錠の読み取りの直後に呼ぶフック（同時実行の再現用）
        self.after_lock_read: Callable[[], None] | None = None
        self.fail_dedup_with: Exception | None = None

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Item"]["job_id"]["S"]
        if key.startswith("dedup_") and self.fail_dedup_with is not None:
            raise self.fail_dedup_with
        with self._lock:
            current = self.items.get(key)
            condition = kwargs.get("ConditionExpression")
            if condition == "attribute_not_exists(job_id)":
                if current is not None:
                    raise _ConditionalCheckFailedError
            elif condition == "#target_job_id = :expected_target":
                name = kwargs["ExpressionAttributeNames"]["#target_job_id"]
                expected = kwargs["ExpressionAttributeValues"][":expected_target"]
                if current is None or current.get(name) != expected:
                    raise _ConditionalCheckFailedError
            else:
                raise AssertionError(f"unexpected condition: {condition!r}")
            self.items[key] = copy.deepcopy(kwargs["Item"])
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"] is True
        key = kwargs["Key"]["job_id"]["S"]
        if key.startswith("dedup_") and self.fail_dedup_with is not None:
            raise self.fail_dedup_with
        with self._lock:
            item = copy.deepcopy(self.items.get(key))
        if key.startswith("dedup_") and self.after_lock_read is not None:
            self.after_lock_read()
        return {"Item": item} if item is not None else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """状態遷移（_transition / heartbeat）の式のうち、status 条件と SET だけを評価する。"""
        key = kwargs["Key"]["job_id"]["S"]
        with self._lock:
            item = self.items.get(key)
            values = kwargs["ExpressionAttributeValues"]
            condition = kwargs["ConditionExpression"]
            expected = [
                v for k, v in values.items() if k.startswith(":expected_status_") and k in condition
            ]
            if ":running" in values and "#status = :running" in condition:
                expected.append(values[":running"])
            if item is None or (expected and item.get("status") not in expected):
                raise _ConditionalCheckFailedError
            names = kwargs["ExpressionAttributeNames"]
            set_part = kwargs["UpdateExpression"].partition(" REMOVE ")[0].removeprefix("SET ")
            for assignment in set_part.split(", "):
                attribute, placeholder = assignment.split(" = ", maxsplit=1)
                item[names[attribute]] = copy.deepcopy(values[placeholder])
        return {}

    def job_rows(self) -> dict[str, dict[str, dict[str, str]]]:
        with self._lock:
            return {k: v for k, v in self.items.items() if k.startswith("omy_")}

    def set_status(self, job_id: str, status: str) -> None:
        with self._lock:
            self.items[job_id]["status"] = {"S": status}


@dataclass
class _Clock:
    value: datetime = _T0

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value = self.value + timedelta(**kwargs)


class _ManualLauncher:
    """背景実行は走らせない（受付の即答だけを見る＝ジョブは queued のまま残る）。"""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, target: Callable[[], None], name: str) -> None:
        with self._lock:
            self.names.append(name)


def _offline_searcher(*args: Any, **kwargs: Any) -> Any:
    raise TikTokScrapeError("TIKTOK_SEARCH_FAILED")


def _input(
    brand: str = "ブランドA",
    competitors: tuple[str, ...] = ("競合B", "競合C"),
    keywords: tuple[str, ...] = ("シャンプー", "ヘアケア"),
    **extra: Any,
) -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        brand=brand, competitors=list(competitors), keywords=list(keywords), **extra
    )


def _ctx(user_id: str = "U_SALES", request_id: str = "req-1") -> SkillContext:
    return SkillContext(request_id=request_id, user_id=user_id, metadata={})


@dataclass
class _Env:
    ddb: _FakeDynamo
    store: ProposalJobStore
    clock: _Clock
    launcher: _ManualLauncher
    skill: OmiyageReportSubmitSkill


def _env(*, admission_limit: int = 10, dedup_seconds: int | None = 30 * 60) -> _Env:
    ddb = _FakeDynamo()
    clock = _Clock()
    store = ProposalJobStore(table_name="proposal-jobs", dynamodb_client=ddb, clock=clock)
    launcher = _ManualLauncher()
    skill = OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=launcher,
        searcher=_offline_searcher,
        heartbeat_seconds=0,
        admission=JobAdmission(admission_limit),
        dedup_seconds=dedup_seconds,
        clock=clock,
    )
    return _Env(ddb=ddb, store=store, clock=clock, launcher=launcher, skill=skill)


def test_same_request_twice_creates_one_job_and_returns_the_existing_id() -> None:
    env = _env()

    first = env.skill.run(_input(), _ctx(request_id="req-1"))
    env.clock.advance(seconds=1)
    second = env.skill.run(_input(), _ctx(request_id="req-2"))

    assert first.status == "queued" and first.deduplicated is False
    assert second.status == "queued"
    assert second.deduplicated is True
    assert second.job_id == first.job_id
    assert second.retry_after_seconds == first.retry_after_seconds
    assert "二重には作りません" in second.message
    assert "omiyage_" not in second.message
    assert len(env.ddb.job_rows()) == 1
    assert len(env.launcher.names) == 1


def test_order_width_space_and_case_differences_are_the_same_request() -> None:
    env = _env()

    first = env.skill.run(
        _input(brand="DHC", competitors=("競合B", "競合C"), keywords=("シャンプー", "ヘア ケア")),
        _ctx(),
    )
    second = env.skill.run(
        _input(
            brand="ＤＨＣ",  # 全角
            competitors=("競合Ｃ", " 競合B "),  # 並び順違い・全角・前後空白
            keywords=("ヘア　ケア", "シャンプー"),  # 全角空白・並び順違い
        ),
        _ctx(),
    )
    third = env.skill.run(
        _input(brand="dhc", competitors=("競合c", "競合b"), keywords=("ヘア  ケア", "シャンプー")),
        _ctx(),
    )

    assert second.deduplicated is True and second.job_id == first.job_id
    assert third.deduplicated is True and third.job_id == first.job_id
    assert len(env.ddb.job_rows()) == 1


def test_different_brand_keyword_or_user_creates_another_job() -> None:
    env = _env()

    base = env.skill.run(_input(brand="ブランドA"), _ctx())
    other_brand = env.skill.run(_input(brand="ブランドZ"), _ctx())
    other_keywords = env.skill.run(_input(keywords=("シャンプー",)), _ctx())
    other_user = env.skill.run(_input(brand="ブランドA"), _ctx(user_id="U_OTHER"))

    outputs = [base, other_brand, other_keywords, other_user]
    assert [o.deduplicated for o in outputs] == [False, False, False, False]
    assert len({o.job_id for o in outputs}) == 4
    assert len(env.ddb.job_rows()) == 4


def test_done_job_older_than_the_window_is_not_reused() -> None:
    env = _env()
    first = env.skill.run(_input(), _ctx())
    env.ddb.set_status(first.job_id, "done")

    env.clock.advance(minutes=29)
    within = env.skill.run(_input(), _ctx())
    env.clock.advance(minutes=2)  # 受付から 31 分
    expired = env.skill.run(_input(), _ctx())

    assert within.deduplicated is True and within.job_id == first.job_id
    assert "作成が終わり" in within.message
    assert expired.deduplicated is False
    assert expired.job_id != first.job_id
    assert len(env.ddb.job_rows()) == 2
    # 新しい錠は 2 本目を指す（3 回目は 2 本目に寄る）
    third = env.skill.run(_input(), _ctx())
    assert third.deduplicated is True and third.job_id == expired.job_id


def test_job_still_running_after_the_window_is_reused() -> None:
    env = _env()
    first = env.skill.run(_input(), _ctx())
    assert env.store.mark_running(first.job_id)

    env.clock.advance(minutes=35)  # 窓（30 分）は過ぎたがまだ走っている（心拍は新しい）
    assert env.store.heartbeat(first.job_id)
    again = env.skill.run(_input(), _ctx())

    assert again.deduplicated is True
    assert again.job_id == first.job_id
    assert "作成中" in again.message
    assert len(env.ddb.job_rows()) == 1


def test_failed_or_stale_job_does_not_block_a_new_request() -> None:
    env = _env()
    first = env.skill.run(_input(), _ctx())
    env.ddb.set_status(first.job_id, "failed")

    retry = env.skill.run(_input(), _ctx())
    assert retry.deduplicated is False and retry.job_id != first.job_id

    # 2 本目は queued のまま心拍が途絶えた（再起動で止まった）→ 待たせず 3 本目を作る
    env.clock.advance(minutes=5)
    after_restart = env.skill.run(_input(), _ctx())
    assert after_restart.deduplicated is False
    assert after_restart.job_id not in (first.job_id, retry.job_id)
    assert len(env.ddb.job_rows()) == 3


def test_concurrent_identical_submits_create_exactly_one_job() -> None:
    env = _env()
    barrier = threading.Barrier(2, timeout=5)
    seen = threading.local()

    def both_read_before_either_writes() -> None:
        # 各スレッドの最初の錠読み取りだけ待ち合わせる＝両方「錠なし」を見てから書きに行く
        if not getattr(seen, "done", False):
            seen.done = True
            barrier.wait()

    env.ddb.after_lock_read = both_read_before_either_writes
    results: list[Any] = []
    errors: list[BaseException] = []

    def submit(request_id: str) -> None:
        try:
            results.append(env.skill.run(_input(), _ctx(request_id=request_id)))
        except BaseException as exc:  # pragma: no cover - 失敗時の診断用
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(f"req-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sorted(r.deduplicated for r in results) == [False, True]
    assert results[0].job_id == results[1].job_id
    assert len(env.ddb.job_rows()) == 1
    assert len(env.launcher.names) == 1


def test_concurrent_identical_submits_on_the_memory_backend_create_one_job() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    skill = OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=_ManualLauncher(),
        searcher=_offline_searcher,
        heartbeat_seconds=0,
        admission=JobAdmission(10),
        dedup_seconds=1800,
    )
    start = threading.Barrier(8, timeout=5)
    results: list[Any] = []

    def submit(i: int) -> None:
        start.wait()
        results.append(skill.run(_input(), _ctx(request_id=f"req-{i}")))

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 8
    assert len({r.job_id for r in results}) == 1
    assert sum(not r.deduplicated for r in results) == 1
    assert len(memory) == 1  # 台帳の行数＝ジョブ数（錠は別 mapping）


def test_busy_rejection_releases_the_lock_so_the_retry_is_accepted() -> None:
    env = _env(admission_limit=1)
    blocker = env.skill.run(_input(brand="先行ブランド"), _ctx(user_id="U_OTHER"))
    assert blocker.status == "queued"

    busy = env.skill.run(_input(), _ctx())
    assert busy.status == "busy" and busy.job_id == ""

    env.skill._admission.release()  # 先行ジョブが終わって枠が空いた
    retry = env.skill.run(_input(), _ctx())
    assert retry.status == "queued"
    assert retry.deduplicated is False
    assert len(env.ddb.job_rows()) == 2


def test_thread_start_failure_releases_the_lock() -> None:
    ddb = _FakeDynamo()
    clock = _Clock()
    store = ProposalJobStore(table_name="proposal-jobs", dynamodb_client=ddb, clock=clock)

    def broken_launcher(target: Callable[[], None], name: str) -> None:
        raise RuntimeError("cannot start thread")

    broken = OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=broken_launcher,
        searcher=_offline_searcher,
        heartbeat_seconds=0,
        admission=JobAdmission(3),
        dedup_seconds=1800,
        clock=clock,
    )
    failed = broken.run(_input(), _ctx())
    assert failed.status == "failed"

    lock_id = next(k for k in ddb.items if k.startswith("dedup_"))
    assert ddb.items[lock_id]["target_job_id"] == {"S": ""}  # 返却済み

    healthy = OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=_ManualLauncher(),
        searcher=_offline_searcher,
        heartbeat_seconds=0,
        admission=JobAdmission(3),
        dedup_seconds=1800,
        clock=clock,
    )
    retry = healthy.run(_input(), _ctx())
    assert retry.status == "queued" and retry.deduplicated is False
    assert retry.job_id != failed.job_id


def test_second_request_while_the_first_is_between_lock_and_job_write_is_deduplicated() -> None:
    """錠を取った直後・ジョブ行を書く前に 2 本目が来ても、受付中とみなして寄せる。"""
    env = _env()
    lock_id = env.skill._dedup_lock_id(_input(), "U_SALES")
    in_flight = "omy_" + "0" * 32
    assert env.store.put_dedup_lock(lock_id, in_flight, expected_target=None)

    second = env.skill.run(_input(), _ctx())
    assert second.deduplicated is True and second.job_id == in_flight

    # 猶予（120 秒）を過ぎてもジョブ行が無い＝書込に失敗した錠 → 奪って新規
    env.clock.advance(minutes=3)
    third = env.skill.run(_input(), _ctx())
    assert third.deduplicated is False and third.job_id != in_flight
    assert len(env.ddb.job_rows()) == 1


def test_lock_store_failure_fails_open_and_still_accepts() -> None:
    env = _env()
    env.ddb.fail_dedup_with = _ThrottledError()

    first = env.skill.run(_input(), _ctx())
    second = env.skill.run(_input(), _ctx())

    # 重複判定はあきらめるが、受付は止めない
    assert [first.status, second.status] == ["queued", "queued"]
    assert first.deduplicated is False and second.deduplicated is False
    assert len(env.ddb.job_rows()) == 2


def test_requests_without_user_id_or_with_dedup_disabled_are_not_merged() -> None:
    env = _env()
    a = env.skill.run(_input(), _ctx(user_id=""))
    b = env.skill.run(_input(), _ctx(user_id=""))
    assert a.job_id != b.job_id and not b.deduplicated

    off = _env(dedup_seconds=0)
    c = off.skill.run(_input(), _ctx())
    d = off.skill.run(_input(), _ctx())
    assert c.job_id != d.job_id and not d.deduplicated
    assert not any(k.startswith("dedup_") for k in off.ddb.items)


def test_dedup_window_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMIYAGE_SUBMIT_DEDUP_MINUTES", "5")
    ddb = _FakeDynamo()
    clock = _Clock()
    store = ProposalJobStore(table_name="proposal-jobs", dynamodb_client=ddb, clock=clock)
    skill = OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=_ManualLauncher(),
        searcher=_offline_searcher,
        heartbeat_seconds=0,
        admission=JobAdmission(10),
        clock=clock,
    )
    first = skill.run(_input(), _ctx())
    ddb.set_status(first.job_id, "done")
    clock.advance(minutes=6)
    assert skill.run(_input(), _ctx()).deduplicated is False


def test_lock_rows_never_overwrite_job_rows() -> None:
    store = ProposalJobStore(table_name="proposal-jobs", dynamodb_client=_FakeDynamo())
    with pytest.raises(ValueError):
        store.put_dedup_lock("omy_" + "1" * 32, "omy_x", expected_target=None)
    with pytest.raises(ValueError):
        store.get_dedup_lock("pb_" + "1" * 32)


def test_normalize_text_handles_width_space_and_case() -> None:
    assert normalize_text(" ＤＨＣ　 薬用  ") == "dhc 薬用"
    assert normalize_text("ﾍｱｹｱ") == normalize_text("ヘアケア")
