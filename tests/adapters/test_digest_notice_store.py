"""お知らせ DM の重複止め（digest_notice）— claim が DB の一意制約に委ねられているか。

フェイクは本番の失敗モードを再現する（test_digest_delivery_store.py と同流儀）:
  - ``ON CONFLICT DO NOTHING`` は 2 回目の rowcount が **0**
  - DB 障害（migration 0027 未適用・接続断）は **例外**
  - 期限切れ掃除の DELETE は params 無しで飛ぶ（本番と同じ呼び方）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from teamagent.adapters.digest_notice_store import (
    NOTICE_CALENDAR_UNLINKED,
    DigestNoticeStore,
)

DAY = _dt.date(2026, 9, 14)  # 月曜
USER = "komata@vectorinc.co.jp"


class _FakeCursor:
    def __init__(self, owner: _FakePg) -> None:
        self._owner = owner
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._owner.statements.append(sql.strip().split()[0])
        if params is None:
            self._owner.purges += 1
            expired = [k for k, v in self._owner.rows.items() if v["expires_at"] <= self._owner.now]
            for k in expired:
                del self._owner.rows[k]
            self.rowcount = len(expired)
            return
        # ⚠️ 主キーは (user_email, notice_kind, notice_date)。kind を鍵から外すと
        #   「種類違いのお知らせが互いを潰す」本番事故を再現できなくなる。
        key = (params["email"], params["kind"], params["day"])
        if key in self._owner.rows:
            self.rowcount = 0
        else:
            self._owner.rows[key] = {
                "expires_at": self._owner.now + _dt.timedelta(days=int(params.get("ttl_days", 14)))
            }
            self.rowcount = 1


class _FakeConn:
    def __init__(self, owner: _FakePg) -> None:
        self._owner = owner

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._owner)

    def commit(self) -> None:
        self._owner.commits += 1


class _FakePg:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: dict[tuple[str, str, _dt.date], dict[str, Any]] = {}
        self.statements: list[str] = []
        self.commits = 0
        self.purges = 0
        self.now = _dt.datetime(2026, 9, 14, 0, 30, tzinfo=_dt.UTC)
        self.connect_kwargs: list[dict[str, Any]] = []
        self._fail = fail

    def connection(self, **kwargs: Any) -> Any:
        self.connect_kwargs.append(kwargs)
        if self._fail:
            raise RuntimeError('relation "digest_notice" does not exist')
        return _FakeConn(self)


def _claim(store: DigestNoticeStore, day: _dt.date = DAY, *, email: str = USER) -> bool:
    return store.claim(email, day, kind=NOTICE_CALENDAR_UNLINKED, request_id="r")


def test_first_claim_wins_second_loses() -> None:
    """同一 (user, kind, date) は 1 回だけ通る＝planner 再実行でも DM は 1 通。"""
    store = DigestNoticeStore(_FakePg())
    assert _claim(store) is True
    assert _claim(store) is False


def test_different_days_are_independent() -> None:
    """翌週分は別の印（印が「永久に黙る」側へ倒れない）。"""
    store = DigestNoticeStore(_FakePg())
    assert _claim(store) is True
    assert _claim(store, DAY + _dt.timedelta(days=7)) is True


def test_other_users_are_not_blocked_by_someone_elses_mark() -> None:
    store = DigestNoticeStore(_FakePg())
    assert _claim(store) is True
    assert _claim(store, email="a@vectorinc.co.jp") is True


def test_db_failure_is_fail_closed_not_fail_open() -> None:
    """migration 未適用・接続断は **送らない** 側へ倒す。

    変異: ``except`` 節を ``return True``（fail-open）にすると赤。
    週 1 回のお知らせなので、1 回落ちても翌週に出る。
    """
    assert _claim(DigestNoticeStore(_FakePg(fail=True))) is False


def test_claim_binds_rls_to_the_owner() -> None:
    pg = _FakePg()
    _claim(DigestNoticeStore(pg))
    assert pg.connect_kwargs[0]["app_role"] == "teamagent_app"
    assert pg.connect_kwargs[0]["user_email"] == USER


def test_invalid_inputs_are_rejected_without_touching_db() -> None:
    pg = _FakePg()
    store = DigestNoticeStore(pg)
    assert store.claim("", DAY, kind=NOTICE_CALENDAR_UNLINKED, request_id="r") is False
    assert store.claim("not-an-email", DAY, kind=NOTICE_CALENDAR_UNLINKED, request_id="r") is False
    # migration 0027 の CHECK に無い種類は DB へ行かせない（INSERT で落とさない）。
    assert store.claim(USER, DAY, kind="unknown", request_id="r") is False
    assert pg.connect_kwargs == []


def test_claim_purges_expired_rows_in_the_same_transaction() -> None:
    """14 日の保持は claim が自分で実現する（掃除ジョブ・cron を増やさない）。

    変異: ``claim`` の ``cur.execute(_PURGE_SQL)`` を外すと期限切れ行が残って赤。
    """
    pg = _FakePg()
    stale = ("old@vectorinc.co.jp", NOTICE_CALENDAR_UNLINKED, DAY - _dt.timedelta(days=30))
    pg.rows[stale] = {"expires_at": pg.now - _dt.timedelta(days=16)}

    assert _claim(DigestNoticeStore(pg)) is True
    assert pg.purges == 1
    assert stale not in pg.rows
    assert (USER, NOTICE_CALENDAR_UNLINKED, DAY) in pg.rows


def test_purge_runs_after_rowcount_is_read() -> None:
    """掃除を先に流すと claim の判定が DELETE の rowcount になり、毎回 False になる。"""
    pg = _FakePg()
    assert _claim(DigestNoticeStore(pg)) is True
    assert pg.statements == ["INSERT", "DELETE"]


def test_sql_matches_the_migration_key() -> None:
    """ON CONFLICT の arbiter が migration 0027 の主キーと一致している。

    ここがズレると本番だけ ``ON CONFLICT`` が例外になり、fail-closed で **お知らせが
    永久に出ない**（テストは通るのに本番だけ無音）。
    """
    from pathlib import Path

    from teamagent.adapters import digest_notice_store as mod

    assert "ON CONFLICT (user_email, notice_kind, notice_date) DO NOTHING" in mod._CLAIM_SQL
    root = Path(__file__).resolve().parent.parent.parent
    ddl = (root / "infra" / "migrations" / "0027_digest_notice.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (user_email, notice_kind, notice_date)" in ddl
    assert f"notice_kind IN ('{NOTICE_CALENDAR_UNLINKED}')" in ddl
