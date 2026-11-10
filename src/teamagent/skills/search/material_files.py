"""一覧に使う資料の原本を補い、本人が開ける同一ファイルだけをまとめる。"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills._shared.slack_file_delivery import _can_fallback, file_reader
from teamagent.skills._shared.source_url import ingested_slack_file_id, slack_file_permalink
from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first


def normalized_name(name: str) -> str:
    return re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", name).casefold())


def _message_location(uri: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"slack://([A-Z0-9]+)/(\d+\.\d+)", uri)
    if match:
        return match[1], match[2]
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if not (parsed.hostname or "").endswith(".slack.com"):
        return None
    match = re.fullmatch(r"/archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})", parsed.path)
    return (match[1], f"{match[2]}.{match[3]}") if match else None


def prepare_list_hits(skill: Any, hits: list[SearchHit], ctx: SkillContext) -> list[SearchHit]:
    if not any(
        h.metadata.get("source_type") == "slack" or ingested_slack_file_id(h.metadata) for h in hits
    ):
        return hits
    return asyncio.run(_prepare(skill, hits, ctx))


def _drive_reader(skill: Any, ctx: SkillContext) -> Any | None:
    from teamagent.adapters.gdrive_client import GDriveClient
    from teamagent.orchestrator.factory import _build_token_store

    store = getattr(skill, "_google_token_store", None)
    store = store if store is not None else _build_token_store()
    token = store.get(ctx.metadata["user_email"])
    return GDriveClient.from_user_token(token, readonly=True) if token is not None else None


async def _prepare(skill: Any, hits: list[SearchHit], ctx: SkillContext) -> list[SearchHit]:
    log = ctx.bind_logger("search_material_files")
    reader = file_reader(ctx, slack_store=getattr(skill, "_slack_store", None))
    enriched: list[SearchHit] = []
    verified_slack: set[str] = set()
    infos: dict[str, dict[str, Any]] = {}
    for hit in hits:
        meta = dict(hit.metadata)
        ref = drive_first.canonical_reference(meta, hit.content)
        if ref.kind == "drive" or meta.get("source_type") not in {"slack", "other"}:
            enriched.append(hit)
            continue
        files = list(meta.get("files") or [])
        ids = list(ref.slack_file_ids)
        if ingested := ingested_slack_file_id(meta):
            ids = [ingested]
        if not ids and not files and reader is not None and meta.get("source_type") == "slack":
            location = _message_location(str(meta.get("source_uri") or ""))
            if location:
                try:
                    files = await reader.message_files(*location)
                except Exception as exc:
                    log.info("message_files_unavailable", error=type(exc).__name__)
        ids = list(dict.fromkeys([*ids, *(str(f.get("id") or "") for f in files)]))
        ids = [fid for fid in ids if re.fullmatch(r"F[A-Z0-9]+", fid)]
        if not ids:
            names = drive_first.candidate_names(meta, hit.content)
            if meta.get("source_type") == "slack" and names:
                hit = replace(
                    hit,
                    metadata={
                        **meta,
                        "title": names[0],
                        drive_first.REFERENCE_KEY: ref.model_copy(update={"title": names[0]}),
                    },
                )
            enriched.append(hit)
            continue
        before = len(enriched)
        for index, fid in enumerate(ids):
            file: dict[str, Any] = next((f for f in files if f.get("id") == fid), {})
            if reader is not None:
                try:
                    if fid not in infos:
                        infos[fid] = await reader.file_info(fid)
                    if infos[fid].get("id") == fid:
                        file = infos[fid]
                        verified_slack.add(fid)
                except Exception as exc:
                    log.info("file_info_unavailable", error=type(exc).__name__)
                    if not _can_fallback({**meta, **ref.slack_file_metadata}, file):
                        continue
            names = list(meta.get("file_names") or [])
            name = str(
                file.get("name")
                or file.get("title")
                or (names[index] if index < len(names) else ref.title)
            )
            version = meta.get("slack_file_version") or {}
            size = file.get("size") or meta.get("size") or version.get("size")
            url = (
                ref.url
                if ingested_slack_file_id(meta)
                else slack_file_permalink(file.get("permalink")) or ref.url
            )
            own_meta = {
                **meta,
                "title": name,
                "size": size,
                "md5_checksum": file.get("md5") or meta.get("md5_checksum"),
                "slack_file_ids": [fid],
                "file_names": [name],
                drive_first.REFERENCE_KEY: ref.model_copy(
                    update={
                        "kind": "slack_attachment",
                        "title": name,
                        "url": url,
                        "slack_file_id": fid,
                        "slack_file_ids": [fid],
                        "slack_file_metadata": {
                            **ref.slack_file_metadata,
                            **{
                                key: file[key]
                                for key in ("is_public", "is_im", "is_mpim", "ims")
                                if key in file
                            },
                        },
                    }
                ),
            }
            enriched.append(replace(hit, metadata=own_meta))
        if len(enriched) == before and meta.get("source_type") == "slack":
            enriched.append(
                replace(
                    hit,
                    metadata={
                        **meta,
                        "files": [],
                        "slack_file_ids": [],
                        drive_first.REFERENCE_KEY: ref.model_copy(
                            update={
                                "kind": "slack_conversation",
                                "url": drive_first.canonical_reference(
                                    {"source_type": "slack", "source_uri": meta.get("source_uri")}
                                ).url,
                                "slack_file_id": None,
                                "slack_file_ids": [],
                            }
                        ),
                    },
                )
            )

    slack_names = {
        normalized_name(drive_first.canonical_reference(h.metadata, h.content).title)
        for h in enriched
        if drive_first.canonical_reference(h.metadata, h.content).kind == "slack_attachment"
    }
    verified_drive: set[str] = set()
    denied_drive: set[str] = set()
    drive = None
    drive_names = {
        normalized_name(ref.title)
        for h in enriched
        if (ref := drive_first.canonical_reference(h.metadata, h.content)).kind == "drive"
    }
    unresolved = [
        replace(h, metadata={"source_type": "slack", "title": ref.title})
        for h in enriched
        if (ref := drive_first.canonical_reference(h.metadata, h.content)).kind
        == "slack_attachment"
        and h.metadata.get("size")
        and normalized_name(ref.title) not in drive_names
    ]
    # 名前の照合だけでは付け替えない。候補の原本権限・名前・サイズも確認する。
    try:
        urls = skill._resolve_file_urls(unresolved, ctx) if unresolved else {}
    except Exception as exc:
        log.info("drive_copy_lookup_unavailable", error=type(exc).__name__)
        urls = {}
    for index, hit in enumerate(enriched):
        ref = drive_first.canonical_reference(hit.metadata, hit.content)
        copy_id = drive_first.drive_file_id(urls.get(ref.title))
        if ref.kind != "slack_attachment" or not copy_id or not hit.metadata.get("size"):
            continue
        try:
            drive = drive if drive is not None else _drive_reader(skill, ctx)
            if drive is None:
                continue
            info = drive.get_file_metadata(copy_id, ctx.request_id, for_ingest=True)
            if (
                info.get("id") == copy_id
                and not info.get("trashed")
                and normalized_name(str(info.get("name") or "")) == normalized_name(ref.title)
                and str(info.get("size")) == str(hit.metadata["size"])
                and not (
                    info.get("md5Checksum")
                    and hit.metadata.get("md5_checksum")
                    and info["md5Checksum"] != hit.metadata["md5_checksum"]
                )
            ):
                verified_drive.add(copy_id)
                enriched[index] = replace(
                    hit,
                    metadata={
                        **hit.metadata,
                        drive_first.REFERENCE_KEY: ref.model_copy(
                            update={
                                "kind": "drive",
                                "drive_file_id": copy_id,
                                "url": f"https://drive.google.com/file/d/{copy_id}/view",
                            }
                        ),
                    },
                )
        except Exception as exc:
            log.info("drive_copy_unavailable", error=type(exc).__name__)
    for index, hit in enumerate(enriched):
        ref = drive_first.canonical_reference(hit.metadata, hit.content)
        if (
            ref.kind != "drive"
            or normalized_name(ref.title) not in slack_names
            or not ref.drive_file_id
            or ref.drive_file_id in verified_drive
        ):
            continue
        try:
            if drive is None:
                drive = _drive_reader(skill, ctx)
                if drive is None:
                    continue
            info = drive.get_file_metadata(ref.drive_file_id, ctx.request_id, for_ingest=True)
            if info.get("id") != ref.drive_file_id or info.get("trashed"):
                denied_drive.add(ref.drive_file_id)
                continue
            verified_drive.add(ref.drive_file_id)
            enriched[index] = replace(
                hit,
                metadata={
                    **hit.metadata,
                    "size": info.get("size"),
                    "md5_checksum": info.get("md5Checksum"),
                },
            )
        except Exception as exc:
            # RLSで見える索引と現在の原本権限が違う場合は、取得できた原本を優先する。
            denied_drive.add(ref.drive_file_id)
            log.info("drive_file_unavailable", error=type(exc).__name__)

    result: list[SearchHit] = []
    for hit in enriched:
        ref = drive_first.canonical_reference(hit.metadata, hit.content)
        duplicate = None
        for index, prior in enumerate(result):
            old = drive_first.canonical_reference(prior.metadata, prior.content)
            same_id = bool(ref.slack_file_id and ref.slack_file_id == old.slack_file_id)
            cross_source = {ref.kind, old.kind} == {"drive", "slack_attachment"}
            size = hit.metadata.get("size")
            same_file = (
                cross_source
                and size is not None
                and str(size).isdigit()
                and int(size) > 0
                and str(size) == str(prior.metadata.get("size"))
                and normalized_name(ref.title) == normalized_name(old.title)
                and not (
                    hit.metadata.get("md5_checksum")
                    and prior.metadata.get("md5_checksum")
                    and hit.metadata["md5_checksum"] != prior.metadata["md5_checksum"]
                )
            )
            if same_id or same_file:
                preferred = (
                    hit
                    if ref.drive_file_id in verified_drive or ingested_slack_file_id(hit.metadata)
                    else prior
                )
                drive_id = ref.drive_file_id or old.drive_file_id
                slack_id = ref.slack_file_id or old.slack_file_id
                if same_file and drive_id not in verified_drive:
                    if drive_id not in denied_drive or slack_id not in verified_slack:
                        continue
                    preferred = hit if ref.kind == "slack_attachment" else prior
                duplicate = index
                result[index] = preferred
                break
        if duplicate is None:
            result.append(hit)
    return result
