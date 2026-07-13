"""x_buzz_measure（X発話量 効果測定）の投函/照会アダプタ（3層: Adapter層）。

tiktok_task_store と同じ A′トポロジ:
  - submit: DynamoDB に queued を記録し、SQS にジョブJSONを送る（即return）
  - get_status: DynamoDB の状態を読む。done なら S3 の集計結果(JSON)を読み込んで返す
  - cache_report: status 照会時に生成したレポートURL/山分析を DynamoDB へキャッシュ
    （再照会のたびに Sonnet+HTML を再生成しない）
RunTask/PassRole は一切持たない（権限分離＝Lambda dispatcher 側）。
boto3 は遅延import・失敗は graceful。

env:
  X_TASK_QUEUE   SQSキューURL（submit先）
  X_JOBS_TABLE   DynamoDBテーブル名（状態）
  X_S3_BUCKET    成果物バケット（既定 teamagent-dev-raw-files）
  AWS_REGION     既定 ap-northeast-1
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"
_TTL_S = 2592000  # 30日


class XTaskStore:
    """SQS(送信) + DynamoDB(状態) + S3(結果読込) の薄いラッパ。"""

    def __init__(self) -> None:
        self._region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._queue_url = os.environ.get("X_TASK_QUEUE", "")
        self._table = os.environ.get("X_JOBS_TABLE", "")
        self._bucket = os.environ.get("X_S3_BUCKET") or _DEFAULT_BUCKET

    def _session(self) -> Any:
        import boto3

        return boto3.session.Session()

    # ---- submit -------------------------------------------------------------
    def submit(self, spec: dict[str, Any]) -> bool:
        """DynamoDBに queued を記録し、SQSにジョブJSONを送る。成功で True。"""
        if not self._queue_url or not self._table:
            logger.warning(
                "x_buzz_submit_misconfigured",
                has_queue=bool(self._queue_url),
                has_table=bool(self._table),
            )
            return False
        now = int(time.time())
        try:
            sess = self._session()
            ddb = sess.client("dynamodb", region_name=self._region)
            ddb.put_item(
                TableName=self._table,
                Item={
                    "job_id": {"S": spec["job_id"]},
                    "status": {"S": "queued"},
                    "created_at": {"N": str(now)},
                    "ttl": {"N": str(now + _TTL_S)},
                    "requested_by": {"S": str(spec.get("requested_by") or "unknown")},
                    "detail": {
                        "S": json.dumps(
                            {"status": "queued", "keyword": spec.get("keyword", "")},
                            ensure_ascii=False,
                        )
                    },
                },
            )
            sqs = sess.client("sqs", region_name=self._region)
            sqs.send_message(
                QueueUrl=self._queue_url, MessageBody=json.dumps(spec, ensure_ascii=False)
            )
            logger.info("x_buzz_submitted", job_id=spec["job_id"])
            return True
        except Exception as e:
            logger.warning("x_buzz_submit_failed", job_id=spec.get("job_id"), error=type(e).__name__)
            return False

    # ---- status -------------------------------------------------------------
    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """DynamoDBの状態を読む。未登録は None。読取障害は unknown を返す。"""
        if not self._table:
            return None
        try:
            sess = self._session()
            ddb = sess.client("dynamodb", region_name=self._region)
            resp = ddb.get_item(TableName=self._table, Key={"job_id": {"S": job_id}})
            item = resp.get("Item")
            if not item:
                return None
            detail: dict[str, Any] = {}
            try:
                detail = json.loads(item.get("detail", {}).get("S", "{}"))
            except Exception:
                detail = {}
            return {
                "job_id": job_id,
                "status": item.get("status", {}).get("S", "unknown"),
                "progress": detail.get("progress"),
                "error_code": detail.get("error_code"),
                "s3_prefix": detail.get("s3_prefix"),
                "total_cost_usd": detail.get("total_cost_usd", 0.0),
                "report_url": item.get("report_url", {}).get("S") or None,
                "spike_analysis": item.get("spike_analysis", {}).get("S") or "",
            }
        except Exception as e:
            logger.warning("x_buzz_status_failed", job_id=job_id, error=type(e).__name__)
            return {"job_id": job_id, "status": "unknown", "error_code": "STATUS_READ_FAILED"}

    def read_results(self, s3_prefix: str) -> dict[str, Any] | None:
        """done ジョブの集計結果（results.json）を S3 から読む。失敗は None。"""
        prefix = s3_prefix if s3_prefix.endswith("/") else s3_prefix + "/"
        try:
            s3 = self._session().client("s3", region_name=self._region)
            obj = s3.get_object(Bucket=self._bucket, Key=f"{prefix}results.json")
            data: dict[str, Any] = json.loads(obj["Body"].read().decode("utf-8"))
            return data
        except Exception as e:
            logger.warning("x_buzz_results_read_failed", prefix=s3_prefix, error=type(e).__name__)
            return None

    def cache_report(self, job_id: str, *, report_url: str, spike_analysis: str) -> None:
        """status 初回照会で生成したレポートURL/山分析をキャッシュ（失敗はWARNのみ）。"""
        if not self._table:
            return
        try:
            ddb = self._session().client("dynamodb", region_name=self._region)
            ddb.update_item(
                TableName=self._table,
                Key={"job_id": {"S": job_id}},
                UpdateExpression="SET report_url = :u, spike_analysis = :a",
                ExpressionAttributeValues={
                    ":u": {"S": report_url},
                    ":a": {"S": spike_analysis[:20000]},
                },
            )
        except Exception as e:
            logger.warning("x_buzz_cache_report_failed", job_id=job_id, error=type(e).__name__)


def new_job_id() -> str:
    return f"xb_{uuid.uuid4().hex[:12]}"


__all__ = ["XTaskStore", "new_job_id"]
