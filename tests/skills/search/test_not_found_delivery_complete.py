"""宅配の存在・不在・未完了を通常検索とfanoutの最終配送で区別する。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_campaign_files import _video, _video_campaign
from tests.skills.search.test_fanout import META, _bedrock, make


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", _bedrock)


def context(kinds: tuple[str, ...] = ("not_found",)) -> SkillContext:
    return SkillContext(
        metadata={
            **META,
            "_search_direct_answer": True,
            "_search_direct_answer_kinds": kinds,
        }
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize(
    "query", ["宅配の事例", "宅配の事例を教えて", "TTOの宅配の事例", "ＴＴＯの宅配の事例"]
)
def test_delivery_case_exists_with_width_variation_never_becomes_not_found(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, query: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    hit = _video_campaign(
        1,
        "宅配会社",
        "ＴＴＯ 宅配",
        [_video("https://example.test/delivery", "宅配の紹介", 1000, 10)],
    )
    skill, _ = make([hit])
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("宅配の事例です。", 0.0))
    out = skill.run(SearchInput(query=query), context())
    assert out.found
    assert "見つかりませんでした" not in out.answer
    assert "https://example.test/delivery" in out.answer
    assert out.direct_answer_kind is None  # not_foundだけを許可


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_absent_delivery_case_is_one_sentence_and_logs_final_scope_without_content(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    hit = _video_campaign(
        1, "物流会社", "採用", [_video("https://example.test/unrelated", "採用紹介", 1000, 10)]
    )
    skill, _ = make([hit])
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._summarize = MagicMock(side_effect=AssertionError("不在の要約は禁止"))
    with capture_logs() as logs:
        out = skill.run(SearchInput(query="宅配の事例"), context())
    assert out.direct_answer_kind == "not_found"
    scope = "社内資料・施策実績・Slack" if fanout_enabled else "社内資料・施策実績"
    assert out.direct_answer == f"宅配の事例は見つかりませんでした（探した範囲: {scope}）。"
    row = next(r for r in logs if r["event"] == "search_found_decision")
    assert row["reason"] == "case_target_mismatch"
    assert row["complete"] is True and row["found"] is False
    assert row["hit_count"] == 0
    assert "宅配" not in str(row) and "物流会社" not in str(row)


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("failure", ["alias_incomplete", "slack_error", "slack_not_connected"])
def test_incomplete_delivery_search_never_delivers_not_found(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, failure: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([])
    if failure == "alias_incomplete":
        skill._alias_expansion = True
        monkeypatch.setattr(skill, "_retry_aliases", lambda *a: ([], ["宅配の事例"], True))
    else:
        lookup = SlackLookup(
            status="error" if failure == "slack_error" else "not_connected", reason="search_failed"
        )
        skill._composite = True
        monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: object())
        monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
        monkeypatch.setattr(fanout, "lookup_slack", lambda *a, **kw: lookup)
    with capture_logs() as logs:
        out = skill.run(SearchInput(query="宅配の事例"), context())
    assert out.direct_answer_kind is None and out.direct_answer is None
    assert "確認できませんでした" in out.answer
    assert "見つかりませんでした" not in out.answer
    row = next(r for r in logs if r["event"] == "search_found_decision")
    assert row["complete"] is False and row["reason"] == "incomplete_search"


@pytest.mark.parametrize("failure", ["vault_timeout", "case_error"])
def test_fanout_failed_required_lane_never_delivers_not_found(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    skill, _ = make([])
    if failure == "vault_timeout":
        monkeypatch.setattr(skill, "_retrieve", MagicMock(side_effect=TimeoutError()))
    else:
        from teamagent.skills.search.fanout_sources import SourceResult

        monkeypatch.setattr(
            fanout,
            "case_lookup_by_industry",
            lambda *a, **kw: SourceResult(source="case", status="failed", reason="db_error"),
        )
    out = skill.run(SearchInput(query="宅配の事例", filter_industry="運輸・交通"), context())
    assert out.direct_answer is None and out.direct_answer_kind is None
    assert "確認できませんでした" in out.answer
    assert "見つかりませんでした" not in out.answer


def test_direct_not_found_tracks_retrieval_failure_when_legacy_guards_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    hit = _video_campaign(
        1, "物流会社", "採用", [_video("https://example.test/unrelated", "採用紹介", 1000, 10)]
    )
    skill, _ = make([hit])
    skill._result_guard = False
    skill._not_found_answer = False

    def incomplete(*args: object, **kwargs: object) -> list[object]:
        probe = kwargs["probe"]
        if isinstance(probe, dict):
            probe["incomplete"] = "true"
        return [hit]

    monkeypatch.setattr(skill, "_retrieve", incomplete)
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    out = skill.run(SearchInput(query="宅配の事例"), context())
    assert out.direct_answer is None
    assert "確認できませんでした" in out.answer


def _slack(lookup: SlackLookup, skill: object, monkeypatch: pytest.MonkeyPatch) -> None:
    skill._composite = True  # type: ignore[attr-defined]
    monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: object())
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
    monkeypatch.setattr(fanout, "lookup_slack", lambda *a, **kw: lookup)


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_hidden_slack_matches_keep_the_lock_notice_and_are_not_called_absent(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    """非公開の場所に一致があるときは「無い」と直接届けず、🔒 の案内つきの従来の文にする。"""
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([])
    _slack(SlackLookup(status="ok", reason="ok", hidden_count=2), skill, monkeypatch)
    out = skill.run(SearchInput(query="宅配の資料"), context())
    assert out.direct_answer_kind is None and out.direct_answer is None
    assert "検索が完了せず" not in out.answer
