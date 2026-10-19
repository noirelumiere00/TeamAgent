"""元の事例対象語を業種へ広げて直接回答しない回帰テスト（外部接続なし）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.not_found import case_query_target
from teamagent.skills.search.result_guard import WEAK_RESULT_NOTICE
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_campaign_files import _video, _video_campaign
from tests.skills.search.test_fanout import META, _bedrock, make

DIRECT_META = {
    **META,
    "_search_direct_answer": True,
    "_search_direct_answer_kinds": ("not_found", "comparison", "list_fast", "video_metrics"),
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.delenv("SEARCH_FANOUT_LIST_FAST", raising=False)
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", _bedrock)


@pytest.mark.parametrize(
    ("query", "industry", "target"),
    [
        ("宅配の事例を教えて", "運輸・交通", "宅配"),
        ("食品メーカーの事例ってある？", "食品", "食品"),
        ("食品メーカーの宅配の事例ってある？", "食品", "食品メーカーの宅配"),
        ("INPEXの事例を教えて", "エネルギー", "INPEX"),
    ],
)
def test_case_target_preserves_original_subject_before_industry_expansion(
    query: str, industry: str, target: str
) -> None:
    assert case_query_target(query, industry) == target


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_delivery_case_with_only_logistics_sends_one_not_found_sentence(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    campaign = _video_campaign(
        1,
        "物流会社",
        "採用動画",
        [_video(f"https://example.test/logistics/{i}", "採用紹介", 1000, 10) for i in range(29)],
    )
    campaign.metadata["cls_industry"] = "運輸・交通"
    skill, _ = make([campaign])
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    summary = MagicMock(return_value=("物流の採用動画29本があります。", 0.01))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query="宅配の事例を教えて", filter_industry="運輸・交通"),
        SkillContext(metadata=DIRECT_META),
    )
    assert (
        out.direct_answer
        == out.answer
        == (
            "宅配の事例は見つかりませんでした（探した範囲: 社内資料・施策実績"
            + ("・Slack" if fanout_enabled else "")
            + "）。"
        )
    )
    assert out.direct_answer_kind == "not_found" and not out.found
    summary.assert_not_called()


def test_logistics_case_row_cannot_replace_original_delivery_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    skill, pg = make([])
    pg.list_case_studies.return_value = [
        {
            "title": "物流会社の採用動画",
            "source_uri": "https://example.test/logistics-case",
            "case_client": "物流会社",
            "case_industry": "運輸・交通",
        }
    ]
    out = skill.run(
        SearchInput(
            query="宅配の事例を教えて", filter_industry="運輸・交通", filter_client="物流会社"
        ),
        SkillContext(metadata=DIRECT_META),
    )
    assert out.direct_answer == (
        "宅配の事例は見つかりませんでした（探した範囲: 社内資料・施策実績・Slack）。"
    )
    assert not out.found


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_food_manufacturer_case_keeps_industry_campaigns(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    food = _video_campaign(
        1, "食品会社", "試食", [_video("https://example.test/food", "食べ方", 1000, 10)]
    )
    skill, _ = make([food])
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("食品会社の試食施策です。", 0.01))
    out = skill.run(
        SearchInput(query="食品メーカーの事例ってある？", filter_industry="食品"),
        SkillContext(metadata=DIRECT_META),
    )
    assert out.found and "見つかりません" not in out.answer
    assert "食品会社" in out.answer and "https://example.test/food" in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_weak_reference_answer_is_never_sent_directly(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    campaign = _video_campaign(
        1, "食品会社", "試食", [_video("https://example.test/food", "食べ方", 1000, 10)]
    )
    skill, _ = make([campaign])
    skill._campaign_files = True
    skill._not_found_answer = False
    skill._result_guard = True
    skill._weak_result_threshold = 0.95
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("参考資料です。", 0.01))
    out = skill.run(
        SearchInput(query="資料と施策を教えて"),
        SkillContext(metadata=DIRECT_META),
    )
    assert out.answer.startswith(WEAK_RESULT_NOTICE)
    assert out.direct_answer is None and out.direct_answer_kind is None
