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
  - **payload の PII 方針（2026-07-14 改訂）**: 参加者は載せない。予定タイトルは
    「本人の予定を本人 DM に出す」用途に限り short title（≤60字）を載せる。UX 上
    「何の予定か分からない」を解消するための意図的な方針変更（ユーザー要望）。
    緩和策: SQS は SSE 暗号化（reminders.tf）・Lambda はタイトルを CloudWatch に
    出さない（handler は件数ログのみ）・title は 60 字で切り詰め。
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
        title: str = "",
        end_iso: str = "",
        location: str = "",
    ) -> bool:
        """予定開始前リマインドのワンタイム schedule を登録する（冪等・fail-open）。

        payload: channel・開始時刻 HH:MM・リンク・short title（≤60字・本人DM表示用）。
        title は「本人の予定を本人 DM に出す」ためだけに載せる（2026-07-14・§docstring）。
        end_hm / loc は 2026-08-14 拡張（「実際の予定を表示させたい」）。旧 Lambda は
        未知キーを無視するだけなので配備順に依存しない。
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
        payload: dict[str, Any] = {"v": 1, "channel": channel, "start_hm": start_hm, "url": url}
        short_title = (title or "").strip().replace("\n", " ")
        if short_title:
            payload["title"] = short_title[:60]
        end_hm = ""
        try:
            if end_iso:
                parsed_end = _dt.datetime.fromisoformat(end_iso)
                if parsed_end.tzinfo is None:
                    parsed_end = parsed_end.replace(tzinfo=_JST)
                end_hm = parsed_end.astimezone(_JST).strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        if end_hm:
            payload["end_hm"] = end_hm
        short_loc = (location or "").strip().replace("\n", " ")
        if short_loc:
            payload["loc"] = short_loc[:60]
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
