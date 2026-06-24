"""QueryPlanner の単体テスト（ネットワーク無し・fake bedrock）。

検証観点:
- 正常系: converse が返す JSON が QueryPlan に正しくマップされる。
- 壊れた JSON / converse 例外 / 空クエリ → fallback（元クエリ 1 本・HyDE 空）。
- doc_type 正規化・client_names 安全化・is_aggregation。
"""

from __future__ import annotations

import json
from typing import Any

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.skills.search.query_planner import QueryPlan, QueryPlanner


def _resp(text: str) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0001,
        ),
        model_id="jp.anthropic.claude-haiku-4-5",
        latency_ms=1,
        stop_reason="end_turn",
    )


class _FakeBedrock:
    """converse() が固定 text を返す（or 例外を投げる）fake。"""

    def __init__(self, *, text: str | None = None, raises: bool = False) -> None:
        self._text = text
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> ConverseResponse:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("boom")
        assert self._text is not None
        return _resp(self._text)


def test_plan_maps_full_json() -> None:
    payload = {
        "paraphrases": ["食品メーカーの提案事例", "食品業界向けの提案実績"],
        "hyde_answer": "食品メーカー向けに実施したショート動画施策の提案書です。",
        "industry": "食品",
        "doc_type": "提案書",
        "client_names": ["アース製薬"],
        "is_aggregation": False,
    }
    bedrock = _FakeBedrock(text=json.dumps(payload, ensure_ascii=False))
    plan = QueryPlanner(bedrock).plan("食品の提案事例ある？", request_id="req-1")

    assert isinstance(plan, QueryPlan)
    assert plan.paraphrases == ["食品メーカーの提案事例", "食品業界向けの提案実績"]
    assert plan.hyde_answer.startswith("食品メーカー向け")
    assert plan.industry == "食品"
    assert plan.doc_type == "提案書"
    assert plan.client_names == ["アース製薬"]
    assert plan.is_aggregation is False
    # system / cache_system / user message が渡っていること。
    assert bedrock.calls[0]["system"]
    assert bedrock.calls[0]["cache_system"] is True


def test_plan_normalizes_doc_type_and_aggregation() -> None:
    payload = {
        "paraphrases": ["案件の一覧"],
        "hyde_answer": "",
        "industry": None,
        "doc_type": "見積書",  # 語彙外 → 部分一致で正規化されない（"見積" は phase 側）
        "client_names": [],
        "is_aggregation": True,
    }
    bedrock = _FakeBedrock(text=json.dumps(payload, ensure_ascii=False))
    plan = QueryPlanner(bedrock).plan("案件を全部出して", request_id="req-2")

    assert plan.is_aggregation is True
    assert plan.industry is None
    # "見積書" は doc_type 語彙（提案書/議事録/...）に部分一致しないので None。
    assert plan.doc_type is None


def test_plan_normalizes_doc_type_partial_match() -> None:
    payload = {
        "paraphrases": ["提案の資料"],
        "hyde_answer": "x",
        "doc_type": "営業提案書ドラフト",  # "提案書" を含む → 正規化される
    }
    bedrock = _FakeBedrock(text=json.dumps(payload, ensure_ascii=False))
    plan = QueryPlanner(bedrock).plan("提案資料", request_id="req-3")
    assert plan.doc_type == "提案書"


def test_plan_filters_non_str_client_names() -> None:
    payload = {
        "paraphrases": ["x"],
        "hyde_answer": "y",
        "client_names": ["アース製薬", 123, None, "花王"],
    }
    bedrock = _FakeBedrock(text=json.dumps(payload, ensure_ascii=False))
    plan = QueryPlanner(bedrock).plan("資料", request_id="req-4")
    assert plan.client_names == ["アース製薬", "花王"]


def test_fallback_on_broken_json() -> None:
    bedrock = _FakeBedrock(text="これはJSONではありません {壊れた")
    plan = QueryPlanner(bedrock).plan("元のクエリ", request_id="req-5")
    assert plan == QueryPlan(
        paraphrases=["元のクエリ"],
        hyde_answer="",
        industry=None,
        doc_type=None,
        client_names=[],
        is_aggregation=False,
    )


def test_fallback_on_converse_exception() -> None:
    bedrock = _FakeBedrock(raises=True)
    plan = QueryPlanner(bedrock).plan("落ちても元クエリ", request_id="req-6")
    assert plan.paraphrases == ["落ちても元クエリ"]
    assert plan.hyde_answer == ""
    assert plan.industry is None


def test_fallback_on_empty_query_skips_bedrock() -> None:
    bedrock = _FakeBedrock(text="{}")
    plan = QueryPlanner(bedrock).plan("   ", request_id="req-7")
    assert plan.paraphrases == ["   "]
    assert bedrock.calls == []  # 空クエリは Bedrock を呼ばない


def test_empty_paraphrases_falls_back_to_query() -> None:
    payload = {"paraphrases": [], "hyde_answer": "なにか"}
    bedrock = _FakeBedrock(text=json.dumps(payload, ensure_ascii=False))
    plan = QueryPlanner(bedrock).plan("言い換え無し", request_id="req-8")
    # 言い換えが空でも最低 1 本（元クエリ）を担保する。
    assert plan.paraphrases == ["言い換え無し"]
    assert plan.hyde_answer == "なにか"
