from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from teamagent.media.contracts import TikTokAcquireOperation, TikTokClientConfig
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import (
    MediaOperationError,
    _child_environment,
    _node_json,
    _tiktok_acquire,
)


def test_child_environment_excludes_task_credentials_and_unused_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("MEDIA_BLOCKED_VPC_CIDRS", "172.31.0.0/16")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/credential")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("APIFY_API_TOKEN", "unused")
    monkeypatch.setenv("PROXY_PASSWORD", "unused")

    environment = _child_environment(HOME="/tmp/job-home")

    assert environment["HOME"] == "/tmp/job-home"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["MEDIA_BLOCKED_VPC_CIDRS"] == "172.31.0.0/16"
    assert environment["PATH"] == "/safe/bin"
    assert set(environment) <= {
        "CHROMIUM_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "MEDIA_BLOCKED_VPC_CIDRS",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
    }
    for forbidden in (
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_SECRET_ACCESS_KEY",
        "APIFY_API_TOKEN",
        "PROXY_PASSWORD",
    ):
        assert forbidden not in environment


def test_node_failure_exposes_only_bounded_waf_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ok": False,
        "errorCode": "TIKTOK_BOT_WALL",
        "error": "secret provider message",
        "diag": {
            "pagesFetched": 0,
            "captchaDetected": True,
            "gridFound": False,
            "ssrCount": 0,
            "sessionsRun": 1,
            "videosFound": 0,
            "providerToken": "must-not-escape",
        },
    }
    monkeypatch.setattr(
        "teamagent.media.operations._run_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["node"],
            2,
            json.dumps(payload).encode(),
            b"hostile stderr",
        ),
    )

    with pytest.raises(MediaOperationError) as caught:
        _node_json(
            ["node", "search.mjs"],
            workdir=tmp_path,
            budget=DeadlineBudget(200, clock=lambda: 100),
            timeout_s=90,
        )

    assert caught.value.code == "MEDIA_TIKTOK_BOT_WALL"
    assert caught.value.diagnostics == {
        "pagesFetched": 0,
        "captchaDetected": True,
        "gridFound": False,
        "ssrCount": 0,
        "sessionsRun": 1,
    }
    assert "secret provider message" not in str(caught.value)


def test_hashtag_acquire_preserves_semantics_metadata_and_per_keyword_shortfall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = tmp_path / "node"
    scraper = tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    commands: list[list[str]] = []

    def fake_node(command: list[str], **_kwargs: Any) -> dict[str, Any]:
        commands.append(command)
        query = command[command.index("--query") + 1]
        if query == "blocked":
            raise MediaOperationError(
                "MEDIA_TIKTOK_BOT_WALL",
                "blocked",
                diagnostics={"captchaDetected": True, "pagesFetched": 0},
            )
        return {
            "ok": True,
            "type": "hashtag",
            "videos": [
                {
                    "url": "https://www.tiktok.com/@creator/video/123456789",
                    "desc": "launch",
                    "createTime": 123,
                    "duration": 42,
                    "coverUrl": "",
                    "hashtags": ["launch", "製品"],
                    "music": {"title": "Theme"},
                    "author": {
                        "uniqueId": "creator",
                        "nickname": "Creator",
                        "followerCount": 100,
                    },
                    "stats": {
                        "playCount": 1000,
                        "diggCount": 100,
                        "commentCount": 10,
                        "shareCount": 5,
                        "collectCount": 20,
                    },
                }
            ],
        }

    monkeypatch.setattr("teamagent.media.operations._node_json", fake_node)
    monkeypatch.setattr(
        "teamagent.media.operations._fetch_public_image",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "teamagent.media.operations.validate_acquire_url",
        lambda url: url,
    )
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        search_type="hashtag",
        keywords=("launch", "blocked"),
        n_per_kw=2,
        videos_per_kw=0,
        client=TikTokClientConfig(client="Client"),
    )

    output = _tiktok_acquire(
        operation,
        tmp_path,
        DeadlineBudget(200, clock=lambda: 100),
    )

    assert all(command[command.index("--type") + 1] == "hashtag" for command in commands)
    posts = json.loads((tmp_path / "posts.normalized.json").read_text())["posts"]
    assert posts[0]["search_type"] == "hashtag"
    assert posts[0]["duration"] == 42
    assert posts[0]["hashtags"] == ["launch", "製品"]
    assert posts[0]["music_title"] == "Theme"
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["kws"] == ["launch", "blocked"]
    assert output.metadata["counts"]["per_kw"] == {"launch": 1, "blocked": 0}
    assert output.metadata["shortfalls"] == [
        {
            "kw": "launch",
            "requested": 2,
            "actual": 1,
            "reason": "MEDIA_TIKTOK_RESULT_SHORTFALL",
        },
        {
            "kw": "blocked",
            "requested": 2,
            "actual": 0,
            "reason": "MEDIA_TIKTOK_BOT_WALL",
            "diagnostics": {"captchaDetected": True, "pagesFetched": 0},
        },
    ]
