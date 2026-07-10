"""VideoQuotaStore＋video skill クォータ配線（v0.3 Task 10）のテスト（外部I/O無し）。

検証主眼: 原子的 UPSERT の SQL 契約（消費/ブロックの分岐）・JST 月キー・fail-open・
count>limit 事前拒否・skill 配線（超過は VIDEO_QUOTA_EXCEEDED で raise・キャッシュヒットは
消費しない・既定OFFで不発）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.adapters.quota_store import QuotaResult, VideoQuotaStore, current_month_jst
from teamagent.skills.base import SkillContext
from teamagent.skills.video.schema import VideoAnalysisInput
from teamagent.skills.video.skill import VideoAnalysisSkill

ME = "me@vectorinc.co.jp"


# ── フェイク DB（psycopg 互換の最小面・RLS はここでは対象外＝SQL 契約の検証） ──


class _FakeCursor:
    def __init__(self, store: dict[tuple[str, str], int], limit_probe: list[int]) -> None:
        self._store = store
        self._limit_probe = limit_probe
        self._result: Any = None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        key = (params["email"], params["month"])
        if "INSERT INTO video_usage" in sql:
            self._limit_probe.append(params["limit"])
            new_used = self._store.get(key, 0) + params["count"]
            if new_used <= params["limit"]:
                self._store[key] = new_used
                self._result = {"used": new_used}
            else:
                self._result = None  # 条件付き UPDATE 不成立＝0行
        else:  # PEEK
            self._result = {"used": self._store.get(key, 0)} if key in self._store else None

    def fetchone(self) -> Any:
        return self._result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class _FakeConn:
    def __init__(self, store: dict[tuple[str, str], int], probe: list[int]) -> None:
        self._store = store
        self._probe = probe

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store, self._probe)

    def commit(self) -> None:
        pass

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class _FakePg:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], int] = {}
        self.limit_probe: list[int] = []
        self.rls_calls: list[dict[str, Any]] = []

    def connection(self, **kw: Any) -> _FakeConn:
        self.rls_calls.append(kw)
        return _FakeConn(self.store, self.limit_probe)


def _store(limit: int = 3) -> tuple[VideoQuotaStore, _FakePg]:
    pg = _FakePg()
    return VideoQuotaStore(pg, limit=limit), pg


def test_consume_until_blocked() -> None:
    store, pg = _store(limit=3)
    month = current_month_jst()
    for expected in (1, 2, 3):
        r = store.try_consume(ME, 1, request_id="r")
        assert r.allowed and r.used == expected
    r = store.try_consume(ME, 1, request_id="r")
    assert not r.allowed and r.used == 3 and r.limit == 3  # 4本目はブロック・消費なし
    assert pg.store[(ME, month)] == 3
    # RLS: app_role + 本人 email で接続している（越権しない）。
    assert pg.rls_calls[0] == {"app_role": "teamagent_app", "user_email": ME}


def test_multi_count_consume_and_boundary() -> None:
    store, _ = _store(limit=3)
    assert store.try_consume(ME, 2, request_id="r").used == 2  # video_algorithm 相当（実本数）
    assert not store.try_consume(ME, 2, request_id="r").allowed  # 2+2>3 は不成立＝消費なし
    assert store.try_consume(ME, 1, request_id="r").used == 3  # ちょうど上限は OK


def test_count_over_limit_rejected_upfront() -> None:
    store, pg = _store(limit=3)
    assert not store.try_consume(ME, 5, request_id="r").allowed
    assert pg.store == {}  # INSERT 経路の WHERE 無効化を突かせない（事前拒否）


def test_db_failure_fail_open() -> None:
    class _Boom:
        def connection(self, **kw: Any) -> Any:
            raise RuntimeError("db down")

    r = VideoQuotaStore(_Boom(), limit=3).try_consume(ME, 1, request_id="r")
    assert r.allowed  # 裁定: コスト制御はfail-open（分析を止めない・WARNはops監視）


def test_month_is_jst() -> None:
    # UTC 23:30 の 6/30 は JST では 7/1＝月境界は JST で切る（裁定）。
    utc_edge = _dt.datetime(2026, 6, 30, 23, 30, tzinfo=_dt.UTC)
    assert current_month_jst(utc_edge) == "2026-07"


# ── skill 配線 ──────────────────────────────────────────────────────────────


class _Resp:
    text = "分析"
    cost_usd = 0.5
    model_id = "gemini-2.5-flash"


class _FakeGemini:
    def __init__(self) -> None:
        self.calls = 0

    def analyze_video_url(self, **kw: Any) -> _Resp:
        self.calls += 1
        return _Resp()


def _run(skill: VideoAnalysisSkill) -> Any:
    return skill.run(
        VideoAnalysisInput(url="https://www.youtube.com/watch?v=abc123XYZ_-"),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )


def test_skill_raises_quota_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as qs

    monkeypatch.setattr(
        qs.VideoQuotaStore,
        "try_consume",
        lambda self, email, count, request_id: QuotaResult(allowed=False, used=20, limit=20),
    )
    gemini = _FakeGemini()
    skill = VideoAnalysisSkill(gemini=gemini)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="VIDEO_QUOTA_EXCEEDED"):
        _run(skill)
    assert gemini.calls == 0  # Gemini に到達しない（課金前に止める）


def test_skill_quota_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_QUOTA_ENABLED", raising=False)
    gemini = _FakeGemini()
    skill = VideoAnalysisSkill(gemini=gemini)  # type: ignore[arg-type]
    out = _run(skill)
    assert gemini.calls == 1 and out.total_cost_usd == 0.5  # 既定OFF＝従来挙動
