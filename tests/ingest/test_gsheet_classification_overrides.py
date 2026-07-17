"""Google Sheets exact-ID 分類 override の純関数テスト。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from teamagent.ingest.gsheet_classification_overrides import (
    _RAW_INDUSTRY_OVERRIDES,
    GSheetIndustryOverride,
    _validate_industry_overrides,
    apply_gsheet_industry_override,
)


@pytest.mark.parametrize(
    ("row_idx", "client_name", "expected_industry"),
    [
        (44, "ポート", "人材"),
        (63, "アイホン", "電子機器"),
        (123, "東京ドーム", "エンタテインメント"),
    ],
)
def test_exact_rows_override_only_industry_and_keep_input_immutable(
    row_idx: int,
    client_name: str,
    expected_industry: str,
) -> None:
    external_id = f"1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo:278789217:{row_idx}"
    original = {
        "cls_project": "モデル案件名",
        "cls_industry": "誤分類",
        "industry": "誤分類",
        "cls_doc_type": "提案書",
        "cls_phase": "提案",
        "cls_solution": "動画広告",
        "cls_budget": "不明",
    }

    actual = apply_gsheet_industry_override(
        external_id,
        client_name=client_name,
        classification_metadata=original,
    )

    assert original["cls_industry"] == "誤分類"  # 呼び出し元 dict を mutate しない
    assert actual == {
        **original,
        "cls_industry": expected_industry,
        "industry": expected_industry,
    }


def test_non_target_row_is_unchanged() -> None:
    original = {"cls_industry": "食品", "industry": "食品", "cls_doc_type": "報告書"}
    actual = apply_gsheet_industry_override(
        "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo:278789217:45",
        client_name="ポート",
        classification_metadata=original,
    )
    assert actual == original
    assert actual is not original


def test_registry_excludes_display_equivalent_row_222() -> None:
    """JR 東日本の表記差は alias で吸収し、semantic override には追加しない。"""
    overridden_rows = {
        int(entry.external_id.rsplit(":", 1)[1]) for entry in _RAW_INDUSTRY_OVERRIDES
    }
    assert overridden_rows == {44, 63, 123}
    assert 222 not in overridden_rows

    aliases_path = (
        Path(__file__).resolve().parents[2] / "data" / "connect_web_filters" / "tag_alias.json"
    )
    aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
    assert aliases["industry"]["運輸・交通"] == "交通・運輸"


def test_exact_row_rejects_changed_human_identity() -> None:
    with pytest.raises(ValueError, match="identity mismatch"):
        apply_gsheet_industry_override(
            "1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo:278789217:44",
            client_name="別会社",
            classification_metadata={"cls_industry": "IT"},
        )


@pytest.mark.parametrize(
    "bad_entry",
    [
        replace(_RAW_INDUSTRY_OVERRIDES[0], external_id="bad-id"),
        replace(
            _RAW_INDUSTRY_OVERRIDES[0],
            external_id="1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY:537831563:44",
        ),
        replace(_RAW_INDUSTRY_OVERRIDES[0], cls_industry=""),
        replace(_RAW_INDUSTRY_OVERRIDES[0], cls_industry=" 人材"),
        replace(_RAW_INDUSTRY_OVERRIDES[0], rationale="bad\nreason"),
    ],
)
def test_invalid_configuration_fails_loud(bad_entry: GSheetIndustryOverride) -> None:
    with pytest.raises(ValueError):
        _validate_industry_overrides((bad_entry,))


def test_duplicate_external_id_fails_loud() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _validate_industry_overrides((_RAW_INDUSTRY_OVERRIDES[0], _RAW_INDUSTRY_OVERRIDES[0]))
