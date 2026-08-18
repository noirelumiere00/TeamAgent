"""会話内の添付ファイル発見と受入判定（純関数・外部 I/O 無し）。

Slack の ``files`` 配列は「本文に貼られたもの」だけではない。外部共有
（Google Drive / Box 等）のリンクも ``is_external`` / ``external_type`` 付きで混ざる。
url_private へは bot token を載せて GET するため、**外部ファイルは download 対象から
外す**（token 漏洩経路を塞ぐ）。ホスト検証は adapters/slack_file_guard に 1 実装だけ置き、
ここはその関数を呼ぶ。

サイズは **download する前に** Slack の file metadata（``size``）で拒否する。
「落としてから測る」だと 30MB 超のファイルでも一旦全部メモリに載る
（共有 mcp タスクはメモリ 3GB・16 名共用＝OOM 経路になる）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from teamagent.adapters.slack_file_guard import (
    SlackFileGuardError,
    is_external_file,
    validate_slack_file_url,
)
from teamagent.ingest.office_extract import DOCX_MIME, PPTX_MIME, XLSX_MIME

PDF_MIME = "application/pdf"

# mime → 内部 kind。ここに無い mime は「非対応」で fail-closed（画像/動画/zip/旧 Office 等）。
_MIME_KIND: dict[str, str] = {
    PDF_MIME: "pdf",
    DOCX_MIME: "docx",
    PPTX_MIME: "pptx",
    XLSX_MIME: "xlsx",
}

# 拡張子 → 内部 kind（プレーンテキスト系。Slack は text/plain 以外の mime も付ける）。
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {"txt", "md", "markdown", "csv", "tsv", "json", "log", "yaml", "yml"}
)
_TEXT_MIME_PREFIXES: tuple[str, ...] = ("text/",)
_TEXT_MIMES: frozenset[str] = frozenset({"application/json"})

# 受入拒否の理由コード（Output.error にそのまま載る）。
REASON_EXTERNAL = "external_file"
REASON_TOO_LARGE = "too_large"
REASON_UNSUPPORTED = "unsupported_type"
REASON_BAD_URL = "bad_url"


@dataclass(frozen=True)
class AttachmentCandidate:
    """会話内で読取対象にできる添付 1 件。"""

    file_id: str
    name: str
    kind: str  # pdf / docx / pptx / xlsx / text
    mime: str
    size: int  # Slack metadata 上のバイト数（0 = 不明）
    url: str  # 検証済み url_private
    ts: float  # 投稿時刻（新しい順に並べるためのキー）
    # Slack が返す原本 permalink（``https://<ws>.slack.com/files/…``）。出典表示用。
    # ⚠️ url_private とは別物（permalink はブラウザで本人の権限で開く画面 URL で、
    #    bot token を載せて GET する取得用 URL ではない）。取れなければ空文字。
    permalink: str = ""


@dataclass(frozen=True)
class RejectedAttachment:
    """受入できなかった添付 1 件（利用者に理由を伝えるため名前と理由だけ保持）。"""

    name: str
    reason: str


def _extension(name: str) -> str:
    _, _, ext = name.rpartition(".")
    return ext.lower() if ext and ext != name else ""


def classify_kind(mime: str, name: str, filetype: str = "") -> str:
    """mime / 拡張子 / Slack filetype から内部 kind を決める。非対応は空文字。"""
    m = (mime or "").split(";", 1)[0].strip().lower()
    if m in _MIME_KIND:
        return _MIME_KIND[m]
    ext = _extension(name) or (filetype or "").strip().lower()
    if ext == "pdf":
        return "pdf"
    if ext == "docx":
        return "docx"
    if ext == "pptx":
        return "pptx"
    if ext == "xlsx":
        return "xlsx"
    if ext in _TEXT_EXTENSIONS:
        return "text"
    if m in _TEXT_MIMES or (m and m.startswith(_TEXT_MIME_PREFIXES)):
        return "text"
    return ""


def _int_or_zero(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _safe_permalink(raw: Any) -> str:
    """Slack file の ``permalink`` を表示に使ってよい形だけ通す（それ以外は空文字）。

    自 WS の Slack 画面 URL（``https://<何か>.slack.com/…``）だけを許す。file dict は
    外部由来のデータなので、ここを緩めると任意 URL を「出典」として提示させられる。
    """
    url = str(raw or "").strip()
    if not url or len(url) > 2048:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    try:
        if parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    host = (parsed.hostname or "").rstrip(".").lower()
    return url if host == "slack.com" or host.endswith(".slack.com") else ""


def evaluate_file(
    file: dict[str, Any],
    *,
    max_bytes: int,
    ts: float = 0.0,
    allowed_hosts: frozenset[str] | None = None,
    request_id: str | None = None,
) -> tuple[AttachmentCandidate | None, RejectedAttachment | None]:
    """Slack file dict 1 件を受入判定する。返り値 ``(candidate, rejected)``（片方が None）。

    判定順は **安全側から**:
      1. 外部共有ファイル（is_external / external_type）→ 拒否（bot token を出さない）
      2. url_private のホスト allowlist（files.slack.com 系）→ 外れたら拒否
      3. metadata の ``size`` で事前拒否（download 前・OOM 経路を塞ぐ）
      4. 対応種別か
    """
    name = str(file.get("name") or file.get("title") or "").strip() or "(名称不明)"

    # ① 外部共有ファイルは対象外（url_private が外部ホストを指し得る＝token 漏洩経路）。
    if is_external_file(file):
        return None, RejectedAttachment(name=name, reason=REASON_EXTERNAL)

    # ② ホスト allowlist（adapters/slack_file_guard の 1 実装を共有）。
    try:
        url = validate_slack_file_url(
            str(file.get("url_private") or ""),
            allowed=allowed_hosts,
            request_id=request_id,
        )
    except SlackFileGuardError:
        return None, RejectedAttachment(name=name, reason=REASON_BAD_URL)

    # ③ size 事前拒否（「落としてから測る」をしない）。
    size = _int_or_zero(file.get("size"))
    if size > max_bytes:
        return None, RejectedAttachment(name=name, reason=REASON_TOO_LARGE)

    # ④ 対応種別。
    kind = classify_kind(str(file.get("mimetype") or ""), name, str(file.get("filetype") or ""))
    if not kind:
        return None, RejectedAttachment(name=name, reason=REASON_UNSUPPORTED)

    return (
        AttachmentCandidate(
            file_id=str(file.get("id") or ""),
            name=name,
            kind=kind,
            mime=str(file.get("mimetype") or ""),
            size=size,
            url=url,
            ts=ts,
            permalink=_safe_permalink(file.get("permalink")),
        ),
        None,
    )


def collect_candidates(
    messages: Iterable[Any],
    *,
    max_bytes: int,
    allowed_hosts: frozenset[str] | None = None,
    request_id: str | None = None,
) -> tuple[list[AttachmentCandidate], list[RejectedAttachment]]:
    """SlackMessage 群から受入可能な添付を新しい順に集める。

    同一 file_id が親メッセージと reply の双方に現れても 1 件に畳む。
    """
    accepted: list[AttachmentCandidate] = []
    rejected: list[RejectedAttachment] = []
    seen: set[str] = set()
    for msg in messages:
        try:
            ts = float(getattr(msg, "ts", "") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        for raw in getattr(msg, "files", ()) or ():
            if not isinstance(raw, dict):
                continue
            cand, rej = evaluate_file(
                raw,
                max_bytes=max_bytes,
                ts=ts,
                allowed_hosts=allowed_hosts,
                request_id=request_id,
            )
            if cand is not None:
                key = cand.file_id or f"{cand.name}:{cand.size}"
                if key in seen:
                    continue
                seen.add(key)
                accepted.append(cand)
            elif rej is not None:
                rejected.append(rej)
    # 新しい順（同 ts はファイル名で決定的に）。
    accepted.sort(key=lambda c: (-c.ts, c.name))
    return accepted, rejected


def select_candidate(
    candidates: list[AttachmentCandidate], file_name: str = ""
) -> AttachmentCandidate | None:
    """file_name 指定があれば部分一致（大小無視）で、無ければ最新を選ぶ。"""
    if not candidates:
        return None
    needle = (file_name or "").strip().lower()
    if not needle:
        return candidates[0]
    for c in candidates:
        if c.name.lower() == needle:
            return c
    for c in candidates:
        if needle in c.name.lower():
            return c
    return None


__all__ = [
    "REASON_BAD_URL",
    "REASON_EXTERNAL",
    "REASON_TOO_LARGE",
    "REASON_UNSUPPORTED",
    "AttachmentCandidate",
    "RejectedAttachment",
    "classify_kind",
    "collect_candidates",
    "evaluate_file",
    "select_candidate",
]
