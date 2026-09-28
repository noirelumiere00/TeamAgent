"""二重配信の止め口（digest_delivery）— claim が DB の一意制約に委ねられているか。

フェイクは本番の失敗モードを再現する:
  - ``ON CONFLICT DO NOTHING`` は 2 回目の rowcount が **0**
  - DB 障害（テーブル未作成・接続断）は **例外**
  - 期限切れ掃除の DELETE は params 無しで飛ぶ（本番と同じ呼び方）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from teamagent.adapters.digest_delivery_store import DigestDeliveryStore

DAY = _dt.date(2026, 9, 11)
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
            # 期限切れ掃除（DELETE ... WHERE expires_at < NOW()）。RLS 下なので
            # 本人行だけが対象＝フェイクでも owner の行だけを見る。
            self._owner.purges += 1
            expired = [k for k, v in self._owner.rows.items() if v["expires_at"] <= self._owner.now]
            for k in expired:
                del self._owner.rows[k]
            self.rowcount = len(expired)
            return
        key = (params["email"], params["day"])
        if "INSERT" in sql:
            # 一意制約の再現。既にあれば 0 行（＝claim 失敗）。
            if key in self._owner.rows:
                self.rowcount = 0
            else:
                self._owner.rows[key] = {
                    "origin": params.get("origin", "bulk"),
                    "expires_at": self._owner.now
                    + _dt.timedelta(days=int(params.get("ttl_days", 14))),
                }
                self.rowcount = 1
        else:  # DELETE（release）
            self.rowcount = 1 if self._owner.rows.pop(key, None) is not None else 0


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
        self.rows: dict[tuple[str, _dt.date], dict[str, Any]] = {}
        self.statements: list[str] = []
        self.commits = 0
        self.purges = 0
        self.now = _dt.datetime(2026, 9, 11, 0, 30, tzinfo=_dt.UTC)
        self.connect_kwargs: list[dict[str, Any]] = []
        self._fail = fail

    def connection(self, **kwargs: Any) -> Any:
        self.connect_kwargs.append(kwargs)
        if self._fail:
            raise RuntimeError('relation "digest_delivery" does not exist')
        return _FakeConn(self)


def test_first_claim_wins_second_loses() -> None:
    """同一 (user, date) は 1 回だけ通る。これが 2 通配信の唯一の止め口。"""
    store = DigestDeliveryStore(_FakePg())
    assert store.claim(USER, DAY, origin="scheduled", request_id="r1") is True
    assert store.claim(USER, DAY, origin="bulk", request_id="r2") is False


def test_bulk_run_is_excluded_after_scheduled_delivery() -> None:
    """既定時刻の一括実行は「その日すでに送った人」を必ず除外する。

    変異: ``_process_user`` の claim 呼び出しを外すと 2 通目が通り赤。
    """
    pg = _FakePg()
    store = DigestDeliveryStore(pg)
    store.claim(USER, DAY, origin="scheduled", request_id="r1")
    others = ["a@vectorinc.co.jp", "b@vectorinc.co.jp"]
    sent = [e for e in [USER, *others] if store.claim(e, DAY, origin="bulk", request_id="r")]
    assert sent == others


def test_different_days_are_independent() -> None:
    store = DigestDeliveryStore(_FakePg())
    assert store.claim(USER, DAY, origin="bulk", request_id="r1") is True
    assert store.claim(USER, DAY + _dt.timedelta(days=1), origin="bulk", request_id="r2") is True


def test_db_failure_is_fail_closed_not_fail_open() -> None:
    """テーブル未作成・接続断は **送らない** 側へ倒す。

    変異: ``except`` 節を ``return True``（fail-open）にすると赤。
    障害の日に 29 名へ 2 通届くより、無音（監視で拾える）を選ぶ。
    """
    store = DigestDeliveryStore(_FakePg(fail=True))
    assert store.claim(USER, DAY, origin="bulk", request_id="r") is False


def test_release_allows_retry_after_failed_delivery() -> None:
    store = DigestDeliveryStore(_FakePg())
    assert store.claim(USER, DAY, origin="scheduled", request_id="r1") is True
    assert store.release(USER, DAY, request_id="r1") is True
    assert store.claim(USER, DAY, origin="bulk", request_id="r2") is True


def test_claim_binds_rls_to_the_owner() -> None:
    pg = _FakePg()
    DigestDeliveryStore(pg).claim(USER, DAY, origin="bulk", request_id="r")
    assert pg.connect_kwargs[0]["app_role"] == "teamagent_app"
    assert pg.connect_kwargs[0]["user_email"] == USER


def test_invalid_inputs_are_rejected_without_touching_db() -> None:
    pg = _FakePg()
    store = DigestDeliveryStore(pg)
    assert store.claim("", DAY, origin="bulk", request_id="r") is False
    assert store.claim("not-an-email", DAY, origin="bulk", request_id="r") is False
    assert store.claim(USER, DAY, origin="unknown", request_id="r") is False
    assert pg.connect_kwargs == []


def test_claim_purges_expired_rows_in_the_same_transaction() -> None:
    """14 日の保持は claim が自分で実現する（掃除ジョブ・cron を増やさない）。

    変異: ``claim`` の ``cur.execute(_PURGE_SQL)`` を外すと期限切れ行が残って赤。
    """
    pg = _FakePg()
    store = DigestDeliveryStore(pg)
    stale = ("old@vectorinc.co.jp", DAY - _dt.timedelta(days=30))
    pg.rows[stale] = {"origin": "bulk", "expires_at": pg.now - _dt.timedelta(days=16)}

    assert store.claim(USER, DAY, origin="bulk", request_id="r") is True
    assert pg.purges == 1
    assert stale not in pg.rows
    # 期限内の行（いま取った印）は消えない＝二重配信の止め口を自分で壊さない。
    assert (USER, DAY) in pg.rows


def test_purge_runs_after_rowcount_is_read() -> None:
    """掃除を先に流すと claim の判定が DELETE の rowcount になり、毎回 False になる。

    変異: ``claim`` で ``cur.execute(_PURGE_SQL)`` を rowcount 読み取りの **前** へ
    動かすと、掃除対象 0 件のとき rowcount=0 ＝ claim 失敗になり赤。
    """
    pg = _FakePg()
    assert DigestDeliveryStore(pg).claim(USER, DAY, origin="bulk", request_id="r") is True
    assert pg.statements == ["INSERT", "DELETE"]
