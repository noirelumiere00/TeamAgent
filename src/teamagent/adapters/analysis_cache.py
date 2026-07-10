"""Gemini 動画分析結果の S3 キャッシュ（v0.3 Task 10・FinOps）。

同一動画×同一プロンプトの再分析を回避する（Gemini は Vertex/GCP 課金＝AWS Budgets の
監視対象外であり、本キャッシュ＋クォータが唯一のガード＝#154 との補完関係）。

キー設計（監査指摘どおり二本立て・レビュー済みの落とし穴に対応）:
  - YouTube 経路: **動画IDの正規化**（watch?v= / youtu.be/ / shorts/ を同一視）。
    file_uri 直渡しで bytes を持たないため URL 正規化キーを使う
  - DL 経路: **コンテンツ sha256**（同一動画の別URL・再アップロードも同一視）
  - どちらも prompt_version / model_id / focus をキーに含める（含めないと
    プロンプト更新・モデル更新後も古い分析を返し続ける事故になる）

保存するのは **Gemini 出力テキストのみ**（動画 bytes は現行設計どおり即破棄＝
著作権配慮を崩さない）。S3 キーはハッシュのみ（生 URL を鍵に使わない＝§3 Don't の精神）。
fail-open: get/put の失敗は分析本体を止めない。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"
_DEFAULT_PREFIX = "analysis-cache/"

# YouTube 動画IDの抽出（watch?v= / youtu.be/ / shorts/ / embed/）。
_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{6,20})"
)


def normalize_video_url(url: str) -> str | None:
    """URL からキャッシュ基底（正規化キー）を作る。YouTube は動画IDへ正規化。

    YouTube 以外は None（＝URL キャッシュ不可。DL 経路のコンテンツハッシュに委ねる。
    TikTok 等はトラッキングパラメータ・短縮URL・再配信で同一動画でも URL が揺れるため、
    生 URL キーは誤ヒットより取りこぼし側に倒す）。
    """
    m = _YT_ID_RE.search(url or "")
    return f"yt:{m.group(1)}" if m else None


def content_basis(data: bytes) -> str:
    """DL 経路のキャッシュ基底（動画 bytes の sha256）。"""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True)
class CachedAnalysis:
    """キャッシュヒットの内容（分析テキスト＋来歴）。"""

    text: str
    model_id: str
    original_cost_usd: float


class AnalysisCache:
    """S3 上の分析結果キャッシュ（ANALYSIS_CACHE_ENABLED=1 のときのみ有効・既定OFF）。"""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket or os.environ.get("ANALYSIS_CACHE_BUCKET") or _DEFAULT_BUCKET
        self._prefix = prefix or os.environ.get("ANALYSIS_CACHE_PREFIX") or _DEFAULT_PREFIX
        self._client = client

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("ANALYSIS_CACHE_ENABLED", "").strip().lower() in {"1", "true", "yes"}

    def _ensure_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    @staticmethod
    def cache_key(*, basis: str, prompt_version: str, model_id: str, focus: str = "") -> str:
        """決定的キー（生 URL/生 focus 文字列は S3 キーに露出させない）。"""
        raw = f"{basis}|{prompt_version}|{model_id}|{focus.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, *, request_id: str) -> CachedAnalysis | None:
        start = time.perf_counter()
        try:
            resp = self._ensure_client().get_object(
                Bucket=self._bucket, Key=f"{self._prefix}{key}.json"
            )
            payload = json.loads(resp["Body"].read().decode("utf-8"))
            text = str(payload.get("text") or "")
            if not text:
                return None
            logger.info(
                "analysis_cache_hit",
                request_id=request_id,
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
            return CachedAnalysis(
                text=text,
                model_id=str(payload.get("model_id") or ""),
                original_cost_usd=float(payload.get("cost_usd") or 0.0),
            )
        except Exception as e:
            if type(e).__name__ not in ("NoSuchKey", "ClientError"):
                logger.warning(
                    "analysis_cache_get_failed", request_id=request_id, error=type(e).__name__
                )
            return None  # miss / 障害 いずれも fail-open（分析本体へ進む）

    def put(self, key: str, *, text: str, model_id: str, cost_usd: float, request_id: str) -> None:
        if not text:
            return
        try:
            body = json.dumps(
                {"text": text, "model_id": model_id, "cost_usd": cost_usd},
                ensure_ascii=False,
            ).encode("utf-8")
            self._ensure_client().put_object(
                Bucket=self._bucket,
                Key=f"{self._prefix}{key}.json",
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
            logger.info("analysis_cache_put", request_id=request_id)
        except Exception as e:  # fail-open
            logger.warning(
                "analysis_cache_put_failed", request_id=request_id, error=type(e).__name__
            )


__all__ = ["AnalysisCache", "CachedAnalysis", "content_basis", "normalize_video_url"]
