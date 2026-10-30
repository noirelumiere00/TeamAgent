"""本人限定の Drive 正本選択。元出典は変更せず、未読添付と会話を区別する。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from teamagent.mcp_gateway.allowlist import email_allowed
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.search.saved_file_refs import resolve_saved_files
from teamagent.skills.search.schema import DocumentReference, ReferenceKind

REFERENCE_KEY = "_drive_first_reference"
_FILE_NAME = re.compile(r"[^\s\u3000|｜/\\]{4,120}?\.(?:pdf|pptx|ppt|docx|doc|xlsx|xls)")


def enabled(metadata: Mapping[str, Any]) -> bool:
    allowed = {
        s.strip().lower()
        for s in os.environ.get("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "").split(",")
        if s.strip()
    }
    return (
        os.environ.get("SEARCH_DRIVE_FIRST", "").strip().lower() in {"1", "true", "yes"}
        and metadata.get("identity_verified") is True
        and str(metadata.get("channel_id") or "").startswith("D")
        and email_allowed(metadata.get("user_email"), allowed)
    )


def scope_hits(query: str, hits: Sequence[Any], doc_type: str | None = None) -> list[Any]:
    """Keep the requested material type and explicitly named campaigns in every lane."""
    from teamagent.skills.search.campaign_files import campaign_key, is_campaign_hit
    from teamagent.skills.search.knowledge_query import document_type, extract_knowledge_filters

    wanted = doc_type or (extract_knowledge_filters(query) or {}).get("cls_doc_type")
    selected = list(hits)
    if wanted:
        selected = [
            h
            for h in selected
            if document_type(
                str((h.metadata or {}).get("title") or (h.metadata or {}).get("file_name") or ""),
                (h.metadata or {}).get("cls_doc_type"),
            )
            == wanted
        ]
    campaigns = {
        name
        for h in hits
        if is_campaign_hit(h) and (pair := campaign_key(h))
        if (name := pair[1]) and name.casefold() in query.casefold()
    }
    if campaigns:
        selected = [
            h
            for h in selected
            if not is_campaign_hit(h) or not (pair := campaign_key(h)) or pair[1] in campaigns
        ]
    return selected


def drive_file_id(value: object) -> str | None:
    text = str(value or "").strip()
    if text.startswith("gdrive://"):
        candidate = text[9:].split("/")[0].split("?")[0]
    else:
        try:
            parsed = urlsplit(text)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"drive.google.com", "docs.google.com"}
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
            ):
                return None
        except ValueError:
            return None
        match = re.search(
            r"/(?:file|document|presentation|spreadsheets|drawings)/d/([A-Za-z0-9_-]+)(?=/|$)",
            parsed.path,
        )
        ids = set(parse_qs(parsed.query).get("id", []))
        if match:
            ids.add(match.group(1))
        candidate = next(iter(ids)) if len(ids) == 1 else ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]+", candidate) else None


def drive_urls(text: str) -> tuple[str, ...]:
    urls = re.findall(r"https://[^\s<>|\"()\[\]]+", text, re.IGNORECASE)
    return tuple(
        dict.fromkeys(url.rstrip(".,、。'") for url in urls if drive_file_id(url.rstrip(".,、。'")))
    )[:5]


def candidate_names(meta: Mapping[str, Any], content: str = "") -> list[str]:
    names: list[str] = []
    for raw in (meta.get("title"), meta.get("file_name"), *meta.get("file_names", ())):
        text = str(raw or "").strip()
        if text and (_FILE_NAME.fullmatch(text) or raw in meta.get("file_names", ())):
            if text not in names:
                names.append(text)
    for match in _FILE_NAME.finditer(content or ""):
        if match.group(0) not in names:
            names.append(match.group(0))
    return names[:8]


def canonical_reference(
    meta: Mapping[str, Any],
    content: str = "",
    *,
    file_urls: Mapping[str, str] | None = None,
    file_names: Sequence[str] = (),
    file_ids: Sequence[str] = (),
) -> DocumentReference:
    """RLS下で取得した資料・確認済み対応だけから、正本と表示種別を一度決める。"""
    existing = meta.get(REFERENCE_KEY)
    if isinstance(existing, DocumentReference):
        return existing
    uri = str(meta.get("source_uri") or "") or None
    source_type = str(meta.get("source_type") or "") or None
    names = list(file_names) or candidate_names(meta, content)
    ids = list(dict.fromkeys(file_ids or meta.get("slack_file_ids", ())))
    title = str(meta.get("title") or meta.get("file_name") or (names[0] if names else "記録"))
    direct = str(meta.get("drive_url") or "")
    # 記録シート（自動保管の台帳・事例シート等）の URL は資料そのものではないので原本に使わない。
    if "/spreadsheets/" in direct:
        direct = ""
    # source_uri はその文書自体の出典。シート行を資料ファイルへ読み替えない。
    own = (
        drive_file_id(uri)
        if source_type in {"gdrive", "other"} and "/spreadsheets/" not in (uri or "")
        else None
    )
    file_urls = {} if source_type == "slack" else (file_urls or {})
    file_urls = {k: v for k, v in file_urls.items() if "/spreadsheets/" not in str(v or "")}
    resolved = {drive_file_id(file_urls.get(name)) for name in names} - {None}
    complete_match = bool(names) and all(drive_file_id(file_urls.get(name)) for name in names)
    fid = (
        own
        or drive_file_id(direct)
        or (next(iter(resolved)) if complete_match and len(resolved) == 1 else None)
    )
    url = f"https://drive.google.com/file/d/{fid}/view" if fid else hit_doc_url({"source_uri": uri})
    if not fid and drive_file_id(uri) and source_type != "gsheets":
        url = None
    kind: ReferenceKind
    if fid:
        kind = "drive"
        if not own:
            title = str(meta.get("drive_title") or (names[0] if names else title))
    elif (
        source_type == "slack" or "slack.com/" in (url or "") or (uri or "").startswith("slack://")
    ):
        kind = (
            "slack_attachment"
            if file_names or meta.get("file_names") or ids
            else "slack_conversation"
        )
    elif source_type == "gsheets" or "/spreadsheets/" in (uri or ""):
        kind = "sheet"
    elif "/app" in (uri or ""):
        kind = "vault"
    else:
        kind = "unknown"
    return DocumentReference(
        document_id=str(meta.get("document_id") or meta.get("id") or "") or None,
        drive_file_id=fid,
        slack_file_id=ids[0] if len(ids) == 1 else None,
        slack_file_ids=ids,
        file_record_number=meta.get("file_record_number"),
        file_record_branch=meta.get("file_record_branch"),
        unread=kind == "slack_attachment"
        or (source_type == "slack" and bool(meta.get("file_names") or ids)),
        title=title,
        url=url,
        source_uri=uri,
        source_type=source_type,
        kind=kind,
    )


def label(ref: DocumentReference) -> str:
    if ref.kind == "drive" and ref.source_type == "slack":
        return (
            "Drive資料（紹介投稿の記録・添付本文未読）"
            if ref.unread
            else "Drive資料（会話からの参照）"
        )
    return {
        "drive": "Drive資料",
        "slack_conversation": "Slackの会話記録",
        "slack_attachment": "Slack にだけある資料（本文未読）",
        "sheet": "記録シート",
        "vault": "Vault入口",
        "unknown": "出典未確認",
    }[ref.kind]


def slack_hit(item: Any, index: int = 0) -> Any:
    from teamagent.adapters.pgvector_client import SearchHit

    return SearchHit(
        chunk_id=-10000 - index,
        content=item.text,
        score=1.0,
        metadata={
            "title": item.text.splitlines()[0][:100] if item.text else "Slackの記録",
            "source_uri": item.hit.permalink,
            "source_type": "slack",
            "file_names": list(item.original_file_names or item.hit.file_names),
            "slack_file_ids": item.hit.file_ids,
            "explicit_drive_urls": item.explicit_drive_urls,
            **(
                {
                    "case_state_evidence": item.hit.case_state_evidence.model_dump(mode="json"),
                    "case_state_external_id": item.hit.case_state_evidence.source_ref.external_id,
                    "occurred_at": item.hit.posted_on,
                }
                if item.hit.case_state_evidence is not None
                else {}
            ),
            **({REFERENCE_KEY: item.hit.reference} if item.hit.reference else {}),
        },
    )


def annotate(
    skill: Any, groups: Sequence[Sequence[Any]], items: Sequence[Any], ctx: Any
) -> tuple[list[list[Any]], tuple[Any, ...], dict[str, str]]:
    """表示・要約に使う全候補を同じ本人RLS下で一括照合する。"""
    slack = [slack_hit(item, i) for i, item in enumerate(items)]
    all_hits = [h for group in groups for h in group] + slack
    # Slack添付は同名資料で付け替えず、GASの投稿・連番＋枝番の対応だけを使う。
    urls = skill._resolve_file_urls(
        [h for h in all_hits if (h.metadata or {}).get("source_type") != "slack"], ctx
    )
    saved = resolve_saved_files(skill, all_hits, ctx)
    # 投稿本文の明示Drive IDも、本人から見える索引に存在するときだけ使う。
    explicit_ids = {
        fid
        for h in all_hits
        for url in [
            *(h.metadata or {}).get("explicit_drive_urls", ()),
            *(
                drive_urls(h.content)
                if (h.metadata or {}).get("source_type") not in {"gdrive", "other"}
                else ()
            ),
            (h.metadata or {}).get("drive_url"),
            (h.metadata or {}).get("source_uri")
            if (h.metadata or {}).get("source_type") not in {"gdrive", "other", "gsheets"}
            else None,
        ]
        if (fid := drive_file_id(url))
    }
    visible_ids: set[str] = set()
    drive_titles: dict[str, str] = {}
    if explicit_ids:
        try:
            from teamagent.skills.search.fanout_sources import rls_connection

            with rls_connection(skill._pgvector, ctx.metadata, app_role=skill._app_role)() as conn:
                rows = skill._pgvector.list_authorized_document_references(
                    conn, drive_file_ids=list(explicit_ids), request_id=ctx.request_id
                )
                visible_ids = {
                    str(r["external_id"]) for r in rows if r.get("source_type") == "gdrive"
                }
                drive_titles = {
                    str(r["external_id"]): str(r.get("title") or "")
                    for r in rows
                    if r.get("source_type") == "gdrive"
                }
        except Exception:
            pass

    def bind(hit: Any, record: Any) -> Any:
        meta = dict(hit.metadata or {})
        meta.pop(REFERENCE_KEY, None)
        direct_id = drive_file_id(meta.get("drive_url"))
        if direct_id and direct_id not in visible_ids:
            meta.pop("drive_url", None)
        explicit = {
            fid
            for url in meta.get("explicit_drive_urls", ())
            if (fid := drive_file_id(url)) is not None and fid in visible_ids
        }
        if meta.get("source_type") not in {"gdrive", "other"}:
            explicit.update(
                fid
                for url in drive_urls(hit.content)
                if (fid := drive_file_id(url)) is not None and fid in visible_ids
            )
        if meta.get("source_type") not in {"gdrive", "other", "gsheets"}:
            source_id = drive_file_id(meta.get("source_uri"))
            if source_id is not None and source_id in visible_ids:
                explicit.add(source_id)
        attachment = meta.get("source_type") == "slack" and (
            meta.get("file_names") or meta.get("slack_file_ids")
        )
        if attachment:
            meta.pop("drive_url", None)
        if len(explicit) == 1 and not attachment:
            fid = next(iter(explicit))
            meta["drive_url"] = f"https://drive.google.com/file/d/{fid}/view"
            meta["drive_title"] = drive_titles.get(fid)
        if record is not None:
            meta.update(
                drive_url=record.drive_url,
                drive_title=record.title,
                file_record_number=record.record_number,
                file_record_branch=record.branch_number,
            )
        # 分割事例も元deckのACLで取得済み。原本IDの補完はDrive対象者の照合時だけ行う。
        stored_id = str(meta.get("drive_file_id") or "")
        if (
            not meta.get("drive_url")
            and meta.get("source_type") == "other"
            and meta.get("case_source") == "deck"
            and "/spreadsheets/" not in str(meta.get("source_uri") or "")
            and re.fullmatch(r"[A-Za-z0-9_-]+", stored_id)
        ):
            meta["drive_url"] = f"https://drive.google.com/file/d/{stored_id}/view"
        if meta.get("campaign_aggregate") == "true" and not meta.get("drive_url"):
            # related_files は本人RLS下で広告主・施策を照合済み。台帳URLは正本にしない。
            related = next(
                (
                    row
                    for row in meta.get("related_files", ())
                    if drive_file_id(row.get("url"))
                    and "/spreadsheets/" not in str(row.get("url") or "")
                ),
                None,
            )
            if related is not None:
                meta.update(drive_url=related["url"], drive_title=related.get("title"))
        meta[REFERENCE_KEY] = canonical_reference(
            meta, hit.content, file_urls={} if meta.get("source_type") == "slack" else urls
        )
        return replace(hit, metadata=meta)

    bindings = iter(saved)
    bound_groups = [[bind(h, next(bindings)) for h in group] for group in groups]
    bound_items = tuple(
        replace(
            item,
            hit=item.hit.model_copy(
                update={"reference": bind(hit, next(bindings)).metadata[REFERENCE_KEY]}
            ),
        )
        for item, hit in zip(items, slack, strict=True)
    )
    return bound_groups, bound_items, urls
