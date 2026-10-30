"""Daily entry point: python -m teamagent.ingest.video_library_sync [--dry-run]."""

from __future__ import annotations

import argparse
import os
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from teamagent.adapters.gsheets_client import GSheetsClient, TabRows
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.campaign_aggregate import POST_DATE_TAB_NAME
from teamagent.ingest.loader import load_ingest_sources
from teamagent.ingest.video_library import (
    ACCOUNTS_TABS,
    CATEGORY_NAMES,
    MASTER_TAB,
    SyncBatch,
    build_batch,
)
from teamagent.ingest.video_library_repository import VideoLibraryRepository
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)
# 本番イメージに tzdata が無いので IANA 名は使わず固定 +9 時間で日本時間を表す
JST = timezone(timedelta(hours=9))
SOURCES_PATH = Path(__file__).resolve().parents[3] / "data" / "ingest_sources.yaml"


def read_batch(
    sheets: GSheetsClient,
    *,
    request_id: str,
    now: datetime,
    sources_path: Path = SOURCES_PATH,
    business_categories: Mapping[str, str] = CATEGORY_NAMES,
) -> SyncBatch:
    sources = load_ingest_sources(sources_path)
    source = next((s for s in sources.gsheets if s.sheet_id_env == "SHORT_VIDEO_DB_SHEET_ID"), None)
    if source is None or len(source.tabs) != 1:
        raise ValueError("short_video_source_unconfigured")
    metadata = sheets.get_sheet_metadata(source.sheet_id, request_id)
    tab = next((t for t in metadata.tabs if t.gid == source.tabs[0].gid), None)
    if tab is None:
        raise ValueError("database_gid_missing")
    database = sheets.get_tab_rows(source.sheet_id, tab.title, request_id)
    # Raw is required: an API failure must not erase previously imported metadata.
    raw = sheets.get_tab_rows(source.sheet_id, POST_DATE_TAB_NAME, request_id)
    master = None
    if any(t.title == MASTER_TAB for t in metadata.tabs):
        master = sheets.get_tab_rows(source.sheet_id, MASTER_TAB, request_id)
    ledger = None
    missing_account_tabs = 0
    ledger_id = os.environ.get("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "").strip()
    if ledger_id:
        ledger_metadata = sheets.get_sheet_metadata(ledger_id, request_id)
        available_tabs = {tab.title for tab in ledger_metadata.tabs}
        tables = []
        for name in ACCOUNTS_TABS:
            if name not in available_tabs:
                missing_account_tabs += 1
                continue
            tables.append(sheets.get_tab_rows(ledger_id, name, request_id))
        headers = tuple(
            dict.fromkeys(header.strip() for table in tables for header in table.headers)
        )
        rows = []
        for table in tables:
            for row in table.rows:
                cells = dict(zip((header.strip() for header in table.headers), row, strict=False))
                rows.append(tuple(cells.get(header, "") for header in headers))
        ledger = TabRows(
            sheet_id=ledger_id,
            tab_name=" / ".join(table.tab_name for table in tables),
            headers=headers,
            rows=tuple(rows),
            row_count=len(rows),
        )
    batch = build_batch(
        database,
        raw,
        captured_on=now.astimezone(JST).date(),
        campaign_master=master,
        account_ledger=ledger,
        business_categories=business_categories,
    )
    if missing_account_tabs:
        batch.warnings["accounts_tab_missing"] = missing_account_tabs
    return batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Read sheets and print counts without DB access"
    )
    args = parser.parse_args(argv)
    # 他の実行口と同じく JSON 化はタスク定義の STRUCTLOG_FORMAT に任せ、強制再設定しない
    # （force=True は cache 済みロガーを差し替え、同一プロセスの他処理のログを壊す）
    configure_logging()
    request_id = f"vl-{uuid.uuid4().hex}"
    started_at = datetime.now(JST)
    started = time.perf_counter()
    pg = None
    repository = None
    batch = None
    with structlog.contextvars.bound_contextvars(request_id=request_id, skill="video_library_sync"):
        try:
            if not args.dry_run:
                pg = PgVectorClient.from_env()
                repository = VideoLibraryRepository(pg)
            categories = repository.business_categories() if repository else CATEGORY_NAMES
            batch = read_batch(
                GSheetsClient.from_env(),
                request_id=request_id,
                now=started_at,
                business_categories=categories,
            )
            for reason, count in batch.warnings.items():
                logger.warning("video_library_unset", reason=reason, count=count)
            changed = (
                repository.save(batch, request_id=request_id, started_at=started_at)
                if repository
                else 0
            )
            logger.info(
                "video_library_complete",
                dry_run=args.dry_run,
                videos_upserted=changed,
                **batch.summary(),
                latency_ms=int((time.perf_counter() - started) * 1000),
                token_usage=0,
                cost_usd=0,
            )
            return 0
        except Exception as exc:
            # Exception messages/tracebacks can contain SQL parameters or sheet cell contents.
            error = type(exc).__name__
            logger.error("video_library_failed", error=error)
            if repository:
                try:
                    repository.record_failure(
                        request_id=request_id, started_at=started_at, error=error, batch=batch
                    )
                except Exception as record_exc:
                    logger.error("video_library_run_record_failed", error=type(record_exc).__name__)
            return 1
        finally:
            if pg:
                pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
