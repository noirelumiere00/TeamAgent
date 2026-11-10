"""業種指定の一覧は一致根拠のある事例だけ。架空ヒット・外部接続なし。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout, material_files
from teamagent.skills.search.campaign_files import scope_by_industry
from teamagent.skills.search.case_list import render
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_case_list_composed import hit
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


def industry_hits():
    return [
        replace(hit(3, industry=None, score=1), content="架空社のドリンク紹介動画。"),
        hit(4, industry=None, score=1),
        hit(5, industry=None, score=1),
        hit(6, industry=None, score=1),
        hit(7, industry="化粧品", score=1),
        # 本文に指定業種の語があっても、既知の別業種は出さない。
        replace(hit(8, industry="エネルギー", score=1), content="ドリンクを飲む場面もある動画。"),
        hit(1),
        hit(2),
    ]


@pytest.mark.parametrize(
    ("target", "asked_industry"), [("飲料", None), (None, "飲料"), ("飲料", "飲料")]
)
@pytest.mark.parametrize("matching", [False, True])
def test_list_fast_excludes_unknown_and_other_industries(target, asked_industry, matching):
    hits = industry_hits()
    if not matching:
        hits = hits[1:6]
    model = MagicMock(return_value="{}")
    refs = []
    with capture_logs() as logs:
        answer = render(
            hits,
            target,
            lambda h: None,
            requested_count=5,
            asked_industry=asked_industry,
            summarize=model,
            displayed_refs=refs,
        )
    assert [ref.drive_file_id for ref in refs] == (["FILE1", "FILE2", "FILE3"] if matching else [])
    if matching:
        assert [h.chunk_id for h in model.call_args.args[0]] == [1, 2, 3]
        assert answer.count("|資料を見る>") == 3
        assert "見つかったのは 3 件です。" in answer
    else:
        assert answer == ""
        model.assert_not_called()
    assert logs == [
        {
            "event": "search_case_list_industry_filter",
            "industry": "飲料",
            "matched": 2 if matching else 0,
            "mentioned": 1 if matching else 0,
            "dropped_unknown": 3,
            "log_level": "info",
        }
    ]


def test_no_industry_keeps_five_and_legacy_soft_selection_keeps_unknown():
    hits = industry_hits()
    model = MagicMock(return_value="{}")
    with capture_logs() as logs:
        answer = render(hits, None, lambda h: None, requested_count=5, summarize=model)
    assert answer.count("|資料を見る>") == 5
    assert len(model.call_args.args[0]) == 5
    assert logs == []
    assert [h.chunk_id for h in scope_by_industry(hits, "飲料", soft=True)] == [1, 2, 3, 4, 5, 6]
    legacy = render(hits, "飲料", lambda h: None, requested_count=5, cards=True, drive_only=True)
    assert legacy.count("|資料を見る>") == 5


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_mode", [False, True])
@pytest.mark.parametrize("matching", [False, True])
def test_search_lists_only_matching_cases_or_returns_not_found(
    monkeypatch, fanout_enabled, drive_mode, matching
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_mode)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    hits = industry_hits()
    if not matching:
        hits = hits[1:6]
    skill, _ = make(hits)
    monkeypatch.setattr(skill, "_retrieve", lambda *a, **kw: hits)
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    model = MagicMock(return_value=("{}", 0.0))
    monkeypatch.setattr(skill, "_summarize", model)
    out = skill.run(SearchInput(query="飲料の事例を5件見せて"), SkillContext(metadata=META))
    assert out.found == matching
    assert out.answer.count("|資料を見る>") == (3 if matching else 0)
    assert f"見つかったのは {3 if matching else 0} 件です。" in out.answer
    assert all(f"FILE{i}/view" not in out.answer for i in range(4, 9))
    if matching:
        assert [h.chunk_id for h in model.call_args.args[1]] == [1, 2, 3]
    else:
        assert out.answer.startswith("飲料の事例は見つかりませんでした")
        assert "https://" not in out.answer
        model.assert_not_called()
