"""不在確定前の共通救済。DB・Slack・Bedrock はすべてフェイク。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_fanout import META, _bedrock, _vault_hit, make


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_NOT_FOUND_RECOVERY", raising=False)
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 0, 0.0)
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_summary_client", _bedrock)


@pytest.mark.parametrize("fanout_on", [False, True])
@pytest.mark.parametrize("drive_on", [False, True])
@pytest.mark.parametrize("recovery_on", [False, True])
def test_first_zero_then_wider_search_finds_document(
    monkeypatch: pytest.MonkeyPatch, fanout_on: bool, drive_on: bool, recovery_on: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_on)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_on)))
    monkeypatch.setenv("SEARCH_NOT_FOUND_RECOVERY", str(int(recovery_on)))
    skill, pg = make([])
    skill._embedder.embed = MagicMock(wraps=skill._embedder.embed)
    hit = _vault_hit(source_uri="https://drive.google.com/file/d/FOUND/view")
    pg.search_similar_new_schema.side_effect = [[], [hit]]
    out = skill.run(SearchInput(query="資料を探して"), SkillContext(metadata=META))
    assert out.found == recovery_on
    if recovery_on:
        assert out.direct_answer_kind != "not_found"
        assert "見つかりませんでした" not in out.answer
    else:
        baseline, _ = make([])
        expected = baseline._run_once(
            SearchInput(query="資料を探して"), SkillContext(metadata=META)
        )
        assert out.model_dump() == expected.model_dump()
    assert pg.search_similar_new_schema.call_count == (2 if recovery_on else 1)
    skill._embedder.embed.assert_called_once_with("資料を探して")
    assert skill._min_relevance == 0.4 and skill._not_found_threshold == 0.16
    assert all(c.kwargs["user_email"] == META["user_email"] for c in pg.connection.call_args_list)


def test_recovery_relaxes_both_thresholds_and_marks_weak_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    slack = MagicMock(return_value=SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "lookup_slack", slack)
    skill, pg = make([_vault_hit()])
    skill._bedrock = _bedrock(scores=[0.1])
    skill._embedder.embed = MagicMock(wraps=skill._embedder.embed)
    out = skill.run(SearchInput(query="資料を探して"), SkillContext(metadata=META))
    assert out.found and out.hits[0].is_low_confidence
    assert pg.search_similar_new_schema.call_count == 3
    assert out.direct_answer_kind != "not_found"
    skill._embedder.embed.assert_called_once_with("資料を探して")
    slack.assert_called_once()


def test_slack_only_evidence_rescues_drive_case_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    post = "https://example.slack.com/archives/CSHARED/p1234567890123456"
    item = SlackItem(SlackHitOut(channel="#shared", permalink=post), "宅配の動画事例です。")
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", (item,)))
    skill, _ = make([])
    out = skill.run(SearchInput(query="宅配の事例"), SkillContext(metadata=META))
    assert out.found and "見つかりませんでした" not in out.answer
    assert post in out.answer and "会話の記録" in out.answer


@pytest.mark.parametrize("complete", [False, True])
def test_final_miss_reports_scope_and_words_without_handoff(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setattr(
        fanout, "lookup_slack", lambda **kw: SlackLookup("ok" if complete else "error", "ok")
    )
    skill, pg = make([])
    out = skill.run(
        SearchInput(query="宅配の事例"),
        SkillContext(
            metadata={
                **META,
                "_search_direct_answer": True,
                "_search_direct_answer_kinds": ("not_found",),
            }
        ),
    )
    assert pg.search_similar_new_schema.call_count == 3
    assert ("見つかりませんでした" in out.answer) == complete
    assert (out.direct_answer_kind == "not_found") == complete
    assert all(word in out.answer for word in ("金庫", "Slack", "カルテ", "事例", "宅配の事例"))
    assert len([line for line in out.answer.splitlines() if line.startswith("探した範囲")]) == 1
    assert not any(word in out.answer for word in ("に聞いて", "自分で", "してください"))
    assert out.suggested_next is None


def test_success_on_first_search_does_not_add_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, pg = make()
    slack = MagicMock(side_effect=AssertionError("追加の検索は禁止"))
    monkeypatch.setattr(fanout, "lookup_slack", slack)
    assert skill.run(SearchInput(query="資料を探して"), SkillContext(metadata=META)).found
    assert pg.search_similar_new_schema.call_count == 1
    slack.assert_not_called()


@pytest.mark.parametrize("source", ["named", "karte", "case"])
def test_non_vector_sources_can_rescue_a_miss(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, pg = make([])
    hit = _vault_hit(
        client_name="日本ガイシ", source_uri="https://drive.google.com/file/d/RESCUE/view"
    )
    if source == "named":
        pg.find_documents_for_advertiser.return_value = [hit]
    elif source == "karte":
        pg.list_client_timeline_recent.return_value = [hit]
    else:
        pg.list_case_studies.return_value = [
            {
                "title": "日本ガイシの施策資料",
                "source_uri": "https://drive.google.com/file/d/RESCUE/view",
                "case_client": "日本ガイシ",
                "case_effect": "日本ガイシ ケイパ提案の内容",
            }
        ]
    out = skill.run(SearchInput(query="日本ガイシの資料"), SkillContext(metadata=META))
    assert out.found and out.direct_answer_kind != "not_found"
    assert "RESCUE" in out.answer


def test_late_case_filter_miss_still_expands_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.skills.search.test_alias_expansion import _Queries

    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, pg = make([])
    embedder = _Queries()
    skill._embedder = embedder
    pg.search_similar_new_schema.side_effect = lambda **kw: [
        _vault_hit(
            title="本田技研工業の事例"
            if "本田技研" in embedder.values[int(kw["embedding"][0])]
            else "他社",
            source_uri="https://drive.google.com/file/d/HONDA/view",
        )
    ]
    out = skill.run(SearchInput(query="ホンダの事例"), SkillContext(metadata=META))
    assert out.found and "HONDA" in out.answer
    assert any("本田技研" in q for q in embedder.values.values())


def test_broadening_does_not_drop_explicit_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, pg = make([])
    pg.find_documents_for_advertiser.return_value = [
        _vault_hit(client_name="日本ガイシ", cls_budget="〜100万", cls_solution="SNS運用")
    ]
    out = skill.run(
        SearchInput(query="日本ガイシの資料", filter_budget="500万〜", filter_solution="PR"),
        SkillContext(metadata=META),
    )
    assert not out.found
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["sticky_filters"] == {"cls_budget": "500万〜", "cls_solution": "PR"}


def test_unconnected_slack_does_not_return_work_to_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setattr(
        fanout, "lookup_slack", lambda **kw: SlackLookup("not_connected", "reconnect_required")
    )
    skill, _ = make([])
    out = skill.run(SearchInput(query="宅配の事例"), SkillContext(metadata=META))
    assert "確認できませんでした" in out.answer and "連携切れ" in out.answer
    assert "話しかけ" not in out.answer and "してください" not in out.answer
    assert out.suggested_next is None


def test_embeddings_are_reused_across_stages_but_not_across_requests() -> None:
    from tests.skills.search.test_alias_expansion import _search

    skill, _, pg = _search(lambda *_: [])
    query = "インペックスの資料"
    input = SearchInput(query=query)
    for request_count in (1, 2):
        out = skill.run(input, SkillContext(metadata=META))
        assert not out.found
        assert list(skill._embedder.values.values()) == [query, "INPEXの資料"] * request_count
        assert pg.search_similar_new_schema.call_count == 6 * request_count
        assert skill._embedding_cache is None


def test_legacy_alias_switch_still_disables_aliases_during_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_alias_expansion import _search

    monkeypatch.setenv("SEARCH_ALIAS_EXPANSION", "0")
    skill, _, pg = _search(lambda *_: [])
    query = "インペックスの資料"
    out = skill.run(SearchInput(query=query), SkillContext(metadata=META))
    assert not out.found and "探した範囲" in out.answer
    assert list(skill._embedder.values.values()) == [query]
    assert pg.search_similar_new_schema.call_count == 3
