"""生成済みHTMLレポートを非公開S3にアップし、署名付きURL（既定7日）を返す。

会社プロキシ下のローカルでも boto3→S3 は疎通する（Secrets Manager 等と同じ）。
バケットは非公開のまま、リンクを知る人だけ時限で閲覧できる署名付きURLを発行する
（恒久公開しない＝社外秘リスク最小）。失敗は graceful（None）。

env:
  VSEO_REPORT_BUCKET  既定 teamagent-dev-raw-files（既存の非公開バケット）
  VSEO_REPORT_PREFIX  既定 vseo-reports/
"""

from __future__ import annotations

import datetime as _dt
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"
_DEFAULT_PREFIX = "vseo-reports/"
_EXPIRES_S = 604800  # 7日（SigV4 署名付きURLの上限）


# 「7日有効」と言い切るための最低線。これを下回る presigned は握ったまま渡してはいけない。
_MIN_ACCEPTABLE_S = 6 * 24 * 3600


def presigned_expiry_epoch(url: str) -> int | None:
    """presigned URL の失効時刻（epoch秒）。判定できなければ None。

    SigV2 は ``Expires``（epoch）、SigV4 は ``X-Amz-Date`` + ``X-Amz-Expires``（相対秒）。
    """
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    if "Expires" in q:
        try:
            return int(q["Expires"][0])
        except (ValueError, IndexError):
            return None
    if "X-Amz-Date" in q and "X-Amz-Expires" in q:
        try:
            start = _dt.datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=_dt.UTC
            )
            return int(start.timestamp()) + int(q["X-Amz-Expires"][0])
        except (ValueError, IndexError):
            return None
    return None


def presigned_lifetime_s(url: str, *, now: int | None = None) -> int | None:
    """presigned URL の残り寿命（秒）。判定できなければ None。"""
    expiry = presigned_expiry_epoch(url)
    if expiry is None:
        return None
    current = now if now is not None else int(_dt.datetime.now(_dt.UTC).timestamp())
    return expiry - current


def _warn_if_temporary_credentials(session: Any, *, request_id: str) -> None:
    """一時認証情報で署名している場合に警告する（presigned の実効寿命がトークン依存になる）。"""
    try:
        credentials = session.get_credentials()
        token = getattr(credentials, "token", None) if credentials else None
    except Exception:  # 資格情報の取り方は環境依存。判定できないなら黙る。
        return
    if token:
        logger.warning(
            "report_presign_temporary_credentials",
            request_id=request_id,
            hint="一時認証情報(STS)で署名したため、URL の実効寿命はセッション寿命で頭打ちになる"
            "（X-Amz-Expires の値は当てにならない）。人へは /r 短縮URLを渡すこと",
        )


def _bucket_region(s3: Any, bucket: str) -> str:
    try:
        loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        return str(loc) if loc else "us-east-1"  # us-east-1 は None で返る
    except Exception:
        return os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-1"


@dataclass(frozen=True)
class PublishedObject:
    """発行結果: presigned GET URL に加え bucket/key/region を返す（短縮URLトークン生成用）。"""

    url: str
    bucket: str
    key: str
    region: str = ""  # 発行時に解決したバケットリージョン（/r の presign 再生成で使う）


