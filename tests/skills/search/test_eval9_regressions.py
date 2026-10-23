"""Eval 9: anonymous requests through both retrieval paths, without services."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search.alias_expansion import resolve_topic_client
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_comparison import _campaign
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("doc_type", [None, "事例"])
@pytest.mark.parametrize("named", [False, True])
def test_case_intent_keeps_proposals_reports_and_unclassified_documents(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, doc_type: str | None, named: bool
) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    rows = [
        _case(
            i,
            title=f"{{client_a}}_{kind or '資料'}.pdf",
            cls_doc_type=kind,
            client_name="{client_a}",
        )
        for i, kind in enumerate(("提案書", "報告書", None), 1)
    ]
    skill, pg = make(rows)
    pg.list_client_names.return_value = ["{client_a}", "{other_client}"]
    skill._summarize = MagicMock(return_value=("事例があります。[S:1]", 0.0))
    out = skill.run(
        SearchInput(
            query="{client_a}の事例ある？"
            if named
            else "食品メーカーのショート動画の事例ってある？",
            filter_client="{client_a}" if named else None,
            filter_doc_type=doc_type,
        ),
        SkillContext(metadata=META),
    )
    assert out.found
    assert {ref.drive_file_id for ref in out.displayed_refs} == {"FILE1", "FILE2", "FILE3"}
    assert (
        "見つかりません" not in out.answer
        and "ご指定のクライアントの資料ではありません" not in out.answer
    )
    assert all(
        c.kwargs.get("doc_type") != "事例" for c in pg.find_documents_for_advertiser.call_args_list
    )
    assert all(
        (c.kwargs.get("sticky_filters") or {}).get("cls_doc_type") != "事例"
        for c in pg.search_similar_new_schema.call_args_list
    )


def test_topic_resolution_does_not_change_case_client_or_invent_industry_client() -> None:
    vocabulary = ["{client_a}", "{client_a}ホールディングス", "{other_client}"]
    assert resolve_topic_client("{client_a}の事例ある？", vocabulary) == (
        "{client_a}",
        "{client_a}",
    )
    assert resolve_topic_client("{client_a}の事例ある？", vocabulary, "{client_a}") is None
    assert resolve_topic_client("食品メーカーのショート動画の事例ってある？", vocabulary) is None


@pytest.mark.parametrize("blocked", ["flag", "email", "identity", "channel"])
def test_case_filter_normalization_stays_in_the_allowed_verified_dm(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0" if blocked == "flag" else "1")
    monkeypatch.setenv(
        "SEARCH_DRIVE_FIRST_ALLOWED_EMAILS",
        "other@example.test" if blocked == "email" else META["user_email"],
    )
    meta = {**META}
    if blocked == "identity":
        meta["identity_verified"] = False
    if blocked == "channel":
        meta["channel_id"] = "CPUBLIC"
    skill, pg = make([])
    skill.run(
        SearchInput(query="{client_a}の事例ある？", filter_doc_type="事例"),
        SkillContext(metadata=meta),
    )
    assert pg.search_similar_new_schema.call_args_list
    assert all(
        (call.kwargs.get("sticky_filters") or {}).get("cls_doc_type") == "事例"
        for call in pg.search_similar_new_schema.call_args_list
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_metrics_only_does_not_satisfy_an_effect_question(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    row = replace(
        _campaign(
            1,
            "{campaign_a}",
            "施策実績 {client_a} {campaign_a}\n上位の投稿（再生数順）:\n"
            "1. 再生 331300、保存 127、@creator、調理実演 https://example.test/video1\n"
            "投稿文の特徴: 調理実演で利用場面を伝える。",
        ),
        metadata={
            **_case(1).metadata,
            "campaign_aggregate": "true",
            "advertiser": "{client_a}",
            "campaign": "{campaign_a}",
        },
    )
    skill, _ = make([row])
    skill._summarize = MagicMock(
        side_effect=[
            ("- 調理実演が関心につながった可能性があります。[S:1]", 0.0),
            ("調理実演で利用場面が伝わった可能性があります。[S:1]", 0.0),
        ]
    )
    out = skill.run(
        SearchInput(query="{client_a}の{campaign_a}の動画ごとの結果と、何が効いたか教えて"),
        SkillContext(metadata=META),
    )
    assert skill._summarize.call_count == 2
    assert "調理実演で利用場面" in out.answer
