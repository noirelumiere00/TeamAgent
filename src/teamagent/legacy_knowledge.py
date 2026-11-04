"""旧 Vault の名前を、呼び出し元が RLS で取得した集合内だけで解決する。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any
from uuid import UUID

from teamagent.client_identity import client_identity_key

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\[\]#^\x00-\x1f]+')
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
}


def safe_legacy_filename(name: str | None, *, fallback: str = "untitled", max_len: int = 80) -> str:
    # scripts は本番パッケージ外なので移植。export_vault との一致をテストする。
    base = unicodedata.normalize("NFC", str(name or "")).strip()
    base = _FORBIDDEN.sub("_", base)
    base = re.sub(r"\.{2,}", "_", base)
    base = re.sub(r"\s+", " ", base).strip().strip(". ")
    base = base[:max_len].strip(". ")
    if not base or not base.strip("_ "):
        return fallback
    return base + "_" if base.lower() in _RESERVED else base


def legacy_document_stem(record: dict[str, Any]) -> str | None:
    metadata = record.get("metadata") or {}
    discriminator = bool(
        metadata.get("x_research_tool")
        or record.get("source_type") == "gsheets"
        or (metadata.get("topic") == "案件決定" and record.get("source_type") == "slack")
    )
    base = safe_legacy_filename(
        record.get("title"), fallback="document", max_len=55 if discriminator else 80
    )
    if discriminator:
        external_id = str(record.get("external_id") or "").strip()
        if not external_id:
            return None
        base += "-" + hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]
    return base


def validate_legacy_requests(entries: list[dict[str, Any]]) -> None:
    if not 1 <= len(entries) <= 100:
        raise ValueError("一度に確認できるのは1〜100件です。")
    for entry in entries:
        kind, value, legacy = entry.get("kind"), entry.get("value"), entry.get("legacy")
        if (
            set(entry) != {"kind", "value", "legacy"}
            or kind not in ("client", "doc", "open")
            or not isinstance(legacy, bool)
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 500
            or any(unicodedata.category(c).startswith("C") for c in value)
        ):
            raise ValueError("リンクの形式が正しくありません。")
        if not legacy:
            if kind == "open":
                raise ValueError("保存IDの形式が正しくありません。")
            if kind == "doc" and str(UUID(value)) != value.lower():
                raise ValueError("資料IDの形式が正しくありません。")
        elif kind in ("doc", "open"):
            stem = re.sub(r"^(?:clients|docs)/", "", value) if kind == "open" else value
            if len(stem) > 128 or _FORBIDDEN.search(stem) or stem in (".", ".."):
                raise ValueError("旧リンクの形式が正しくありません。")


def _stem_matches(value: str, base: str | None) -> bool:
    if base is None:
        return False
    value, base = unicodedata.normalize("NFC", value).casefold(), base.casefold()
    # shortcut: export 時の衝突付番は復元できない。隠れた資料を調べず可視候補を提示する。
    return value == base or bool(
        re.fullmatch(re.escape(base) + r"(?:_|-dup-)(?:[2-9]|[1-9][0-9]+)", value)
    )


def _same_stem(value: str, base: str | None) -> bool:
    return base is not None and unicodedata.normalize("NFC", value).casefold() == base.casefold()


def resolve_legacy_entries(
    records: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """全件が本人に可視であることを前提に、本文や ACL を返さず候補を作る。"""
    validate_legacy_requests(entries)
    clients: dict[str, dict[str, Any]] = {}
    documents = []
    for record in records:
        metadata = record.get("metadata") or {}
        fb = metadata.get("is_sales_fb") in (True, "true")
        primary, project = (
            str(metadata.get(key) or "").strip() for key in ("client_name", "cls_project")
        )
        for name in [primary or project] if fb else [primary, project]:
            key = client_identity_key(name)
            if key:
                client = clients.setdefault(key, {"kind": "client", "id": name, "names": set()})
                client["id"] = min(client["id"], name)
                client["names"].add(name)
        if not fb:
            documents.append(record)
    results = []
    for entry in entries:
        kind, value, legacy = entry["kind"], entry["value"], entry["legacy"]
        candidates: dict[tuple[str, str], dict[str, str]] = {}
        inferred = False
        scope = (
            "client"
            if value.startswith("clients/")
            else "doc"
            if value.startswith("docs/")
            else kind
        )
        stem = value.split("/", 1)[1] if kind == "open" and scope != "open" else value
        if kind == "client" or (kind == "open" and scope != "doc"):
            for key, client in clients.items():
                matched = key == client_identity_key(stem) or (
                    legacy
                    and any(
                        _stem_matches(stem, safe_legacy_filename(n, fallback="client"))
                        for n in client["names"]
                    )
                )
                if matched:
                    if legacy and key != client_identity_key(stem):
                        inferred |= not any(
                            _same_stem(stem, safe_legacy_filename(n, fallback="client"))
                            for n in client["names"]
                        )
                    name = client["id"]
                    candidates[("client", name)] = {"kind": "client", "id": name, "title": name}
        if kind == "doc" or (kind == "open" and scope != "client"):
            for record in documents:
                if (
                    _stem_matches(stem, legacy_document_stem(record))
                    if legacy
                    else str(record["id"]) == value.lower()
                ):
                    if legacy:
                        inferred |= not _same_stem(stem, legacy_document_stem(record))
                    doc_id = str(record["id"])
                    candidates[("doc", doc_id)] = {
                        "kind": "doc",
                        "id": doc_id,
                        "title": record.get("title") or "（無題）",
                    }
        ordered = sorted(candidates.values(), key=lambda c: (c["title"], c["kind"], c["id"]))
        results.append(
            {
                "status": "resolved"
                if len(ordered) == 1 and not inferred
                else "ambiguous"
                if ordered
                else "missing",
                "candidates": ordered,
            }
        )
    return results
