"""Slack チャンネルの履歴を複数ページ読む（history_pages・10-06）。

再現する本番の失敗: #proj-01（案件決定）の取り込みが最新 100 件の 1 ページだけで、
それより前の案件決定（ADK・博報堂経由など）が金庫に入らなかった。
固定すること:
- 既定（history_pages=1）は今と同じ 1 回呼び・oldest も渡さない
- 2 以上は oldest_days より新しい範囲をカーソルで辿る・上限で止まる・最後のページで止まる
- yaml の値は 1〜20 だけ受ける
- #proj-01 は 10 ページ
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage
from teamagent.ingest.loader import SlackChannelSpec, _history_pages, load_ingest_sources
from teamagent.ingest.pipeline import _fetch_channel_history

ROOT = Path(__file__).resolve().parents[2]


def _msg(i: int) -> SlackMessage:
    return SlackMessage(ts=f"17000000{i:02d}.000001", user="U001", text=f"m{i}")


class _Client:
    def __init__(self, pages: list[tuple[list[int], str | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_channel_history(self, **kw: Any) -> HistoryBatch:
        self.calls.append(kw)
        ids, nxt = self.pages[len(self.calls) - 1]
        return HistoryBatch(
            messages=tuple(_msg(i) for i in ids), next_cursor=nxt, has_more=nxt is not None
        )


def _spec(pages: int, oldest_days: int | None = 365) -> SlackChannelSpec:
    return SlackChannelSpec(
        channel_id="C0XYZ",
        channel_name="#t",
        description="",
        oldest_days=oldest_days,
        history_pages=pages,
    )


def test_default_reads_one_page_like_before() -> None:
    client = _Client([([1, 2], "c2")])
    batch = _fetch_channel_history(client, _spec(1), request_id="r")
    assert [m.text for m in batch.messages] == ["m1", "m2"]
    assert client.calls == [{"channel_id": "C0XYZ", "request_id": "r", "limit": 100}]


def test_pages_follow_the_cursor_within_oldest_days() -> None:
    client = _Client([([1, 2], "c2"), ([3, 4], "c3"), ([5], None)])
    before = time.time()
    batch = _fetch_channel_history(client, _spec(10), request_id="r")
    assert [m.text for m in batch.messages] == ["m1", "m2", "m3", "m4", "m5"]
    assert [c["cursor"] for c in client.calls] == [None, "c2", "c3"]
    oldest = client.calls[0]["oldest"]
    assert before - 365 * 86400 - 5 <= oldest <= time.time() - 365 * 86400 + 5
    assert batch.has_more is False


def test_page_limit_stops_and_reports_truncation() -> None:
    client = _Client([([1], "c2"), ([2], "c3"), ([3], "c4")])
    batch = _fetch_channel_history(client, _spec(2), request_id="r")
    assert len(client.calls) == 2
    assert [m.text for m in batch.messages] == ["m1", "m2"]
    assert batch.has_more is True


def test_yaml_value_is_bounded() -> None:
    assert _history_pages(10, "C1") == 10
    for bad in (0, 21, "x"):
        with pytest.raises(ValueError):
            _history_pages(bad, "C1")


def test_proj01_reads_ten_pages() -> None:
    sources = load_ingest_sources(ROOT / "data/ingest_sources.yaml", skip_placeholder=True)
    proj01 = next(s for s in sources.slack_channels if s.channel_id == "C08MH3MG02F")
    assert proj01.history_pages == 10 and proj01.oldest_days == 365
    others = [s for s in sources.slack_channels if s.channel_id != "C08MH3MG02F"]
    assert all(s.history_pages == 1 for s in others)  # ほかは今と同じ
