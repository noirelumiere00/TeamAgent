"""EventBridge Scheduler の薄いアダプタ（v0.3 Task 5・予定リマインド用）。

朝ダイジェスト実行時に「予定開始 N 分前にワンタイムで発火する schedule」を登録する。
発火先は SQS（teamagent-dev-reminders.fifo）で、通知本体は Lambda consumer が担う
（アプリ層に sqs:SendMessage / Slack 通知権限を持たせない＝tiktok_acquire.tf の
「実行系権限は Lambda に集約」原則の踏襲）。

設計:
  - **ActionAfterCompletion=DELETE**: 発火後に schedule 自体が消える（掃除不要・指示書どおり）
  - **決定的な schedule 名**＝同じ予定への再登録は ConflictException → 冪等成功扱い
    （朝バッチの再実行・リトライで二重リマインドを作らない）
  - **fail-open**: 登録失敗はダイジェスト配信を絶対に止めない（False を返すだけ）
  - **payload に PII を載せない**: 予定タイトル・参加者は含めない（G3。通知文は
    「まもなく予定があります（HH:MM〜）＋リンク」で成立する＝Lambda 側も PII 非接触）
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_JST = _dt.timezone(_dt.timedelta(hours=9))


def reminder_schedule_name(channel: str, start_iso: str) -> str:
    """決定的な schedule 名（channel×開始時刻由来・64文字以内・[0-9a-zA-Z-_.]）。"""
    digest = hashlib.sha256(f"{channel}|{start_iso}".encode()).hexdigest()[:32]
    return f"rem-{digest}"


class SchedulerClient:
    """EventBridge Scheduler → SQS のワンタイム schedule 登録（リマインド専用・fail-open）。"""

    def __init__(
        self,
        *,
        group_name: str,
        queue_arn: str,
        role_arn: str,
        client: Any | None = None,
    ) -> None:
        if not (group_name and queue_arn and role_arn):
            raise ValueError("scheduler の group/queue/role が未設定です")
        self._group = group_name
        self._queue_arn = queue_arn
        self._role_arn = role_arn
        self._client = client

    @classmethod
    def from_env(cls) -> SchedulerClient:
        """env から構築（REMINDER_SCHEDULER_GROUP / _QUEUE_ARN / _SCHEDULER_ROLE_ARN）。"""
        return cls(
            group_name=os.environ.get("REMINDER_SCHEDULER_GROUP", "").strip(),
            queue_arn=os.environ.get("REMINDER_QUEUE_ARN", "").strip(),
            role_arn=os.environ.get("REMINDER_SCHEDULER_ROLE_ARN", "").strip(),
        )

    def _ensure_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("scheduler")
        return self._client

    def schedule_reminder(
        self,
        *,
        channel: str,
        start_iso: str,
        fire_at: _dt.datetime,
        url: str,
        request_id: str,
    ) -> bool:
        """予定開始前リマインドのワンタイム schedule を登録する（冪等・fail-open）。

        payload はタイトル等の PII を含まない（channel・開始時刻 HH:MM・リンクのみ）。
        """
        name = reminder_schedule_name(channel, start_iso)
        fire_jst = fire_at.astimezone(_JST)
        start_hm = ""
        try:
            parsed = _dt.datetime.fromisoformat(start_iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_JST)  # naive は JST（runner と同解釈・レビュー L2）
            start_hm = parsed.astimezone(_JST).strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        payload = {"v": 1, "channel": channel, "start_hm": start_hm, "url": url}
        started = time.perf_counter()
        try:
            self._ensure_client().create_schedule(
                Name=name,
                GroupName=self._group,
                # at() はローカル秒精度・timezone 指定で JST 発火。
                ScheduleExpression=f"at({fire_jst.strftime('%Y-%m-%dT%H:%M:%S')})",
                ScheduleExpressionTimezone="Asia/Tokyo",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": self._queue_arn,
                    "RoleArn": self._role_arn,
                    "Input": json.dumps(payload, ensure_ascii=False),
                    # FIFO: 同一ユーザー(DM channel)内は順序保証＋content dedup。
                    "SqsParameters": {"MessageGroupId": channel},
                    "RetryPolicy": {"MaximumRetryAttempts": 3},
                },
            )
        except Exception as e:
            if type(e).__name__ == "ConflictException":
                # 同名 schedule 既存＝同じ予定に登録済み（朝バッチ再実行）→ 冪等成功。
                logger.info("reminder_schedule_exists", request_id=request_id)
                return True
            logger.warning(
                "reminder_schedule_failed", request_id=request_id, error=type(e).__name__
            )
            return False
        logger.info(
            "reminder_scheduled",
            request_id=request_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return True


__all__ = ["SchedulerClient", "reminder_schedule_name"]
