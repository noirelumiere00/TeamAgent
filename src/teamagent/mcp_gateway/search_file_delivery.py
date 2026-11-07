"""一覧本文の投稿成功後に、同じ本人 DM・スレッドへ資料を添付する。"""

from __future__ import annotations

import asyncio
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
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchOutput


def candidates(output: Any) -> list[tuple[str, str]]:
    if not isinstance(output, SearchOutput) or output.answer_format != "list":
        return []
    selected: dict[str, str] = {}
    for ref in output.attachment_refs:
        if ref.kind != "slack_attachment" or ref.drive_file_id:
            continue
        for fid in ref.slack_file_ids:
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
                for ref in output.attachment_refs
                for fid in ref.slack_file_ids
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
    if not is_slack_list(output):
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
