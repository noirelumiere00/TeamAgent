"""会話（スレッド / DM）の添付から切出し対象の動画を選ぶ純関数群。

ここは **Slack API を呼ばない**（呼ぶのは adapter 層）。file メタデータだけを見て
「取ってよいか」を決める門番なので、単体テストで全分岐を固定できる形にしてある。

門番の条件（どれか 1 つでも欠けたら候補にしない）:
  1. ``mimetype`` が ``video/`` で始まる
  2. 外部ファイルでない（``is_external`` / ``external_type`` / ``mode`` が external 系）
     — 外部ホスト実体には bot token を送らない＝トークン漏洩と SSRF の遮断
  3. tombstone（削除済み）でない
  4. ``url_private_download`` / ``url_private`` のいずれかを持つ
  5. ``size`` が上限以内（**取りに行く前に**弾く。落としてから測らない）
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_EXTERNAL_MODES = frozenset({"external", "hosted_external"})


def _as_float_ts(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def is_video_attachment(file: Mapping[str, Any]) -> bool:
    """動画添付として扱ってよいか（サイズ以外の全条件）。"""

    mimetype = str(file.get("mimetype") or "").lower()
    if not mimetype.startswith("video/"):
        return False
    if file.get("is_external") or file.get("external_type"):
        return False
    if str(file.get("mode") or "").lower() in _EXTERNAL_MODES:
        return False
    if file.get("is_tombstoned") or str(file.get("mode") or "").lower() == "tombstone":
        return False
    return bool(file.get("url_private_download") or file.get("url_private"))


def attachment_download_url(file: Mapping[str, Any]) -> str:
    """添付の取得 URL（download 優先）。"""

    return str(file.get("url_private_download") or file.get("url_private") or "")


def select_video_file(
    messages: Sequence[Any],
    *,
    file_id: str = "",
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str]:
    """会話メッセージ列から対象動画を 1 件選ぶ。

    返り値 ``(file, reason)``。``file`` が None のとき ``reason`` は
    ``"not_found"``（候補なし）か ``"too_large"``（候補はあったが上限超）。
    新しいメッセージを優先する（「さっき貼った動画」を素直に拾う）。

    ``file_id`` 指定時は**会話内に実在する添付だけ**が対象。エージェントが申告した
    ID で任意ファイルを取りに行かせない（url_private に bot token が載るため）。
    """

    ordered = sorted(
        messages,
        key=lambda message: _as_float_ts(getattr(message, "ts", "")),
        reverse=True,
    )
    oversized = False
    for message in ordered:
        files: Iterable[Mapping[str, Any]] = getattr(message, "files", ()) or ()
        for file in files:
            if not isinstance(file, Mapping) or not is_video_attachment(file):
                continue
            if file_id and str(file.get("id") or "") != file_id:
                continue
            size = file.get("size")
            if isinstance(size, int) and size > max_bytes:
                oversized = True
                continue
            return dict(file), ""
    return None, "too_large" if oversized else "not_found"


__all__ = [
    "attachment_download_url",
    "is_video_attachment",
    "select_video_file",
]
