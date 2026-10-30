"""Node と共通の固定入出力で、直接配信の送信境界まで検査する。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from teamagent.mcp_gateway import detached_jobs, direct_summary
from teamagent.skills._shared.slack_blocks import RichMessage
from teamagent.skills._shared.slack_mrkdwn import outbound_permanent_urls, scrub_outbound_reply

CASES = json.loads(Path(__file__).with_name("outbound_reply_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_fixed_outbound_scrub(case: dict[str, Any]) -> None:
    cleaned = scrub_outbound_reply(case["input"], case.get("permanent_urls"))
    assert cleaned == case["expected"]
    assert scrub_outbound_reply(cleaned) == cleaned


def test_existing_compact_budget_preserves_whole_links() -> None:
    assert scrub_outbound_reply("あ" * 3000) == "あ" * 2499 + "…"
    for link in (
        "https://example.test/" + "x" * 800,
        "[資料](https://example.test/" + "x" * 800 + ")",
        "<https://example.test/" + "x" * 800 + "|資料>",
    ):
        assert scrub_outbound_reply("あ" * 2300 + "\n" + link) == "あ" * 2300 + "…"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("rich", [False, True])
def test_delivery_boundary_scrubs_every_surface(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, Any], rich: bool
) -> None:
    posts: list[dict[str, Any]] = []

    async def post_message(**kwargs: Any) -> Any:
        posts.append(kwargs)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(
        detached_jobs, "_slack_client", lambda _: SimpleNamespace(post_message=post_message)
    )
    message = RichMessage(
        text=case["input"],
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": case["input"]}}],
    )
    assert asyncio.run(
        detached_jobs._post_once(
            case["input"],
            detached_jobs.Destination("D12345678", None),
            request_id="scrub-test",
            timeout_s=1,
            rich=message if rich else None,
            permanent_urls=case.get("permanent_urls"),
        )
    )
    expected = case["expected"] if rich else detached_jobs.slack_escape(case["expected"])
    assert posts[0]["text"] == expected
    if case["input"] != case["expected"]:
        assert "blocks" not in posts[0]


def test_original_is_used_only_from_the_same_result_record(monkeypatch: pytest.MonkeyPatch) -> None:
    temporary = "https://s3.example.test/a?X-Amz-Signature=secret"
    original = "https://drive.google.com/file/d/ORIGINAL/view"
    data = {"slack_summary": temporary, "report_url": temporary, "drive_url": original}
    assert outbound_permanent_urls(data) == {temporary: original}
    assert outbound_permanent_urls({"report_url": temporary}) == {}
    assert outbound_permanent_urls({"report_url": temporary, "drive_url": temporary}) == {}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(direct_summary, "rich_message", lambda *a, **k: None)

    def post(*args: Any, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "posted"

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    assert (
        direct_summary.deliver(
            data, detached_jobs.Destination("D12345678", None), request_id="test"
        )
        == "posted"
    )
    assert calls[0]["permanent_urls"] == {temporary: original}
