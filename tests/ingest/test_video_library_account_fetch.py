"""Nightly account discovery, with transactional storage and Apify failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient, ApifyRunResult
from teamagent.adapters.video_library_page import MAX_IMAGE_BYTES
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest import video_library_account_fetch as fetch
from teamagent.ingest.video_library_url_repository import JST

NOW = datetime(2026, 11, 6, 23, tzinfo=JST)


def post(handle: str = "sample", vid: str = "123", **values: Any) -> dict[str, Any]:
    return {
        "id": vid,
        "authorMeta": {"name": handle, "nickName": "表示名"},
        "webVideoUrl": f"https://www.tiktok.com/@{handle}/video/{vid}?tracking=1",
        "text": "caption #ＰＲ #test #test",
        "createTimeISO": "2026-11-05T01:00:00Z",
        "videoMeta": {"duration": 12.5},
        "musicMeta": {"musicName": "曲", "musicAuthor": "作者", "musicOriginal": True},
        "playCount": "100",
        "diggCount": 10,
        "commentCount": 2,
        "shareCount": 3,
        "collectCount": 4,
        "isAd": True,
        **values,
    }


def response(*items: dict[str, Any], cost_usd: float = 0.12) -> ApifyRunResult:
    return ApifyRunResult(list(items), ACTOR_TIKTOK_VIDEO, "fake-run", "SUCCEEDED", cost_usd)


class FakePg:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.videos: dict[str, tuple[Any, ...]] = {}
        self.metrics: dict[tuple[str, Any], tuple[Any, ...]] = {}
        self.fetched: dict[str, Any] = {}
        self.fail_save = False
        self.thumbnails: dict[str, str] = {}
        self.in_transaction = False

    @contextmanager
    def connection(self, **session: Any) -> Iterator[MagicMock]:
        assert session == dict(app_role="teamagent_app", user_email=None, user_role="admin")
        assert not self.in_transaction
        self.in_transaction = True
        before = deepcopy((self.videos, self.metrics, self.fetched, self.thumbnails))
        conn = MagicMock()
        conn.execute.side_effect = self.execute
        try:
            yield conn
        except Exception:
            self.videos, self.metrics, self.fetched, self.thumbnails = before
            raise
        finally:
            self.in_transaction = False

    def execute(self, query: str, params: tuple[Any, ...]) -> MagicMock:
        if "SELECT thumbnail_s3_key" in query:
            row = (
                {"thumbnail_s3_key": self.thumbnails.get(params[1])}
                if params[1] in self.videos
                else None
            )
            return MagicMock(fetchone=lambda: row)
        if "UPDATE vl_videos SET thumbnail_s3_key" in query:
            assert "AND thumbnail_s3_key IS NULL" in query
            key, platform, video_id = params
            assert platform == "tiktok"
            updated = video_id in self.videos and video_id not in self.thumbnails
            if updated:
                self.thumbnails[video_id] = key
            return MagicMock(rowcount=int(updated))
        if query.startswith("SELECT"):
            return MagicMock(fetchall=lambda: self.rows)
        if "INSERT INTO vl_videos" in query:
            exists = params[0] in self.videos
            self.videos.setdefault(params[0], params)
            return MagicMock(rowcount=int(not exists))
        if "INSERT INTO vl_public_metrics" in query:
            if self.fail_save:
                raise RuntimeError("private database input")
            self.metrics[params[:2]] = params
            return MagicMock(rowcount=1)
        assert "UPDATE vl_accounts" in query
        for handle in params[-1]:
            self.fetched[handle] = params[0]
        return MagicMock(rowcount=len(params[-1]))


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNT_FETCH_ENABLED", "1")
    monkeypatch.setenv("APIFY_API_TOKEN", "fake")
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNT_FETCH_MAX_USD", "10")


def test_failed_batch_retries_success_updates_and_bad_items_are_counted(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = FakePg(
        [
            {"handle": f"a{i:02}", "oldest": NOW.date().replace(day=4), "initial": False}
            for i in range(21)
        ]
        + [{"handle": "sample", "oldest": NOW.date().replace(month=1, day=1), "initial": True}]
    )
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.side_effect = [
        RuntimeError("caption https://private.test employee@example.test"),
        response(),
        response(
            post(), post(playCount=200), post("unrequested"), post(id=None), post(authorMeta={})
        ),
    ]
    log = MagicMock()
    monkeypatch.setattr(fetch, "logger", log)
    result = fetch.run(pg, client, request_id="nightly", now=NOW)  # type: ignore[arg-type]
    assert result.accounts == 22 and result.batches == 3 and result.failed_batches == 1
    assert pg.fetched == {"a20": NOW.date(), "sample": NOW.date()}
    assert result.items == 5 and result.inserted == 1 and result.metrics == 2
    assert result.skipped == {"unrequested_handle": 1, "missing_required": 2}
    assert result.cost_usd == pytest.approx(0.24)
    assert pg.videos["123"][-2:] == (True, True)
    assert len(pg.metrics) == 1 and pg.metrics[("123", NOW.date())][3:8] == (200, 10, 2, 3, 4)
    calls = client.run_actor_sync.call_args_list
    assert [c.args[1]["oldestPostDateUnified"] for c in calls] == [
        "2026-11-04",
        "2026-11-04",
        "2026-01-01",
    ]
    assert [len(c.args[1]["profiles"]) for c in calls] == [20, 1, 1]
    assert calls[0].args[0] == ACTOR_TIKTOK_VIDEO
    assert calls[0].args[1] == {
        "profiles": [f"a{i:02}" for i in range(20)],
        "resultsPerPage": 10,
        "profileSorting": "latest",
        "profileScrapeSections": ["videos"],
        "excludePinnedPosts": True,
        "oldestPostDateUnified": "2026-11-04",
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadVideos": False,
    }
    assert calls[0].kwargs == dict(
        max_items=200, deadline_s=900, request_id="nightly", usage_mode="tiktok_profiles_date"
    )
    assert "private.test" not in str(log.mock_calls) and "employee@example.test" not in str(
        log.mock_calls
    )
    final = log.bind.return_value.info.call_args.kwargs
    assert final["failed_batches"] == 1 and final["latency_ms"] >= 0
    assert log.bind.call_args.kwargs["token_usage"] == 0


@pytest.mark.parametrize(
    "initial,per_account,budget,attempts",
    [
        (False, 10, "0", 0),
        (False, 10, "0.79", 0),
        (False, 10, "0.80", 1),
        (False, 10, "0.84", 2),
        (True, 30, "2.39", 0),
        (True, 30, "2.40", 1),
        (True, 30, "2.52", 2),
    ],
)
def test_budget_uses_actual_spend_and_size_of_next_batch(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
    per_account: int,
    budget: str,
    attempts: int,
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNT_FETCH_MAX_USD", budget)
    pg = FakePg(
        [{"handle": f"a{i:02}", "oldest": NOW.date(), "initial": initial} for i in range(21)]
    )
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.side_effect = [
        response(cost_usd=2.4 if initial else 0.8),
        response(cost_usd=0.12 if initial else 0.04),
    ]
    result = fetch.run(pg, client, request_id="budget", now=NOW)  # type: ignore[arg-type]
    assert client.run_actor_sync.call_count == attempts
    assert result.reason == (None if attempts == 2 else "cost_limit")
    assert len(pg.fetched) == min(attempts * 20, 21)
    if attempts == 2:
        assert client.run_actor_sync.call_args.kwargs["max_items"] == per_account


@pytest.mark.parametrize("cost_usd,attempts", [(0.05, 13), (0.25, 12), (0.8, 6)])
def test_260_accounts_incremental_first_and_stop_preserves_unfetched_checkpoints(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    cost_usd: float,
    attempts: int,
) -> None:
    monkeypatch.delenv("VIDEO_LIBRARY_ACCOUNT_FETCH_MAX_USD")
    pg = FakePg(
        [
            {"handle": f"new{i:03}", "oldest": NOW.date().replace(month=1, day=1), "initial": True}
            for i in range(20)
        ]
        + [
            {"handle": f"daily{i:03}", "oldest": NOW.date() - timedelta(days=2), "initial": False}
            for i in range(240)
        ]
    )
    pg.fetched = {
        row["handle"]: None if row["initial"] else NOW.date() - timedelta(days=1) for row in pg.rows
    }
    before = dict(pg.fetched)
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(cost_usd=cost_usd)
    result = fetch.run(pg, client, request_id="260-accounts", now=NOW)  # type: ignore[arg-type]
    assert result.accounts == 260 and result.batches == attempts and result.failed_batches == 0
    assert result.cost_usd == pytest.approx(attempts * cost_usd)
    assert result.reason == (None if attempts == 13 else "cost_limit")
    calls = client.run_actor_sync.call_args_list
    fetched = set()
    for index, call in enumerate(calls):
        initial = index == 12
        profiles = call.args[1]["profiles"]
        assert len(profiles) == 20
        assert all(h.startswith("new" if initial else "daily") for h in profiles)
        assert call.args[1]["resultsPerPage"] == (30 if initial else 10)
        assert call.kwargs["max_items"] == (600 if initial else 200)
        fetched.update(profiles)
    assert len(fetched) == attempts * 20
    assert pg.fetched == {
        handle: NOW.date() if handle in fetched else last for handle, last in before.items()
    }


@pytest.mark.parametrize("reason", ["disabled", "apify_unconfigured"])
def test_skip_never_opens_db_or_calls_apify(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    monkeypatch.setattr(fetch, "configure_logging", lambda: None)
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNT_FETCH_ENABLED", "0" if reason == "disabled" else "1")
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    pg, client, log = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(fetch, "logger", log)
    monkeypatch.setattr(fetch.PgVectorClient, "from_env", pg)
    monkeypatch.setattr(fetch.ApifyClient, "from_env", client)
    assert fetch.main([]) == 0
    assert log.info.call_args.args == ("video_library_account_fetch_skipped",)
    assert log.info.call_args.kwargs["reason"] == reason
    assert fetch.run(pg, client, request_id="skip").reason == reason
    assert not pg.mock_calls and not client.mock_calls


def test_storage_failure_rolls_back_video_and_checkpoint(enabled: None) -> None:
    pg = FakePg([{"handle": "sample", "oldest": NOW.date(), "initial": False}])
    pg.fail_save = True
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(post())
    result = fetch.run(pg, client, request_id="db-failure", now=NOW)  # type: ignore[arg-type]
    assert result.failed_batches == 1 and result.inserted == result.metrics == 0
    assert not pg.videos and not pg.metrics and not pg.fetched
    assert result.cost_usd == 0.12


def test_is_ad_does_not_imply_hashtag_pr(enabled: None) -> None:
    assert fetch._video(post(text="organic", isAd=True), {"sample"}).is_pr is False
    assert fetch._video(post(), {"sample"}).is_pr is True


@pytest.mark.parametrize("value", [True, -1, "bad", float("nan"), float("inf"), 2**63, {}])
def test_invalid_metrics_are_missing(value: Any) -> None:
    assert fetch._metric(value) is None


def test_main_reports_partial_failure_and_closes_pg(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "configure_logging", lambda: None)
    pg = MagicMock()
    monkeypatch.setattr(fetch.PgVectorClient, "from_env", lambda: pg)
    monkeypatch.setattr(fetch.ApifyClient, "from_env", MagicMock())
    monkeypatch.setattr(fetch, "run", lambda *a, **k: fetch.FetchResult(failed_batches=1))
    assert fetch.main([]) == 1
    pg.close.assert_called_once()


COVER = "https://p16-sign-sg.tiktokcdn.com/cover.jpeg?signature=private"


def test_covers_save_independently_after_batch_commit(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = FakePg([{"handle": "sample", "oldest": NOW.date(), "initial": False}])
    pg.videos["1"] = ()
    pg.thumbnails["1"] = "existing.jpg"
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(
        post(vid="1", videoMeta={"coverUrl": COVER + "&id=1"}),
        post(vid="2", videoMeta={}),
        post(vid="3", videoMeta={"coverUrl": "http://169.254.169.254/private"}),
        post(vid="4", videoMeta={"coverUrl": COVER + "&id=4"}),
        post(vid="5", videoMeta={"coverUrl": COVER + "&id=5"}),
        post(vid="6", videoMeta={"coverUrl": COVER + "&id=6"}),
        post(vid="7", videoMeta={"coverUrl": COVER + "&id=7"}),
        post(vid="8", videoMeta={"originalCoverUrl": COVER + "&id=8"}),
        post(vid="9", videoMeta={"coverUrl": "", "originalCoverUrl": COVER + "&id=9"}),
        post(
            vid="10",
            videoMeta={"coverUrl": COVER + "&id=10", "originalCoverUrl": "https://invalid.test"},
        ),
    )
    s3 = MagicMock()
    storage = VideoLibraryStorage(bucket="private", client=s3)
    log = MagicMock()
    monkeypatch.setattr(fetch, "logger", log)
    requested = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert not pg.in_transaction
        assert (
            len(pg.videos) == 10 and len(pg.metrics) == 10 and pg.fetched == {"sample": NOW.date()}
        )
        vid = request.url.params["id"]
        requested.append(vid)
        if vid == "4":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nnot jpeg")
        if vid == "5":
            return httpx.Response(403, text="Forbidden")
        if vid == "6":
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES)
        return httpx.Response(200, content=b"\xff\xd8\xffimage")

    def put(**kwargs: Any) -> None:
        assert not pg.in_transaction
        if kwargs["Key"].endswith("/7.jpg"):
            raise RuntimeError("https://private.test/secret")

    s3.put_object.side_effect = put
    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        result = fetch.run(
            pg, client, request_id="covers", now=NOW, storage=storage, http_client=http_client
        )  # type: ignore[arg-type]
    assert result.failed_batches == 0 and result.metrics == 10
    assert result.covers_saved == 3
    assert result.covers_skipped == {
        "already_present": 1,
        "cover_missing": 1,
        "invalid_cover_url": 1,
        "invalid_cover_image": 1,
        "http_403": 1,
        "cover_too_large": 1,
        "RuntimeError": 1,
    }
    assert requested == ["4", "5", "6", "7", "8", "9", "10"]
    assert pg.thumbnails == {
        "1": "existing.jpg",
        "8": "video-library/thumbs/tiktok/8.jpg",
        "9": "video-library/thumbs/tiktok/9.jpg",
        "10": "video-library/thumbs/tiktok/10.jpg",
    }
    output = str(log.mock_calls)
    assert "https://" not in output and "169.254" not in output and "signature" not in output
    final = log.bind.return_value.info.call_args.kwargs
    assert final["covers_saved"] == 3 and final["covers_skipped"] == result.covers_skipped


def test_covers_without_storage_never_download(enabled: None) -> None:
    pg = FakePg([{"handle": "sample", "oldest": NOW.date(), "initial": False}])
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(post(videoMeta={"coverUrl": COVER}))
    http_client = MagicMock(spec=httpx.Client)
    result = fetch.run(
        pg, client, request_id="no-storage", now=NOW, storage=None, http_client=http_client
    )  # type: ignore[arg-type]
    assert result.covers_saved == 0 and result.covers_skipped == {"storage_unconfigured": 1}
    assert result.inserted == 1 and result.metrics == 1
    assert not http_client.mock_calls and not pg.thumbnails


@pytest.mark.parametrize("bucket", [None, "private"])
def test_main_configures_cover_storage_and_closes_http_client(
    enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    bucket: str | None,
) -> None:
    if bucket is None:
        monkeypatch.delenv("VIDEO_LIBRARY_S3_BUCKET", raising=False)
    else:
        monkeypatch.setenv("VIDEO_LIBRARY_S3_BUCKET", bucket)
    monkeypatch.setattr(fetch, "configure_logging", lambda: None)
    pg, storage, http_client = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(fetch.PgVectorClient, "from_env", lambda: pg)
    monkeypatch.setattr(fetch.ApifyClient, "from_env", MagicMock())
    storage_factory = MagicMock(return_value=storage)
    monkeypatch.setattr(fetch, "VideoLibraryStorage", storage_factory)
    monkeypatch.setattr(fetch, "page_client", lambda: http_client)
    runner = MagicMock(return_value=fetch.FetchResult())
    monkeypatch.setattr(fetch, "run", runner)
    assert fetch.main([]) == 0
    assert runner.call_args.kwargs["storage"] is (storage if bucket else None)
    assert runner.call_args.kwargs["http_client"] is http_client.__enter__.return_value
    assert storage_factory.call_count == int(bucket is not None)
    http_client.__exit__.assert_called_once()
    pg.close.assert_called_once()
