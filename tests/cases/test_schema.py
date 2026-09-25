"""CaseRecord v1 の契約: 語彙検証・派生 similar_keys・strict。

変異テスト（赤くなることを確認済み）:
- schema.py の ``_check_one`` から ``raise`` を外す → test_sector_outside_vocab_is_rejected が赤
- ``_derive_similar_keys`` を外す → test_similar_keys_are_derived_not_trusted が赤
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.cases.schema import (
    CHANNEL_VOCAB,
    OTHER,
    PRODUCT_STATE_VOCAB,
    PURPOSE_VOCAB,
    SECTOR_VOCAB,
    TAG_VOCAB,
    TRAITS_VOCAB,
    CaseRecord,
    allowed_values,
    build_similar_keys,
)

_AOKI_DB = Path.home() / ".claude" / "skills" / "proposal-builder" / "assets" / "事例DB.json"


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "gdrive-1#1",
        "case_group": "kaneka|q10グミ|2025-04",
        "client_internal": "カネカ",
        "client_masked": "機能性食品メーカー様",
        "sector": "飲料食品",
        "purpose": ["認知拡大", "指名検索・VSEO"],
        "product_state": "新商品",
        "channel": ["店頭小売", "EC"],
        "traits": ["薬機・景表規制", "検証型"],
        "product": "新商品グミ（機能性）",
        "scale": "100本を集中投下",
        "period": {"start": "2025-04", "end": "2025-06"},
        "metrics": [
            {
                "name": "再生目標比",
                "value": "130%",
                "unit": "",
                "source_url": "https://drive.google.com/file/d/abc/view",
            }
        ],
        "result_masked": "目標比130%前後の再生",
        "winpattern": "実演×テンポ",
        "similar_keys": [],
        "competitors": ["他社グミ"],
        "external_use": "ok",
        "sources": [
            {
                "external_id": "gdrive-1",
                "url": "https://drive.google.com/file/d/abc/view",
                "excerpt": "目標比130%",
            }
        ],
        "confidence": 0.8,
        "reviewed": False,
    }
    base.update(overrides)
    return base


def test_vocab_sizes_match_aoki_db_meta() -> None:
    assert len(PURPOSE_VOCAB) == 7
    assert len(SECTOR_VOCAB) == 11
    assert len(PRODUCT_STATE_VOCAB) == 4
    assert len(CHANNEL_VOCAB) == 6
    assert len(TRAITS_VOCAB) == 9
    for kind, vocab in TAG_VOCAB.items():
        assert OTHER not in vocab, kind
        assert OTHER in allowed_values(kind), kind


@pytest.mark.skipif(not _AOKI_DB.is_file(), reason="青木 事例DB.json はローカルにしか無い")
def test_vocab_is_verbatim_copy_of_aoki_db() -> None:
    payload = json.loads(_AOKI_DB.read_text(encoding="utf-8"))
    for kind, vocab in payload["_meta"]["tag_vocab"].items():
        assert tuple(vocab) == TAG_VOCAB[kind], kind


def test_valid_record_round_trips() -> None:
    record = CaseRecord.model_validate(_record())
    assert record.sector == "飲料食品"
    assert record.metrics[0].value == "130%"
    assert record.external_use == "ok"
    assert record.reviewed is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sector", "食品"),
        ("product_state", "発売中"),
        ("purpose", ["認知獲得"]),
        ("channel", ["TikTok"]),
        ("traits", ["規制あり"]),
    ],
)
def test_sector_outside_vocab_is_rejected(field: str, value: Any) -> None:
    """語彙外は schema が拒む（extract 側が その他 へ倒す前提）。"""
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(**{field: value}))


def test_other_is_accepted_everywhere() -> None:
    record = CaseRecord.model_validate(
        _record(sector=OTHER, product_state=OTHER, purpose=[OTHER], channel=[OTHER], traits=[OTHER])
    )
    assert record.sector == OTHER


def test_duplicate_tags_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(purpose=["認知拡大", "認知拡大"]))


def test_purpose_requires_at_least_one() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(purpose=[]))


def test_similar_keys_are_derived_not_trusted() -> None:
    """入力の similar_keys は無視され、構造 4 項目から組み直される。"""
    record = CaseRecord.model_validate(_record(similar_keys=["sector:捏造"]))
    assert record.similar_keys == build_similar_keys(
        sector="飲料食品",
        purpose=["認知拡大", "指名検索・VSEO"],
        product_state="新商品",
        traits=["薬機・景表規制", "検証型"],
    )
    assert record.similar_keys[0] == "sector:飲料食品"
    assert "traits:検証型" in record.similar_keys


def test_external_use_is_three_valued() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(external_use="maybe"))


def test_metric_value_is_string_not_number() -> None:
    """数値は本文の文字列そのまま。strict なので int/float は拒む。"""
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(
            _record(
                metrics=[{"name": "再生", "value": 130, "unit": "%", "source_url": "https://x/y"}]
            )
        )


def test_metric_requires_source_url() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(
            _record(metrics=[{"name": "再生", "value": "130%", "unit": "", "source_url": ""}])
        )


def test_sources_required_and_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(sources=[]))
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(unexpected="x"))


def test_confidence_bounded() -> None:
    with pytest.raises(ValidationError):
        CaseRecord.model_validate(_record(confidence=1.5))