def _put_and_presign(
    body: bytes,
    *,
    content_type: str,
    ext: str,
    prefix: str | None,
    request_id: str,
    query: str,
    bucket: str | None = None,
) -> PublishedObject | None:
    """body を非公開S3へ置き、PublishedObject(url, bucket, key) を返す。失敗で None。

    publish_file / *_result 系の共通コア（put_object + presign を1か所に集約）。
    """
    if not body:
        return None
    try:
        import boto3

        bkt = bucket or os.environ.get("VSEO_REPORT_BUCKET") or _DEFAULT_BUCKET
        key_prefix = prefix or os.environ.get("VSEO_REPORT_PREFIX") or _DEFAULT_PREFIX
        key = f"{key_prefix}{uuid.uuid4().hex}{ext}"
        sess = boto3.session.Session()
        region = _bucket_region(sess.client("s3"), bkt)
        s3 = sess.client("s3", region_name=region)
        # ContentType を付けてブラウザでインライン表示/正しいDLにする（octet-stream を避ける）
        s3.put_object(
            Bucket=bkt,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl="private, max-age=604800",
        )
        url: str = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bkt, "Key": key}, ExpiresIn=_EXPIRES_S
        )
        # ⚠️ ExpiresIn に 7日 を渡しても、**一時認証情報(STS)で署名すると実効寿命はトークン側で
        # 頭打ち**になる（実測 2026-09-01: /r のリダイレクト先が約30分で AccessDenied）。
        # 検出は2段構え:
        #   (1) 署名クエリから読める寿命（SigV2 の Expires はここで短く出る）
        #   (2) **一時認証情報かどうか**（SigV4 の X-Amz-Expires は要求値がそのまま書かれ、
        #       トークン失効は opaque な X-Amz-Security-Token 側なので (1) では見えない）
        # 配信自体は止めない（/r 経路は開くたびに署名し直すため実害が無い）。
        _warn_if_temporary_credentials(sess, request_id=request_id)
        lifetime = presigned_lifetime_s(url)
        if lifetime is not None and lifetime < _MIN_ACCEPTABLE_S:
            logger.warning(
                "report_presign_shortlived",
                request_id=request_id,
                lifetime_s=lifetime,
                hint="一時認証情報で署名されたため7日に満たない。"
                "presigned を人へ直接渡さず /r 短縮URL（開く度に再署名）で配布すること",
            )
        # query（商材/テーマ/keyword＝機密でありうる）は CloudWatch に残さない。key は uuid で
        # 商材名を含まないため bucket/key/region のみ記録する（有無だけ has_query で可観測化）。
        logger.info(
            "report_published",
            request_id=request_id,
            bucket=bkt,
            key=key,
            region=region,
            has_query=bool(query),
        )
        return PublishedObject(url=url, bucket=bkt, key=key, region=region)
    except Exception as e:
        logger.warning("report_publish_failed", request_id=request_id, error=type(e).__name__)
        return None


