"""直接便では MCP 用の文字数予算で動画の行を切らない。実送信経路で確認する。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from teamagent.mcp_gateway import detached_jobs, server
from teamagent.skills.search import fanout
from teamagent.skills.search.answer_sections import Entry, LaneStatus, Sections, compose_answer
from teamagent.skills.search.campaign_files import video_stats_footer
from tests.mcp_gateway.test_direct_search_answer import enable
from tests.mcp_gateway.test_direct_search_answer import isolated as isolated
from tests.mcp_gateway.test_surface_video_followup import (
    ME,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)
from tests.skills.search.test_campaign_files import _video, _video_campaign
from tests.skills.search.test_fanout import _converse, make
from tests.skills.search.test_fanout import setup as setup


async def test_direct_status_keeps_one_short_answer_without_unrequested_video_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable(monkeypatch)
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", ME)
    campaigns = [
        _video_campaign(
            20 + i,
            "食品会社",
            f"施策{i}",
            [
                _video(
                    f"https://example.test/{'video' * 30}/{i}/{n}",
                    "投稿の説明" * 16,
                    507_700 + i,
                    n,
                )
                for n in range(1, 6)
            ],
        )
        for i in range(3)
    ]
    for hit in campaigns:
        hit.metadata["source_uri"] = f"gdrive://CAMPAIGN_{hit.chunk_id}"
    skill, _ = make(campaigns)
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    summary = "施策は完了し、商談は検討中です。"
    skill._summarize = MagicMock(return_value=(summary + "[S:20]", 0.01))
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)

    contents = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": "施策実績は今どうなっている？"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim()),
    )

    assert json.loads(contents[0].text)["delivered"] is True
    assert len(slack.posts) == 1
    posted = slack.posts[0]["text"]
    assert posted.startswith(summary)
    # 2500 字以内へ押し込んだ場合に最終施策や行が「…」で切れる本番の形。
    assert len(video_stats_footer(campaigns, "")) > 2500
    assert "📊" not in posted and "https://example.test/" not in posted
    assert "探した範囲" not in posted
    assert "https://drive.google.com/file/d/CAMPAIGN_20/view" in posted
    assert len(posted) <= 2500
    assert "\n\n\n" not in posted


@pytest.mark.parametrize("comparison", [False, True])
async def test_gateway_delivers_comparison_or_exact_case_miss(
    monkeypatch: pytest.MonkeyPatch, comparison: bool
) -> None:
    enable(monkeypatch)
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", ME)
    campaigns = [
        _video_campaign(
            i, "取引先", f"施策{i}", [_video(f"https://example.test/{i}", "投稿", plays, i)]
        )
        for i, plays in ((1, 507_700), (2, 300_000))
    ]
    for hit in campaigns:
        hit.metadata["cls_industry"] = "食品" if comparison else "運輸・交通"
    skill, _ = make(campaigns)
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._bedrock.converse.return_value = _converse("投稿の内容を紹介しています。[S:1]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    contents = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {
            "query": "施策1と施策2を比較して何が効いた？" if comparison else "宅配の事例を教えて",
            "filter_industry": "食品" if comparison else "運輸・交通",
        },
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim()),
    )
    payload = json.loads(contents[0].text)
    assert payload["delivered"] is True and len(slack.posts) == 1
    posted = slack.posts[0]["text"]
    if comparison:
        assert payload["delivery"]["answer_kind"] == "comparison"
        assert "# 施策比較表" not in posted
        assert "投稿の内容を紹介しています。" in posted
        metrics = posted.split("📊", 1)[1]
        assert "507,700" in metrics and "300,000" in metrics
        assert "https://example.test/1" in metrics and "https://example.test/2" in metrics
    else:
        assert payload["delivery"]["answer_kind"] == "not_found"
        assert (
            posted == "宅配の事例は見つかりませんでした（探した範囲: 社内資料・施策実績・Slack）。"
        )
        skill._bedrock.converse.assert_not_called()


def test_metrics_boundary_uses_one_blank_line_with_following_sections() -> None:
    from tests.mcp_gateway.test_direct_video_metrics import SECTION

    answer = compose_answer(
        head="要約です。\n\n" + SECTION,
        sections=Sections(entries={"vault": [Entry(title="報告書", kind="vault")]}),
        lanes={"vault": LaneStatus.found(1)},
    )
    assert SECTION + "\n\n📁" in answer
    assert "\n\n\n" not in answer


def test_direct_answer_keeps_full_head_before_timeline_and_required_files() -> None:
    head = "施策は完了し、商談は検討中です。" * 200
    answer = compose_answer(
        head=head,
        sections=Sections(
            entries={
                "vault": [Entry(title="報告書", url="https://example.test/report", kind="vault")],
                "deal": [Entry(title="商談の記録", kind="deal")],
            },
            timeline_lines={"deal": ("検討中、11月に連絡予定",)},
            required_counts={"vault": 1, "deal": 1},
        ),
        lanes={"vault": LaneStatus.found(2)},
        limit=None,
    )
    assert answer.startswith(head)
    assert "https://example.test/report" in answer
    assert "検討中、11月に連絡予定" in answer
    assert "探した範囲" in answer
