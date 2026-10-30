"""モデルの状態・日付の書き足しを出口で止める（実サービスなし）。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import timeline_status as timeline
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_case_state_evidence import row
from tests.skills.search.test_search_dates_exposure import _build, _pgvector


@pytest.mark.parametrize("query", ["提案書", "案件の現状", "事例を一覧で"])
@pytest.mark.parametrize("legacy_timeline", ["0", "1"])
def test_model_added_won_and_fabricated_date_never_reach_answer(
    monkeypatch: pytest.MonkeyPatch, query: str, legacy_timeline: str
) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "reader@example.test")
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", legacy_timeline)
    skill = _build(_pgvector([row(state="proposed")]))
    monkeypatch.setattr(
        skill,
        "_summarize",
        lambda *a, **kw: ("受注した実績です。2099-12-31に完了。失注も確認。", 0),
    )
    out = skill.run(
        SearchInput(query=query), SkillContext(metadata={"user_email": "reader@example.test"})
    )
    assert "受注" not in out.answer
    assert "実績" not in out.answer
    assert "完了" not in out.answer
    assert "失注" not in out.answer
    assert "2099-12-31" not in out.answer
    assert "2026-08-15 時点の正式状態: 提案" in out.answer
    assert "根拠日（確認日）: 2026-09-01" in out.answer
    assert "https://example.test/source#1" in out.answer


@pytest.mark.parametrize(
    "emails,email",
    [("", "reader@example.test"), ("other@example.test", "reader@example.test"), ("*", None)],
)
def test_gate_off_keeps_model_answer(
    monkeypatch: pytest.MonkeyPatch, emails: str, email: Any
) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", emails)
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "0")
    skill = _build(_pgvector([row(state="proposed")]))
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("旧回答: 受注", 0))
    out = skill.run(SearchInput(query="提案書"), SkillContext(metadata={"user_email": email}))
    assert "旧回答: 受注" in out.answer


def test_renderer_keeps_conflicting_sources_and_never_borrows_another_cases_state() -> None:
    won, lost = row(), row(2, state="lost")
    proposal = row(3, state="proposed")
    proposal.metadata["case_state_evidence"]["subject_ref"] = "case:anonymous-002"
    text = timeline.render_case_states([won, lost, proposal])
    assert text == timeline.render_case_states([proposal, lost, won])
    assert "正式状態は不明（根拠が競合）" in text
    assert "2026-08-15 時点の正式状態: 提案" in text
    assert all(f"https://example.test/source#{n}" in text for n in range(1, 4))
    assert "受注" not in text and "失注" not in text


@pytest.mark.parametrize("state,word", [("won", "受注"), ("completed", "完了"), ("lost", "失注")])
def test_final_word_guard_only_allows_the_resolved_official_state(state: str, word: str) -> None:
    statuses = timeline.resolve_statuses([row(state=state)], case_state=True)
    checked = timeline.guard_state_words("受注・実績・完了・失注", statuses)
    assert checked.count("不明") == 3
    assert word in checked
    assert timeline.guard_state_words("受注・実績・完了・失注") == "不明・不明・不明・不明"


def test_report_and_file_date_do_not_become_state_time_or_official_completion() -> None:
    source = row(state="completed", authority="reported")
    text = timeline.render_case_states([source])
    assert "正式状態は不明" in text
    assert "報告（正式性未確認） / 根拠日（確認日）: 2026-09-01" in text
    assert "2026-10-01" not in text and "完了" not in text


def test_public_hits_share_conflict_resolution_and_keep_their_own_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.search import fanout

    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "reader@example.test")
    hits = [row(), row(2, state="lost")]
    skill = _build(_pgvector(hits))
    out = skill.run(
        SearchInput(query="提案書", include_answer=False),
        SkillContext(metadata={"user_email": "reader@example.test"}),
    )
    parallel = fanout._output_hits(skill, hits, {}, case_state=True)
    for normal, other in zip(out.hits, parallel, strict=True):
        assert normal.case_state_evidence == other.case_state_evidence
        assert normal.case_state_evidence is not None
        assert normal.case_state_evidence.resolution == "conflict"
        assert normal.case_state_evidence.state == "unknown"
        assert (
            normal.case_state_evidence.source_ref.url
            == f"https://example.test/source#{normal.chunk_id}"
        )
    assert out.answer == "" and out.case_state_answer is not None
    assert "正式状態は不明（根拠が競合）" in out.case_state_answer


def test_untrusted_labels_and_links_cannot_inject_state_claims_or_mentions() -> None:
    source = row(state="proposed")
    source.metadata["title"] = "受注の実績\n<!here> 完了・失注"
    text = timeline.render_case_states([source])
    assert not any(word in text for word in ("受注", "実績", "完了", "失注", "<!here>"))
    assert "&lt;!here&gt;" in text


@pytest.mark.parametrize("bundle", [None, {"version": 0}, {"version": True}])
def test_legacy_or_invalid_rows_render_unknown_without_classification_inference(
    bundle: Any,
) -> None:
    source = row()
    source.metadata["case_state_evidence"] = bundle
    text = timeline.render_case_states([source])
    assert "正式状態は不明（正本未確認）" in text
    assert "根拠日: 不明" in text
    assert "受注" not in text and "2026-10-01" not in text


def test_two_stage_is_synchronous_for_case_state_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "reader@example.test")
    monkeypatch.setenv("USE_SEARCH_TWO_STAGE", "1")
    skill = _build(_pgvector([row(state="proposed")]))
    assert not skill._should_defer_answer(
        [row()], SkillContext(metadata={"user_email": "reader@example.test"})
    )


def test_live_slack_only_answer_uses_post_date_and_permalink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters.slack_user_reader import SlackSearchMatch
    from teamagent.skills.search.composite import SlackLookup, _to_item

    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "reader@example.test")
    match = SlackSearchMatch(
        channel_id="C12345678",
        channel_name="test",
        ts="1790784000.000002",
        text="*商流*\n直販\n*商談感触（BANT）*\nA\n*商談フェーズ*\n提案",
        permalink="https://example.slack.com/archives/C12345678/p1790784000000002",
        channel_is_private=False,
        channel_is_channel=True,
    )
    item = _to_item(match, case_state=True)
    skill = _build(_pgvector([]))
    monkeypatch.setattr(skill, "_composite_active", lambda ctx: True)
    monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: (None, 0))
    monkeypatch.setattr(
        skill,
        "_collect_slack_lookup",
        lambda *a, **kw: SlackLookup(status="ok", reason="ok", items=(item,)),
    )
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("受注の実績。完了。", 0))
    out = skill.run(
        SearchInput(query="提案書"), SkillContext(metadata={"user_email": "reader@example.test"})
    )
    assert "正式状態は不明" in out.answer
    assert f"根拠日（投稿日）: {item.hit.posted_on}" in out.answer
    assert "報告の記載（正式状態ではありません）: 提案" in out.answer
    assert item.hit.permalink in out.answer
    assert "受注" not in out.answer and "完了" not in out.answer
