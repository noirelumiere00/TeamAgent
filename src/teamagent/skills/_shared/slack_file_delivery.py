"""段階公開された資料配信。公開資料だけ他の連携者の user token でも取得する。"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from teamagent.mcp_gateway.allowlist import email_allowed
from teamagent.skills._shared.drive_slack_delivery import PreparedFile, safe_filename
from teamagent.skills.base import SkillContext

ENABLED_ENV = "USE_SEARCH_FILE_DELIVERY"
ALLOWED_EMAILS_ENV = "SEARCH_FILE_DELIVERY_ALLOWED_EMAILS"


def enabled(metadata: Mapping[str, Any]) -> bool:
    allowed = {
        v.strip().lower() for v in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",") if v.strip()
    }
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    } and email_allowed(metadata.get("user_email"), allowed)


def file_metadata(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: meta[key]
        for key in (
            "source_uri",
            "public_channel_ids",
            "private_channel_ids",
            "channel_ids",
            "channel_type",
            "channel_name",
            "is_im",
            "is_mpim",
            "is_public",
            "ims",
        )
        if key in meta
    }


def _reader(store: Any, owner: str) -> Any | None:
    from teamagent.adapters.slack_client import SlackClient

    scopes = store.scopes(owner)
    if scopes is None or "files:read" not in scopes:
        return None
    token = store.get(owner)
    if token is None or "files:read" not in token.scopes:
        return None
    return SlackClient.from_user_token(token.access_token)


def file_reader(ctx: SkillContext, *, slack_store: Any = None) -> Any | None:
    requester = str(ctx.metadata.get("user_email") or "").strip()
    if not requester:
        return None
    try:
        from teamagent.skills.oauth_connect.skill import _build_slack_store

        store = slack_store if slack_store is not None else _build_slack_store()
        return _reader(store, requester)
    except Exception as exc:
        ctx.bind_logger("slack_file_delivery").warning(
            "slack_file_reader_unavailable", error=type(exc).__name__
        )
        return None


def _can_fallback(metadata: Mapping[str, Any], info: Mapping[str, Any]) -> bool:
    for value in (metadata, info):
        if (
            value.get("is_im") is True
            or value.get("is_mpim") is True
            or value.get("ims")
            or value.get("channel_type") in {"dm", "im", "mpim", "group_dm"}
            or str(value.get("channel_name") or "").startswith("mpdm-")
            or any(str(cid).startswith("D") for cid in (value.get("channel_ids") or ()))
            or re.search(r"(?:slack://|/archives/)D[A-Z0-9]+/", str(value.get("source_uri") or ""))
        ):
            return False
    # 現在の Slack 応答で公開が否定されたら、古い取込メタデータで広げない。
    if info.get("is_public") is False:
        return False
    public = metadata.get("public_channel_ids")
    return (
        info.get("is_public") is True
        or metadata.get("is_public") is True
        or (
            isinstance(public, (list, tuple))
            and any(isinstance(cid, str) and re.fullmatch(r"C[A-Z0-9]+", cid) for cid in public)
        )
    )


async def prepare_slack_files(
    candidates: list[tuple[str, str]],
    ctx: SkillContext,
    *,
    slack_store: Any = None,
    metadata_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str | None, list[PreparedFile]]:
    if not candidates or not enabled(ctx.metadata):
        return None, []
    requester = str(ctx.metadata.get("user_email") or "").strip().lower()
    log = ctx.bind_logger("slack_file_delivery")
    try:
        from teamagent.skills.oauth_connect.skill import _build_slack_store

        store = slack_store if slack_store is not None else _build_slack_store()
        tmpdir = tempfile.mkdtemp(prefix="aila_knowledge_slack_")
    except Exception as exc:
        log.warning("slack_file_prepare_failed", error=type(exc).__name__)
        return None, []
    reader = file_reader(ctx, slack_store=store)
    prepared: list[PreparedFile] = []
    others: list[str] | None = None

    async def download(client: Any, file_id: str, info: Mapping[str, Any]) -> bytes:
        if info.get("id") != file_id:
            raise ValueError("slack_file_id_mismatch")
        data: bytes = await client.download_file_guarded(
            info.get("url_private_download") or info["url_private"],
            request_id=ctx.request_id,
            max_bytes=256 * 1024 * 1024,
        )
        return data

    for file_id, filename in candidates[:3]:
        if not re.fullmatch(r"F[A-Z0-9]+", file_id):
            continue
        metadata = (metadata_by_id or {}).get(file_id, {})
        owner_info: dict[str, Any] = {}
        data: bytes | None = None
        if reader is not None:
            try:
                info = await reader.file_info(file_id)
                if info.get("id") != file_id:
                    continue
                owner_info = info
                data = await download(reader, file_id, owner_info)
            except Exception as exc:
                log.info("slack_file_owner_download_failed", error=type(exc).__name__)
        if data is None:
            if not _can_fallback(metadata, owner_info):
                continue
            try:
                if others is None:
                    others = store.list_file_reader_emails()
                for owner in others:
                    if owner.strip().lower() == requester:
                        continue
                    try:
                        alternate = _reader(store, owner)
                        if alternate is None:
                            continue
                        info = await alternate.file_info(file_id)
                        if not _can_fallback({**metadata, **file_metadata(owner_info)}, info):
                            continue
                        data = await download(alternate, file_id, info)
                        break
                    except Exception as exc:
                        log.info("slack_file_public_download_failed", error=type(exc).__name__)
            except Exception as exc:
                log.info("slack_file_readers_unavailable", error=type(exc).__name__)
        if data is None:
            continue
        filename = safe_filename(filename)
        try:
            path = Path(tmpdir) / file_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            prepared.append((file_id, str(path), filename))
        except OSError:
            log.warning("slack_file_tmpwrite_failed")
    return tmpdir, prepared
