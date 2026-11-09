"""Cluster selection and real-shaped Apify/subtitle failures without paid calls."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient, ApifyRunResult
from teamagent.ingest import video_library_case_discovery as discovery
from teamagent.ingest.video_library_case_discovery import (
    DiscoveryConfig,
    DiscoveryHistory,
    DiscoveryVideo,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)
SUBTITLE_URL = "https://v16.tiktokcdn.com/subtitle.vtt?signature=secret"
VTT = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n字幕の秘密\n"


def video(vid: int, *tags: str, posted_at: datetime | None = NOW) -> DiscoveryVideo:
    return DiscoveryVideo(
        video_id=str(vid),
        url=f"https://www.tiktok.com/@sample/video/{vid}",
        caption="投稿文の秘密",
        hashtags=list(tags),
        posted_at=posted_at,
    )


def select(
    videos: list[DiscoveryVideo], history: list[DiscoveryHistory] | None = None, **kwargs: Any
) -> list[discovery.Cluster]:
    return discovery.select_clusters(
        videos, history or [], now=NOW, config=DiscoveryConfig(**{"min_videos": 1} | kwargs)
    )


class FakePg:
    def __init__(self, videos: list[DiscoveryVideo]) -> None:
        self.videos = videos
        self.rows: dict[str, dict[str, Any]] = {}
        self.in_connection = False

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[FakePg]:
        if "user_groups" in kwargs:
            assert isinstance(kwargs.pop("user_groups"), list)
        assert kwargs == {"app_role": "teamagent_app", "user_email": None, "user_role": "admin"}
        before = deepcopy(self.rows)
        self.in_connection = True
        try:
            yield self
        except Exception:
            self.rows = before
            raise
        finally:
            self.in_connection = False

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> MagicMock:
        cursor = MagicMock()
        if "FROM vl_videos_effective" in sql:
            cursor.fetchall.return_value = [v.model_dump() for v in self.videos]
        elif "SELECT tag,status,attempted_at" in sql:
            cursor.fetchall.return_value = list(self.rows.values())
        elif "INSERT INTO vl_case_discovery" in sql:
            tag, count, ids, samples, status, now = params
            self.rows[tag] = dict(
                tag=tag,
                video_count=count,
                sample_video_ids=ids,
                samples=samples.obj,
                status=status,
                attempted_at=now,
            )
        else:
            raise AssertionError(sql)
        return cursor


def response(*items: dict[str, Any]) -> ApifyRunResult:
    return ApifyRunResult(
        actor_id=ACTOR_TIKTOK_VIDEO,
        run_id="test",
        status="SUCCEEDED",
        items=list(items),
        estimated_cost_usd=len(items) * 0.004,
    )


def item(vid: int, *links: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return (
        dict(id=str(vid), text="取得した投稿文の秘密", videoMeta={"subtitleLinks": list(links)})
        | kwargs
    )


def test_normalization_generic_exclusion_count_order_and_latest_two() -> None:
    videos = [
        video(1, "＃ＱＴＴＡ", "qtta", "PR", "ＦＹＰ", "おすすめ"),
        video(2, "Qtta", "tiktok", "広告", "提供", "プロモーション", "ad", "sponsored"),
        video(3, "qtta", posted_at=NOW + timedelta(days=1)),
        video(4, "qtta", posted_at=None),
        video(5, "other"),
        video(6, "other"),
        video(7, "too_small"),
    ]
    clusters = select(videos, min_videos=2)
    assert [c.tag for c in clusters] == ["qtta", "other"]
    assert [len(c.video_ids) for c in clusters] == [4, 2]
    assert [v.video_id for v in clusters[0].videos] == ["3", "2"]


@pytest.mark.parametrize(
    "overlap,expected", [(7, ["largest", "smaller"]), (8, ["largest"]), (10, ["largest"])]
)
def test_overlap_is_per_candidate_and_includes_exactly_80_percent(
    overlap: int, expected: list[str]
) -> None:
    videos = [video(i, "largest", *(["smaller"] if i < overlap else [])) for i in range(12)]
    videos.extend(video(100 + i, "smaller") for i in range(10 - overlap))
    assert [c.tag for c in select(videos)] == expected


def test_duplicate_tags_and_rows_count_each_video_once() -> None:
    videos = [video(1, "Ａ", "a", "#a"), video(1, "a"), video(2, "a")]
    assert select(videos, min_videos=3) == []
    assert select(videos)[0].video_ids == {"1", "2"}


@pytest.mark.parametrize(
    "status,days,eligible",
    [
        ("registered", 1000, False),
        ("existing", 1000, False),
        ("candidate", 29, False),
        ("candidate", 30, True),
        ("failed", 29, False),
        ("failed", 30, True),
        ("unidentifiable", 30, False),
        ("unidentifiable", 89, False),
        ("unidentifiable", 90, True),
        ("sampled", 0, True),
    ],
)
def test_status_cooldowns(status: Any, days: int, eligible: bool) -> None:
    history = [DiscoveryHistory(tag="a", status=status, attempted_at=NOW - timedelta(days=days))]
    assert bool(select([video(1, "a")], history)) is eligible


def test_cooldown_tags_do_not_suppress_other_overlapping_tags() -> None:
    history = [DiscoveryHistory(tag="a", status="existing", attempted_at=NOW)]
    assert [c.tag for c in select([video(1, "a", "b")], history)] == ["b"]


def test_default_and_zero_tag_limits() -> None:
    videos = [video(i, f"tag{i}") for i in range(8)]
    assert len(select(videos)) == 5
    assert select(videos, max_tags=0) == []


def test_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("MIN_VIDEOS", "MAX_TAGS", "MAX_USD"):
        monkeypatch.delenv("VIDEO_LIBRARY_CASE_DISCOVERY_" + key, raising=False)
    assert DiscoveryConfig.from_env() == DiscoveryConfig()
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_MIN_VIDEOS", "20")
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_MAX_TAGS", "2")
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_MAX_USD", "0.016")
    assert DiscoveryConfig.from_env() == DiscoveryConfig(min_videos=20, max_tags=2, max_usd="0.016")


@pytest.mark.parametrize(
    "config",
    [
        {"min_videos": 0},
        {"max_tags": -1},
        {"max_usd": "-1"},
        {"max_usd": "NaN"},
        {"max_usd": "Infinity"},
    ],
)
def test_invalid_limits_fail_closed(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DiscoveryConfig(**config)


def test_webvtt_body_only_and_length_bound() -> None:
    body = (
        "\ufeffWEBVTT\r\nKind: captions\r\nLanguage: ja\r\n\r\n"
        "NOTE comment\r\nnot evidence\r\n\r\n"
        "1\r\n00:00:00.000 --> 00:00:02.000 align:start\r\n最初\r\n\r\n"
        "cue-two\r\n00:02.000 --> 00:04.000\r\n<b>次</b>&amp;最後\r\n\r\n"
    )
    assert discovery.webvtt_text(body) == "最初\n次&最後"
    assert (
        discovery.webvtt_text("WEBVTT\n\n1\n00:00.000 --> 00:01.000\n" + "あ" * 4000) == "あ" * 3000
    )
    assert discovery.webvtt_text("<html>challenge</html>") == ""
    assert discovery.webvtt_text("WEBVTT\n\n1\n00:00.000 --> 00:01.000\n123") == "123"


@pytest.mark.parametrize(
    "url",
    [
        "https://tiktok.com/a",
        "https://www.tiktok.com/a",
        SUBTITLE_URL,
        "https://v.tiktokcdn-us.com/a",
        "https://v.tiktokcdn-eu.com/a",
    ],
)
def test_subtitle_allowed_hosts(url: str) -> None:
    assert discovery.is_subtitle_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://v.tiktokcdn.com/a",
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://tiktokcdn.com.evil.test/a",
        "https://eviltiktok.com/a",
        "https://tiktok.com@evil.test/a",
        "https://user@www.tiktok.com/a",
        "https://www.tiktok.com:444/a",
        "https://www.tiktok.com:bad/a",
        "https://www.tiktok.com/a\n",
        "https://[invalid/a",
        "https://api.apify.com/subtitle.vtt",
        "https://v.byteoversea.com/a",
    ],
)
def test_subtitle_rejected_hosts(url: str) -> None:
    assert not discovery.is_subtitle_url(url)


def test_collect_real_shaped_failures_caption_fallback_and_safe_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = FakePg([video(i, f"tag{(i - 1) // 2}") for i in range(1, 11)])
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(
        item(
            1,
            {"language": "eng-US", "downloadLink": SUBTITLE_URL + "&lang=en"},
            {"language": "jpn-JP", "downloadLink": SUBTITLE_URL},
        ),
        item(2),
        {"error": "Not found: https://secret.test", "id": "3"},
        item(999, {"downloadLink": SUBTITLE_URL + "&unrequested=1"}),
        item(5, {"downloadLink": "https://evil.test/private.vtt"}),
        item(6, {"tiktokLink": SUBTITLE_URL + "&forbidden=1"}),
        item(7, {"tiktokLink": SUBTITLE_URL + "&fallback=1"}),
    )
    fetched = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert not pg.in_connection
        fetched.append(str(request.url))
        if "forbidden" in request.url.params:
            return httpx.Response(403, text="private forbidden response")
        return httpx.Response(200, text=VTT)

    log = MagicMock()
    monkeypatch.setattr(discovery, "logger", log)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
        result = discovery.collect(
            pg,
            client,
            http_client=http_client,
            request_id="test",
            config=DiscoveryConfig(min_videos=1),
            now=NOW,
        )  # type: ignore[arg-type]
    assert result.sampled == 5 and result.tags == 5 and result.videos == 10
    assert result.reserved_usd == 0.04
    assert result.cost_usd == pytest.approx(0.028)
    assert result.counts == {
        "error_items": 1,
        "unrequested_items": 1,
        "subtitle_url_rejected": 1,
        "subtitle_http_error": 1,
        "missing_items": 5,
        "no_subtitle": 8,
    }
    assert fetched == [SUBTITLE_URL, SUBTITLE_URL + "&forbidden=1", SUBTITLE_URL + "&fallback=1"]
    samples = {s["video_id"]: s for row in pg.rows.values() for s in row["samples"]}
    assert samples["1"] == {
        "video_id": "1",
        "caption": "取得した投稿文の秘密",
        "subtitle_text": "字幕の秘密",
        "no_subtitle": False,
    }
    assert samples["3"]["caption"] == "投稿文の秘密" and samples["3"]["no_subtitle"]
    assert samples["5"]["caption"] == "取得した投稿文の秘密" and samples["5"]["no_subtitle"]
    assert samples["6"]["no_subtitle"] and samples["7"]["subtitle_text"] == "字幕の秘密"
    assert all(
        row["status"] == "sampled" and row["attempted_at"] == NOW for row in pg.rows.values()
    )
    client.run_actor_sync.assert_called_once()
    args, kwargs = client.run_actor_sync.call_args
    assert args[0] == ACTOR_TIKTOK_VIDEO
    assert args[1] == {
        "postURLs": [video.url for c in select(pg.videos) for video in c.videos],
        "shouldDownloadSubtitles": True,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
    }
    assert kwargs["max_items"] == 10
    output = str(log.mock_calls)
    assert "秘密" not in output and "https://" not in output and "signature" not in output


@pytest.mark.parametrize(
    "budget,calls,reserved", [("0", 0, 0), ("0.007999", 0, 0), ("0.008", 1, 0.008)]
)
def test_budget_reserved_before_call_and_never_retried(
    budget: str, calls: int, reserved: float
) -> None:
    pg = FakePg([video(1, "a"), video(2, "a"), video(3, "b"), video(4, "b")])
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.side_effect = RuntimeError("timeout https://secret.test")
    result = discovery.collect(
        pg,
        client,
        http_client=MagicMock(),
        request_id="budget",
        config=DiscoveryConfig(min_videos=1, max_usd=budget),
        now=NOW,
    )  # type: ignore[arg-type]
    assert client.run_actor_sync.call_count == calls
    assert result.reserved_usd == result.cost_usd == reserved
    assert result.failed == calls and result.sampled == 0
    assert all(row["status"] == "failed" for row in pg.rows.values())
    if calls:
        discovery.collect(
            pg,
            client,
            http_client=MagicMock(),
            request_id="cooldown",
            config=DiscoveryConfig(min_videos=1, max_usd=budget),
            now=NOW,
        )  # type: ignore[arg-type]
        # The failed tag is cooled down; the other tag can be attempted on the next run.
        assert client.run_actor_sync.call_count == 2


def test_shared_sample_is_requested_and_reserved_once() -> None:
    pg = FakePg(
        [video(1, "a", "b")]
        + [video(i, "a") for i in range(2, 6)]
        + [video(i, "b") for i in range(6, 10)]
    )
    pg.videos[0].posted_at = NOW + timedelta(days=1)
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response()
    result = discovery.collect(
        pg,
        client,
        http_client=MagicMock(),
        request_id="shared",
        config=DiscoveryConfig(min_videos=1, max_usd="0.012"),
        now=NOW,
    )  # type: ignore[arg-type]
    assert result.tags == 2 and result.videos == 3 and result.reserved_usd == 0.012
    assert len(client.run_actor_sync.call_args.args[1]["postURLs"]) == 3


@pytest.mark.parametrize("failure", ["redirect", "timeout", "too_large", "non_vtt"])
def test_subtitle_failures_continue_without_text(failure: str) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SUBTITLE_URL
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://169.254.169.254/secret"})
        if failure == "timeout":
            raise httpx.ReadTimeout("private URL", request=request)
        if failure == "too_large":
            return httpx.Response(200, content=b"a" * (discovery.MAX_SUBTITLE_BYTES + 1))
        return httpx.Response(200, text="<html>challenge</html>")

    pg, client = FakePg([video(1, "a")]), MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response(item(1, {"downloadLink": SUBTITLE_URL}))
    with httpx.Client(
        transport=httpx.MockTransport(transport), follow_redirects=True
    ) as http_client:
        result = discovery.collect(
            pg,
            client,
            http_client=http_client,
            request_id="failure",
            config=DiscoveryConfig(min_videos=1),
            now=NOW,
        )  # type: ignore[arg-type]
    assert result.counts["no_subtitle"] == 1 and result.sampled == 1
    assert pg.rows["a"]["samples"][0]["caption"] == "取得した投稿文の秘密"


def test_no_candidates_never_calls_external_services() -> None:
    client, http_client = MagicMock(), MagicMock()
    result = discovery.collect(
        FakePg([video(1, "PR")]), client, http_client=http_client, request_id="empty", now=NOW
    )  # type: ignore[arg-type]
    assert result.tags == result.videos == result.reserved_usd == 0
    assert not client.mock_calls and not http_client.mock_calls


def test_invalid_sample_url_fails_before_paid_call() -> None:
    sample = video(1, "a")
    sample.url = "https://evil.test/video/1"
    client = MagicMock()
    with pytest.raises(ValueError, match="invalid_sample_url"):
        discovery.collect(
            FakePg([sample]),
            client,
            http_client=MagicMock(),
            request_id="url",
            config=DiscoveryConfig(min_videos=1),
            now=NOW,
        )  # type: ignore[arg-type]
    assert not client.mock_calls


def test_migration_is_admin_only_forward_and_rerunnable() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "infra/migrations/0045_video_library_case_discovery.sql"
    )
    sql = path.read_text()
    assert "CREATE TABLE IF NOT EXISTS vl_case_discovery" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql and "FORCE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL ON vl_case_discovery FROM PUBLIC" in sql
    assert sql.count("current_setting('app.user_role', true) = 'admin'") == 4
    assert "DROP TABLE" not in sql and "GRANT ALL" not in sql


@pytest.mark.parametrize("failed", [0, 1])
def test_main_closes_resources_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch, failed: int
) -> None:
    monkeypatch.setattr(discovery, "configure_logging", lambda: None)
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("APIFY_API_TOKEN", "fake")
    monkeypatch.setattr(discovery.BedrockClient, "credentials_available", lambda: True)
    from tests.ingest.test_video_library_case_inference import client

    monkeypatch.setattr(discovery.BedrockClient, "from_env", lambda **_: client())
    monkeypatch.setattr(discovery, "decide", lambda *_, **__: discovery.DecisionResult())
    pg, http_client = MagicMock(), MagicMock()
    monkeypatch.setattr(discovery.PgVectorClient, "from_env", lambda: pg)
    monkeypatch.setattr(discovery.ApifyClient, "from_env", MagicMock())
    monkeypatch.setattr(discovery.httpx, "Client", lambda: http_client)
    monkeypatch.setattr(
        discovery, "collect", MagicMock(return_value=discovery.CollectResult(failed=failed))
    )
    assert discovery.main([]) == failed
    pg.close.assert_called_once()
    http_client.__exit__.assert_called_once()
