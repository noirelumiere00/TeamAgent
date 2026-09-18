"""scripts/extract_cases.py の純関数（DB / Bedrock 非依存）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.cases.schema import CaseRecord

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "extract_cases", _ROOT / "scripts" / "extract_cases.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["extract_cases"] = _mod
_spec.loader.exec_module(_mod)


def _record(case_id: str = "gdrive-1#1") -> CaseRecord:
    return CaseRecord.model_validate(
        {
            "case_id": case_id,
            "case_group": "g",
            "client_internal": "カネカ",
            "client_masked": "機能性食品メーカー様",
            "sector": "飲料食品",
            "purpose": ["認知拡大"],
            "product_state": "新商品",
            "channel": [],
            "traits": ["検証型"],
            "product": "Q10グミ",
            "scale": "",
            "period": {"start": "", "end": ""},
            "metrics": [{"name": "再生", "value": "130%", "unit": "", "source_url": "https://d/x"}],
            "result_masked": "目標比130%",
            "winpattern": "実演×テンポ",
            "competitors": [],
            "external_use": "ok",
            "sources": [{"external_id": "gdrive-1", "url": "https://d/x", "excerpt": "e"}],
            "confidence": 0.7,
            "reviewed": False,
        }
    )


def test_parse_doc_types_handles_fullwidth_separators_and_dupes() -> None:
    assert _mod.parse_doc_types("提案書,報告書、施策実績，報告書, ") == [
        "提案書",
        "報告書",
        "施策実績",
    ]
    assert _mod.parse_doc_types(" , ") == []
    assert _mod.DEFAULT_DOC_TYPES == "提案書,報告書,施策実績"


def test_parse_since_validates_iso_date() -> None:
    assert _mod.parse_since(None) is None
    assert _mod.parse_since(" ") is None
    assert _mod.parse_since("2025-01-31") == "2025-01-31"
    with pytest.raises(ValueError):
        _mod.parse_since("2025/01/31")


def test_document_meta_from_row_maps_drive_uri_and_external_use() -> None:
    row: dict[str, Any] = {
        "document_id": "uuid-1",
        "external_id": "1AbC",
        "source_uri": "gdrive://1AbC",
        "title": "報告書",
        "cls_doc_type": "報告書",
        "client_name": "",
        "cls_project": "カネカ",
        "case_external_use": "NG",
        "modified_at": "2025-06-30",
    }
    meta = _mod.document_meta_from_row(row)
    assert meta.external_id == "1AbC"
    assert meta.url == "https://drive.google.com/file/d/1AbC/view"  # SearchSkill._doc_url の再利用
    assert meta.client_name == "カネカ"  # client_name が空なら cls_project
    assert meta.external_use == "ng"
    assert meta.doc_type == "報告書"


def test_document_meta_unknown_external_use_and_http_uri() -> None:
    meta = _mod.document_meta_from_row(
        {"external_id": "x", "source_uri": "https://example.com/doc", "case_external_use": "たぶん"}
    )
    assert meta.external_use == "unknown"
    assert meta.url == "https://example.com/doc"


def test_summary_text_has_no_client_name() -> None:
    text = _mod.summary_text(_record())
    assert "カネカ" not in text
    assert "商材: Q10グミ" in text and "結果: 目標比130%" in text and "勝ち筋: 実演×テンポ" in text


def test_render_summary_dry_run_lists_counts_and_examples() -> None:
    stats = _mod.RunStats(docs_scanned=3, docs_with_cases=2, docs_failed=1, records=2, upserted=0)
    out = _mod.render_summary(stats, [_record(), _record("gdrive-2#1")], dry_run=True, examples=1)
    assert "dry-run" in out
    assert "documents scanned: 3" in out
    assert "case records: 2" in out
    assert "upserted: 0" in out
    assert out.count("case_id:") == 1
    assert "機能性食品メーカー様 / 飲料食品 / 認知拡大 / 新商品 / traits=検証型" in out
    assert "カネカ" not in out  # 実社名は表示しない


def test_main_refuses_to_run_without_model_or_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CASE_EXTRACT_MODEL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    assert _mod.main(["--dry-run"]) == 2
    assert _mod.main(["--dsn", "postgresql://x", "--dry-run"]) == 2
    assert _mod.main(["--dsn", "postgresql://x", "--model-id", "m", "--since", "bad"]) == 2
    assert _mod.main(["--dsn", "postgresql://x", "--model-id", "m", "--doc-types", ","]) == 2
    assert "必要" in capsys.readouterr().err
