"""tiktok_acquire の S3 出力を読む取得ソース（Adapter層）。

video_algorithm 等の「取得段」を、bot プロセス内スクレイプから tiktok_acquire(隔離Fargate)
の成果物読み出しへ差し替えるためのアダプタ。取得=隔離サービス / 分析=既存スキル の責務分界。

S3 prefix 配下（tiktok_acquire が書く）:
  posts.normalized.json   … 指標メタ（rank_display順）
  videos/manifest.json    … pid↔mp4↔tiktok_url の索引
  videos/<pid>.mp4        … 動画本体（上位N本のみ）
  thumbs/<pid>.jpg        … サムネ

VideoMeta には依存しない（呼び出し側スキルが dict→VideoMeta を写像する）。
boto3 は遅延import・失敗は例外を上げる（スキル側が cover-only へ縮退）。
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"


class TikTokS3Source:
    """1つの取得ジョブの S3 prefix を、posts(メタ) と videos(本体) として読む。"""

    def __init__(
        self, prefix: str, *, bucket: str | None = None, region: str | None = None
    ) -> None:
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"
        self._bucket = bucket or os.environ.get("TIKTOK_S3_BUCKET") or _DEFAULT_BUCKET
        self._region = region or os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._posts: list[dict[str, Any]] | None = None
        self._url_to_pid: dict[str, str] | None = None

    def _s3(self) -> Any:
        import boto3

        return boto3.session.Session().client("s3", region_name=self._region)

    def _get_json(self, s3: Any, key: str) -> Any:
        obj = s3.get_object(Bucket=self._bucket, Key=f"{self._prefix}{key}")
        return json.loads(obj["Body"].read().decode("utf-8"))

    def _ensure_loaded(self) -> None:
        if self._posts is not None:
            return
        s3 = self._s3()
        data = self._get_json(s3, "posts.normalized.json")
        self._posts = list(data.get("posts", []))
        url_to_pid: dict[str, str] = {}
        try:
            manifest = self._get_json(s3, "videos/manifest.json")
            for it in manifest.get("items", []):
                if it.get("tiktok_url") and it.get("downloaded"):
                    url_to_pid[it["tiktok_url"]] = it["pid"]
        except Exception as e:
            logger.info("tiktok_s3_manifest_skipped", error=type(e).__name__)
        self._url_to_pid = url_to_pid

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        """posts.normalized.json の items（rank_display順）。n で上位切り出し。"""
        self._ensure_loaded()
        posts = self._posts or []
        return posts[:n] if n else posts

    def download(self, url: str) -> tuple[bytes, str]:
        """tiktok_url から該当 videos/<pid>.mp4 を S3 から取得。未保存/失敗は例外。"""
        self._ensure_loaded()
        pid = (self._url_to_pid or {}).get(url)
        if not pid:
            raise FileNotFoundError(f"no downloaded video for url in manifest: {url[:60]}")
        s3 = self._s3()
        obj = s3.get_object(Bucket=self._bucket, Key=f"{self._prefix}videos/{pid}.mp4")
        return obj["Body"].read(), "video/mp4"
