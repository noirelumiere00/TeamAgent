"""Google Drive 動画ダウンロード (動画審査用)。

Drive 共有 URL から動画ファイルを bytes で取得する。Gemini は Drive URL を
file_uri で直接読めない (gs:// / 公開HTTP / YouTube のみ対応) ため、Drive API で
DL して inline bytes (Part.from_bytes) として渡す経路を取る。TikTok/IG と同じ
「DL → bytes → Gemini」パターン。

認証は既存の GDriveClient (OAuth/SA) を流用する。

注意 (アクセス権): サービスアカウント認証の場合、「リンクを知っている全員」共有でも
Drive API で読めるとは限らない (組織の外部共有ポリシー依存)。確実なのは
(a) 貼付ユーザーの OAuth、または (b) SA に明示共有された専用フォルダ運用。

3 層分離: Adapter 層。Skill からは download_drive_video() / extract_drive_file_id() を使う。
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Drive 共有 URL の主要バリエーションからファイル ID を抽出する。
# 研究結果のパターンを優先順に試す: /file/d/{id} → ?id={id}/open?id= → ID形状フォールバック。
_DRIVE_FILE_D = re.compile(r"drive\.google\.com/file/d/([-\w]+)")
_DRIVE_ID_PARAM = re.compile(r"[?&]id=([-\w]+)")
_DRIVE_OPEN = re.compile(r"drive\.google\.com/open\?id=([-\w]+)")
# ID 形状 (英数 + - _ で 25 文字以上)。URL 種別を問わない最終フォールバック。
_DRIVE_ID_SHAPE = re.compile(r"([-\w]{25,})")

# ドメイン判定 (Drive URL かどうか)
_DRIVE_DOMAIN = re.compile(r"(drive|docs)\.google\.com", re.IGNORECASE)

_MIME_BY_EXT: dict[str, str] = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mpeg": "video/mpeg",
    "mpg": "video/mpeg",
    "m4v": "video/mp4",
    "3gp": "video/3gpp",
}


class DriveVideoError(RuntimeError):
    """Drive 動画取得失敗。呼び出し側がユーザー向け案内に変換する。"""


def is_drive_url(url: str) -> bool:
    """Drive / Docs の URL かどうか。"""
    return bool(_DRIVE_DOMAIN.search(url))


def extract_drive_file_id(url: str) -> str | None:
    """Drive 共有 URL からファイル ID を抽出する。

    対応形式:
    - https://drive.google.com/file/d/{id}/view
    - https://drive.google.com/open?id={id}
    - https://drive.google.com/uc?id={id}&export=download
    - ...?id={id} 汎用
    抽出できなければ None。
    """
    for pat in (_DRIVE_FILE_D, _DRIVE_OPEN, _DRIVE_ID_PARAM):
        m = pat.search(url)
        if m:
            return m.group(1)
    # フォールバック: Drive URL 内の ID 形状 (フォルダ URL も拾うので最後に試す)
    if is_drive_url(url):
        m = _DRIVE_ID_SHAPE.search(url)
        if m:
            return m.group(1)
    return None


def _guess_mime(name: str) -> str:
    """ファイル名の拡張子から MIME を推定 (不明なら mp4)。"""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _MIME_BY_EXT.get(ext, "video/mp4")


def download_drive_video(
    url_or_id: str,
    *,
    request_id: str | None = None,
    max_mb: int = 20,
    client: Any | None = None,
) -> tuple[bytes, str]:
    """Drive 共有 URL (またはファイル ID) から動画を bytes で取得する。

    Args:
        url_or_id: Drive 共有 URL もしくはファイル ID。
        request_id: トレース ID。
        max_mb: 取得を許容する最大サイズ (Gemini inline 上限を考慮、既定 20MB)。
            超過した場合は DriveVideoError を上げる (大尺は GCS 経由が別途必要)。
        client: GDriveClient の差し替え (テスト用)。

    Returns:
        (bytes, mime_type)。

    Raises:
        DriveVideoError: ID 抽出不可 / アクセス不可 / サイズ超過 等。
    """
    file_id = (
        url_or_id
        if "/" not in url_or_id and "." not in url_or_id
        else extract_drive_file_id(url_or_id)
    )
    if not file_id:
        raise DriveVideoError(
            "DRIVE_BAD_URL: Drive のファイル URL からファイル ID を抽出できませんでした"
        )

    gdrive = client
    if gdrive is None:
        from teamagent.adapters.gdrive_client import GDriveClient

        gdrive = GDriveClient.from_env()

    # 動画 bytes を取得 (GDriveClient.download_file_bytes は files.get_media)。
    # サイズはダウンロード後にチェックする (事前メタ取得 API は未実装のため)。
    try:
        data = gdrive.download_file_bytes(file_id, request_id or "drive-video")
    except Exception as e:
        logger.warning("drive_video_download_failed", request_id=request_id, error=type(e).__name__)
        raise DriveVideoError(
            "DRIVE_DOWNLOAD_FAILED: Drive から動画を取得できませんでした。"
            "共有設定 (このアカウントに共有されているか) を確認してください"
        ) from e

    if len(data) > max_mb * 1024 * 1024:
        raise DriveVideoError(
            f"DRIVE_FILE_TOO_LARGE: {len(data) / 1024 / 1024:.0f}MB > {max_mb}MB "
            "(大きい動画は GCS 経由が別途必要)"
        )

    # MIME は URL の拡張子から推定 (Drive URL に拡張子が無ければ mp4)。
    final_mime = _guess_mime(url_or_id)
    logger.info(
        "drive_video_downloaded",
        request_id=request_id,
        file_id=file_id,
        size_mb=round(len(data) / 1024 / 1024, 2),
        mime=final_mime,
    )
    return data, final_mime
