"""extract_cases: 偽 Bedrock（MagicMock）で JSON 修復・語彙外→その他・出典固定・数値検証を固定する。

変異テスト（赤くなることを確認済み）:
- extract.py の ``normalize_tag`` が ``OTHER`` を返さず入力をそのまま返す
  → test_out_of_vocab_tags_become_other が赤（schema が拒み CaseExtractionError）
- ``_coerce_metrics`` の ``metric_value_in_text`` 判定を外す → test_metric_not_in_text_is_dropped が赤
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.cases.extract import (
    CaseDocumentMeta,
    CaseExtractionError,
    build_case_group,
    build_system_prompt,
    extract_cases,
    metric_value_in_text,
    normalize_client,
    normalize_tag,
    normalize_tags,
)
from teamagent.cases.schema import OTHER, TAG_VOCAB

_DOC_URL = "https://drive.google.com/file/d/abc123/view"
_META = CaseDocumentMeta(
    external_id="gdrive-abc123",
    url=_DOC_URL,
    title="カネカ Q10グミ 報告書",
    doc_type="報告書",
    client_name="カネカ",
    external_use="ok",
    modified_at="2025-06-30",
)
_TEXT = (
    "カネカ Q10グミ ショート動画施策 報告書\n"
    "投稿本数 100本を集中投下。再生は目標比130%前後で推移し、"
    "初週売上は前年新商品の2倍。総再生数 1,250,000回。"
    "薬機表現を回避しコンセプト訴求で伸長。"
)


def _resp(text: str) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=10,
        stop_reason="end_turn",
    )


def _bedrock(*texts: str) -> MagicMock:
    client = MagicMock()
    client.converse.side_effect = [_resp(t) for t in texts]
    return client


def _case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client_internal": "カネカ",
        "client_masked": "機能性食品メーカー様",
        "sector": "飲料食品",
        "purpose": ["認知拡大", "指名検索・VSEO"],
        "product_state": "新商品",
        "channel": ["店頭小売"],
        "traits": ["薬機・景表規制", "検証型"],
        "product": "Q10グミ",
        "scale": "100本を集中投下",
        "period": {"start": "2025-04", "end": "2025-06"},
        "metrics": [
            {"name": "再生目標比", "value": "130%", "unit": "", "source_url": "https://evil/x"},
            {"name": "総再生数", "value": "1,250,000", "unit": "回", "source_url": ""},
        ],
        "result_masked": "目標比130%前後の再生、初週売上が前年新商品の2倍",
        "winpattern": "実演×テンポ",
        "competitors": ["他社グミ"],
        "excerpt": "再生は目標比130%前後で推移し",
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


def _run(bedrock: MagicMock, **kwargs: Any) -> Any:
    return extract_cases(
        _TEXT,
        document_meta=_META,
        bedrock=bedrock,
        model_id="jp.anthropic.claude-sonnet-4-6",
        request_id="req-test",
        **kwargs,
    )


# ── プロンプト ────────────────────────────────────────────────


def test_system_prompt_embeds_every_vocab_and_rules() -> None:
    prompt = build_system_prompt()
    assert "{{" not in prompt
    for vocab in TAG_VOCAB.values():
        for word in vocab:
            assert word in prompt
    assert OTHER in prompt
    assert "そのまま" in prompt  # 数値は本文の文字列そのまま
    assert "DOCUMENT_URL" in prompt  # 出典は文書に固定
    assert '{"cases": []}' in prompt  # 事例が無ければ空配列
    assert "JSON のみ" in prompt


# ── 正常系 ────────────────────────────────────────────────────


def test_fenced_json_is_parsed_and_sources_fixed_to_document() -> None:
    bedrock = _bedrock(
        "説明\n```json\n" + json.dumps({"cases": [_case()]}, ensure_ascii=False) + "\n```"
    )
    records = _run(bedrock)
    assert len(records) == 1
    record = records[0]
    assert record.case_id == "gdrive-abc123#1"
    assert record.sources[0].external_id == "gdrive-abc123"
    assert record.sources[0].url == _DOC_URL
    assert record.sources[0].excerpt == "再生は目標比130%前後で推移し"
    # 出典はモデルの URL ではなく文書に固定
    assert {m.source_url for m in record.metrics} == {_DOC_URL}
    assert [m.value for m in record.metrics] == ["130%", "1,250,000"]
    assert record.external_use == "ok"  # 文書 metadata の写し（モデルは決めない）
    assert record.reviewed is False
    assert record.confidence == 0.85
    assert bedrock.converse.call_count == 1
    kwargs = bedrock.converse.call_args.kwargs
    assert kwargs["cache_system"] is True
    assert "DOCUMENT_URL: " + _DOC_URL in kwargs["messages"][0]["content"][0]["text"]


def test_bare_array_is_accepted() -> None:
    records = _run(_bedrock(json.dumps([_case(), _case(product="別商材")], ensure_ascii=False)))
    assert [r.case_id for r in records] == ["gdrive-abc123#1", "gdrive-abc123#2"]
    assert records[0].case_group != records[1].case_group


def test_no_cases_returns_empty_without_repair() -> None:
    bedrock = _bedrock('{"cases": []}')
    assert _run(bedrock) == []
    assert bedrock.converse.call_count == 1


def test_empty_document_does_not_call_bedrock() -> None:
    bedrock = _bedrock()
    assert (
        extract_cases("   ", document_meta=_META, bedrock=bedrock, model_id="m", request_id="r")
        == []
    )
    assert bedrock.converse.call_count == 0


# ── 語彙外 → その他 ───────────────────────────────────────────


def test_out_of_vocab_tags_become_other() -> None:
    bedrock = _bedrock(
        json.dumps(
            {
                "cases": [
                    _case(
                        sector="食品",
                        purpose=["認知獲得", "認知拡大", "認知拡大"],
                        product_state="発売中",
                        channel=["TikTok", "ＥＣ"],
                        traits=[],
                    )
                ]
            },
            ensure_ascii=False,
        )
    )
    record = _run(bedrock)[0]
    assert record.sector == OTHER
    assert record.purpose == [OTHER, "認知拡大"]  # 重複除去・順序維持
    assert record.product_state == OTHER
    assert record.channel == [OTHER, "EC"]  # 全角は NFKC で語彙へ寄せる
    assert record.traits == []
    assert bedrock.converse.call_count == 1  # 語彙外で repair を浪費しない


def test_normalize_helpers() -> None:
    assert normalize_tag("sector", " 飲料食品 ") == "飲料食品"
    assert normalize_tag("sector", "") == OTHER
    assert normalize_tags("purpose", None, require_one=True) == [OTHER]
    assert normalize_tags("purpose", "認知拡大") == ["認知拡大"]
    assert (
        normalize_client("株式会社 カネカ 様") == "かねか"
        or normalize_client("株式会社 カネカ 様") == "カネカ".lower()
    )
    assert build_case_group("カネカ", "Q10グミ", "2025年4月") == "カネカ|q10グミ|2025年4月"


# ── 数値の安全装置 ────────────────────────────────────────────


def test_metric_not_in_text_is_dropped() -> None:
    """本文に無い数値（LLM が計算した値）は出典つきで持たせない。"""
    bedrock = _bedrock(
        json.dumps(
            {
                "cases": [
                    _case(
                        metrics=[
                            {"name": "再生目標比", "value": "130%", "unit": "", "source_url": ""},
                            {"name": "平均再生", "value": "12,500", "unit": "回", "source_url": ""},
                        ]
                    )
                ]
            },
            ensure_ascii=False,
        )
    )
    record = _run(bedrock)[0]
    assert [m.value for m in record.metrics] == ["130%"]


def test_metric_value_in_text_ignores_thousand_separators() -> None:
    assert metric_value_in_text("1250000", "総再生数 1,250,000回")
    assert metric_value_in_text("１，２５０，０００", "総再生数 1,250,000回")
    assert not metric_value_in_text("2,000,000", "総再生数 1,250,000回")
    assert not metric_value_in_text("", "x")


# ── マスク ────────────────────────────────────────────────────


def test_client_masked_leaking_client_name_is_replaced() -> None:
    record = _run(
        _bedrock(json.dumps({"cases": [_case(client_masked="カネカ様")]}, ensure_ascii=False))
    )[0]
    assert record.client_masked == "飲料食品企業様"
    assert "カネカ" not in record.client_masked


def test_excerpt_not_in_document_falls_back_to_head() -> None:
    record = _run(
        _bedrock(json.dumps({"cases": [_case(excerpt="本文に無い一節")]}, ensure_ascii=False))
    )[0]
    assert record.sources[0].excerpt == _TEXT.replace("\n", " ")[:160]


# ── repair ────────────────────────────────────────────────────


def test_invalid_json_is_repaired_once_with_error_text() -> None:
    bedrock = _bedrock("{cases: [oops", json.dumps({"cases": [_case()]}, ensure_ascii=False))
    records = _run(bedrock)
    assert len(records) == 1
    assert bedrock.converse.call_count == 2
    second_messages = bedrock.converse.call_args_list[1].kwargs["messages"]
    assert [m["role"] for m in second_messages] == ["user", "assistant", "user"]
    repair_text = second_messages[2]["content"][0]["text"]
    assert "契約に合いませんでした" in repair_text
    assert "JSON のみ" in repair_text


def test_schema_violation_is_repaired_with_validation_error_text() -> None:
    # sources を空にする経路は無いので、cases が配列でない契約違反で repair を起こす
    bedrock = _bedrock('{"cases": {"a": 1}}', json.dumps({"cases": [_case()]}, ensure_ascii=False))
    assert len(_run(bedrock)) == 1
    repair_text = bedrock.converse.call_args_list[1].kwargs["messages"][2]["content"][0]["text"]
    assert "配列" in repair_text


def test_repair_exhausted_raises() -> None:
    bedrock = _bedrock("not json", "still not json")
    with pytest.raises(CaseExtractionError):
        _run(bedrock)
    assert bedrock.converse.call_count == 2


def test_max_repair_zero_fails_fast() -> None:
    bedrock = _bedrock("not json")
    with pytest.raises(CaseExtractionError):
        _run(bedrock, max_repair=0)
    assert bedrock.converse.call_count == 1


def test_too_many_cases_is_rejected() -> None:
    bedrock = _bedrock(json.dumps({"cases": [_case()] * 21}, ensure_ascii=False), '{"cases": []}')
    assert _run(bedrock) == []
    assert bedrock.converse.call_count == 2
