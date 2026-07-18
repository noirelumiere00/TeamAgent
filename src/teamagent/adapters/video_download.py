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

import hashlib
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
    _skip_url_guard: bool = False,
) -> tuple[bytes, str]:
    """動画 URL を一時ダウンロードして (bytes, mime_type) を返す。

    Gemini inline の実用上限に収めるため max_filesize_mb でサイズを制限する
    (TikTok/IG のショート動画は通常 2〜10MB)。一時ディレクトリは関数終了時に
    自動削除され、動画はディスクに残らない。

    `_skip_url_guard=True` は download_video_chained が外側で SSRF 検証済みのときに使う
    （二重 DNS 解決の回避）。直接呼ぶ場合は False のまま＝必ず SSRF 関門を通す。

    会社プロキシ下では `HTTPS_PROXY` を yt-dlp に渡し、CA バンドルは `SSL_CERT_FILE`/
    `REQUESTS_CA_BUNDLE` を yt-dlp の内部 urllib が自動採用する（追加コードは proxy のみ）。

    Raises:
        VideoDownloadError: URL 非許可(SSRF) / 取得不可 / 上限超過 / フォーマット無し等。
    """
    from teamagent.adapters.media_job import MediaJobClient

    if MediaJobClient.is_configured():
        fingerprint = hashlib.sha256(f"{url}\0{max_filesize_mb}".encode()).hexdigest()
        try:
            return MediaJobClient().acquire_video(
                url,
                request_fingerprint=f"{request_id or 'no-request'}:download:{fingerprint}",
                max_bytes=max_filesize_mb * 1024 * 1024,
            )
        except Exception as exc:
            raise VideoDownloadError(f"VIDEO_MEDIA_JOB_FAILED: {type(exc).__name__}") from exc
    if not MediaJobClient.local_runtime_enabled():
        raise VideoDownloadError("VIDEO_MEDIA_JOB_NOT_CONFIGURED")

    # §N: SSRF 必須関門。全 DL 経路（video_analysis/video_algorithm）が必ず通る backstop。
    if not _skip_url_guard:
        from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

        try:
            url = validate_scrape_url(url, request_id=request_id)
        except UrlGuardError as e:
            # 生 URL はログに残さない（message に URL は含まれない）
            logger.warning("video_download_url_blocked", request_id=request_id, reason=str(e))
            raise VideoDownloadError(f"VIDEO_URL_BLOCKED: {e}") from e

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
        # 会社プロキシ下のみ proxy を明示注入（本番EC2=未設定→キー無し＝通常DL）。
        _proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if _proxy:
            ydl_opts["proxy"] = _proxy
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


# 3層DLチェーンの取得経路名（VIDEO_DL_ORDER で順序を切替）。cover-only は skill 層が担う最終層。
_VALID_DL_STEPS = ("browser", "ytdlp")


def _dl_order() -> list[str]:
    """VIDEO_DL_ORDER（カンマ区切り）から有効な取得順を返す。既定 browser→ytdlp。

    ローカル(会社プロキシ下)=ブラウザ内DL優先 / 本番EC2(プロキシ外)=ytdlp,browser を推奨。
    未知の値は無視し、空になったら既定にフォールバック（必ず1経路は試す）。
    """
    raw = os.environ.get("VIDEO_DL_ORDER", "browser,ytdlp")
    order = [s.strip() for s in raw.split(",") if s.strip() in _VALID_DL_STEPS]
    return order or ["browser", "ytdlp"]


def download_video_chained(
    url: str,
    *,
    max_filesize_mb: int = 20,
    request_id: str | None = None,
) -> tuple[bytes, str]:
    """ブラウザ内DL → yt-dlp の順（VIDEO_DL_ORDER で切替）で動画取得を試みる。

    どの層も best-effort。SSRF はこの関数の冒頭で **1回だけ** 完全検証し、内側の各経路は
    `_skip_url_guard=True` で二重 DNS 解決を避ける。全経路が落ちたら ALL_DOWNLOAD_FAILED を
    送出し、skill 層が cover-only 軽量分析へ縮退する（深掘りは全滅しても board は無傷）。

    Raises:
        VideoDownloadError: URL 非許可(SSRF) / 全取得経路が失敗。
    """
    from teamagent.adapters.media_job import MediaJobClient

    if MediaJobClient.is_configured():
        fingerprint = hashlib.sha256(f"{url}\0{max_filesize_mb}".encode()).hexdigest()
        try:
            return MediaJobClient().acquire_video(
                url,
                request_fingerprint=f"{request_id or 'no-request'}:download:{fingerprint}",
                max_bytes=max_filesize_mb * 1024 * 1024,
            )
        except Exception as exc:
            raise VideoDownloadError(f"VIDEO_MEDIA_JOB_FAILED: {type(exc).__name__}") from exc
    if not MediaJobClient.local_runtime_enabled():
        raise VideoDownloadError("VIDEO_MEDIA_JOB_NOT_CONFIGURED")

    from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

    try:
        url = validate_scrape_url(url, request_id=request_id)
    except UrlGuardError as e:
        logger.warning("video_download_url_blocked", request_id=request_id, reason=str(e))
        raise VideoDownloadError(f"VIDEO_URL_BLOCKED: {e}") from e

    last_error: str | None = None
    for step in _dl_order():
        try:
            if step == "browser":
                from teamagent.adapters.tiktok_scraper import download_tiktok_video

                return download_tiktok_video(url, request_id=request_id, _skip_url_guard=True)
            # step == "ytdlp"
            return download_video(
                url,
                max_filesize_mb=max_filesize_mb,
                request_id=request_id,
                _skip_url_guard=True,
            )
        except Exception as e:  # 各層失敗は次経路へ（生URLはログに残さない）
            last_error = type(e).__name__
            logger.info(
                "video_download_chain_step_failed",
                request_id=request_id,
                step=step,
                error=last_error,
            )

    raise VideoDownloadError(f"ALL_DOWNLOAD_FAILED: 全ての取得経路が失敗しました ({last_error})")
