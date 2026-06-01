"""adapters/tiktok_scraper.py の単体テスト (Node subprocess をモック)。

実ブラウザ / Node を起動せず、subprocess.run をモックして JSON I/F の
パースとエラーハンドリングを検証する。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from teamagent.adapters import tiktok_scraper
from teamagent.adapters.tiktok_scraper import (
    TikTokScrapeError,
    search_tiktok,
)

_SAMPLE = {
    "ok": True,
    "query": "新宿 ランチ",
    "type": "keyword",
    "count": 2,
    "videos": [
        {
            "id": "111",
            "url": "https://www.tiktok.com/@a/video/111",
            "desc": "新宿の神コスパランチ #新宿グルメ",
            "createTime": 1764061063,
            "duration": 27,
            "coverUrl": "https://cdn/x.jpg",
            "author": {"uniqueId": "a", "nickname": "A店", "followerCount": 18400},
            "stats": {
                "playCount": 742400,
                "diggCount": 14800,
                "commentCount": 22,
                "shareCount": 1506,
                "collectCount": 8750,
            },
            "hashtags": ["新宿グルメ", "新宿ランチ"],
            "music": {"title": "オリジナル楽曲", "authorName": "Mi", "original": True},
        },
        {
            "id": "222",
            "url": "https://www.tiktok.com/@b/video/222",
            "desc": "海鮮丼",
            "createTime": 0,
            "duration": 17,
            "coverUrl": "",
            "author": {"uniqueId": "b", "nickname": "B", "followerCount": 16000},
            "stats": {
                "playCount": 0,  # 再生 0 → engagement_rate 0 になる
                "diggCount": 100,
                "commentCount": 0,
                "shareCount": 0,
                "collectCount": 0,
            },
            "hashtags": [],
            "music": None,
        },
    ],
    "error": None,
}


def _mock_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def test_search_parses_videos() -> None:
    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(
            tiktok_scraper.subprocess, "run", return_value=_mock_proc(json.dumps(_SAMPLE))
        ),
    ):
        res = search_tiktok("新宿 ランチ", max_videos=10)

    assert res.count == 2
    assert res.search_type == "keyword"
    v0 = res.videos[0]
    assert v0.id == "111"
    assert v0.author.unique_id == "a"
    assert v0.author.follower_count == 18400
    assert v0.play_count == 742400
    assert v0.digg_count == 14800
    assert "新宿グルメ" in v0.hashtags
    assert v0.music_title == "オリジナル楽曲"
    # engagement_rate = (14800+22+1506+8750)/742400
    assert v0.engagement_rate == pytest.approx((14800 + 22 + 1506 + 8750) / 742400, abs=1e-4)
    # 再生 0 の動画は engagement_rate 0 (ゼロ除算しない)
    assert res.videos[1].engagement_rate == 0.0


def test_search_passes_correct_cli_args() -> None:
    captured = {}

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _mock_proc(json.dumps(_SAMPLE))

    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(tiktok_scraper.subprocess, "run", side_effect=_fake_run),
    ):
        search_tiktok("新宿", search_type="hashtag", max_videos=15)

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/node"
    assert "--query" in cmd and "新宿" in cmd
    assert "--type" in cmd and "hashtag" in cmd
    assert "--max" in cmd and "15" in cmd


def test_search_empty_result_raises() -> None:
    payload = {
        "ok": False,
        "query": "x",
        "type": "keyword",
        "count": 0,
        "videos": [],
        "error": "captcha",
    }
    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(
            tiktok_scraper.subprocess,
            "run",
            return_value=_mock_proc(json.dumps(payload), returncode=2),
        ),
    ):
        with pytest.raises(TikTokScrapeError, match="TIKTOK_EMPTY_RESULT"):
            search_tiktok("x")


def test_search_no_output_raises() -> None:
    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(tiktok_scraper.subprocess, "run", return_value=_mock_proc("", returncode=1)),
    ):
        with pytest.raises(TikTokScrapeError, match="TIKTOK_NO_OUTPUT"):
            search_tiktok("x")


def test_search_bad_json_raises() -> None:
    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(tiktok_scraper.subprocess, "run", return_value=_mock_proc("not json at all")),
    ):
        with pytest.raises(TikTokScrapeError, match="TIKTOK_BAD_JSON"):
            search_tiktok("x")


def test_search_timeout_raises() -> None:
    with (
        patch.object(tiktok_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(
            tiktok_scraper.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="node", timeout=120),
        ),
    ):
        with pytest.raises(TikTokScrapeError, match="TIKTOK_TIMEOUT"):
            search_tiktok("x")


def test_empty_query_raises() -> None:
    with pytest.raises(TikTokScrapeError, match="TIKTOK_EMPTY_QUERY"):
        search_tiktok("   ")


def test_node_unavailable_raises() -> None:
    with (
        patch.object(tiktok_scraper.shutil, "which", return_value=None),
        patch.dict(tiktok_scraper.os.environ, {}, clear=False),
    ):
        # TIKTOK_NODE_BIN を一時的に外す
        tiktok_scraper.os.environ.pop("TIKTOK_NODE_BIN", None)
        with pytest.raises(TikTokScrapeError, match="TIKTOK_NODE_UNAVAILABLE"):
            search_tiktok("新宿 ランチ")
