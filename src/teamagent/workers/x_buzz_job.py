"""x_buzz_measure（④効果測定）の使い捨て Fargate ワーカー。

Lambda dispatcher が SQS からデキューし、ECS RunTask の containerOverrides で
env X_JOB_JSON にジョブ仕様を注入して本モジュールを起動する:
    python -m teamagent.workers.x_buzz_job

処理: 期間を1日ずつ分割して apidojo/tweet-scraper で取得（厳密な件数比較のための
カタログ裁定）→ 日別発話数 / バズ投稿TOP10全文 を S3 `<s3_prefix>results.json` へ書き、
DynamoDB を running(progress) → done/failed に更新する。chromium/yt-dlp 不要の軽量
Python（0.5vCPU/1GB で足りる）。

日単位の取得失敗は degrade（count=-1 で欠測記録・全体は止めない）。
コストは CostGuard(check/record) が月次台帳に記帳（予算超過は即 failed）。

env:
  X_JOB_JSON       ジョブ仕様(JSON)
  X_JOBS_TABLE     DynamoDB テーブル
  X_S3_BUCKET      成果物バケット（既定 teamagent-dev-raw-files）
  APIFY_API_TOKEN  Apify トークン（Secrets 注入）
  COST_GUARD_TABLE 任意（あれば記帳）
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from dataclasses import asdict
from typing import Any

import structlog

from teamagent.adapters.apify_client import ApifyClient, ApifyError
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceeded

logger = structlog.get_logger(__name__)

_TOP_N = 10
_PER_DAY_DEADLINE_S = 150  # 1日分の actor 実行デッドライン（62日でも合計 ~2.5h 以内）


def _ddb() -> Any:
    import boto3

    return boto3.session.Session().client(
        "dynamodb", region_name=os.environ.get("AWS_REGION") or "ap-northeast-1"
    )


def _update_status(table: str, job_id: str, status: str, detail: dict[str, Any]) -> None:
    try:
        _ddb().update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression="SET #s = :s, detail = :d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":d": {"S": json.dumps({"status": status, **detail}, ensure_ascii=False)},
            },
        )
    except Exception as e:
        logger.warning("x_buzz_worker_status_update_failed", job_id=job_id, error=type(e).__name__)


def _put_s3(bucket: str, key: str, body: str, content_type: str) -> None:
    import boto3

    boto3.session.Session().client(
        "s3", region_name=os.environ.get("AWS_REGION") or "ap-northeast-1"
    ).put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType=content_type)


def run_job(spec: dict[str, Any], *, apify: ApifyClient | None = None) -> int:
    """ジョブ本体（apify はテスト注入用。None なら env から構築）。"""
    job_id = str(spec["job_id"])
    table = os.environ.get("X_JOBS_TABLE", "")
    bucket = os.environ.get("X_S3_BUCKET") or "teamagent-dev-raw-files"
    s3_prefix = str(spec.get("s3_prefix") or f"x-research/{job_id}/")
    keyword = str(spec["keyword"])
    start = _dt.date.fromisoformat(str(spec["start_date"]))
    end = _dt.date.fromisoformat(str(spec["end_date"]))
    max_per_day = int(spec.get("max_items_per_day", 100))
    min_faves = int(spec.get("min_faves", 0))
    requested_by = str(spec.get("requested_by") or "unknown")
    request_id = str(spec.get("request_id") or job_id)
    days_total = (end - start).days + 1

    apify = apify or ApifyClient.from_env(ledger=CostGuard.from_env())
    daily_counts: list[dict[str, Any]] = []
    all_posts: list[dict[str, Any]] = []
    total_cost = 0.0
    failed_days: list[str] = []
    started = time.monotonic()

    for i in range(days_total):
        day = (start + _dt.timedelta(days=i)).isoformat()
        try:
            posts, cost = apify.search_posts_period(
                [keyword],
                start=day,
                end=day,
                minimum_favorites=min_faves,
                max_items=max_per_day,
                deadline_s=_PER_DAY_DEADLINE_S,
                request_id=request_id,
                user_email=requested_by,
            )
            total_cost += cost
            daily_counts.append({"date": day, "count": len(posts)})
            all_posts.extend(asdict(p) for p in posts)
        except CostLimitExceeded as e:
            _update_status(
                table,
                job_id,
                "failed",
                {
                    "error_code": "COST_LIMIT",
                    "stop_reason": str(e),
                    "s3_prefix": s3_prefix,
                    "total_cost_usd": round(total_cost, 4),
                },
            )
            logger.warning("x_buzz_worker_cost_limit", job_id=job_id)
            return 2
        except ApifyError as e:
            # 欠測として記録し続行（1日の失敗で全体を止めない）
            daily_counts.append({"date": day, "count": -1})
            failed_days.append(day)
            logger.warning(
                "x_buzz_worker_day_failed", job_id=job_id, day=day, error=str(e)[:120]
            )
        _update_status(
            table,
            job_id,
            "running",
            {
                "progress": {"days_done": i + 1, "days_total": days_total},
                "s3_prefix": s3_prefix,
                "total_cost_usd": round(total_cost, 4),
            },
        )

    top_posts = sorted(all_posts, key=lambda p: int(p.get("like_count", 0)), reverse=True)[:_TOP_N]
    # 欠測(-1)は 0 扱いでなく除外もせず、そのまま可視化側で扱えるよう 0 に落とし警告を残す
    clean_daily = [
        {"date": d["date"], "count": max(0, int(d["count"]))} for d in daily_counts
    ]
    results = {
        "spec": {
            "keyword": keyword,
            "start_date": str(spec["start_date"]),
            "end_date": str(spec["end_date"]),
            "campaign_date": spec.get("campaign_date"),
            "min_faves": min_faves,
        },
        "daily_counts": clean_daily,
        "top_posts": top_posts,
        "failed_days": failed_days,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_s": int(time.monotonic() - started),
    }
    try:
        _put_s3(
            bucket,
            f"{s3_prefix}results.json",
            json.dumps(results, ensure_ascii=False),
            "application/json; charset=utf-8",
        )
        _put_s3(
            bucket,
            f"{s3_prefix}posts.jsonl",
            "\n".join(json.dumps(p, ensure_ascii=False) for p in all_posts),
            "application/x-ndjson; charset=utf-8",
        )
    except Exception as e:
        _update_status(
            table,
            job_id,
            "failed",
            {"error_code": "S3_WRITE_FAILED", "stop_reason": type(e).__name__},
        )
        return 3

    _update_status(
        table,
        job_id,
        "done",
        {
            "progress": {"days_done": days_total, "days_total": days_total},
            "s3_prefix": s3_prefix,
            "total_cost_usd": round(total_cost, 4),
            "warnings": [f"取得失敗日: {', '.join(failed_days)}"] if failed_days else [],
        },
    )
    logger.info(
        "x_buzz_worker_done",
        job_id=job_id,
        days=days_total,
        posts=len(all_posts),
        cost_usd=round(total_cost, 4),
    )
    return 0


def main() -> int:
    raw = os.environ.get("X_JOB_JSON", "")
    if not raw:
        logger.error("x_buzz_worker_no_job_json")
        return 1
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("x_buzz_worker_bad_job_json")
        return 1
    try:
        return run_job(spec)
    except Exception as e:
        table = os.environ.get("X_JOBS_TABLE", "")
        job_id = str(spec.get("job_id") or "unknown")
        _update_status(
            table, job_id, "failed", {"error_code": "WORKER_CRASH", "stop_reason": type(e).__name__}
        )
        logger.error("x_buzz_worker_crashed", job_id=job_id, error=type(e).__name__)
        return 4


if __name__ == "__main__":
    sys.exit(main())
