"""Eval 8: anonymous fixtures, no service calls or changes to scoring rules."""

from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill
from teamagent.skills.search import compact, fanout
from teamagent.skills.search.answer_mode import strip_advice
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.eval8_checks import assert_eval_checks
from tests.skills.knowledge_deliver.test_knowledge_deliver import (
    _ctx,
    _gdrive_mock,
    _hit,
    _search_mock,
    _slack_mock,
)
from tests.skills.search.test_comparison import _campaign
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])


def test_named_proposal_does_not_deliver_a_competitor_deck() -> None:
    started = monotonic()
    search = _search_mock(
        [
            _hit(
                source_type="gdrive",
                source_uri="gdrive://COMPETITOR",
                title="競合資料_{client_a}【ショート動画事例】.pdf",
                doc_type="提案書",
            ),
            _hit(
                source_type="gdrive",
                source_uri="gdrive://PROPOSAL",
                title="提案_{client_a}【ご提案書】.pdf",
                doc_type=None,
            ),
        ]
    )
    slack, gdrive = _slack_mock(), _gdrive_mock()
    slack.upload_files = AsyncMock(return_value="posted")
    ctx = _ctx(email=META["user_email"])
    out = KnowledgeDeliverSkill(search=search, slack=slack, gdrive=gdrive).run(
        KnowledgeDeliverInput(query="{client_a}の提案書 PDF を出して", top_k=1), ctx
    )
    sent_names = (
        [name for _, name in slack.upload_files.await_args.args[1]]
        if slack.upload_files.await_args
        else [slack.upload_file.await_args.kwargs["title"]]
    )
    assert_eval_checks(
        "named_proposal", [out.answer], elapsed=monotonic() - started, files=sent_names
    )
    assert out.delivered_count == 1
    assert gdrive.download_file_bytes.call_args.kwargs["file_id"] == "PROPOSAL"
    assert "PROPOSAL" in out.answer and "COMPETITOR" not in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_named_proposal_is_recalled_even_when_dense_pool_has_only_competitors(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    competitor = _case(
        1, title="競合資料_{client_a}【ショート動画事例】.pdf", cls_doc_type="提案書"
    )
    proposal = _case(2, title="提案_{client_a}【ご提案書】.pdf", cls_doc_type=None)
    skill, pg = make([competitor])
    pg.find_documents_for_advertiser.return_value = [proposal]
    skill._summarize = MagicMock(return_value=("提案書があります。[S:2]", 0.0))
    out = skill.run(
        SearchInput(query="{client_a}の提案書 PDF を出して", filter_client="{client_a}", top_k=1),
        SkillContext(metadata=META),
    )
    assert [h.chunk_id for h in out.hits] == [2]
    assert any(
        c.kwargs.get("doc_type") == "提案書"
        for c in pg.find_documents_for_advertiser.call_args_list
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_metrics_scope_explanation_and_link(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    started = monotonic()
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    rows = [
        replace(
            _campaign(
                i,
                name,
                f"施策実績 {{client_a}} {name}\n再生数: 平均 {i * 100}\n上位の投稿（再生数順）:\n1. 再生 {i * 1000}、保存 {i * 10}、@creator、調理実演 https://example.test/video{i}\n投稿文の特徴: 調理実演と盛り付けを紹介。",
            ),
            metadata={
                **_case(i).metadata,
                "campaign_aggregate": "true",
                "advertiser": "{client_a}",
                "campaign": name,
            },
        )
        for i, name in enumerate(("{campaign_a}", "{campaign_b}"), 1)
    ]
    skill, _ = make(rows)
    skill._summarize = MagicMock(
        return_value=(
            "調理の手順を見せた点が関心につながった可能性があります。[S:1]\n盛り付けで利用場面を伝えた点も仮説です。[S:1]",
            0.0,
        )
    )
    out = skill.run(
        SearchInput(query="{client_a}の{campaign_a}の動画ごとの結果と、何が効いたか教えて"),
        SkillContext(metadata=META),
    )
    assert_eval_checks("metrics_explanation", [out.answer], elapsed=monotonic() - started)
    assert "{campaign_b}" not in out.answer
    assert "調理の手順" in out.answer and "盛り付け" in out.answer
    assert "https://drive.google.com/" in out.answer
    assert all(
        h.metadata.get("campaign") != "{campaign_b}" for h in skill._summarize.call_args.args[1]
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_effect_question_still_summarizes_when_comparison_facts_exist(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    started = monotonic()
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    rows = [
        replace(
            _campaign(
                i,
                name,
                f"施策実績 {{client_a}} {name}\n再生数: 平均 {i * 100}\n上位の投稿（再生数順）:\n1. 再生 {i * 1000}、保存 {i * 10}、@creator、調理実演 https://example.test/video{i}\n投稿文の特徴: 調理実演。",
            ),
            metadata={
                **_case(i).metadata,
                "campaign_aggregate": "true",
                "advertiser": "{client_a}",
                "campaign": name,
            },
        )
        for i, name in enumerate(("{campaign_a}", "{campaign_b}"), 1)
    ]
    skill, _ = make(rows)
    skill._summarize = MagicMock(
        return_value=(
            "調理実演が利用場面を伝えた可能性があります。[S:1]\n盛り付けの紹介も関心につながった仮説です。[S:2]",
            0.0,
        )
    )
    first = skill.run(
        SearchInput(query="{campaign_a}と{campaign_b}の結果を見せて"),
        SkillContext(metadata=META),
    )
    skill._summarize.reset_mock()
    out = skill.run(SearchInput(query="比較して何が効いた？"), SkillContext(metadata=META))
    assert_eval_checks("compare_effect", [first.answer, out.answer], elapsed=monotonic() - started)
    skill._summarize.assert_called_once()
    assert "調理実演" in out.answer and "盛り付け" in out.answer
    assert "https://drive.google.com/" in out.answer
    assert "📊" not in out.answer and "再生平均" not in out.answer


def test_completed_status_drops_unsolicited_next_action() -> None:
    answer = "実施は完了しています。\n\n次のアクション\n再提案しましょう。\n\n出典: https://drive.google.com/file/d/REPORT/view"
    assert (
        strip_advice(answer, "{client_a}の{campaign_a}は今どうなっている？")
        == "実施は完了しています。\n\n出典: https://drive.google.com/file/d/REPORT/view"
    )


def test_status_answer_preserves_evidence_date() -> None:
    started = monotonic()
    hit = replace(_case(1, updated_at="2026-09-30"), content="{campaign_a}は完了。今期は見送り。")
    answer = compact.build(
        "実施は完了し、今期は見送りです。[S:1]",
        [hit],
        (),
        lambda h: "https://drive.google.com/file/d/FILE1/view",
        query="{campaign_a}は今どうなっている？",
        displayed_refs=[],
    )
    assert_eval_checks("completed_status", [answer], elapsed=monotonic() - started)
    assert "2026-09-30" in answer and "時点" in answer
    assert "https://drive.google.com/" in answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_unjoined_topic_keeps_the_actual_slack_source(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    started = monotonic()
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    url = "https://example.slack.com/archives/C12345678/p1792041600123456"
    item = SlackItem(
        SlackHitOut(channel="#public-exhibition", permalink=url),
        "{newspaper}の{exhibition}は展示企画で、当社は動画制作を担当。",
    )
    lookup = SlackLookup("ok", "ok", (item,))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: lookup)
    skill, _ = make([])
    skill._start_slack_lookup = MagicMock(return_value=MagicMock())
    skill._collect_slack_lookup = MagicMock(return_value=lookup)
    skill._summarize = MagicMock(
        return_value=("展示企画で、当社は動画制作を担当しています。[S:slack0]", 0.0)
    )
    out = skill.run(
        SearchInput(query="{newspaper}の{exhibition}って何？うちも関わってる？"),
        SkillContext(metadata=META),
    )
    assert_eval_checks(
        "unjoined_channel_topic", [out.answer], elapsed=monotonic() - started, slack_url=url
    )
    assert url in out.answer and "会話の記録" in out.answer
    assert "動画制作" in out.answer
