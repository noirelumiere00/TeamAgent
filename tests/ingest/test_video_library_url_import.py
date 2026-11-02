"""A01-A08: real HTTP transport behavior, deterministic parsers, and worker orchestration."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from teamagent.adapters.video_library_page import PAGE_USER_AGENT, page_client
from teamagent.ingest import video_library_url_worker as worker
from teamagent.ingest.video_library_url_import import expand_short_url, process_url, submit_import
from teamagent.ingest.video_library_url_models import (
    URLImportError,
    inspect_input,
    normalize_url,
    parse_observation,
)

URL = "https://www.tiktok.com/@sample/video/123"
NOW = datetime(2026, 11, 2, tzinfo=UTC)
COVER = "https://p16-sign-sg.tiktokcdn.com/test.jpeg"


def item(**changes: Any) -> dict[str, Any]:
    return {
        "id": "123",
        "desc": "投稿 #PR",
        "createTime": "1793548800",
        "author": {"uniqueId": "sample"},
        "isAd": False,
        "adLabelVersion": 2,
        "stats": dict(
            zip(
                ("playCount", "diggCount", "commentCount", "shareCount", "collectCount"),
                ("100", "10", "0", "3", "4"),
                strict=True,
            )
        ),
        "video": {"cover": COVER},
        **changes,
    }


def page(data: dict[str, Any] | None = None) -> str:
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "itemInfo": {
                    "itemStruct": data if data is not None else item(),
                }
            }
        }
    }
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(payload) + "</script>"


def repository() -> MagicMock:
    repo = MagicMock()
    repo.thumbnail_missing.return_value = True
    repo.resolve_row.return_value = None
    repo.save.side_effect = lambda observation, **kwargs: kwargs["result"]
    return repo


def test_a01_limits_count_before_dedup_and_preserve_line_numbers() -> None:
    assert len(inspect_input("\n".join([URL] * 100))) == 100
    with pytest.raises(URLImportError, match="too_many_lines"):
        inspect_input("\n".join([URL] * 101))
    with pytest.raises(URLImportError, match="empty_input"):
        inspect_input(" \n\n")
    rows = inspect_input("\n" + URL + "\n\ninvalid\n")
    assert [r.line_no for r in rows] == [2, 4]
    assert [r.status for r in rows] == ["queued", "invalid"]


def test_a01_rejection_never_touches_repository_or_wakes_worker() -> None:
    repo, wake = MagicMock(), MagicMock()
    # The actual repository validates before opening a connection.
    from teamagent.ingest.video_library_url_repository import URLImportRepository

    with pytest.raises(URLImportError, match="too_many_lines"):
        submit_import(
            "\n".join([URL] * 101),
            URLImportRepository(repo),
            requested_by="member@example.test",
            idempotency_key="key",
            wake=wake,
        )
    repo.connection.assert_not_called()
    wake.assert_not_called()


def test_a02_queries_and_handles_deduplicate_by_video_id() -> None:
    rows = inspect_input(URL + "?tracking=secret\n" + URL.replace("sample", "other") + "#fragment")
    assert rows[0].canonical_url == URL
    assert rows[1].status == "duplicate" and rows[1].duplicate_of == 1


@pytest.mark.parametrize(
    "url,reason",
    [
        ("https://www.tiktok.com.evil.test/@a/video/1", "unsupported_host"),
        ("https://user@www.tiktok.com/@a/video/1", "invalid_url"),
        ("https://www.tiktok.com:8443/@a/video/1", "invalid_url"),
        ("https://127.0.0.1/@a/video/1", "unsupported_host"),
        ("https://www.tiktok.com/@a", "unsupported_path"),
        ("https://www.tiktok.com/@a/photo/1", "unsupported_path"),
        ("https://www.tiktok.com/@a/live", "unsupported_path"),
        ("https://www.tiktok.com/@%2f/video/1", "unsupported_path"),
        ("https://www.tiktok.com/@a/video/1\n", "invalid_url"),
        ("https://www.tiktok.com:bad/@a/video/1", "invalid_url"),
        ("https://www.tiktok.com\\@evil.test/@a/video/1", "invalid_url"),
        ("https://www.instagram.com/reel/a", "unsupported_host"),
        ("https://youtu.be/a", "unsupported_host"),
    ],
)
def test_a03_rejects_unsafe_or_unsupported_urls(url: str, reason: str) -> None:
    with pytest.raises(URLImportError, match=reason):
        normalize_url(url)


@pytest.mark.parametrize(
    "host,path",
    [("vt.tiktok.com", "/abc"), ("vm.tiktok.com", "/abc"), ("www.tiktok.com", "/t/abc")],
)
def test_short_redirect_body_never_read(host: str, path: str) -> None:
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            raise AssertionError("short response body was read")

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["User-Agent"] == PAGE_USER_AGENT
        return httpx.Response(302, headers={"location": URL}, stream=Unreadable())

    before = MagicMock()
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        result = expand_short_url(client, normalize_url(f"https://{host}{path}"), before)
    assert result.url == URL and len(calls) == 1 and before.call_count == 1


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/",
        "http://www.tiktok.com/@a/video/1",
        "https://169.254.169.254/",
        "https://www.tiktok.com:444/@a/video/1",
        "https://u@www.tiktok.com/@a/video/1",
    ],
)
def test_redirect_refuses_untrusted_destination_before_request(location: str) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(URLImportError),
    ):
        expand_short_url(client, normalize_url("https://vt.tiktok.com/a/"), lambda: None)
    assert len(calls) == 1


@pytest.mark.parametrize("redirects,success", [(5, True), (6, False)])
def test_redirect_maximum_five(redirects: int, success: bool) -> None:
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            302, headers={"location": URL if count == redirects else f"/step{count}/"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if success:
            assert (
                expand_short_url(
                    client, normalize_url("https://vt.tiktok.com/start"), lambda: None
                ).url
                == URL
            )
        else:
            with pytest.raises(URLImportError, match="redirect_limit"):
                expand_short_url(client, normalize_url("https://vt.tiktok.com/start"), lambda: None)
    assert count == 5


@pytest.mark.parametrize(
    "response,code",
    [
        ("<html>" + " " * 1387 + "</html>", "page_data_missing"),
        ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{bad}</script>', "page_data_missing"),
        ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{}</script>', "page_data_missing"),
        (page(item(id="999")), "video_id_mismatch"),
        ("<html>security challenge captcha</html>", "challenge"),
    ],
)
def test_a04_empty_missing_broken_and_mismatched_pages(response: str, code: str) -> None:
    repo = repository()
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=response))
    ) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=300,
        )
    assert result.status == "failed" and result.error_code == code
    repo.save.assert_not_called()
    if code == "challenge":
        assert repo.record_failure.call_args.kwargs["stop"] == "challenge"


def test_a04_string_metrics_statsv2_and_zero() -> None:
    observation = parse_observation(page(), normalize_url(URL), NOW)
    assert observation.metrics == [100, 10, 0, 3, 4]
    assert observation.account_handle == "sample" and observation.posted_at is not None
    v2 = item(stats=None, statsV2=item()["stats"])
    assert parse_observation(page(v2), normalize_url(URL), NOW).metrics == [100, 10, 0, 3, 4]
    # A partially present stats container must never be mixed with statsV2.
    v2["stats"] = {"playCount": "7"}
    assert parse_observation(page(v2), normalize_url(URL), NOW).metrics == [
        7,
        None,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize("value", [-1, "-1", True, 1.5, "NaN", None, "", 2**63, "9" * 100])
def test_a04_bad_metric_is_missing_with_reason(value: Any) -> None:
    data = item()
    data["stats"]["playCount"] = value
    observation = parse_observation(page(data), normalize_url(URL), NOW)
    assert observation.metrics[0] is None
    assert "metric_missing:playCount" in observation.warnings


@pytest.mark.parametrize(
    "caption,pr,disclosure",
    [
        ("#PR", True, True),
        ("#pr", True, True),
        ("＃ＰＲ", True, True),
        ("#PRADA", False, False),
        ("#ad", False, True),
        ("【PR】", False, True),
        ("", False, False),
    ],
)
@pytest.mark.parametrize("is_ad", [True, False, None])
def test_a05_pr_rule_does_not_use_is_ad(
    caption: str, pr: bool, disclosure: bool, is_ad: bool | None
) -> None:
    observation = parse_observation(page(item(desc=caption, isAd=is_ad)), normalize_url(URL), NOW)
    assert observation.is_pr is pr and bool(observation.disclosures) is disclosure
    assert observation.page_is_ad is is_ad


def test_a05_missing_caption_and_ad_never_become_false_observations() -> None:
    data = item()
    del data["desc"], data["isAd"]
    observation = parse_observation(page(data), normalize_url(URL), NOW)
    assert observation.caption is None and observation.page_is_ad is None
    assert "caption_missing" in observation.warnings


@pytest.mark.parametrize("status", [403, 429])
def test_a08_http_blocks_pause_without_retries(status: int) -> None:
    repo, sleep = repository(), MagicMock()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=300,
            sleep=sleep,
        )
    assert result.error_code == f"http_{status}"
    assert repo.before_request.call_count == 1
    assert repo.record_failure.call_args.kwargs["stop"] == f"http_{status}"
    sleep.assert_not_called()


def test_a08_retries_count_every_attempt_and_backoff() -> None:
    repo, sleep = repository(), MagicMock()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=300,
            sleep=sleep,
        )
    assert result.status == "failed" and repo.before_request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]


def test_a08_thumbnail_failure_preserves_page_and_metrics_but_pauses() -> None:
    repo = repository()

    def handle(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, text=page())
            if request.url.host == "www.tiktok.com"
            else httpx.Response(429)
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="p3b",
            requested_by="member@example.test",
            daily_limit=300,
        )
    assert result.status == "partial" and "http_429" in result.warnings
    assert repo.save.call_args.kwargs["stop"] == "http_429"
    assert repo.save.call_args.args[0].metrics == [100, 10, 0, 3, 4]


def test_a08_database_failure_is_never_success() -> None:
    repo = repository()
    repo.save.side_effect = RuntimeError("secret SQL inputs")

    def handle(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, text=page())
            if request.url.host == "www.tiktok.com"
            else httpx.Response(200, content=b"\xff\xd8\xffimage")
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=300,
        )
    assert result.status == "failed" and result.error_code == "storage_error"
    assert "secret" not in result.model_dump_json()


def test_a08_quota_defers_without_network() -> None:
    repo = repository()
    repo.before_request.side_effect = URLImportError("daily_limit")
    transport = MagicMock(side_effect=AssertionError("network must not start"))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=0,
        )
    assert result.status == "deferred"
    transport.assert_not_called()


def test_a08_queue_completion_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_LIBRARY_IMPORT_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("VIDEO_LIBRARY_IMPORT_STALE_SECONDS", raising=False)
    assert worker.settings() == (300, 600)
    repo = repository()

    @contextmanager
    def lock() -> Any:
        yield True

    repo.fetch_lock = lock
    repo.claim.side_effect = [
        {
            "batch_id": "b",
            "line_no": 1,
            "source": "url_manual",
            "requested_by": "member@example.test",
            "canonical_url": URL,
            "input_url": URL,
        },
        None,
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page()))
    ) as client:
        assert worker.drain_queue(repo, client, MagicMock()) == 1
    assert repo.claim.call_count == 2
    repo.claim.assert_called_with(stale_seconds=600)


def test_intake_commits_before_wake() -> None:
    events = []
    repo = MagicMock()
    repo.submit.side_effect = lambda *args, **kwargs: events.append("commit") or "batch"
    assert (
        submit_import(
            URL,
            repo,
            requested_by="member@example.test",
            idempotency_key="key",
            wake=lambda: events.append("wake"),
        )
        == "batch"
    )
    assert events == ["commit", "wake"]


def test_shared_client_has_timeout_and_no_auto_redirect() -> None:
    with page_client() as client:
        assert client.timeout.read == 20 and client.follow_redirects is False
        assert client.headers["User-Agent"] == PAGE_USER_AGENT


async def test_worker_lifespan_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_LIBRARY_IMPORT_WORKER_ENABLED", raising=False)
    factory = MagicMock()
    monkeypatch.setattr(worker, "URLImportWorker", factory)
    async with worker.worker_lifespan(MagicMock()):
        factory.assert_not_called()


async def test_worker_lifespan_starts_and_stops_one_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_IMPORT_WORKER_ENABLED", "1")
    instance = MagicMock()
    monkeypatch.setattr(worker, "URLImportWorker", lambda: instance)
    async with worker.worker_lifespan(MagicMock()):
        instance.thread.start.assert_called_once()
    instance.stop.set.assert_called_once()
    instance.thread.join.assert_called_once()


@pytest.mark.parametrize("location", ["/again/", None])
def test_short_redirect_loop_or_missing_location(location: str | None) -> None:
    headers = {"location": location} if location else {}
    expected = "redirect_loop" if location else "redirect_location_missing"
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(302, headers=headers))
    ) as client:
        with pytest.raises(URLImportError, match=expected):
            expand_short_url(client, normalize_url("https://vt.tiktok.com/again/"), lambda: None)


def test_short_200_is_not_expanded_by_reading_html() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="meta refresh"))
    ) as client:
        with pytest.raises(URLImportError, match="short_url_unresolved"):
            expand_short_url(client, normalize_url("https://vt.tiktok.com/a/"), lambda: None)


def test_unsafe_cover_is_refused_before_http() -> None:
    from teamagent.adapters.video_library_page import save_cover

    calls = MagicMock(side_effect=AssertionError("untrusted HTTP"))
    with httpx.Client(transport=httpx.MockTransport(calls)) as client:
        with pytest.raises(ValueError, match="invalid_cover_url"):
            save_cover(client, MagicMock(), "tiktok", "123", "https://169.254.169.254/a")
    calls.assert_not_called()


def test_oversized_page_is_a_fetch_failure() -> None:
    repo = repository()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1))
        )
    ) as client:
        result = process_url(
            URL,
            repo,
            client,
            MagicMock(),
            source="url_manual",
            requested_by="member@example.test",
            daily_limit=300,
        )
    assert result.status == "failed" and result.error_code == "page_too_large"
    repo.save.assert_not_called()


def test_one_post_uses_global_fetch_lock() -> None:
    from teamagent.ingest.video_library_url_import import import_one_url

    repo = repository()

    @contextmanager
    def lock() -> Any:
        yield False

    repo.fetch_lock = lock
    calls = MagicMock(side_effect=AssertionError("busy worker must not fetch"))
    with httpx.Client(transport=httpx.MockTransport(calls)) as client:
        result = import_one_url(
            URL, repo, client, MagicMock(), source="p3b", requested_by="member@example.test"
        )
    assert result.status == "queued" and result.error_code == "worker_busy"
    calls.assert_not_called()


def test_a08_one_failed_row_does_not_discard_later_success() -> None:
    repo = repository()

    @contextmanager
    def lock() -> Any:
        yield True

    repo.fetch_lock = lock
    rows = [
        {
            "batch_id": "batch",
            "line_no": number,
            "source": "url_manual",
            "requested_by": "member@example.test",
            "input_url": URL,
            "canonical_url": URL.replace("123", str(number)),
        }
        for number in (1, 2)
    ]
    repo.claim.side_effect = [*rows, None]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1"):
            return httpx.Response(200, text="<html>" + " " * 1387 + "</html>")
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=page(item(id="2")))
        return httpx.Response(200, content=b"\xff\xd8\xffimage")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert worker.drain_queue(repo, client, MagicMock()) == 2
    assert repo.finish_failure.call_args.args[1].status == "failed"
    assert repo.save.call_count == 1
    assert repo.save.call_args.args[0].video_id == "2"
    assert repo.save.call_args.kwargs["result"].status == "succeeded"
