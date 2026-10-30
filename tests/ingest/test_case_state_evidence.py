"""匿名入力だけで正本・報告・投稿観測と再取り込みの境界を検証する。"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from teamagent.case_state import enabled
from teamagent.ingest import case_state_evidence as evidence
from teamagent.ingest.pipeline import _differential_pipeline_config
from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "reviews.json"
    path.write_text('{"version": 1, "reviews": []}')
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("CASE_STATE_REVIEW_MANIFEST", str(path))
    return path


def document(**metadata: Any) -> DocumentUpsert:
    return DocumentUpsert(
        source_type="gsheets",
        external_id="sheet:0:2",
        owner_email="reader@example.test",
        source_uri="https://example.test/sheet#row=2",
        title="提案書",
        modified_at="2026-10-01T00:00:00Z",
        metadata={"row_idx": 2, **metadata},
    )


def chunks(text: str = "提案のみ") -> list[ChunkUpsert]:
    return [ChunkUpsert(chunk_idx=0, content=text, embedding=[])]


def official_review(
    doc: DocumentUpsert, body: list[ChunkUpsert], **overrides: Any
) -> dict[str, Any]:
    return {
        "subject_ref": "case:anonymous-001",
        "record": {
            "state": "won",
            "authority": "official",
            "source_ref": evidence.source_ref(doc, body).model_dump(),
            "evidence_date": "2026-09-01",
            "evidence_date_kind": "reviewed",
            "state_as_of": "2026-08",
            "state_as_of_precision": "month",
            "reviewed_by": "reviewer-001",
            "reviewed_on": "2026-09-01",
            "review_ref": "review:001",
            **overrides,
        },
    }


@pytest.mark.parametrize(
    "allowed,email,want",
    [
        ("", "reader@example.test", False),
        ("*", None, False),
        ("*", "invalid", False),
        ("*", "reader@example.test", True),
        (" READER@example.test ", "reader@example.test", True),
        ("other@example.test", "reader@example.test", False),
    ],
)
def test_allowlist(monkeypatch: pytest.MonkeyPatch, allowed: str, email: Any, want: bool) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", allowed)
    assert enabled(email) is want


def test_off_does_not_read_manifest_or_modify_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", raising=False)
    monkeypatch.setenv("CASE_STATE_REVIEW_MANIFEST", "/nonexistent")
    doc = document(deal_phase="受注")
    assert evidence.attach(doc, chunks()) is doc
    assert evidence.input_version() == ""
    assert "case_state_evidence" not in _differential_pipeline_config(classify=False)


def test_default_manifest_uses_runtime_workdir(
    manifest: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CASE_STATE_REVIEW_MANIFEST")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "case_state_reviews.yaml").write_bytes(manifest.read_bytes())
    assert evidence.attach(document(), chunks()).metadata[evidence.KEY]["state"] == "unknown"


def test_proposal_report_and_post_numbers_are_independent(manifest: Path) -> None:
    doc = document(
        is_sales_fb=True,
        deal_phase="受注",
        cls_phase="受注",
        cls_doc_type="提案書",
        campaign_aggregate="true",
        video_count="6",
        plays_total="12345",
        first_posted_on="2026-08-01",
        last_posted_on="2026-08-15",
    )
    bundle = evidence.attach(doc, chunks()).metadata[evidence.KEY]
    assert bundle["state"] == "unknown"
    assert bundle["document_kind"] == "提案書"
    assert bundle["records"][0]["authority"] == "reported"
    assert bundle["records"][0]["raw_value"] == "受注"
    assert bundle["records"][0]["state_as_of"] is None
    assert bundle["records"][0]["evidence_date"] is None
    observation = bundle["delivery_observation"]
    assert observation["video_count"] == 6
    assert observation["metrics"] == {"plays_total": "12345"}
    assert observation["source_ref"] == bundle["source_ref"]


def test_review_survives_reingest_and_changes_skip_input(manifest: Path) -> None:
    doc, body = document(), chunks()
    before = _differential_pipeline_config(classify=False)
    manifest.write_text(json.dumps({"version": 1, "reviews": [official_review(doc, body)]}))
    first = evidence.attach(doc, body)
    second = evidence.attach(doc, body)
    assert first.metadata == second.metadata
    assert first.metadata[evidence.KEY]["records"][0]["authority"] == "official"
    assert first.metadata[evidence.KEY]["subject_ref"] == "case:anonymous-001"
    assert before != _differential_pipeline_config(classify=False)


@pytest.mark.parametrize("change", ["body", "position", "date"])
def test_review_rejects_changed_source_revision(manifest: Path, change: str) -> None:
    doc, body = document(), chunks()
    manifest.write_text(json.dumps({"version": 1, "reviews": [official_review(doc, body)]}))
    if change == "body":
        body = chunks("新しい提案")
    elif change == "position":
        doc = replace(doc, metadata={"row_idx": 3})
    else:
        doc = replace(doc, modified_at="2026-10-02T00:00:00Z")
    with pytest.raises(ValueError, match="revision/position mismatch"):
        evidence.attach(doc, body)


def test_review_never_matches_another_source_by_name(manifest: Path) -> None:
    doc, body = document(), chunks()
    manifest.write_text(json.dumps({"version": 1, "reviews": [official_review(doc, body)]}))
    other = replace(doc, external_id="sheet:0:3")
    assert evidence.attach(other, body).metadata[evidence.KEY]["records"] == []


def test_classification_drift_cannot_change_source_revision(manifest: Path) -> None:
    first = document(cls_industry="分類A", industry="分類A", cls_phase="提案")
    second = document(cls_industry="分類B", industry="分類B", cls_phase="受注")
    assert evidence.source_ref(first, chunks()) == evidence.source_ref(second, chunks())


def test_review_binds_the_actual_quote_and_page(manifest: Path) -> None:
    doc = document()
    body = [
        ChunkUpsert(chunk_idx=0, page_num=3, content="前文\n正式受注を確認\n後文", embedding=[])
    ]
    review = official_review(doc, body)
    review["record"]["source_ref"].update(locator="page:3", excerpt="正式受注を確認")
    manifest.write_text(json.dumps({"version": 1, "reviews": [review]}))
    saved = evidence.attach(doc, body).metadata[evidence.KEY]["records"][0]
    assert saved["source_ref"]["locator"] == "page:3"
    review["record"]["source_ref"]["locator"] = "page:4"
    manifest.write_text(json.dumps({"version": 1, "reviews": [review]}))
    with pytest.raises(ValueError, match="revision/position mismatch"):
        evidence.attach(doc, body)


@pytest.mark.parametrize(
    "override",
    [
        {"review_ref": ""},
        {"reviewed_by": " "},
        {"evidence_date": "2026-02-30"},
        {"state_as_of": "2026-08-01", "state_as_of_precision": "month"},
        {"state": "未受注"},
    ],
)
def test_invalid_review_stops_application(manifest: Path, override: dict[str, Any]) -> None:
    doc, body = document(), chunks()
    manifest.write_text(
        json.dumps({"version": 1, "reviews": [official_review(doc, body, **override)]})
    )
    with pytest.raises(ValueError):
        evidence.attach(doc, body)


def test_reply_report_uses_reply_time_and_link(manifest: Path) -> None:
    messages = [
        SimpleNamespace(
            ts="1780272000.000001", text="*商流*\n直販\n*商談感触（BANT）*\nA\n*商談フェーズ*\n提案"
        ),
        SimpleNamespace(
            ts="1790784000.000002", text="*商流*\n直販\n*商談感触（BANT）*\nA\n*商談フェーズ*\n受注"
        ),
    ]
    records = evidence.message_reports("C_TEST", "C_TEST:1780272000.000001", messages)
    assert [r["raw_value"] for r in records] == ["提案", "受注"]
    assert records[0]["evidence_date"] != records[1]["evidence_date"]
    assert records[1]["source_ref"]["locator"] == "message:1790784000.000002"
    assert records[1]["source_ref"]["url"].endswith("/p1790784000000002")
    assert all(r["state_as_of"] is None and r["authority"] == "reported" for r in records)


def test_slack_pipeline_review_change_bypasses_differential_skip(
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.ingest.test_ingest_differential import (
        _FakeDifferentialRepository,
        _install_slack_channel,
        _run_slack,
    )

    monkeypatch.setenv("INGEST_DIFFERENTIAL", "1")
    monkeypatch.setenv("USE_DOC_CLASSIFY", "0")
    monkeypatch.setenv("USE_CONTEXTUAL_INGEST", "0")
    _install_slack_channel(monkeypatch, text="確認済みの受注記録")
    repo = _FakeDifferentialRepository()
    _run_slack(repo)
    saved = repo.upsert_calls[-1]
    doc = DocumentUpsert(**{k: v for k, v in saved.items() if k != "chunk_texts"})
    body = chunks(saved["chunk_texts"][0])
    manifest.write_text(json.dumps({"version": 1, "reviews": [official_review(doc, body)]}))
    _run_slack(repo)
    assert len(repo.upsert_calls) == 2
    assert repo.upsert_calls[-1]["metadata"][evidence.KEY]["records"][0]["authority"] == "official"
    _run_slack(repo)
    assert len(repo.upsert_calls) == 2


@pytest.mark.parametrize("submitted", ["", "2026/10/01 23:30:00"])
def test_sheet_processing_time_is_not_report_date(
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    submitted: str,
) -> None:
    from unittest.mock import MagicMock

    from teamagent.adapters.gsheets_client import TabRows
    from tests.ingest.test_ingest_differential import _FakeDifferentialRepository, _run_gsheet

    monkeypatch.setenv("USE_DOC_CLASSIFY", "0")
    client = MagicMock()
    client.get_tab_rows.return_value = TabRows(
        sheet_id="1V",
        tab_name="フォーム回答 1",
        headers=("商流", "商談フェーズ", "商談感触（BANT）", "処理日時", "タイムスタンプ"),
        rows=(("直販", "受注", "A", "2026/10/03 12:00:00", submitted),),
        row_count=1,
    )
    monkeypatch.setattr("teamagent.adapters.gsheets_client.GSheetsClient.from_env", lambda: client)
    repo = _FakeDifferentialRepository()
    _run_gsheet(repo)
    record = repo.upsert_calls[0]["metadata"][evidence.KEY]["records"][0]
    assert record["evidence_date"] == ("2026-10-01" if submitted else None)
    assert record["evidence_date_kind"] == ("form_submitted" if submitted else "unknown")
    assert record["state_as_of"] is None


def test_offline_diff_does_not_print_source_text(
    manifest: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            [
                {
                    "document": asdict(document()),
                    "chunks": [{"chunk_idx": 0, "content": "private-text"}],
                }
            ]
        )
    )
    monkeypatch.setattr(sys, "argv", ["case_state_evidence", str(path)])
    evidence.main()
    output = capsys.readouterr().out
    change = json.loads(output)[0]
    assert change["changed"] is True and change["state"] == "unknown"
    assert "private-text" not in output and "example.test" not in output
