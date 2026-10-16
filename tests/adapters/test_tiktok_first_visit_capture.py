from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from teamagent.adapters import tiktok_scraper


@pytest.fixture(autouse=True)
def local_search(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    monkeypatch.setattr(tiktok_scraper, "_node_bin", lambda: "/usr/bin/node")


def _payload() -> dict[str, Any]:
    return {
        "ok": True,
        "type": "keyword",
        "videos": [{"id": "12345678", "url": "https://www.tiktok.com/@u/photo/12345678"}],
        "firstVisitCapture": {
            "acquired_on": "2026-10-15T00:00:00.000Z",
            "order_basis": "display",
            "sessions": 1,
            "stop_reason": "target_reached",
            "missingness_preserved": True,
            "posts": [
                {
                    "provider_id": "12345678",
                    "url": "https://www.tiktok.com/@u/photo/12345678",
                    "rank_display": 7,
                    "plays": None,
                    "likes": 0,
                    "isAd": True,
                    "mediaType": "photo",
                    "imageCount": 3,
                    "missingFields": ["stats.playCount"],
                }
            ],
            "sessions_data": [{"responses": [{"data": [{"item": {"id": "12345678"}}]}]}],
        },
    }


def _run(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0 if payload["ok"] else 2, json.dumps(payload), ""
        )

    monkeypatch.setattr(tiktok_scraper.subprocess, "run", fake_run)
    return commands


def test_capture_runs_before_lossy_parse_and_preserves_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload()
    commands = _run(monkeypatch, source)
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tiktok_scraper,
        "_parse_video",
        lambda _video: (_ for _ in ()).throw(ValueError("lossy parse")),
    )
    with pytest.raises(ValueError, match="lossy parse"):
        tiktok_scraper.search_tiktok("coffee", capture=captured.append)
    assert "--first-visit-capture" in commands[0]
    record = captured[0]
    assert record["raw_response"] == source
    assert record["posts"] == source["firstVisitCapture"]["posts"]
    assert record["posts"][0]["plays"] is None
    assert record["posts"][0]["likes"] == 0
    assert record["posts"][0]["isAd"] is True
    assert record["status"] == "ok"


@pytest.mark.parametrize(
    "code,status", [("TIKTOK_TRULY_EMPTY", "observed_zero"), ("TIKTOK_BOT_WALL", "failed")]
)
def test_capture_distinguishes_observed_zero_from_failure(
    monkeypatch: pytest.MonkeyPatch, code: str, status: str
) -> None:
    payload = {
        "ok": False,
        "videos": [],
        "errorCode": code,
        "error": code,
        "diag": {"pagesFetched": 1},
    }
    _run(monkeypatch, payload)
    captured: list[dict[str, Any]] = []
    with pytest.raises(tiktok_scraper.TikTokScrapeError, match=code):
        tiktok_scraper.search_tiktok("coffee", capture=captured.append)
    assert captured[0]["status"] == status
    assert captured[0]["error_code"] == code


def test_capture_callback_failure_does_not_break_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _run(monkeypatch, _payload())

    def fail_capture(_record: dict[str, Any]) -> None:
        raise OSError("S3 failed")

    assert tiktok_scraper.search_tiktok("coffee", capture=fail_capture).count == 1


def test_capture_off_keeps_command_and_return_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _run(monkeypatch, _payload())
    before = tiktok_scraper.search_tiktok("coffee")
    after = tiktok_scraper.search_tiktok("coffee", capture=None)
    assert before == after
    assert commands[0] == commands[1]
    assert "--first-visit-capture" not in commands[0]


@pytest.mark.parametrize("sessions", [1, 2])
def test_capture_without_js_metadata_keeps_normalized_values_and_unknowns(
    monkeypatch: pytest.MonkeyPatch, sessions: int
) -> None:
    payload = {
        "ok": True,
        "type": "keyword(fallback)",
        "videos": [
            {
                "id": "12345678",
                "url": "https://www.tiktok.com/@u/video/12345678",
                "desc": "coffee",
                "stats": {"playCount": 0, "diggCount": 7},
                "author": {"uniqueId": "u", "followerCount": 0},
                "duration": 0,
            },
            {"id": "23456789", "url": "https://www.tiktok.com/@u/video/23456789"},
        ],
    }
    _run(monkeypatch, payload)
    captured: list[dict[str, Any]] = []
    result = tiktok_scraper.search_tiktok(
        "coffee", search_type="hashtag", max_videos=3, sessions=sessions, capture=captured.append
    )
    assert result.count == 2
    record = captured[0]
    assert record["raw_response"] == payload
    assert record["missingness_preserved"] is False
    assert record["query"] == "coffee"
    assert record["search_type"] == "keyword(fallback)"
    assert record["order_basis"] == ("search_display_order" if sessions == 1 else "frequency_rank")
    assert [row["rank_display"] for row in record["posts"]] == [1, 2]
    row = record["posts"][0]
    assert row["plays"] == 0
    assert row["likes"] == 7
    assert row["missingFields"] == []
    assert "stats.playCount" in row["unknownFields"]
    assert "stats.commentCount" in row["unknownFields"]
    assert "author.followerCount" in row["unknownFields"]
    assert "stats.diggCount" not in row["unknownFields"]
    assert record["shortfalls"] == [
        {
            "kw": "coffee",
            "requested": 3,
            "actual": 2,
            "reason": "MEDIA_TIKTOK_RESULT_SHORTFALL",
        }
    ]


def test_explicit_empty_source_capture_does_not_fall_back_to_normalized_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    payload["firstVisitCapture"]["posts"] = []
    _run(monkeypatch, payload)
    captured: list[dict[str, Any]] = []
    assert tiktok_scraper.search_tiktok("coffee", capture=captured.append).count == 1
    assert captured[0]["posts"] == []


def test_capture_timeout_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("node", 120)

    monkeypatch.setattr(tiktok_scraper.subprocess, "run", timeout)
    captured: list[dict[str, Any]] = []
    with pytest.raises(tiktok_scraper.TikTokScrapeError, match="TIKTOK_TIMEOUT"):
        tiktok_scraper.search_tiktok("coffee", capture=captured.append)
    assert captured[0]["status"] == "failed"
    assert captured[0]["error_code"] == "TIKTOK_TIMEOUT"


def test_capture_records_executed_fallback_type_before_legacy_empty_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ok": False,
        "type": "hashtag",
        "errorCode": "TIKTOK_TRULY_EMPTY",
        "videos": [],
        "firstVisitCapture": {
            "executed_search_type": "keyword",
            "acquired_on": "2026-10-15T00:00:00Z",
        },
    }
    _run(monkeypatch, payload)
    captured: list[dict[str, Any]] = []
    with pytest.raises(tiktok_scraper.TikTokScrapeError, match="TIKTOK_TRULY_EMPTY"):
        tiktok_scraper.search_tiktok("coffee", search_type="hashtag", capture=captured.append)
    assert captured[0]["search_type"] == "keyword"
    assert captured[0]["requested_search_type"] == "hashtag"


def test_malformed_optional_capture_metadata_keeps_legacy_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    payload["firstVisitCapture"] = ["malformed optional metadata"]
    _run(monkeypatch, payload)
    captured: list[dict[str, Any]] = []
    assert tiktok_scraper.search_tiktok("coffee", capture=captured.append).count == 1
    assert captured[0]["missingness_preserved"] is False
