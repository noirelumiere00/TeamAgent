"""adapters/rakko_scraper.py の単体テスト (Node subprocess + ログイン状態をモック)。

実ブラウザ / Node / ラッコログインを使わず、subprocess.run と is_logged_in を
モックして JSON I/F のパースとエラーハンドリングを検証する。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from teamagent.adapters import rakko_scraper
from teamagent.adapters.rakko_scraper import RakkoScrapeError, fetch_search_volumes

_SAMPLE = {
    "ok": True,
    "mode": "query",
    "results": {
        "新宿 ランチ": [
            {"kw": "新宿 ランチ", "seo": "25", "vol": "110,000", "cpc": "$0.24"},
            {"kw": "新宿 ランチ 個室", "seo": "34", "vol": "9,900", "cpc": "0.16"},
            {"kw": "新宿 ランチ 安い", "seo": "-", "vol": "-", "cpc": "-"},
        ],
    },
    "error": None,
}


def _mock_proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def test_fetch_parses_volumes() -> None:
    with (
        patch.object(rakko_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(rakko_scraper, "is_logged_in", return_value=True),
        patch.object(rakko_scraper.subprocess, "run", return_value=_mock_proc(json.dumps(_SAMPLE))),
    ):
        res = fetch_search_volumes(["新宿 ランチ"], limit=30)

    assert "新宿 ランチ" in res.by_query
    rows = res.by_query["新宿 ランチ"]
    assert len(rows) == 3
    assert rows[0].kw == "新宿 ランチ"
    assert rows[0].volume == 110000  # "110,000" → int
    assert rows[0].seo == 25
    assert rows[0].cpc == 0.24  # "$0.24" → float
    assert rows[1].cpc == 0.16  # "0.16" (no $) → float
    # "-" は None に正規化
    assert rows[2].volume is None
    assert rows[2].seo is None
    assert rows[2].cpc is None
    assert res.total_keywords == 3


def test_fetch_passes_cli_args() -> None:
    captured = {}

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return _mock_proc(json.dumps(_SAMPLE))

    with (
        patch.object(rakko_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(rakko_scraper, "is_logged_in", return_value=True),
        patch.object(rakko_scraper.subprocess, "run", side_effect=_fake_run),
    ):
        fetch_search_volumes(["新宿 ランチ", "新宿 グルメ"], limit=20)

    cmd = captured["cmd"]
    assert "--queries" in cmd
    assert "新宿 ランチ,新宿 グルメ" in cmd  # カンマ結合
    assert "--limit" in cmd and "20" in cmd


def test_fetch_not_logged_in_raises() -> None:
    with patch.object(rakko_scraper, "is_logged_in", return_value=False):
        with pytest.raises(RakkoScrapeError, match="RAKKO_NOT_LOGGED_IN"):
            fetch_search_volumes(["新宿 ランチ"])


def test_fetch_empty_query_raises() -> None:
    with pytest.raises(RakkoScrapeError, match="RAKKO_EMPTY_QUERY"):
        fetch_search_volumes([])


def test_fetch_empty_result_raises() -> None:
    payload = {"ok": False, "mode": "query", "results": {}, "error": "ログイン切れ"}
    with (
        patch.object(rakko_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(rakko_scraper, "is_logged_in", return_value=True),
        patch.object(
            rakko_scraper.subprocess,
            "run",
            return_value=_mock_proc(json.dumps(payload), returncode=2),
        ),
    ):
        with pytest.raises(RakkoScrapeError, match="RAKKO_EMPTY_RESULT"):
            fetch_search_volumes(["新宿 ランチ"])


def test_fetch_timeout_raises() -> None:
    with (
        patch.object(rakko_scraper, "_node_bin", return_value="/usr/bin/node"),
        patch.object(rakko_scraper, "is_logged_in", return_value=True),
        patch.object(
            rakko_scraper.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="node", timeout=180),
        ),
    ):
        with pytest.raises(RakkoScrapeError, match="RAKKO_TIMEOUT"):
            fetch_search_volumes(["新宿 ランチ"])
