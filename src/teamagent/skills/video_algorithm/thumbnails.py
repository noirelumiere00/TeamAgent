"""サムネ画像（検索一覧タイル）の取得と色算出（ffmpeg + stdlib・新規依存ゼロ）。

検索一覧でクリックを取るのは「サムネの色/トーン」という発想に基づき、動画内の色では
なく **サムネ** を比較する。cover_url を httpx で取得 → ffmpeg で
 (a) 8x8 raw RGB に縮小して主要3色/明度01/暖寒を **純Python算出**、
 (b) 240px JPEG に縮小して base64 data URI 化（レポート埋込用）。
失敗は全段 graceful（None で続行）。Pillow/numpy は使わない。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile

import structlog

from teamagent.skills.video_algorithm.schema import ThumbColor

logger = structlog.get_logger(__name__)

_FFMPEG_TIMEOUT_S = 30
_FETCH_TIMEOUT_S = 12.0
_DISPLAY_W = 240  # 埋込サムネ幅(px)
_GRID = 8  # 8x8=64px から代表色を算出
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_cover(cover_url: str | None, *, request_id: str = "thumb") -> bytes | None:
    """cover_url を取得して画像 bytes を返す（graceful・失敗で None）。"""
    if not cover_url or not cover_url.startswith(("http://", "https://")):
        return None
    try:
        import httpx

        resp = httpx.get(
            cover_url,
            follow_redirects=True,
            timeout=_FETCH_TIMEOUT_S,
            headers={"User-Agent": _UA},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.info("thumb_fetch_nonok", request_id=request_id, status=resp.status_code)
    except Exception as e:  # ネットワーク/SSL/プロキシ等は握りつぶしてレポートを続行
        logger.warning("thumb_fetch_failed", request_id=request_id, error=type(e).__name__)
    return None


def build_thumb(
    cover_url: str | None, *, request_id: str = "thumb"
) -> tuple[str, ThumbColor] | None:
    """cover_url → (data_uri, ThumbColor)。取得 or 解析に失敗したら None。"""
    data = fetch_cover(cover_url, request_id=request_id)
    if not data:
        return None
    return analyze_cover(data, request_id=request_id)


def analyze_cover(data: bytes, *, request_id: str = "thumb") -> tuple[str, ThumbColor] | None:
    """画像 bytes → (240px base64 data_uri, ThumbColor)。フレーム bytes 流用も可。"""
    if not data or not shutil.which("ffmpeg"):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "cover")
            with open(src, "wb") as f:
                f.write(data)
            color = _compute_color(src, tmp, request_id)
            data_uri = _display_uri(src, tmp, request_id)
            if color is None and not data_uri:
                return None
            return data_uri, color or ThumbColor()
    except Exception as e:
        logger.warning("thumb_analyze_failed", request_id=request_id, error=type(e).__name__)
        return None


def _compute_color(src: str, tmp: str, request_id: str) -> ThumbColor | None:
    """8x8 raw RGB に縮小して主要色/明度/暖寒を算出。"""
    raw = os.path.join(tmp, "rgb.raw")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-vf",
        f"scale={_GRID}:{_GRID}:flags=area",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        raw,
    ]
    _ffmpeg(cmd, request_id)
    try:
        with open(raw, "rb") as fh:
            b = fh.read()
    except OSError:
        return None
    px = [(b[i], b[i + 1], b[i + 2]) for i in range(0, len(b) - 2, 3)]
    if not px:
        return None
    n = len(px)
    lum = sum(0.299 * r + 0.587 * g + 0.114 * bl for r, g, bl in px) / n
    warm = sum(r - bl for r, g, bl in px) / n
    return ThumbColor(
        swatches=_palette(px),
        brightness01=round(lum / 255.0, 3),
        warmth=round(max(-1.0, min(1.0, warm / 255.0)), 3),
    )


def _palette(px: list[tuple[int, int, int]]) -> list[str]:
    """画素を粗いビン（各色3bit=8階調）にまとめ、頻出 top3 を代表色 hex で返す。"""
    bins: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for r, g, bl in px:
        bins.setdefault((r >> 5, g >> 5, bl >> 5), []).append((r, g, bl))
    ranked = sorted(bins.values(), key=len, reverse=True)[:3]
    out: list[str] = []
    for group in ranked:
        m = len(group)
        rr = sum(p[0] for p in group) // m
        gg = sum(p[1] for p in group) // m
        bb = sum(p[2] for p in group) // m
        out.append(f"#{rr:02x}{gg:02x}{bb:02x}")
    return out


def _display_uri(src: str, tmp: str, request_id: str) -> str:
    """240px 幅の JPEG に縮小して base64 data URI 化（埋込用）。"""
    dst = os.path.join(tmp, "disp.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-vf",
        f"scale={_DISPLAY_W}:-2:flags=lanczos",
        "-q:v",
        "5",
        dst,
    ]
    _ffmpeg(cmd, request_id)
    try:
        with open(dst, "rb") as fh:
            b = fh.read()
    except OSError:
        return ""
    if not b:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")


def _ffmpeg(cmd: list[str], request_id: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("thumb_ffmpeg_timeout", request_id=request_id)
        return
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-160:] if proc.stderr else ""
        logger.info("thumb_ffmpeg_nonzero", request_id=request_id, stderr_tail=tail)
