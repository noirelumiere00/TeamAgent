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


def test_solution_filter() -> None:
    assert extract_knowledge_filters("SNS運用の事例ある？") == {"cls_solution": "SNS運用"}
    assert extract_knowledge_filters("インフルエンサー施策の提案書") == {
        "cls_doc_type": "提案書",
        "cls_solution": "インフルエンサー",
    }


def test_budget_amount_filter() -> None:
    # 「予算100万くらいの動画広告の事例」→ budget と solution の両方が載る。
    assert extract_knowledge_filters("予算100万くらいの動画広告の事例") == {
        "cls_solution": "動画広告",
        "cls_budget": "100〜500万",
    }
    assert extract_knowledge_filters("予算は80万くらいの施策")["cls_budget"] == "〜100万"
    assert extract_knowledge_filters("予算800万のキャンペーン")["cls_budget"] == "500万〜"


def test_budget_qualitative_filter() -> None:
    assert extract_knowledge_filters("低予算でできるSEO施策")["cls_budget"] == "〜100万"


def test_target_filter() -> None:
    assert extract_knowledge_filters("若年女性向けの提案事例") == {
        "cls_doc_type": "提案書",
        "cls_target": "若年女性",
    }
    assert extract_knowledge_filters("シニア向けの施策")["cls_target"] == "シニア"
    assert extract_knowledge_filters("BtoBの事例")["cls_target"] == "BtoB"


def test_combined_axes() -> None:
    f = extract_knowledge_filters("Z世代向けに予算300万でインフルエンサーの提案事例")
    assert f is not None
    assert f["cls_doc_type"] == "提案書"
    assert f["cls_solution"] == "インフルエンサー"
    assert f["cls_budget"] == "100〜500万"
    assert f["cls_target"] == "Z世代"


def test_empty_returns_none() -> None:
    assert extract_knowledge_filters("") is None


def test_extract_query_industry() -> None:
    assert extract_query_industry("飲料系の提案資料出して") == "飲料"
    assert extract_query_industry("化粧品のコスメ提案ある？") == "化粧品"
    assert extract_query_industry("飲食店向けの事例") == "飲食"
    assert extract_query_industry("アース製薬の過去資料") is None  # 業界語なし
    assert extract_query_industry("") is None
