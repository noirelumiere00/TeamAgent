"""既存GAS台帳を本人のGoogle読取権限で照合する。保存・複製・本文取得はしない。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from teamagent.adapters.file_record_lookup import SavedFileRecord
    from teamagent.skills.base import SkillContext
    from teamagent.skills.search.schema import DocumentReference

logger = structlog.get_logger(__name__)
# data/ingest_sources.yaml の既存ナレッジ共有シート。ファイル記録は本文索引には加えない。
FILE_RECORD_SHEET_ID = "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo"


def resolve_saved_files(
    skill: Any, hits: Sequence[Any], ctx: SkillContext
) -> list[SavedFileRecord | None]:
    from teamagent.adapters.file_record_lookup import slack_source_uri
    from teamagent.adapters.gdrive_client import GDriveClient
    from teamagent.adapters.gsheets_client import GSheetsClient
    from teamagent.skills.search.drive_first import enabled

    empty: list[SavedFileRecord | None] = [None] * len(hits)
    if not enabled(ctx.metadata):
        return empty
    candidates: list[tuple[str, tuple[str, str]] | None] = []
    for hit in hits:
        meta = hit.metadata or {}
        source = slack_source_uri(meta.get("source_uri"))
        key = (str(meta.get("record_number") or ""), str(meta.get("branch_number") or ""))
        if meta.get("source_type") == "slack" and source:
            candidates.append((source, key))
        else:
            candidates.append(None)
    if not any(candidates):
        return empty
    try:
        store = getattr(skill, "_google_token_store", None)
        if store is None:
            from teamagent.orchestrator.factory import _build_token_store

            store = _build_token_store()
        token = store.get(ctx.metadata["user_email"])
        if token is None:
            return empty
        sheet = GSheetsClient.from_user_token(token, write=False)
        records = sheet.lookup_saved_file_records(
            FILE_RECORD_SHEET_ID,
            ctx.request_id,
            slack_sources=list(
                dict.fromkeys(
                    str(hit.metadata["source_uri"])
                    for hit, candidate in zip(hits, candidates, strict=True)
                    if candidate
                )
            ),
            record_keys=list(dict.fromkeys(c[1] for c in candidates if c and all(c[1]))),
        )
        drive = GDriveClient.from_user_token(token, readonly=True)
    except Exception as exc:
        logger.warning(
            "saved_file_record_lookup_failed", request_id=ctx.request_id, error=type(exc).__name__
        )
        return empty

    visible: dict[str, bool] = {}
    result: list[SavedFileRecord | None] = []
    for candidate in candidates:
        matching = []
        if candidate:
            source, key = candidate
            matching = [
                record
                for record in records
                if record.slack_source_uri == source
                and (not any(key) or key == (record.record_number, record.branch_number))
            ]
        if len(matching) != 1:
            result.append(None)
            continue
        record = matching[0]
        fid = record.drive_file_id
        if fid not in visible:
            try:
                metadata = drive.get_file_metadata(fid, ctx.request_id)
                visible[fid] = (
                    metadata.get("id") == fid
                    and not metadata.get("trashed")
                    and metadata.get("mimeType")
                    not in {
                        "application/vnd.google-apps.folder",
                        "application/vnd.google-apps.shortcut",
                    }
                )
            except Exception as exc:
                logger.warning(
                    "saved_file_destination_unverified",
                    request_id=ctx.request_id,
                    error=type(exc).__name__,
                )
                visible[fid] = False
        result.append(record if visible[fid] else None)
    return result


def reauthorize_saved_reference(skill: Any, ref: DocumentReference, ctx: SkillContext) -> bool:
    from teamagent.adapters.pgvector_client import SearchHit

    if not ref.file_record_number or not ref.file_record_branch or not ref.drive_file_id:
        return False
    hit = SearchHit(
        0,
        "",
        0.0,
        {
            "source_type": "slack",
            "source_uri": ref.source_uri,
            "record_number": ref.file_record_number,
            "branch_number": ref.file_record_branch,
        },
    )
    record = resolve_saved_files(skill, [hit], ctx)[0]
    return record is not None and record.drive_file_id == ref.drive_file_id
