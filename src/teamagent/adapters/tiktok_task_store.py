"""tiktok-acquire の投函/照会アダプタ（3層: Adapter層）。

OC(AiLa)の MCPツール(tiktok_acquire / tiktok_acquire_status)から呼ばれ、
  - submit: DynamoDBに queued を記録し、SQSにジョブJSONを送る（重処理ゼロ・即return）
  - get_status: DynamoDBの状態を読み、done なら S3 の成果物を署名付きURL化して返す
を行う。RunTask/PassRole は一切持たない（権限分離＝Lambda dispatcher 側）。
boto3 は遅延import・失敗は graceful（report_publish.py と同方針）。

env:
  TIKTOK_TASK_QUEUE   SQSキューURL（submit先）
  TIKTOK_JOBS_TABLE   DynamoDBテーブル名（状態）
  TIKTOK_S3_BUCKET    成果物バケット（既定 teamagent-dev-raw-files）
  AWS_REGION          既定 ap-northeast-1
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
_PRESIGN_S = 604800  # 7日（SigV4上限）
_TTL_S = 2592000  # 30日（DynamoDB項目のTTL）


class TikTokTaskStore:
    """SQS(送信) + DynamoDB(状態) + S3(署名URL) の薄いラッパ。"""

    def __init__(self) -> None:
        self._region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._queue_url = os.environ.get("TIKTOK_TASK_QUEUE", "")
        self._table = os.environ.get("TIKTOK_JOBS_TABLE", "")
        self._bucket = os.environ.get("TIKTOK_S3_BUCKET") or _DEFAULT_BUCKET

    def _session(self) -> Any:
        import boto3

        return boto3.session.Session()

    # ---- submit -------------------------------------------------------------
    def submit(self, spec: dict[str, Any]) -> bool:
        """DynamoDBに queued を記録し、SQSにジョブJSONを送る。成功で True。"""
        if not self._queue_url or not self._table:
            logger.warning(
                "tiktok_submit_misconfigured",
                has_queue=bool(self._queue_url),
                has_table=bool(self._table),
            )
            return False
        body = json.dumps(spec, ensure_ascii=False)
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
                            {"status": "queued", "keywords": spec.get("keywords", [])},
                            ensure_ascii=False,
                        )
                    },
                },
            )
            sqs = sess.client("sqs", region_name=self._region)
            sqs.send_message(QueueUrl=self._queue_url, MessageBody=body)
            logger.info("tiktok_submitted", job_id=spec["job_id"], kw=len(spec.get("keywords", [])))
            return True
        except Exception as e:
            logger.warning(
                "tiktok_submit_failed", job_id=spec.get("job_id"), error=type(e).__name__
            )
            return False

    # ---- status -------------------------------------------------------------
    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """DynamoDBの状態を読み、done なら S3 成果物を署名URL化して返す。未登録は None。"""
        if not self._table:
            return None
        try:
            sess = self._session()
            ddb = sess.client("dynamodb", region_name=self._region)
            resp = ddb.get_item(TableName=self._table, Key={"job_id": {"S": job_id}})
            item = resp.get("Item")
            if not item:
                return None
            status = item.get("status", {}).get("S", "unknown")
            detail: dict[str, Any] = {}
            try:
                detail = json.loads(item.get("detail", {}).get("S", "{}"))
            except Exception:
                detail = {}
            out: dict[str, Any] = {
                "job_id": job_id,
                "status": status,
                "progress": detail.get("progress"),
                "counts": detail.get("counts"),
                "error_code": detail.get("error_code"),
                "stop_reason": detail.get("stop_reason"),
                "warnings": detail.get("warnings") or [],
            }
            if status == "done":
                prefix = detail.get("s3_prefix") or f"tiktok-acquire/{job_id}/"
                out.update(self._presign_outputs(sess, prefix))
            return out
        except Exception as e:
            logger.warning("tiktok_status_failed", job_id=job_id, error=type(e).__name__)
            return {"job_id": job_id, "status": "unknown", "error_code": "STATUS_READ_FAILED"}

    def _presign_outputs(self, sess: Any, prefix: str) -> dict[str, Any]:
        """成果物(manifest/posts/動画/サムネ)を署名付きURL化する。"""
        s3 = sess.client("s3", region_name=self._region)
        prefix = prefix if prefix.endswith("/") else prefix + "/"

        def presign(key: str) -> str | None:
            try:
                return str(
                    s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self._bucket, "Key": key},
                        ExpiresIn=_PRESIGN_S,
                    )
                )
            except Exception:
                return None

        result: dict[str, Any] = {
            "s3_bucket": self._bucket,
            "s3_prefix": prefix,
            "posts_json_url": presign(f"{prefix}posts.normalized.json"),
            "config_json_url": presign(f"{prefix}config.json"),
            "manifest_url": presign(f"{prefix}videos/manifest.json"),
            "videos": [],
        }
        # manifest を読んで動画/サムネを s3_key + 署名URL の2系統で返す
        try:
            obj = s3.get_object(Bucket=self._bucket, Key=f"{prefix}videos/manifest.json")
            manifest = json.loads(obj["Body"].read().decode("utf-8"))
            vids: list[dict[str, Any]] = []
            for it in manifest.get("items", []):
                vkey = f"{prefix}{it.get('video_path')}"
                tkey = f"{prefix}{it.get('thumb_path')}"
                vids.append(
                    {
                        "pid": it.get("pid"),
                        "kw": it.get("kw"),
                        "downloaded": it.get("downloaded", False),
                        "s3_key": vkey,  # 機械処理(動画分析)用＝直渡し
                        "url": presign(vkey) if it.get("downloaded") else None,  # 人向け
                        "thumb_url": presign(tkey),
                        "tiktok_url": it.get("tiktok_url"),
                    }
                )
            result["videos"] = vids
        except Exception as e:
            logger.info("tiktok_manifest_read_skipped", error=type(e).__name__)
        return result


def new_job_id() -> str:
    return f"tk_{uuid.uuid4().hex[:12]}"
