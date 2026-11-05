"""Drive の通常取込入力から、人手確認用 source_ref と引用候補を JSON 出力する。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from teamagent.adapters.gdrive_client import (
    DEFAULT_EXCLUDE_FOLDER_NAME_RE,
    GDriveClient,
    drive_file_from_metadata,
)
from teamagent.ingest.case_state_evidence import source_positions, source_ref
from teamagent.ingest.loader import IngestSources, load_ingest_sources
from teamagent.ingest.pipeline import (
    _gdrive_source_metadata,
    _is_sales_relevant,
    _prepare_gdrive_file,
    _resolve_drive_file_acl,
)
from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert


def prepare_source(
    file_id: str, *, client: GDriveClient, sources: IngestSources, request_id: str
) -> tuple[DocumentUpsert, list[ChunkUpsert]]:
    """DB と分類器を作らず、設定された取込経路の metadata と抽出処理を再利用する。"""
    raw = client.get_file_metadata(file_id, request_id, for_ingest=True)
    if raw.get("trashed") or raw.get("mimeType") == "application/vnd.google-apps.folder":
        raise ValueError("expected a non-trashed Drive file")
    f = drive_file_from_metadata(raw)
    excluded = sources.gdrive_exclude_folder_name_re
    exclude_re = re.compile(DEFAULT_EXCLUDE_FOLDER_NAME_RE if excluded is None else excluded)
    ancestors: dict[str, dict[str, Any]] = {}

    def paths(parent_ids: tuple[str, ...], visited: frozenset[str]) -> list[tuple[str, ...]]:
        if not parent_ids:
            return [()]
        result = []
        for parent_id in parent_ids:
            if parent_id in visited or len(ancestors) >= 256:
                raise ValueError("Drive ancestry is incomplete or cyclic")
            if parent_id not in ancestors:
                ancestors[parent_id] = client.get_file_metadata(parent_id, request_id)
            for tail in paths(
                tuple(ancestors[parent_id].get("parents") or ()), visited | {parent_id}
            ):
                result.append((parent_id, *tail))
        return result

    ancestry = paths(f.parents, frozenset({file_id}))
    metadata = None
    for spec in sources.gdrive_folders:
        if spec.mime_type_filter and spec.mime_type_filter != f.mime_type:
            continue
        for path in ancestry:
            if spec.folder_id not in path:
                continue
            depth = path.index(spec.folder_id)
            if depth and not spec.include_subfolders:
                continue
            if any(exclude_re.search(str(ancestors[p].get("name", ""))) for p in path[:depth]):
                continue
            metadata = _gdrive_source_metadata(spec)
            break

    # IngestRunner と同じ順番で、後に走る crawl の metadata を優先する。
    crawl = sources.shared_drives_crawl
    if crawl and crawl.enabled and raw.get("driveId"):
        relevant = (
            not crawl.sales_relevance_filter
            or _is_sales_relevant(f, modified_within_days=crawl.modified_within_days)[0]
        )
        if relevant and any(
            not any(exclude_re.search(str(ancestors[p].get("name", ""))) for p in path[:-1])
            for path in ancestry
        ):
            for drive in client.list_shared_drives(request_id=request_id):
                if drive.id == raw["driveId"] and (
                    not crawl.name_filter or any(name in drive.name for name in crawl.name_filter)
                ):
                    metadata = _gdrive_source_metadata(crawl, drive=drive)
                    break
    if metadata is None:
        raise ValueError("file does not match a configured Drive ingest source")
    owner, emails, groups = _resolve_drive_file_acl(
        client=client,
        file_id=f.id,
        request_id=request_id,
        fallback_owner_email=(
            f.owners_email[0]
            if f.owners_email
            else os.environ.get("INGEST_OWNER_EMAIL", "noreply@vectorinc.co.jp")
        ),
    )
    prepared = _prepare_gdrive_file(
        f,
        client=client,
        source_metadata=metadata,
        file_owner_email=owner,
        acl_emails=emails,
        acl_groups=groups,
        request_id=request_id,
    )
    if prepared is None:
        raise ValueError("Drive extraction failed; no review source_ref was produced")
    return prepared


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_id", help="Drive file ID")
    parser.add_argument("--yaml", type=Path, default=Path("data/ingest_sources.yaml"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.file_id):
        parser.error("invalid Drive file ID")
    # 既存 adapter のログは stderr に送り、stdout は JSON だけにする。
    with redirect_stdout(sys.stderr):
        doc, chunks = prepare_source(
            args.file_id,
            client=GDriveClient.from_env(readonly=True),
            sources=load_ingest_sources(args.yaml),
            request_id=f"case-state-ref-{uuid.uuid4().hex[:12]}",
        )
    ref = source_ref(doc, chunks)
    print(
        json.dumps(
            {
                **ref.model_dump(mode="json"),
                "positions": [
                    {"locator": locator, "excerpt": text[:120]}
                    for locator, text in source_positions(ref, chunks).items()
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
