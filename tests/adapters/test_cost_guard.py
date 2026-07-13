"""cost_guard の単体テスト（fake DynamoDB 注入・AWS非接続）。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.adapters.quota_store import current_month_jst


class _FakeDdb:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: dict[str, dict[str, int]] = {}
        self.fail = fail

    def get_item(self, *, TableName: str, Key: dict[str, Any], **kw: Any) -> dict[str, Any]:  # noqa: N803
        if self.fail:
            raise RuntimeError("ddb down")
        key = Key["usage_key"]["S"]
        row = self.rows.get(key)
        if row is None:
            return {}
        return {"Item": {"cost_micro": {"N": str(row["cost_micro"])}}}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: dict[str, Any],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeValues: dict[str, Any],  # noqa: N803
        ConditionExpression: str | None = None,  # noqa: N803
        ReturnValues: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("ddb down")
        key = Key["usage_key"]["S"]
        row = self.rows.setdefault(key, {"cost_micro": 0, "calls": 0, "units": 0})
        if ConditionExpression is not None:
            remaining = int(ExpressionAttributeValues[":remaining"]["N"])
            if row["cost_micro"] > remaining:
                error = RuntimeError("conditional failed")
                error.response = {  # type: ignore[attr-defined]
                    "Error": {"Code": "ConditionalCheckFailedException"}
                }
                raise error
        row["cost_micro"] += int(ExpressionAttributeValues[":c"]["N"])
        if ":one" in ExpressionAttributeValues:
            row["calls"] += int(ExpressionAttributeValues[":one"]["N"])
        if ":u" in ExpressionAttributeValues:
            row["units"] += int(ExpressionAttributeValues[":u"]["N"])
        if ReturnValues == "ALL_NEW":
            return {"Attributes": {"cost_micro": {"N": str(row["cost_micro"])}}}
        return {}


def _guard(ddb: _FakeDdb | None = None) -> CostGuard:
    return CostGuard("t", ddb=ddb or _FakeDdb())


def test_from_env_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COST_GUARD_TABLE", raising=False)
    assert CostGuard.from_env() is None


def test_no_limits_no_ddb_access(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("COST_APIFY_MONTHLY_USD", "COST_APIFY_PER_CALL_USD", "COST_PER_USER_MONTHLY_USD"):
        monkeypatch.delenv(k, raising=False)
    ddb = _FakeDdb(fail=True)  # 触ったら例外になる＝触っていない証明
    warnings = _guard(ddb).check("apify", "a@x.jp", est_cost_usd=1.0, request_id="t")
    assert warnings == []


def test_per_call_limit_fail_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_PER_CALL_USD", "0.5")
    with pytest.raises(CostLimitExceededError, match="上限"):
        _guard().check("apify", "a@x.jp", est_cost_usd=0.6, request_id="t")


def test_monthly_limit_fail_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "10")
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()
    ddb.rows[f"apify#{current_month_jst()}"] = {"cost_micro": 9_950_000, "calls": 1, "units": 1}
    with pytest.raises(CostLimitExceededError, match="使い切りました"):
        _guard(ddb).check("apify", "a@x.jp", est_cost_usd=0.2, request_id="t")


def test_reserve_single_est_over_monthly_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # 単発 est が月次上限そのものを超える場合、空行でも予約は通さず fail-close する
    # （_reserve_micro の attribute_not_exists 短絡ですり抜けたリグレッションの回帰）。
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "0.10")
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()  # 空（月初の最初の呼び出し）
    with pytest.raises(CostLimitExceededError, match="今月の枠"):
        _guard(ddb).reserve("apify", "a@x.jp", est_cost_usd=0.115, request_id="t")
    assert ddb.rows == {}  # 台帳に予約が積まれていない（すり抜けゼロ）


def test_reserve_single_est_over_per_user_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COST_APIFY_MONTHLY_USD", raising=False)
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.setenv("COST_PER_USER_MONTHLY_USD", "0.10")
    ddb = _FakeDdb()
    with pytest.raises(CostLimitExceededError, match="あなたの今月の枠"):
        _guard(ddb).reserve("apify", "a@x.jp", est_cost_usd=0.2, request_id="t")
    assert ddb.rows == {}


def test_monthly_80pct_warns_but_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "10")
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()
    ddb.rows[f"apify#{current_month_jst()}"] = {"cost_micro": 8_500_000, "calls": 1, "units": 1}
    warnings = _guard(ddb).check("apify", "a@x.jp", est_cost_usd=0.1, request_id="t")
    assert warnings and "80%" in warnings[0]


def test_per_user_limit_fail_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COST_APIFY_MONTHLY_USD", raising=False)
    monkeypatch.setenv("COST_PER_USER_MONTHLY_USD", "1")
    ddb = _FakeDdb()
    ddb.rows[f"apify#{current_month_jst()}#a@x.jp"] = {
        "cost_micro": 990_000,
        "calls": 1,
        "units": 1,
    }
    with pytest.raises(CostLimitExceededError, match="あなたの今月"):
        _guard(ddb).check("apify", "A@X.JP", est_cost_usd=0.1, request_id="t")


def test_ddb_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "10")
    warnings = _guard(_FakeDdb(fail=True)).check(
        "apify", "a@x.jp", est_cost_usd=5.0, request_id="t"
    )
    assert warnings == []  # 台帳障害では業務を止めない（quota_store 裁定と同じ）


def test_record_adds_global_and_user_rows() -> None:
    ddb = _FakeDdb()
    _guard(ddb).record("apify", "A@X.JP", cost_usd=0.0004, units=1, request_id="t")
    month = current_month_jst()
    assert ddb.rows[f"apify#{month}"]["cost_micro"] == 400
    assert ddb.rows[f"apify#{month}#a@x.jp"]["cost_micro"] == 400


def test_record_zero_cost_skipped() -> None:
    ddb = _FakeDdb(fail=True)  # 触ったら例外＝触らないことの証明
    _guard(ddb).record("apify", "a@x.jp", cost_usd=0.0, units=0, request_id="t")


def test_record_failure_is_swallowed() -> None:
    _guard(_FakeDdb(fail=True)).record("apify", "a@x.jp", cost_usd=0.1, units=1, request_id="t")


def test_atomic_reservation_blocks_concurrent_overshoot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "1")
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()
    month = current_month_jst()
    ddb.rows[f"apify#{month}"] = {"cost_micro": 700_000, "calls": 0, "units": 0}
    guard = _guard(ddb)

    _, reservation = guard.reserve("apify", "a@x.jp", est_cost_usd=0.2, request_id="r1")
    assert reservation.global_reserved
    with pytest.raises(CostLimitExceededError):
        guard.reserve("apify", "b@x.jp", est_cost_usd=0.2, request_id="r2")
    assert ddb.rows[f"apify#{month}"]["cost_micro"] == 900_000


def test_settle_replaces_reservation_with_actual_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "10")
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()
    guard = _guard(ddb)
    _, reservation = guard.reserve("apify", "a@x.jp", est_cost_usd=1.0, request_id="r1")
    guard.settle(reservation, cost_usd=0.4, units=4, request_id="r1")
    month = current_month_jst()
    assert ddb.rows[f"apify#{month}"]["cost_micro"] == 400_000
    assert ddb.rows[f"apify#{month}#a@x.jp"]["cost_micro"] == 400_000
