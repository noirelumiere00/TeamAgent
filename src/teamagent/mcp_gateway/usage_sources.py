"""usage_events.metadata に残す「出典」と「回答の長さ」（質問と回答の履歴の土台）。

search 系のツール（出典を返すもの）の結果から、上位 5 件の出典 ID と回答の文字数だけを
取り出す。本文・抜粋・タイトル・URL は残さない（metadata の肥大と個人情報を避ける）。

出典 ID の取り方（1 件ずつ・最初に取れたもの）:
  1. ``doc_id`` / ``document_id``         → ``doc_id``
  2. ``external_id``                      → ``external_id``
  3. ``source_uri``（``gdrive://<id>`` / ``slack://<ch>/<ts>`` 等の内部 URI）
                                          → ``external_id``（scheme を外した部分。
                                            http(s)・file は URL/パスなので使わない）
  4. ``url`` が Drive / Docs のファイル URL → ``external_id``（ファイル ID だけ・URL は残さない）
  5. ``chunk_id``                         → ``chunk_id``
結果の形はツールごとに違う（search=hits / knowledge_deliver=references / clientkarte=events）。
想定外の形なら何も足さない。この関数は例外を外へ出さない（記録処理を落とさない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import structlog

from teamagent.skills._shared.drive_slack_delivery import extract_drive_file_id

logger = structlog.get_logger(__name__)

#: 出典を返すツール（結果のどの一覧に出典が入っているか）。
SOURCE_LIST_KEYS: Mapping[str, str] = {
    "search": "hits",
    "knowledge_deliver": "references",
    "clientkarte": "events",
}
MAX_SOURCES = 5
_MAX_ID_CHARS = 200
_MAX_SOURCE_TYPE_CHARS = 40
# URL・ローカルパスの scheme（ID として残さない）
_URL_SCHEMES = frozenset({"http", "https", "file"})
# ファイル ID を取り出してよい URL の host（ID だけ残し、URL 自体は残さない）
_DRIVE_HOSTS = frozenset({"drive.google.com", "docs.google.com"})


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned[:limit] if cleaned else None
    return None


def _from_source_uri(source_uri: Any) -> tuple[str, str | None] | None:
    """内部 URI（scheme://rest）から (external_id, scheme) を取る。URL・パスは捨てる。"""
    text = _text(source_uri, 2000)
    if text is None or "://" not in text:
        return None
    scheme, _sep, rest = text.partition("://")
    scheme = scheme.lower()
    if not scheme or scheme in _URL_SCHEMES:
        return None
    if scheme == "gdrive":
        file_id = extract_drive_file_id(text)
        return (file_id, scheme) if file_id else None
    external_id = _text(rest.strip("/"), _MAX_ID_CHARS)
    return (external_id, scheme) if external_id else None


def _source_entry(item: Any) -> dict[str, str] | None:
    source_type = _text(_field(item, "source_type"), _MAX_SOURCE_TYPE_CHARS)
    entry: dict[str, str] | None = None
    for key, out_key in (("doc_id", "doc_id"), ("document_id", "doc_id")):
        value = _text(_field(item, key), _MAX_ID_CHARS)
        if value:
            entry = {out_key: value}
            break
    if entry is None:
        value = _text(_field(item, "external_id"), _MAX_ID_CHARS)
        if value:
            entry = {"external_id": value}
    if entry is None:
        parsed = _from_source_uri(_field(item, "source_uri"))
        if parsed is not None:
            entry = {"external_id": parsed[0]}
            source_type = source_type or parsed[1]
    if entry is None:
        url = _text(_field(item, "url"), 2000)
        # knowledge_deliver は url 欄に内部 URI（gdrive://…）を入れることがある
        parsed = _from_source_uri(url)
        if parsed is not None:
            entry = {"external_id": parsed[0]}
            source_type = source_type or parsed[1]
        else:
            host = (urlsplit(url).hostname or "").lower() if url else ""
            file_id = extract_drive_file_id(url) if url and host in _DRIVE_HOSTS else None
            if file_id:
                entry = {"external_id": file_id}
                source_type = source_type or "gdrive"
    if entry is None:
        value = _text(_field(item, "chunk_id"), _MAX_ID_CHARS)
        if value:
            entry = {"chunk_id": value}
    if entry is None:
        return None
    if source_type:
        entry["source_type"] = source_type
    return entry


def source_usage_metadata(tool: str, output: Any) -> dict[str, Any]:
    """出典を返すツールの結果から ``source_ids``（上位 5 件）と ``answer_chars`` を作る。

    出典が 1 件も取れなければ空 dict（何も足さない）。例外は出さない。
    """
    list_key = SOURCE_LIST_KEYS.get(tool)
    if list_key is None:
        return {}
    try:
        items = _field(output, list_key)
        if not isinstance(items, (list, tuple)):
            return {}
        sources: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for item in items:
            entry = _source_entry(item)
            if entry is None:
                continue
            marker = tuple(sorted(entry.items()))
            if marker in seen:
                continue
            seen.add(marker)
            sources.append(entry)
            if len(sources) >= MAX_SOURCES:
                break
        if not sources:
            return {}
        metadata: dict[str, Any] = {"source_ids": sources}
        answer = _field(output, "answer")
        if isinstance(answer, str):
            metadata["answer_chars"] = len(answer)
        return metadata
    except Exception as exc:
        logger.warning("usage_source_extract_failed", tool=tool, error=type(exc).__name__)
        return {}


__all__ = ["MAX_SOURCES", "SOURCE_LIST_KEYS", "source_usage_metadata"]
