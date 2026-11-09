"""検索の回答に使った資料を、同じ本人 DM・スレッドへ添付する。"""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

from teamagent.mcp_gateway import detached_jobs, direct_search_answer, direct_summary
from teamagent.skills._shared.drive_slack_delivery import deliver_files
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.slack_file_delivery import (
    ALLOWED_EMAILS_ENV,
    ENABLED_ENV,
    prepare_slack_files,
)
from teamagent.skills._shared.slack_file_delivery import (
    enabled as enabled,
)
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.base import SkillContext
from teamagent.skills.search.drive_first import canonical_reference
from teamagent.skills.search.schema import DocumentReference, SearchOutput


def _file_ids(ref: DocumentReference) -> list[str]:
    return ref.slack_file_ids or ([ref.slack_file_id] if ref.slack_file_id else [])


def _references(output: SearchOutput) -> list[tuple[str | None, DocumentReference]]:
    refs: list[tuple[str | None, DocumentReference]] = [
        (None, ref) for ref in [*output.attachment_refs, *(output.displayed_refs or [])]
    ]
    selected_sources = {ref.source_uri for ref in output.attachment_refs if ref.source_uri}
    for hit in output.hits:
        if output.answer_format == "list" and hit.source_uri in selected_sources:
            continue
        ref = hit.reference or canonical_reference(
            {
                **hit.slack_file_metadata,
                "source_type": hit.source_type,
                "source_uri": hit.source_uri or hit.url,
                "external_id": hit.external_id,
                "drive_url": hit.drive_url,
                "title": hit.title or hit.file_name,
                "channel_type": hit.channel_type or hit.slack_file_metadata.get("channel_type"),
            },
            hit.content,
        )
        refs.append((str(hit.chunk_id), ref))
    for index, item in enumerate(output.slack_hits or []):
        if output.answer_format == "list" and item.permalink in selected_sources:
            continue
        ref = item.reference or canonical_reference(
            {
                "source_type": "slack",
                "source_uri": item.permalink,
                "file_names": item.file_names,
                "channel_type": "im"
                if item.channel == "DM"
                else "mpim"
                if item.channel == "グループDM"
                else None,
            },
            file_ids=item.file_ids,
        )
        refs.append((f"slack{index}", ref))
    return refs


def candidates(output: Any) -> list[tuple[str, str]]:
    if not isinstance(output, SearchOutput):
        return []
    refs = _references(output)
    positions: dict[str, int] = {}
    for match in re.finditer(r"https?://[^\s<>|\"()\[\]]+|\[S:[\w-]+\]", output.answer):
        positions.setdefault(match[0].rstrip(".,、。"), match.start())
    drive_copies = {fid for _, ref in refs if ref.drive_file_id for fid in _file_ids(ref)}
    used: list[tuple[int, DocumentReference]] = []
    for key, ref in refs:
        links = (
            ref.url,
            hit_doc_url({"source_uri": ref.source_uri}),
            f"[S:{key}]" if key is not None else None,
        )
        offsets = [positions[link] for link in links if link is not None and link in positions]
        if offsets:
            used.append((min(offsets), ref))
    selected: dict[str, str] = {}
    for _, ref in sorted(used, key=lambda pair: pair[0]):
        if ref.kind != "slack_attachment" or ref.drive_file_id:
            continue
        for fid in _file_ids(ref):
            if fid not in drive_copies:
                selected.setdefault(fid, ref.title)
    return list(selected.items())[:3]


def attach(output: Any, ctx: SkillContext, destination: detached_jobs.Destination) -> None:
    if not enabled(ctx.metadata):
        return
    if not is_private_surface(
        destination.channel_id, ctx.metadata.get("identity_verified") is True
    ):
        return
    files = candidates(output)
    if not files:
        return

    async def upload() -> None:
        tmpdir = None
        try:
            metadata = {
                fid: {**ref.slack_file_metadata, "source_uri": ref.source_uri}
                for _, ref in reversed(_references(output))
                for fid in _file_ids(ref)
            }
            tmpdir, prepared = await prepare_slack_files(files, ctx, metadata_by_id=metadata)
            if prepared:
                await deliver_files(
                    detached_jobs._slack_client(60),
                    prepared=prepared,
                    comment=None,
                    request_id=ctx.request_id,
                    dm_channel=destination.channel_id,
                    thread_ts=destination.thread_ts,
                    single_post=True,
                )
        except Exception as exc:
            ctx.bind_logger("search_file_delivery").info(
                "attachment_failed", error=type(exc).__name__
            )
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    asyncio.run(upload())


def is_slack_list(output: Any) -> bool:
    return (
        isinstance(output, SearchOutput)
        and output.answer_format == "list"
        and any(
            ref.kind in {"slack_attachment", "slack_conversation"} for ref in output.attachment_refs
        )
    )


def post_list(output: Any, ctx: SkillContext, *, verified_caller: Any) -> dict[str, Any] | None:
    """既存の一覧または添付対象のある回答を投稿し、成功後だけ資料を添付する。"""
    if not is_slack_list(output) and not candidates(output):
        return None
    destination, _ = direct_summary.decide_destination(
        direct_summary.DirectPolicy.from_env(
            enabled_env=ENABLED_ENV,
            allowed_emails_env=ALLOWED_EMAILS_ENV,
        ),
        verified_caller=verified_caller,
        metadata=ctx.metadata,
    )
    if destination is None:
        return None
    posted = direct_search_answer.post(
        output.answer,
        "case_cards",
        destination,
        request_id=ctx.request_id,
        allow_case_links=True,
    )
    if posted.get("delivered") is True:
        attach(output, ctx, destination)
    return posted
