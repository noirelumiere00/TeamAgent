"""動画を Gemini の inline 上限（約20MB）に収める proxy を ffmpeg で生成する。

編集者の納品動画（FIX mp4）は短尺でも高画質で 25〜50MB になることが多く、
Gemini の inline（bytes 直渡し）上限 ~20MB を超える。一次FB審査はテロップ/シーン/
NG の有無を見るだけなので、画質を落とした proxy で十分。長辺を 1280px に抑え、
CRF を段階的に上げて目標サイズ以下に収める。

3 層分離: Adapter 層。subprocess(ffmpeg) は他アダプタ（tiktok/rakko scraper）と同様。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import structlog

logger = structlog.get_logger(__name__)

# Gemini inline 上限(~20MB)の手前で安全マージンを取る
DEFAULT_LIMIT_MB = 18
# 段階的に品質を落とす (CRF 大=低画質小容量, 長辺 px)。先頭から試し、収まれば確定。
_LADDER: tuple[tuple[int, int], ...] = ((28, 1280), (30, 1080), (32, 854), (34, 720))
_FFMPEG_TIMEOUT_S = 180


class VideoProxyError(RuntimeError):
    """ffmpeg proxy 生成に失敗（マーカー文字列を含める）。"""


def ensure_under_limit(
    data: bytes,
    mime: str,
    *,
    limit_mb: int = DEFAULT_LIMIT_MB,
    request_id: str = "proxy",
) -> tuple[bytes, str]:
    """data が limit_mb 以下ならそのまま、超えていれば ffmpeg で縮めて返す。

    返り値は (bytes, mime)。proxy 化したら mime は "video/mp4"。
    ffmpeg が無い / 全段階で収まらない場合は VideoProxyError。
    """
    limit = limit_mb * 1024 * 1024
    if len(data) <= limit:
        return data, mime

    if not shutil.which("ffmpeg"):
        mb = len(data) / 1024 / 1024
        raise VideoProxyError(
            f"VIDEO_PROXY_NO_FFMPEG: 動画が大きく(>{mb:.0f}MB)、ffmpeg も無いため軽量化できません。"
        )

    last: bytes | None = None
    for crf, long_edge in _LADDER:
        out = _transcode(data, crf=crf, long_edge=long_edge, request_id=request_id)
        logger.info(
            "video_proxy_transcoded",
            request_id=request_id,
            crf=crf,
            long_edge=long_edge,
            in_mb=round(len(data) / 1024 / 1024, 1),
            out_mb=round(len(out) / 1024 / 1024, 1),
        )
        last = out
        if len(out) <= limit:
            return out, "video/mp4"

    # 全段階でも超過: 最小品質の結果を返す（Gemini 側が弾く可能性はあるが試行する）
    if last is not None:
        logger.warning("video_proxy_still_over_limit", request_id=request_id)
        return last, "video/mp4"
    raise VideoProxyError("VIDEO_PROXY_FFMPEG_FAILED: proxy を生成できませんでした")


def _transcode(data: bytes, *, crf: int, long_edge: int, request_id: str) -> bytes:
    """ffmpeg で長辺 long_edge・H.264/CRF の mp4 に変換して bytes を返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in")
        dst = os.path.join(tmp, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        # 長辺を long_edge に収める（向き不問・偶数化）。縦型9:16もそのまま縮む。
        vf = (
            f"scale=w={long_edge}:h={long_edge}:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            dst,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as e:
            raise VideoProxyError("VIDEO_PROXY_TIMEOUT: ffmpeg がタイムアウトしました") from e
        if proc.returncode != 0 or not os.path.exists(dst):
            tail = proc.stderr.decode("utf-8", "replace")[-300:] if proc.stderr else ""
            logger.warning("video_proxy_ffmpeg_failed", request_id=request_id, stderr_tail=tail)
            raise VideoProxyError("VIDEO_PROXY_FFMPEG_FAILED: ffmpeg 変換に失敗しました")
        with open(dst, "rb") as f:
            return f.read()
