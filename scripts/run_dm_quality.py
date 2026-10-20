#!/usr/bin/env python3
"""Slack読み取り→固定Haiku判定→本文を含まない週次JSON。社員への送信機能は持たない。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.dm_quality_reader import DmQualityReader, QualityExtraction
from teamagent.adapters.retry import RetryPolicy
from teamagent.dm_quality import (
    HAIKU_MODEL,
    HaikuJudge,
    measure,
    summarize,
    write_report,
)


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=ZoneInfo("Asia/Tokyo")) if result.tzinfo is None else result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", required=True, type=_date, help="包含始点。日付だけならJST 00:00"
    )
    parser.add_argument("--end", required=True, type=_date, help="排他的終点。10/20までなら10/21")
    parser.add_argument("--observed-until", type=_date, help="回答・票の観測締切。既定は起動時刻")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--budget-usd", default=15.0, type=float)
    args = parser.parse_args()
    now = datetime.now(UTC)
    observed = args.observed_until or now
    if not args.start < args.end <= observed <= now:
        parser.error("start < end <= observed-until <= now が必要です")
    if args.output.suffix != ".json":
        parser.error("output は集計JSONのパスを指定してください")
    required = ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"]
    if any(not os.environ.get(name, "").strip() for name in required):
        parser.error("SLACK_BOT_TOKEN / SLACK_TEAM_ID が必要です（値は表示しません）")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = args.output.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from slack_sdk import WebClient

            data = DmQualityReader(
                WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=30, retry_handlers=[])
            ).extract(
                start=args.start,
                end=args.end,
                observed_until=observed,
                expected_team=os.environ["SLACK_TEAM_ID"],
            )
            client = BedrockClient(
                region=os.environ.get("AWS_REGION", "ap-northeast-1"),
                model_id=HAIKU_MODEL,
                retry_policy=RetryPolicy(max_attempts=1),
            )
            report = measure(
                data,
                HaikuJudge(client),
                start=args.start,
                end=args.end,
                observed_until=observed,
                checkpoint=args.output.with_suffix(".checkpoint.json"),
                budget_usd=args.budget_usd,
            )
            write_report(args.output, report)
            print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
            return 0 if report.complete else 2
    except BlockingIOError:
        print("measurement_already_running", file=sys.stderr)
        return 2
    except Exception:
        # API例外・認証情報・会話を端末に出さない。
        failed = summarize(
            QualityExtraction(failures=["measurement_failed"]),
            {},
            start=args.start,
            end=args.end,
            observed_until=observed,
        )
        try:
            write_report(args.output, failed)
        except OSError:
            pass
        print("measurement_failed_no_confirmed_result", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
