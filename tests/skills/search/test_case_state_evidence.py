"""根拠束の旧行互換・公式性・順位非依存・公開面への伝搬（実接続なし）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.case_state import CaseStateEvidence, SourceRef, StateRecord
from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout, fanout_sources
from teamagent.skills.search import timeline_status as timeline
from teamagent.skills.search.schema import SearchHitOut, SearchInput
from tests.skills.search.test_search_dates_exposure import _build, _pgvector


def row(n: int = 1, state: str = "won", **record_values: Any) -> SearchHit:
    ref = SourceRef(
        source_type="other",
        external_id=f"source-{n}",
        locator=f"page:{n}",
        url=f"https://example.test/source#{n}",
        excerpt="確認済みの原文",
        content_sha256=str(n) * 64,
    )
    record = StateRecord.model_validate(
        {
            "state": state,
            "authority": "official",
            "source_ref": ref,
            "evidence_date": "2026-09-01",
            "evidence_date_kind": "reviewed",
            "state_as_of": "2026-08-15",
            "state_as_of_precision": "day",
            "reviewed_by": "reviewer-001",
            "reviewed_on": "2026-09-01",
            "review_ref": "review:001",
            **record_values,
        }
    )
    bundle = CaseStateEvidence(
        subject_ref="case:anonymous-001",
        source_ref=ref,
        records=(record,),
        document_kind="提案書",
    )
    return SearchHit(
        n,
        "提案のみ",
        0.9,
        {
            "source_type": ref.source_type,
            "source_uri": ref.url,
            "title": "提案書",
            "case_state_external_id": ref.external_id,
            "case_state_evidence": bundle.model_dump(mode="json"),
            "updated_at": "2026-10-01",
            "cls_phase": "受注",
        },
    )


def test_official_state_has_its_own_date_and_source() -> None:
    source = row(state_as_of="2026-08", state_as_of_precision="month")
    status = timeline.resolve_statuses([source], case_state=True)[0]
    assert status.evidence is not None
    assert status.evidence.state == "won" and status.evidence.authority == "official"
    assert status.text == "2026-08 時点の正式状態: 受注"
    assert status.evidence.source_ref.url == source.metadata["source_uri"]
    assert "2026-10-01" not in status.text


@pytest.mark.parametrize("authority", ["reported", "unverified"])
def test_nonofficial_completed_record_never_becomes_official(authority: str) -> None:
    status = timeline.resolve_statuses(
        [row(state="completed", authority=authority)], case_state=True
    )[0]
    assert status.evidence is not None and status.evidence.state == "unknown"
    assert "正式状態は不明" in status.text


@pytest.mark.parametrize("bad", [None, [], "wrong", {"version": 0}, {"version": 2}])
def test_missing_invalid_or_old_bundle_is_unknown(bad: Any) -> None:
    source = row()
    source.metadata["case_state_evidence"] = bad
    assert timeline.case_state_of(source).state == "unknown"
    assert (
        timeline.resolve_statuses([source], case_state=True)[0].text
        == "正式状態は不明（正本未確認）"
    )


def test_wrong_source_and_forged_top_level_state_are_not_accepted() -> None:
    source = row()
    source.metadata["case_state_external_id"] = "other-source"
    assert timeline.case_state_of(source).records == ()
    source = row()
    bundle = source.metadata["case_state_evidence"]
    bundle.update(
        {"state": "completed", "authority": "official", "resolution": "resolved", "records": []}
    )
    status = timeline.resolve_statuses([source], case_state=True)[0]
    assert status.evidence is not None and status.evidence.state == "unknown"


def test_invalid_version_type_is_unknown() -> None:
    source = row()
    source.metadata["case_state_evidence"]["version"] = True
    assert timeline.case_state_of(source).records == ()


@pytest.mark.parametrize("as_of,precision", [("2026-08-15", "day"), ("2026-08", "month")])
def test_conflict_is_independent_of_input_order(as_of: str, precision: str) -> None:
    sources = [row(), row(2, state="lost", state_as_of=as_of, state_as_of_precision=precision)]
    forward = timeline.resolve_statuses(sources, case_state=True)
    assert forward == timeline.resolve_statuses(list(reversed(sources)), case_state=True)
    assert len(forward) == 1
    assert forward[0].evidence is not None and forward[0].evidence.resolution == "conflict"
    assert forward[0].evidence.state == "unknown"
    assert len(forward[0].evidence_sources) == 2


def test_latest_state_time_wins_over_file_update_and_future_is_unknown() -> None:
    sources = [row(), row(2, state="completed", state_as_of="2026-09-01")]
    sources[0].metadata["updated_at"] = "2099-12-31"
    status = timeline.resolve_statuses(sources, case_state=True)[0]
    assert status.evidence is not None and status.evidence.state == "completed"
    future = timeline.resolve_statuses([row(state_as_of="2099-01-01")], case_state=True)[0]
    assert future.evidence is not None and future.evidence.state == "unknown"


def test_public_normal_and_fanout_fields_are_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    source = row()
    skill = _build(_pgvector([source]))
    ctx = SkillContext(metadata={"user_email": "reader@example.test"})
    request = SearchInput(query="提案書", include_answer=False)
    monkeypatch.delenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", raising=False)
    baseline = skill.run(input=request, ctx=ctx).hits[0].model_dump()
    assert "case_state_evidence" not in baseline
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "other@example.test")
    assert skill.run(input=request, ctx=ctx).hits[0].model_dump() == baseline
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "*")
    normal = skill.run(input=request, ctx=ctx).hits[0].case_state_evidence
    parallel = fanout._output_hits(skill, [source], {}, case_state=True)[0].case_state_evidence
    assert normal is not None and normal.state == "won"
    assert normal == parallel
    assert (
        "case_state_evidence"
        not in SearchHitOut(chunk_id=1, content="", score=1, source="").model_dump()
    )


def test_adapter_projects_bundle_without_enabling_legacy_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = row()
    pg = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": source.content,
            "score": 0.9,
            "source_type": source.metadata["source_type"],
            "source_uri": source.metadata["source_uri"],
            "case_state_evidence": source.metadata["case_state_evidence"],
            "case_state_external_id": source.metadata["case_state_external_id"],
        }
    ]
    monkeypatch.delenv("SEARCH_TIMELINE_STATUS", raising=False)
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "*")
    found = pg.search_similar_new_schema(conn, [0.1] * 1024)
    assert timeline.case_state_of(found[0]).records[0].authority == "official"
    sql = cursor.execute.call_args.args[0]
    assert "case_state_evidence" in sql and "AS timeline_metadata" not in sql
    monkeypatch.delenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS")
    pg.search_similar_new_schema(conn, [0.1] * 1024)
    assert "case_state_evidence" not in cursor.execute.call_args.args[0]


def test_case_row_keeps_source_binding() -> None:
    source = row()
    item = fanout_sources._to_case_row(
        {
            **source.metadata,
            "case_state_source_type": "other",
        },
        stage=1,
        main=set(),
    )
    result = fanout_sources.SourceResult(source="case", status="ok", reason="ok", items=(item,))
    found = fanout._case_hits(result)
    assert timeline.case_state_of(found[0]).source_ref == timeline.case_state_of(source).source_ref


def test_live_slack_report_keeps_its_date_and_source() -> None:
    from teamagent.adapters.slack_user_reader import SlackSearchMatch
    from teamagent.skills.search.composite import _to_item
    from teamagent.skills.search.drive_first import slack_hit

    match = SlackSearchMatch(
        channel_id="C12345678",
        channel_name="test",
        ts="1790784000.000002",
        text="*商流*\n直販\n*商談感触（BANT）*\nA\n*商談フェーズ*\n受注",
        permalink="https://example.slack.com/archives/C12345678/p1790784000000002",
        channel_is_private=False,
        channel_is_channel=True,
    )
    baseline = _to_item(match)
    assert "case_state_evidence" not in baseline.hit.model_dump()
    item = _to_item(match, case_state=True)
    status = timeline.resolve_statuses([slack_hit(item)], case_state=True)[0]
    assert status.evidence is not None and status.evidence.state == "unknown"
    report = status.evidence.records[0]
    assert report.authority == "reported" and report.evidence_date == item.hit.posted_on
    assert report.source_ref.url == item.hit.permalink
