"""外部SaaS課金のプロバイダ横断$コスト台帳（DynamoDB・月次JST）。

既存防御との役割分担（重複させない）:
  - AWS課金の予算/異常検知       = infra/terraform/budgets.tf（Budgets 3閾値+Cost Anomaly）
  - Bedrock 1実行のコスト上限     = orchestrator/sdk_runner.py の max_budget_usd
  - 動画分析の月間本数クォータ    = adapters/quota_store.py（Postgres video_usage）
  - **外部SaaS（Apify/xAI等）の$** = 本モジュール（この空白だけが新規）

設計は quota_store.py の裁定を踏襲:
  - 予算超過 = fail-close（Actor起動前に概算額を原子予約し、同時実行も上限内に収める）
  - 台帳インフラ障害 = fail-open（WARNログ＋実行は通す。コスト制御で業務を止めない）
  - env 未設定 = ガード無効（後方互換）
  - 月次リセット = JST 月初（quota_store.current_month_jst を共用）

ストアが DynamoDB なのは、④効果測定の使い捨てFargateワーカーが DB資格情報なしに
IAMだけで記帳できるようにするため（tiktok_task_store と同じ流儀）。金額は浮動小数の
累積誤差を避けるためマイクロUSD整数で ADD する。

env:
  COST_GUARD_TABLE            DynamoDBテーブル名（空=ガード無効）
  COST_<PROVIDER>_MONTHLY_USD    プロバイダ別・全体月次上限（例 COST_APIFY_MONTHLY_USD=50）
  COST_<PROVIDER>_PER_CALL_USD   プロバイダ別・1回上限（未設定=無制限）
  COST_PER_USER_MONTHLY_USD      個人月次上限（全プロバイダ共通・未設定=無制限）
  AWS_REGION                  既定 ap-northeast-1
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.adapters.quota_store import current_month_jst

logger = structlog.get_logger(__name__)

_TTL_S = 7776000  # 90日（月次行の掃除用）
_WARN_RATIO = 0.8  # 月次上限の80%超で警告（実行は許可）
_MICRO = 1_000_000  # USD → マイクロUSD


class CostLimitExceededError(RuntimeError):
    """予算超過（fail-close）。str() がそのままユーザー向けメッセージになる。"""


@dataclass(frozen=True)
class CostReservation:
    """Actor起動前に台帳へ確保した概算額。完了時に実費との差額を精算する。"""

    provider: str
    email: str
    month: str
    reserved_micro: int
    global_reserved: bool
    user_reserved: bool


def _envfloat(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class CostGuard:
    """DynamoDB月次台帳への原子reserve/settle（check/recordは互換API）。"""

    def __init__(self, table: str, *, region: str | None = None, ddb: Any | None = None) -> None:
        """ddb: テスト注入用 DynamoDB client。None なら boto3 遅延生成。"""
        self._table = table
        self._region = region or os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._ddb = ddb

    @classmethod
    def from_env(cls) -> CostGuard | None:
        """COST_GUARD_TABLE から構築する。未設定なら None（=ガード無効・後方互換）。"""
        table = os.environ.get("COST_GUARD_TABLE", "").strip()
        if not table:
            return None
        return cls(table)

    def _client(self) -> Any:
        if self._ddb is None:
            import boto3

            self._ddb = boto3.session.Session().client("dynamodb", region_name=self._region)
        return self._ddb

    # ---- 内部: 読み書き ------------------------------------------------------

    def _get_micro(self, key: str) -> int:
        resp = self._client().get_item(
            TableName=self._table, Key={"usage_key": {"S": key}}, ConsistentRead=True
        )
        item = resp.get("Item") or {}
        return int(item.get("cost_micro", {}).get("N", "0"))

    def _add_micro(self, key: str, micro: int, units: int) -> None:
        now = int(time.time())
        self._client().update_item(
            TableName=self._table,
            Key={"usage_key": {"S": key}},
            UpdateExpression=(
                "ADD cost_micro :c, calls :one, units :u SET expires_at = if_not_exists"
                "(expires_at, :ttl)"
            ),
            ExpressionAttributeValues={
                ":c": {"N": str(micro)},
                ":one": {"N": "1"},
                ":u": {"N": str(units)},
                ":ttl": {"N": str(now + _TTL_S)},
            },
        )

    def _adjust_reserved_micro(self, key: str, micro: int) -> None:
        """予約額だけを増減する（calls/units は完了時 settle で加算）。"""
        now = int(time.time())
        self._client().update_item(
            TableName=self._table,
            Key={"usage_key": {"S": key}},
            UpdateExpression="ADD cost_micro :c SET expires_at = if_not_exists(expires_at, :ttl)",
            ExpressionAttributeValues={
                ":c": {"N": str(micro)},
                ":ttl": {"N": str(now + _TTL_S)},
            },
        )

    def _reserve_micro(self, key: str, micro: int, limit_micro: int) -> int:
        """上限内なら概算額を原子加算し、加算後の使用額を返す。"""
        now = int(time.time())
        remaining_before_reserve = max(0, limit_micro - micro)
        resp = self._client().update_item(
            TableName=self._table,
            Key={"usage_key": {"S": key}},
            UpdateExpression="ADD cost_micro :c SET expires_at = if_not_exists(expires_at, :ttl)",
            ConditionExpression=("attribute_not_exists(cost_micro) OR cost_micro <= :remaining"),
            ExpressionAttributeValues={
                ":c": {"N": str(micro)},
                ":remaining": {"N": str(remaining_before_reserve)},
                ":ttl": {"N": str(now + _TTL_S)},
            },
            ReturnValues="ALL_NEW",
        )
        attrs = resp.get("Attributes") or {}
        return int(attrs.get("cost_micro", {}).get("N", str(micro)))

    @staticmethod
    def _is_conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", {})
        if not isinstance(response, dict):
            return False
        error = response.get("Error") or {}
        return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"

    # ---- 公開API -------------------------------------------------------------

    def check(
        self, provider: str, user_email: str, *, est_cost_usd: float, request_id: str
    ) -> list[str]:
        """実行前チェック。1回上限/月次全体/月次個人の3段。

        超過は CostLimitExceededError（fail-close）。月次80%超は警告文字列を返して実行許可。
        台帳読取の失敗は fail-open（空リストで許可・WARN）。
        """
        warnings: list[str] = []
        prov = provider.strip().lower()
        prov_env = prov.upper()

        per_call = _envfloat(f"COST_{prov_env}_PER_CALL_USD")
        if per_call is not None and est_cost_usd > per_call:
            raise CostLimitExceededError(
                f"1回の{prov}実行見積(${est_cost_usd:.2f})が上限(${per_call:.2f})を超えます。"
                "件数を減らして再実行してください。"
            )

        monthly = _envfloat(f"COST_{prov_env}_MONTHLY_USD")
        per_user = _envfloat("COST_PER_USER_MONTHLY_USD")
        if monthly is None and per_user is None:
            return warnings

        month = current_month_jst()
        email = (user_email or "").strip().lower()
        try:
            if monthly is not None:
                used = self._get_micro(f"{prov}#{month}") / _MICRO
                if used + est_cost_usd > monthly:
                    logger.info(
                        "cost_guard_denied",
                        request_id=request_id,
                        provider=prov,
                        scope="monthly",
                        used_usd=round(used, 4),
                        limit_usd=monthly,
                    )
                    raise CostLimitExceededError(
                        f"今月の{prov}利用枠(${monthly:.0f})を使い切りました"
                        f"（使用済み ${used:.2f}）。管理者(小俣)に連絡してください。"
                    )
                if used + est_cost_usd > monthly * _WARN_RATIO:
                    warnings.append(
                        f"今月の{prov}予算の{int(_WARN_RATIO * 100)}%を超えています"
                        f"（${used:.2f}/${monthly:.0f}）"
                    )
            if per_user is not None and email:
                used_u = self._get_micro(f"{prov}#{month}#{email}") / _MICRO
                if used_u + est_cost_usd > per_user:
                    logger.info(
                        "cost_guard_denied",
                        request_id=request_id,
                        provider=prov,
                        scope="per_user",
                        used_usd=round(used_u, 4),
                        limit_usd=per_user,
                    )
                    raise CostLimitExceededError(
                        f"あなたの今月の{prov}利用枠(${per_user:.0f})を使い切りました。"
                        "管理者(小俣)に連絡してください。"
                    )
        except CostLimitExceededError:
            raise
        except Exception as e:
            # fail-open（quota_store 裁定と同じ）: 台帳障害でユーザー業務を止めない。
            logger.warning(
                "cost_guard_check_failed",
                request_id=request_id,
                provider=prov,
                error=type(e).__name__,
            )
        return warnings

    def reserve(
        self, provider: str, user_email: str, *, est_cost_usd: float, request_id: str
    ) -> tuple[list[str], CostReservation]:
        """Actor起動前に概算額を条件付き原子加算して予約する。

        check() と record() の間で同時実行が上限をすり抜けないための実行経路。
        条件不成立（予算超過）は fail-close、DynamoDB障害は既存方針どおり fail-open。
        """
        prov = provider.strip().lower()
        prov_env = prov.upper()
        email = (user_email or "").strip().lower()
        month = current_month_jst()
        micro = max(0, round(est_cost_usd * _MICRO))
        reservation = CostReservation(prov, email, month, micro, False, False)

        per_call = _envfloat(f"COST_{prov_env}_PER_CALL_USD")
        if per_call is not None and est_cost_usd > per_call:
            raise CostLimitExceededError(
                f"1回の{prov}実行見積(${est_cost_usd:.2f})が上限(${per_call:.2f})を超えます。"
                "件数を減らして再実行してください。"
            )

        monthly = _envfloat(f"COST_{prov_env}_MONTHLY_USD")
        per_user = _envfloat("COST_PER_USER_MONTHLY_USD")
        global_reserved = False
        user_reserved = False
        scope = "monthly"
        warnings: list[str] = []
        try:
            if monthly is not None and micro > 0:
                new_total = self._reserve_micro(f"{prov}#{month}", micro, round(monthly * _MICRO))
                global_reserved = True
                if new_total > monthly * _MICRO * _WARN_RATIO:
                    warnings.append(
                        f"今月の{prov}予算の{int(_WARN_RATIO * 100)}%を超えています"
                        f"（${new_total / _MICRO:.2f}/${monthly:.0f}）"
                    )
            if per_user is not None and email and micro > 0:
                scope = "per_user"
                self._reserve_micro(f"{prov}#{month}#{email}", micro, round(per_user * _MICRO))
                user_reserved = True
        except Exception as e:
            # 個人枠の予約に失敗した場合、先に取った全体枠を必ず返す。
            if global_reserved:
                try:
                    self._adjust_reserved_micro(f"{prov}#{month}", -micro)
                except Exception as rollback_error:
                    logger.warning(
                        "cost_guard_reservation_rollback_failed",
                        request_id=request_id,
                        provider=prov,
                        error=type(rollback_error).__name__,
                    )
            if self._is_conditional_failure(e):
                used_key = f"{prov}#{month}" if scope == "monthly" else f"{prov}#{month}#{email}"
                used = self._get_micro(used_key) / _MICRO
                if scope == "monthly":
                    logger.info(
                        "cost_guard_denied",
                        request_id=request_id,
                        provider=prov,
                        scope=scope,
                        used_usd=round(used, 4),
                        limit_usd=monthly,
                    )
                    raise CostLimitExceededError(
                        f"今月の{prov}利用枠(${monthly or 0:.0f})を使い切りました"
                        f"（使用済み ${used:.2f}）。管理者(小俣)に連絡してください。"
                    ) from None
                raise CostLimitExceededError(
                    f"あなたの今月の{prov}利用枠(${per_user or 0:.0f})を使い切りました。"
                    "管理者(小俣)に連絡してください。"
                ) from None
            logger.warning(
                "cost_guard_reserve_failed",
                request_id=request_id,
                provider=prov,
                error=type(e).__name__,
            )
            # 台帳障害はfail-open。予約なしの印を返し、settleで実費記帳を再試行する。
            return warnings, reservation

        return warnings, CostReservation(prov, email, month, micro, global_reserved, user_reserved)

    def settle(
        self,
        reservation: CostReservation,
        *,
        cost_usd: float,
        units: int,
        request_id: str,
    ) -> None:
        """予約を実費へ精算する。失敗は既存record同様WARNのみ。"""
        actual_micro = max(0, round(cost_usd * _MICRO))
        global_delta = actual_micro - (
            reservation.reserved_micro if reservation.global_reserved else 0
        )
        user_delta = actual_micro - (reservation.reserved_micro if reservation.user_reserved else 0)
        try:
            self._add_micro(f"{reservation.provider}#{reservation.month}", global_delta, units)
            if reservation.email:
                self._add_micro(
                    f"{reservation.provider}#{reservation.month}#{reservation.email}",
                    user_delta,
                    units,
                )
            logger.info(
                "cost_guard_settled",
                request_id=request_id,
                provider=reservation.provider,
                cost_usd=round(cost_usd, 6),
                units=units,
            )
        except Exception as e:
            logger.warning(
                "cost_guard_settle_failed",
                request_id=request_id,
                provider=reservation.provider,
                error=type(e).__name__,
            )

    def record(
        self, provider: str, user_email: str, *, cost_usd: float, units: int, request_id: str
    ) -> None:
        """実行後の記帳（全体行＋個人行を原子 ADD）。失敗は WARN のみ（fail-open）。"""
        if cost_usd <= 0:
            return
        prov = provider.strip().lower()
        month = current_month_jst()
        email = (user_email or "").strip().lower()
        micro = round(cost_usd * _MICRO)
        try:
            self._add_micro(f"{prov}#{month}", micro, units)
            if email:
                self._add_micro(f"{prov}#{month}#{email}", micro, units)
            logger.info(
                "cost_guard_recorded",
                request_id=request_id,
                provider=prov,
                cost_usd=round(cost_usd, 6),
                units=units,
            )
        except Exception as e:
            logger.warning(
                "cost_guard_record_failed",
                request_id=request_id,
                provider=prov,
                error=type(e).__name__,
            )


__all__ = ["CostGuard", "CostLimitExceededError", "CostReservation"]
