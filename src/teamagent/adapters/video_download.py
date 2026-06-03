"""動画ダウンロード (yt-dlp)。

Gemini の file_uri は YouTube 系しか直接取得できない (TikTok/Instagram は
URL_ROBOTED で拒否される)。そこで TikTok/IG 等は yt-dlp で一時ダウンロードし、
bytes を Gemini に inline で渡す。

著作権・ToS への配慮 (仕様 §7.2 / S11.6):
- ダウンロードした動画は **分析のためだけの一時取得**で、メモリに読み込んだら
  即座に一時ディレクトリごと破棄する (ディスクに残さない)。
- 公開動画の分析目的での取得。再配布・保存はしない。

3 層分離: Adapter 層。Skill からは download_video() のみ使う。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_MIME_BY_EXT: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


class VideoDownloadError(RuntimeError):
    """動画ダウンロード失敗。呼び出し側がユーザー向け案内に変換する。"""


def download_video(
    url: str,
    *,
    max_filesize_mb: int = 20,
    request_id: str | None = None,
) -> tuple[bytes, str]:
    """動画 URL を一時ダウンロードして (bytes, mime_type) を返す。

    Gemini inline の実用上限に収めるため max_filesize_mb でサイズを制限する
    (TikTok/IG のショート動画は通常 2〜10MB)。一時ディレクトリは関数終了時に
    自動削除され、動画はディスクに残らない。

    Raises:
        VideoDownloadError: 取得不可 / 上限超過 / フォーマット無し等。
    """
    import yt_dlp  # 遅延 import (heavy)

    with tempfile.TemporaryDirectory(prefix="teamagent_vdl_") as tmpdir:
        outtmpl = os.path.join(tmpdir, "v.%(ext)s")
        ydl_opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            # サイズ制限内の最良画質。無ければ mp4/best、最後は worst まで降りて取り切る
            "format": f"best[filesize<{max_filesize_mb}M]/mp4/best/worst",
            "max_filesize": max_filesize_mb * 1024 * 1024,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,  # 一過性ネットワーク揺れの再試行（2→3）
            "fragment_retries": 3,  # HLS/DASH 断片の transient 救済
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            # 生 URL はログに残さない (CLAUDE.md 6-bis)
            logger.warning("video_download_failed", request_id=request_id, error=type(e).__name__)
            raise VideoDownloadError(
                f"VIDEO_DOWNLOAD_FAILED: 動画を取得できませんでした ({type(e).__name__})"
            ) from e

        files = [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl"))]
        if not files:
            # max_filesize 超過などでファイルが残らないケース
            raise VideoDownloadError(
                "VIDEO_DOWNLOAD_FAILED: 取得できる動画がありませんでした"
                f"（{max_filesize_mb}MB 上限超過の可能性）"
            )
        path = os.path.join(tmpdir, files[0])
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        mime = _MIME_BY_EXT.get(ext, "video/mp4")

    logger.info(
        "video_downloaded",
        request_id=request_id,
        size_mb=round(len(data) / 1024 / 1024, 2),
        mime=mime,
    )
    return data, mime