def _read_file(path: str, *, request_id: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.warning("report_publish_read_failed", request_id=request_id, error=type(e).__name__)
        return None


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
    戻り値は従来どおり URL 文字列（bucket/key も要る場合は publish_file_result を使う）。
    """
    result = publish_file_result(
        path,
        content_type=content_type,
        ext=ext,
        prefix=prefix,
        request_id=request_id,
        query=query,
    )
    return result.url if result else None


def publish_file_result(
    path: str,
    *,
    content_type: str,
    ext: str,
    prefix: str | None = None,
    request_id: str = "vseo",
    query: str = "",
) -> PublishedObject | None:
    """publish_file と同じS3発行を行い、url に加え bucket/key を返す（短縮URL化用の純加算）。"""
    body = _read_file(path, request_id=request_id)
    if body is None:
        return None
    return _put_and_presign(
        body,
        content_type=content_type,
        ext=ext,
        prefix=prefix,
        request_id=request_id,
        query=query,
    )


def publish_text(
    text: str,
    *,
    content_type: str = "application/json; charset=utf-8",
    ext: str = ".json",
    prefix: str | None = None,
    bucket: str | None = None,
    request_id: str = "payload",
) -> str | None:
    """テキスト（JSON等）を非公開S3へ直接置き、署名付きGET URL（7日）を返す。失敗で None。

    v0.3 Task8（長文ペイロード退避）用の小拡張。publish_file と同じ bucket/署名規約。
    ファイルを経由しない（一時ファイルの掃除漏れ・競合を持たない）。
    """
    if not text:
        return None
    try:
        import boto3

        bucket = bucket or os.environ.get("VSEO_REPORT_BUCKET") or _DEFAULT_BUCKET
        key_prefix = prefix or os.environ.get("VSEO_REPORT_PREFIX") or _DEFAULT_PREFIX
        key = f"{key_prefix}{uuid.uuid4().hex}{ext}"
        sess = boto3.session.Session()
        region = _bucket_region(sess.client("s3"), bucket)
        s3 = sess.client("s3", region_name=region)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType=content_type,
            CacheControl="private, max-age=604800",
        )
        url: str = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=_EXPIRES_S
        )
        logger.info("report_published", request_id=request_id, bucket=bucket, key=key)
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


def publish_bytes_result(
    body: bytes,
    *,
    content_type: str,
    ext: str,
    prefix: str | None = None,
    request_id: str = "asset",
    bucket: str | None = None,
) -> PublishedObject | None:
    """任意の bytes（画像等）を非公開S3へ置き、PublishedObject を返す。失敗で None。

    レポートに埋める画像アセット用。``query`` はログに残さないため受け取らない。

    **バケットは明示必須**（引数か ``VSEO_REPORT_BUCKET``）。既定バケットへ暗黙に落とすと、
    ローカル/設定漏れの環境から本番バケットへサムネや PPTX を書いてしまう。HTML 本体は
    ``publish_report`` が同じ検査をしているので、アセット側だけ緩いと迂回路になる。
    """
    if not (bucket or os.environ.get("VSEO_REPORT_BUCKET")):
        logger.info("report_asset_skipped_no_bucket", request_id=request_id)
        return None
    return _put_and_presign(
        body,
        content_type=content_type,
        ext=ext,
        prefix=prefix,
        request_id=request_id,
        query="",
        bucket=bucket,
    )


def publish_html_file_result(
    path: str, *, request_id: str = "vseo", query: str = ""
) -> PublishedObject | None:
    """HTMLファイルを非公開S3に置き、PublishedObject(url, bucket, key) を返す（短縮URL化用）。"""
    return publish_file_result(
        path,
        content_type="text/html; charset=utf-8",
        ext=".html",
        request_id=request_id,
        query=query,
    )


def presign_get(
    bucket: str, key: str, *, expires_s: int = _EXPIRES_S, region: str | None = None
) -> str | None:
    """既存S3オブジェクトの presigned GET URL を都度生成する（connect-web /r 用）。失敗で None。

    region を明示（既定は AWS_REGION）することで get_bucket_location 呼び出しを避け、
    connect-web タスクロールに GetBucketLocation 権限を不要化する（最小権限）。
    """
    try:
        import boto3

        rgn = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "ap-northeast-1"
        )
        s3 = boto3.session.Session().client("s3", region_name=rgn)
        url: str = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(expires_s)
        )
        return url
    except Exception as e:
        logger.warning("report_presign_failed", error=type(e).__name__)
        return None


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


# ── HTML-first 統合（docs/v3.2/html_first_proposal_strategy.md Phase I）──────────────
# 資料種別 → (content_type, ext, prefix) を 1 か所に集約。どのスキルも
# publish_artifact(path, kind=...) で同一経路に乗せられる（個別の publish_* は後方互換で据置）。
@dataclass(frozen=True)
class _ArtifactSpec:
    content_type: str
    ext: str
    prefix: str


ARTIFACT_KINDS: dict[str, _ArtifactSpec] = {
    "report_html": _ArtifactSpec("text/html; charset=utf-8", ".html", _DEFAULT_PREFIX),
    "slides_html": _ArtifactSpec("text/html; charset=utf-8", ".html", _DEFAULT_PREFIX),
    "proposal_html": _ArtifactSpec("text/html; charset=utf-8", ".html", _PPTX_PREFIX),
    "pptx": _ArtifactSpec(_PPTX_CT, ".pptx", _PPTX_PREFIX),
    "pdf": _ArtifactSpec("application/pdf", ".pdf", _PPTX_PREFIX),
}


def publish_artifact(
    path: str, kind: str, *, request_id: str = "vseo", query: str = ""
) -> str | None:
    """資料を kind 別の content_type/ext/prefix で非公開S3へ発行し署名付きURL（7日）を返す。

    kind ∈ ARTIFACT_KINDS（report_html/slides_html/proposal_html/pptx/pdf）。
    未知の kind は ValueError。HTML-first 統合の単一入口（既存 publish_* ラッパの上位の汎用版）。
    """
    spec = ARTIFACT_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown artifact kind: {kind!r} (allowed: {sorted(ARTIFACT_KINDS)})")
    return publish_file(
        path,
        content_type=spec.content_type,
        ext=spec.ext,
        prefix=spec.prefix,
        request_id=request_id,
        query=query,
    )


def publish_artifact_result(
    path: str, kind: str, *, request_id: str = "vseo", query: str = ""
) -> PublishedObject | None:
    """publish_artifact の PublishedObject 版（短縮URL /r 化用）。

    生の署名付きURLは ECS タスクロールの一時 credential で署名されるため、宣言上の 7 日
    ではなく**セッション残り（最大1時間）で失効する**（2026-08-31 実測: MaxSessionDuration
    =3600）。受け手に渡せる寿命が要る呼び出し側は、この結果を
    ``skills/_shared/report_delivery.delivery_url()`` へ通して /r 短縮URLにすること。
    """
    spec = ARTIFACT_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown artifact kind: {kind!r} (allowed: {sorted(ARTIFACT_KINDS)})")
    return publish_file_result(
        path,
        content_type=spec.content_type,
        ext=spec.ext,
        prefix=spec.prefix,
        request_id=request_id,
        query=query,
    )
