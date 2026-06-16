"""生成済みHTMLレポートを非公開S3にアップし、署名付きURL（既定7日）を返す。

会社プロキシ下のローカルでも boto3→S3 は疎通する（Secrets Manager 等と同じ）。
バケットは非公開のまま、リンクを知る人だけ時限で閲覧できる署名付きURLを発行する
（恒久公開しない＝社外秘リスク最小）。失敗は graceful（None）。

env:
  VSEO_REPORT_BUCKET  既定 teamagent-dev-raw-files（既存の非公開バケット）
  VSEO_REPORT_PREFIX  既定 vseo-reports/
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"
_DEFAULT_PREFIX = "vseo-reports/"
_EXPIRES_S = 604800  # 7日（SigV4 署名付きURLの上限）


def _bucket_region(s3: Any, bucket: str) -> str:
    try:
        loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        return str(loc) if loc else "us-east-1"  # us-east-1 は None で返る
    except Exception:
        return os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-1"


def publish_file(
    path: str,
    *,
    content_type: str,
    ext: str,
    prefix: str | None = None,
    request_id: str = "vseo",
    query: str = "",
) -> str | None:
    """任意のローカルファイルを非公開S3に置き、署名付きGET URL（7日）を返す。失敗で None。

    HTML/PPTX 等を content_type/ext/prefix の差し替えで共通配布する（§Q-HTML→PPTX で流用）。
    """
    try:
        import boto3

        with open(path, "rb") as f:
            body = f.read()
    except Exception as e:
        logger.warning("report_publish_read_failed", request_id=request_id, error=type(e).__name__)
        return None
    if not body:
        return None
    bucket = os.environ.get("VSEO_REPORT_BUCKET") or _DEFAULT_BUCKET
    key_prefix = prefix or os.environ.get("VSEO_REPORT_PREFIX") or _DEFAULT_PREFIX
    key = f"{key_prefix}{uuid.uuid4().hex}{ext}"
    try:
        sess = boto3.session.Session()
        region = _bucket_region(sess.client("s3"), bucket)
        s3 = sess.client("s3", region_name=region)
        # ContentType を付けてブラウザでインライン表示/正しいDLにする（octet-stream を避ける）
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl="private, max-age=604800",
        )
        url: str = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=_EXPIRES_S
        )
        logger.info(
            "report_published",
            request_id=request_id,
            bucket=bucket,
            key=key,
            region=region,
            query=query[:60],
        )
        return url
    except Exception as e:
        logger.warning("report_publish_failed", request_id=request_id, error=type(e).__name__)
        return None


def publish_html_file(path: str, *, request_id: str = "vseo", query: str = "") -> str | None:
    """HTMLファイルを非公開S3に置き、署名付きGET URL（7日）を返す（publish_file の薄いラッパ）。"""
    return publish_file(
        path,
        content_type="text/html; charset=utf-8",
        ext=".html",
        request_id=request_id,
        query=query,
    )


# §Q-HTML→PPTX: 提案用 PPTX の配布（別 prefix・PPTX MIME）。
_PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PPTX_PREFIX = "vseo-proposals/"


def publish_pptx_file(path: str, *, request_id: str = "vseo", query: str = "") -> str | None:
    """提案用 PPTX を非公開S3に置き署名付きURL（7日）を返す。失敗で None。"""
    return publish_file(
        path,
        content_type=_PPTX_CT,
        ext=".pptx",
        prefix=_PPTX_PREFIX,
        request_id=request_id,
        query=query,
    )


def publish_pdf_file(path: str, *, request_id: str = "vseo", query: str = "") -> str | None:
    """提案用 PDF を非公開S3に置き署名付きURL（7日）を返す。失敗で None（PPTX と同 prefix）。"""
    return publish_file(
        path,
        content_type="application/pdf",
        ext=".pdf",
        prefix=_PPTX_PREFIX,
        request_id=request_id,
        query=query,
    )
