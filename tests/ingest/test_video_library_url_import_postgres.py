"""A02/A06-A08: actual PostgreSQL, RLS, SKIP LOCKED, recovery and snapshot coexistence.

Only TEAMAGENT_TEST_DB_DSN is used. A disposable committed schema supports two sessions;
cleanup drops only that randomly named test schema, never public application tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest import video_library_url_repository as url_repository
from teamagent.ingest.video_library import Account, Campaign, Metrics, SyncBatch, Video
from teamagent.ingest.video_library_repository import VideoLibraryRepository
from teamagent.ingest.video_library_url_models import (
    ImportResult,
    URLImportError,
    normalize_url,
    parse_observation,
)
from teamagent.ingest.video_library_url_repository import URLImportRepository
from teamagent.ingest.video_library_url_worker import drain_queue
from tests.ingest.test_video_library_url_import import NOW, URL, item, page
from tests.migration_postgres import apply_video_library_migration

_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_PRODUCTION_FETCH_LOCK = url_repository.FETCH_LOCK
MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/migrations"
pytestmark = pytest.mark.skipif(
    not _DSN, reason="Actual DB validation requires TEAMAGENT_TEST_DB_DSN"
)


class SchemaPg(PgVectorClient):
    # These tables need no pgvector extension (CI deliberately uses vanilla PostgreSQL).
    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            self._apply_session(
                conn,
                kwargs.get("app_role"),
                kwargs.get("user_email"),
                kwargs.get("user_groups"),
                kwargs.get("user_role"),
                kwargs.get("application_name"),
            )
            yield conn


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PgVectorClient, URLImportRepository]]:
    assert _DSN
    schema = "vl_url_test_" + uuid.uuid4().hex
    with psycopg.connect(_DSN, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            schema_oid = owner.execute(
                "SELECT oid FROM pg_namespace WHERE nspname=%s", (schema,)
            ).fetchone()[0]
            # Advisory locks are database-wide; UUID schemas alone do not isolate workers.
            monkeypatch.setattr(url_repository, "FETCH_LOCK", (0x564C << 32) | schema_oid)
            owner.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema)))
            owner.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
            )
            with owner.transaction():
                for name in (
                    "0035_video_library.sql",
                    "0036_video_library_settings.sql",
                    "0037_video_library_url_import.sql",
                    "0038_video_campaign_links.sql",
                    "0039_video_library_import_ui.sql",
                    "0040_video_library_fetch_resume.sql",
                    "0041_video_library_external_cases.sql",
                    "0042_video_library_account_add.sql",
                    "0043_video_library_insights.sql",
                    "0044_library_mcp_oauth.sql",
                    "0045_video_library_case_discovery.sql",
                ):
                    apply_video_library_migration(owner, MIGRATIONS / name)
            pg = SchemaPg(make_conninfo(_DSN, options=f"-csearch_path={schema},public"))
            yield pg, URLImportRepository(pg)
        finally:
            owner.execute("RESET ROLE")
            owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            owner.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier("mcp_owner_" + schema))
            )


def test_fetch_lock_isolated_from_other_tests_but_shared_within_one_test(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    assert _DSN
    with psycopg.connect(_DSN, autocommit=True) as other_test:
        other_test.execute("SELECT pg_advisory_lock(%s)", (_PRODUCTION_FETCH_LOCK,))
        with repo.fetch_lock() as acquired:
            assert acquired  # Another schema's worker must not make drain_queue return zero.
            with repo.fetch_lock() as competing_worker:
                assert not competing_worker  # Replicas within this test still share one lock.
        with repo.fetch_lock() as reacquired:
            assert reacquired


def save(repo: URLImportRepository, *, at: datetime = NOW, **changes: Any) -> ImportResult:
    observation = parse_observation(page(item(**changes)), normalize_url(URL), at)
    return repo.save(
        observation,
        source="url_manual",
        requested_by="member@example.test",
        thumbnail="thumbnail",
        result=ImportResult(status="succeeded", video_id="123"),
    )


def sheet(pg: PgVectorClient) -> None:
    VideoLibraryRepository(pg).save(
        SyncBatch(
            campaigns=[Campaign(advertiser="advertiser", campaign="campaign")],
            accounts=[
                Account(platform="tiktok", handle="sample", operator_label="ledger operator")
            ],
            videos=[
                Video(
                    platform="tiktok",
                    video_id="123",
                    url=URL,
                    caption="sheet #PR",
                    hashtags=["PR"],
                    is_pr=True,
                    advertiser="advertiser",
                    campaign="campaign",
                )
            ],
            metrics=[
                Metrics(
                    platform="tiktok",
                    video_id="123",
                    captured_on=NOW.date(),
                    organic_plays=11,
                    total_plays=15,
                    ad_cost=200,
                )
            ],
        ),
        request_id=uuid.uuid4().hex,
        started_at=NOW,
    )


@pytest.mark.parametrize("url_first", [True, False])
def test_a06_sheet_always_wins_and_page_public_settings_analysis_survive(
    db: tuple[PgVectorClient, URLImportRepository],
    url_first: bool,
) -> None:
    pg, repo = db
    if url_first:
        save(repo, desc="URL #ad", isAd=True)
        sheet(pg)
    else:
        sheet(pg)
        save(repo, desc="URL #ad", isAd=True)
    with repo.connection() as conn:
        video = conn.execute("SELECT * FROM vl_videos").fetchone()
        assert video is not None
        assert (video["caption"], video["is_pr"], video["origin"], video["campaign"]) == (
            "sheet #PR",
            True,
            "own",
            "campaign",
        )
        assert video["hashtags"] == ["PR"] and video["thumbnail_s3_key"] == "thumbnail"
        assert set(video["ingest_sources"]) == {"sheet", "url_manual"}
        assert video["page_is_ad"] is True and video["page_disclosure"]["caption"] == "URL #ad"
        assert conn.execute(
            "SELECT total_plays,organic_plays,ad_cost FROM vl_metrics"
        ).fetchone() == {"total_plays": 15, "organic_plays": 11, "ad_cost": 200}
        assert conn.execute("SELECT total_plays FROM vl_public_metrics").fetchone() == {
            "total_plays": 100
        }
        assert conn.execute("SELECT operator_label FROM vl_accounts").fetchone() == {
            "operator_label": "ledger operator"
        }
        conn.execute(
            "INSERT INTO vl_campaign_settings(advertiser,campaign,is_private,updated_by) VALUES ('advertiser','campaign',true,'member@example.test')"
        )
        conn.execute(
            "INSERT INTO vl_video_analysis VALUES ('tiktok','123','structure','v1','{}',now())"
        )
        first = video["first_ingested_at"]
    save(repo, at=NOW + timedelta(hours=1), desc="changed #ad", isAd=False)
    sheet(pg)
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT first_ingested_at FROM vl_videos").fetchone()["first_ingested_at"]
            == first
        )
        assert (
            conn.execute("SELECT is_private FROM vl_campaign_settings").fetchone()["is_private"]
            is True
        )
        assert conn.execute("SELECT count(*) AS n FROM vl_video_analysis").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM vl_video_analysis_requests").fetchone()["n"]
            == 1
        )


def test_a07_snapshot_unit_partial_failure_and_jst_date(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    at = datetime(2026, 11, 2, 14, 59, tzinfo=UTC)
    save(repo, at=at)
    stats = item()["stats"]
    stats["playCount"], stats["collectCount"] = "200", None
    save(repo, at=at + timedelta(seconds=30), stats=stats, isAd=None)
    with repo.connection() as conn:
        stored = conn.execute("SELECT * FROM vl_public_metrics").fetchone()
        assert stored["total_plays"] == 100 and stored["total_saves"] == 4
        assert stored["captured_at"] == at
        assert conn.execute("SELECT page_is_ad FROM vl_videos").fetchone()["page_is_ad"] is None
    save(repo, at=at + timedelta(minutes=2), stats=stats)
    with repo.connection() as conn:
        values = conn.execute(
            "SELECT captured_on,total_plays,total_saves FROM vl_public_metrics ORDER BY captured_on"
        ).fetchall()
        assert [(str(r["captured_on"]), r["total_plays"], r["total_saves"]) for r in values] == [
            ("2026-11-02", 100, 4),
            ("2026-11-03", 200, None),
        ]
    repo.record_failure(
        ImportResult(status="failed", video_id="123", error_code="page_data_missing")
    )
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM vl_public_metrics").fetchone()["n"] == 2
        video = conn.execute(
            "SELECT page_is_ad,page_fetch_status,page_fetch_error_code,page_observed_at FROM vl_videos"
        ).fetchone()
        assert (
            video["page_fetch_status"] == "failed"
            and video["page_fetch_error_code"] == "page_data_missing"
        )
        assert video["page_observed_at"] == at + timedelta(minutes=2)
    save(repo, at=at + timedelta(minutes=3), stats=item()["stats"])
    with repo.connection() as conn:
        assert (
            conn.execute(
                "SELECT total_plays FROM vl_public_metrics ORDER BY captured_on DESC LIMIT 1"
            ).fetchone()["total_plays"]
            == 100
        )


def test_a08_claim_skips_locked_row_in_another_session(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    batch = repo.submit(
        URL + "\n" + URL.replace("123", "124"),
        requested_by="member@example.test",
        idempotency_key="claim",
    )
    # Hold the first queue row exactly as a concurrent claimant would, without committing.
    with repo.connection() as conn:
        conn.execute(
            "SELECT * FROM vl_url_import_rows WHERE batch_id=%s AND line_no=1 FOR UPDATE", (batch,)
        ).fetchone()
        other = repo.claim()
        assert other is not None and other["line_no"] == 2
    first = repo.claim()
    assert first is not None and first["line_no"] == 1
    assert first["lease_token"] != other["lease_token"]
    assert repo.claim() is None


def test_a08_two_workers_claim_distinct_rows(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    repo.submit(
        URL + "\n" + URL.replace("123", "124"),
        requested_by="member@example.test",
        idempotency_key="race",
    )
    barrier = Barrier(2)

    def claim() -> Any:
        barrier.wait(timeout=5)
        return repo.claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: claim(), range(2)))
    assert a and b and a["line_no"] != b["line_no"]


def test_a08_processing_recovery_fences_old_worker(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    repo.submit(URL, requested_by="member@example.test", idempotency_key="recover")
    old = repo.claim()
    assert old
    with repo.connection() as conn:
        conn.execute(
            "UPDATE vl_url_import_rows SET started_at=clock_timestamp()-interval '11 minutes'"
        )
    new = repo.claim()
    assert new and new["lease_token"] != old["lease_token"]
    with pytest.raises(URLImportError, match="lease_lost"):
        repo.finish_failure(old, ImportResult(status="failed", error_code="network_error"))
    repo.finish_failure(new, ImportResult(status="failed", error_code="network_error"))
    assert repo.claim() is None


def test_a08_daily_budget_counts_failed_retry_and_defers(
    db: tuple[PgVectorClient, URLImportRepository], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo = db
    monkeypatch.setattr("teamagent.ingest.video_library_url_repository.time.sleep", lambda _: None)
    repo.submit(
        URL + "\n" + URL.replace("123", "124"),
        requested_by="member@example.test",
        idempotency_key="quota",
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    monkeypatch.setattr("teamagent.ingest.video_library_url_import.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert drain_queue(repo, client, MagicMock(), daily_limit=2) == 2
    assert len(requests) == 2
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT used_count FROM vl_public_fetch_daily_usage").fetchone()[
                "used_count"
            ]
            == 2
        )
        rows = conn.execute(
            "SELECT status,attempt_count,eligible_at FROM vl_url_import_rows ORDER BY line_no"
        ).fetchall()
        assert [r["status"] for r in rows] == ["deferred", "deferred"]
        assert [r["attempt_count"] for r in rows] == [2, 0]
        assert all(r["eligible_at"] > datetime.now(UTC) for r in rows)
    assert repo.claim() is None


@pytest.mark.parametrize("status", [403, 429, 200])
def test_a08_pause_is_durable_and_leaves_remaining_queue(
    db: tuple[PgVectorClient, URLImportRepository],
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repo = db
    monkeypatch.setattr("teamagent.ingest.video_library_url_repository.time.sleep", lambda _: None)
    repo.submit(
        "\n".join(URL.replace("123", str(i)) for i in range(8)),
        requested_by="member@example.test",
        idempotency_key="pause",
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text="<html>" + " " * 1387 + "</html>")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert drain_queue(repo, client, MagicMock()) == (5 if status == 200 else 1)
        before = len(requests)
        assert drain_queue(repo, client, MagicMock()) == 0
        assert len(requests) == before
    with repo.connection() as conn:
        state = conn.execute("SELECT paused,pause_reason FROM vl_public_fetch_control").fetchone()
        assert state["paused"] is True
        assert state["pause_reason"] == (
            "consecutive_failures" if status == 200 else f"http_{status}"
        )
        assert conn.execute("SELECT count(*) AS n FROM vl_public_metrics").fetchone()["n"] == 0


def test_a02_intake_idempotence_short_resolution_and_all_input_results(
    db: tuple[PgVectorClient, URLImportRepository], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo = db
    monkeypatch.setattr("teamagent.ingest.video_library_url_repository.time.sleep", lambda _: None)
    text = "https://vt.tiktok.com/short/\n" + URL + "?query=1\nhttps://vm.tiktok.com/other/\nbad"
    first = repo.submit(text, requested_by="member@example.test", idempotency_key="same")
    assert repo.submit(text, requested_by="member@example.test", idempotency_key="same") == first
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host in {"vt.tiktok.com", "vm.tiktok.com"}:
            return httpx.Response(302, headers={"location": URL})
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=page())
        return httpx.Response(200, content=b"\xff\xd8\xffimage")

    storage = MagicMock()
    storage.put.return_value = "thumbnail"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert drain_queue(repo, client, storage) == 3
    assert sum(r.url.host == "www.tiktok.com" for r in requests) == 1
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM vl_videos").fetchone()["n"] == 1
        rows = conn.execute("SELECT status FROM vl_url_import_rows ORDER BY line_no").fetchall()
        assert [r["status"] for r in rows] == ["duplicate", "succeeded", "duplicate", "invalid"]
        assert (
            conn.execute("SELECT count(*) AS n FROM vl_video_analysis_requests").fetchone()["n"]
            == 1
        )


def test_a05_unknown_caption_not_published_or_queued(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    _, repo = db
    result = save(repo, desc=None)
    assert result.status == "classification_pending"
    with repo.connection() as conn:
        for table in ("vl_videos", "vl_public_metrics", "vl_video_analysis_requests"):
            assert (
                conn.execute(
                    sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
                ).fetchone()["n"]
                == 0
            )


def test_rls_all_employees_insert_select_but_do_not_update_results(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    pg, repo = db
    batch = repo.submit(URL, requested_by="member@example.test", idempotency_key="viewer")
    with pg.connection(
        app_role="teamagent_app", user_email="other@example.test", user_role="viewer"
    ) as conn:
        assert conn.execute("SELECT current_user AS role").fetchone()["role"] == "teamagent_app"
        assert conn.execute("SELECT count(*) AS n FROM vl_url_import_batches").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM vl_url_import_rows").fetchone()["n"] == 1
        conn.execute(
            "INSERT INTO vl_public_fetch_daily_usage VALUES ('tiktok','2000-01-01',0,now())"
        )
        assert (
            conn.execute("SELECT used_count FROM vl_public_fetch_daily_usage").fetchone()[
                "used_count"
            ]
            == 0
        )
        assert (
            conn.execute(
                "UPDATE vl_url_import_rows SET status='succeeded' WHERE batch_id=%s", (batch,)
            ).rowcount
            == 0
        )
        assert conn.execute("UPDATE vl_public_fetch_control SET paused=false").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO vl_video_analysis_requests(platform,video_id,kind,version,requested_by,source) VALUES ('tiktok','x','structure','v1','other@example.test','url_manual')"
            )


def test_view_column_contract_unchanged_and_campaign_creator_preserved(
    db: tuple[PgVectorClient, URLImportRepository],
) -> None:
    pg, repo = db
    with repo.connection() as conn:
        cursor = conn.execute("SELECT * FROM vl_videos_effective LIMIT 0")
        names = [column.name for column in cursor.description or ()]
        # Existing columns keep their order; 0041 appends the external fields after them.
        start = names.index("business_category")
        assert names[start : start + 5] == [
            "business_category",
            "is_private",
            "industry",
            "purpose",
            "case_inferred",
        ]
        assert names[start + 5 :] == ["external_advertiser", "is_external_case"]
        assert "page_is_ad" not in names
        conn.execute(
            "INSERT INTO vl_campaigns VALUES ('advertiser','campaign','member@example.test',now(),'url_import')"
        )
    sheet(pg)
    with repo.connection() as conn:
        assert conn.execute("SELECT created_by,created_via FROM vl_campaigns").fetchone() == {
            "created_by": "member@example.test",
            "created_via": "url_import",
        }
