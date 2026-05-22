"""SkillRouter のユニットテスト。"""

from __future__ import annotations

from teamagent.skills.router import QueryType, SkillRouter


def test_route_industry_keyword_extracts_filter() -> None:
    """業界キーワードが含まれていれば conditional + filter 抽出。"""
    router = SkillRouter()
    decision = router.route("飲食業界の提案実績を教えて")
    assert decision.query_type == QueryType.CONDITIONAL
    assert decision.extracted_filter == {"industry": "飲食"}
    assert decision.confidence == 0.8


def test_route_inpex_keyword_maps_to_energy() -> None:
    """INPEX → エネルギー industry にマッピング。"""
    router = SkillRouter()
    decision = router.route("INPEX案件の提案内容は？")
    # INPEX キーワードが「エネルギー」業界として extract される
    assert decision.query_type == QueryType.CONDITIONAL
    assert decision.extracted_filter == {"industry": "エネルギー"}


def test_route_meta_pattern() -> None:
    """meta クエリパターン（業界は？）が META に分類される。"""
    router = SkillRouter()
    decision = router.route("業界は？")
    assert decision.query_type == QueryType.META


def test_route_count_question_is_meta() -> None:
    """何件 / いくつ も META。"""
    router = SkillRouter()
    decision = router.route("提案書は全部で何件ある？")
    assert decision.query_type == QueryType.META


def test_route_default_is_content() -> None:
    """特に判定パターンに一致しない自然文は content（デフォルト）。"""
    router = SkillRouter()
    decision = router.route("ショート動画のアルゴリズムについて教えて")
    assert decision.query_type == QueryType.CONTENT
    assert decision.extracted_filter == {}


def test_route_logging_reason_is_filled() -> None:
    """すべての decision に reason が入る（ログ用）。"""
    router = SkillRouter()
    for q in ["飲食事例", "業界は？", "AとB の違い", "通常検索"]:
        decision = router.route(q)
        assert decision.reason  # 空でない


# ---------- LLM フォールバック ----------


def _make_mock_bedrock(text: str) -> object:
    """LLM router 用の Bedrock client モック。converse() が text を返す。"""
    from unittest.mock import MagicMock

    from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage

    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0003,
        ),
        model_id="us.anthropic.claude-haiku-4-5",
        latency_ms=800,
        stop_reason="end_turn",
    )
    return mock


def test_route_llm_fallback_high_confidence_rule_skipped() -> None:
    """rule-based が高 confidence のときは LLM 判定をスキップ。"""
    from unittest.mock import MagicMock

    mock_bedrock = MagicMock()
    router = SkillRouter(bedrock=mock_bedrock)
    # 業界キーワードあり = rule-based confidence 0.8 → LLM 呼ばない
    decision = router.route("飲食事例")
    assert decision.query_type == QueryType.CONDITIONAL
    mock_bedrock.converse.assert_not_called()


def test_route_llm_fallback_low_confidence_uses_llm() -> None:
    """rule-based が低 confidence のとき LLM を呼ぶ。"""
    mock = _make_mock_bedrock(
        '{"query_type": "conditional", "industry": "不動産", "reason": "森ビルは不動産業界"}'
    )
    router = SkillRouter(bedrock=mock)
    # 「森ビル」というキーワードは _INDUSTRY_KEYWORDS に含まれているので
    # 実は rule-based でヒットする。意図的に rule-based がヒットしないクエリを使う
    decision = router.route("ベクトル社の最新案件を教えて")
    assert decision.query_type == QueryType.CONDITIONAL
    assert decision.extracted_filter == {"industry": "不動産"}
    assert decision.confidence == 0.85
    assert "LLM router" in decision.reason
    mock.converse.assert_called_once()


def test_route_llm_invalid_json_falls_back_to_rule() -> None:
    """LLM が壊れた JSON を返した場合、rule-based 判定を維持。"""
    mock = _make_mock_bedrock("not a json")
    router = SkillRouter(bedrock=mock)
    decision = router.route("意味のない文字列")
    # rule-based fallback → CONTENT
    assert decision.query_type == QueryType.CONTENT
    assert decision.reason.startswith("no specific pattern")


def test_route_llm_unknown_industry_extracts_null() -> None:
    """LLM が industry: null を返した場合、extracted_filter は空。"""
    mock = _make_mock_bedrock('{"query_type": "content", "industry": null, "reason": "業界不明"}')
    router = SkillRouter(bedrock=mock)
    decision = router.route("これってどう？")
    assert decision.query_type == QueryType.CONTENT
    assert decision.extracted_filter == {}
