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
