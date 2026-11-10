"""Real PostgreSQL checkpoint, selection, insert-only and daily public metric checks."""

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from teamagent.adapters.apify_client import ApifyClient
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest import video_library_account_fetch as fetch
from teamagent.ingest.video_library_account_repository import AccountAddRepository
from tests.ingest.test_video_library_account_add_postgres import accounts, db
from tests.ingest.test_video_library_account_fetch import COVER, NOW, enabled, post, response
from tests.ingest.test_video_library_postgres import _DSN

__all__ = ["accounts", "db", "enabled"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def test_selection_dates_existing_video_and_latest_public_metrics(
    accounts: tuple[Any, AccountAddRepository],
    enabled: None,
) -> None:
    conn, repo = accounts
    for handle, operator, classification, last in [
        ("@SAMPLE", "NewsTV", None, None),
        ("sample", "gracemode", None, NOW.date()),
        ("recent", "NewsTV", None, NOW.date()),
        ("failed", "NewsTV", None, NOW.date() - timedelta(days=2)),
        ("inactive", "NewsTV", "現在未稼働です", None),
        ("foreign", "他社", None, None),
        ("x", "NewsTV", None, None),
        ("bad!", "NewsTV", None, None),
        ("a" * 25, "NewsTV", None, None),
    ]:
        conn.execute(
            """INSERT INTO vl_accounts(platform,handle,operator_label,classification,last_fetched_on)
               VALUES ('tiktok',%s,%s,%s,%s)""",
            (handle, operator, classification, last),
        )
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('instagram','other','NewsTV')"
    )
    conn.execute(
        """INSERT INTO vl_videos(platform,video_id,url,caption,origin,is_pr,account_handle)
           VALUES ('tiktok','123','https://www.tiktok.com/@sample/video/123','sheet caption','own',false,'sample')"""
    )
    before = conn.execute("SELECT * FROM vl_videos WHERE video_id='123'").fetchone()
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.side_effect = [
        RuntimeError("one batch fails"),
        response(),
        response(post(), post(vid="456", text="organic")),
    ]
    result = fetch.run(repo.pg, client, request_id="postgres-first", now=NOW)
    assert result.accounts == 3 and result.batches == 3 and result.failed_batches == 1
    assert result.inserted == 1 and result.metrics == 2
    assert [c.args[1]["oldestPostDateUnified"] for c in client.run_actor_sync.call_args_list] == [
        "2026-11-03",
        "2026-11-05",
        "2026-10-07",  # first fetch looks back 30 days only
    ]
    assert [c.args[1]["profiles"] for c in client.run_actor_sync.call_args_list] == [
        ["failed"],
        ["recent"],
        ["sample"],
    ]
    assert [c.args[1]["resultsPerPage"] for c in client.run_actor_sync.call_args_list] == [
        10,
        10,
        10,
    ]
    assert [c.kwargs["max_items"] for c in client.run_actor_sync.call_args_list] == [10, 10, 10]
    fetched = {
        r["handle"]: r["last_fetched_on"]
        for r in conn.execute("SELECT * FROM vl_accounts").fetchall()
    }
    assert fetched["@SAMPLE"] == fetched["sample"] == fetched["recent"] == NOW.date()
    assert fetched["failed"] == NOW.date() - timedelta(days=2)
    assert fetched["inactive"] is None and fetched["foreign"] is None
    assert conn.execute("SELECT * FROM vl_videos WHERE video_id='123'").fetchone() == before
    new = conn.execute("SELECT * FROM vl_videos WHERE video_id='456'").fetchone()
    assert new["origin"] == "own" and "sheet" in new["ingest_sources"]
    assert not new["is_pr"] and new["page_is_ad"] is True
    assert new["duration_seconds"] == 12.5 and new["music_original"] is True
    assert new["url"] == "https://www.tiktok.com/@sample/video/456"
    client.run_actor_sync.side_effect = None
    client.run_actor_sync.return_value = response(
        post(playCount=222, diggCount=22), post(vid="456")
    )
    result = fetch.run(
        repo.pg, client, request_id="postgres-second", now=NOW + timedelta(minutes=1)
    )
    assert result.inserted == 0
    assert conn.execute("SELECT * FROM vl_videos WHERE video_id='123'").fetchone() == before
    metrics = conn.execute("SELECT * FROM vl_public_metrics ORDER BY video_id").fetchall()
    assert len(metrics) == 2
    assert metrics[0]["captured_on"] == NOW.date()
    assert metrics[0]["captured_at"] == NOW + timedelta(minutes=1)
    assert [
        metrics[0][k]
        for k in ("total_plays", "total_likes", "total_comments", "total_shares", "total_saves")
    ] == [222, 22, 2, 3, 4]
    assert metrics[0]["metric_observation"]["source"] == "public_page"
    assert conn.execute("SELECT count(*) AS n FROM vl_metrics").fetchone()["n"] == 0
    assert conn.execute("SELECT current_user AS role").fetchone()["role"] == "teamagent_app"
    assert (
        conn.execute("SELECT current_setting('app.user_role') AS role").fetchone()["role"]
        == "admin"
    )


def test_same_start_date_keeps_initial_and_incremental_separate(
    accounts: tuple[Any, AccountAddRepository],
    enabled: None,
) -> None:
    conn, repo = accounts
    conn.execute(
        """INSERT INTO vl_accounts(platform,handle,operator_label,last_fetched_on)
           VALUES ('tiktok','new','NewsTV',NULL), ('tiktok','daily','NewsTV','2026-10-08')"""
    )
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response()
    result = fetch.run(repo.pg, client, request_id="same-start", now=NOW)
    assert result.accounts == result.batches == 2
    calls = client.run_actor_sync.call_args_list
    assert [c.args[1]["profiles"] for c in calls] == [["daily"], ["new"]]
    assert [c.args[1]["oldestPostDateUnified"] for c in calls] == ["2026-10-07"] * 2
    assert [c.args[1]["resultsPerPage"] for c in calls] == [10, 10]


def test_covers_update_existing_and_new_videos_without_overwriting(
    accounts: tuple[Any, AccountAddRepository],
    enabled: None,
) -> None:
    conn, repo = accounts
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('tiktok','sample','NewsTV')"
    )
    conn.execute(
        """INSERT INTO vl_videos(platform,video_id,url,thumbnail_s3_key)
           VALUES ('tiktok','123','https://www.tiktok.com/@sample/video/123',NULL),
                  ('tiktok','456','https://www.tiktok.com/@sample/video/456','existing.jpg')"""
    )
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(
        post(videoMeta={"coverUrl": COVER}),
        post(vid="456", videoMeta={"coverUrl": COVER}),
        post(vid="789", videoMeta={"originalCoverUrl": COVER}),
    )
    s3 = MagicMock()
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff\xd8\xffimage"))
    ) as http_client:
        result = fetch.run(
            repo.pg,
            client,
            request_id="postgres-covers",
            now=NOW,
            storage=VideoLibraryStorage(bucket="private", client=s3),
            http_client=http_client,
        )
    assert result.failed_batches == 0 and result.covers_saved == 2
    assert result.covers_skipped == {"already_present": 1}
    assert s3.put_object.call_count == 2
    assert conn.execute(
        "SELECT video_id,thumbnail_s3_key FROM vl_videos ORDER BY video_id"
    ).fetchall() == [
        {"video_id": "123", "thumbnail_s3_key": "video-library/thumbs/tiktok/123.jpg"},
        {"video_id": "456", "thumbnail_s3_key": "existing.jpg"},
        {"video_id": "789", "thumbnail_s3_key": "video-library/thumbs/tiktok/789.jpg"},
    ]
