"""knowledge_query: 資料種別フィルタ抽出のテスト（DB 非依存）。"""

from __future__ import annotations

from teamagent.skills.search.knowledge_query import (
    extract_knowledge_filters,
    extract_query_industry,
)


def test_proposal_examples() -> None:
    assert extract_knowledge_filters("食品業界の提案事例を教えて") == {"cls_doc_type": "提案書"}
    assert extract_knowledge_filters("アース製薬の提案書ある？") == {"cls_doc_type": "提案書"}


def test_minutes() -> None:
    assert extract_knowledge_filters("先週の議事録を探して") == {"cls_doc_type": "議事録"}


def test_report_and_price() -> None:
    assert extract_knowledge_filters("効果測定の報告書") == {"cls_doc_type": "報告書"}
    assert extract_knowledge_filters("価格表が見たい") == {"cls_doc_type": "価格表"}


def test_contract() -> None:
    assert extract_knowledge_filters("契約書のテンプレある？") == {"cls_doc_type": "契約"}


def test_no_doc_type_signal_returns_none() -> None:
    assert extract_knowledge_filters("アース製薬の過去資料を見せて") is None
    assert extract_knowledge_filters("SNS運用のコツは？") is None


def test_empty_returns_none() -> None:
    assert extract_knowledge_filters("") is None


def test_extract_query_industry() -> None:
    assert extract_query_industry("飲料系の提案資料出して") == "飲料"
    assert extract_query_industry("化粧品のコスメ提案ある？") == "化粧品"
    assert extract_query_industry("飲食店向けの事例") == "飲食"
    assert extract_query_industry("アース製薬の過去資料") is None  # 業界語なし
    assert extract_query_industry("") is None
