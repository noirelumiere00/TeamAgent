"""Gold set YAML 構造の静的検証。

DB 接続なしで実行可能な smoke test。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GOLD_SET_PATH = PROJECT_ROOT / "data" / "eval" / "sales_gold_set.yaml"


@pytest.fixture
def gold_set() -> dict:
    """gold set YAML を読み込み。"""
    with GOLD_SET_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_gold_set_exists() -> None:
    """gold set ファイルが存在すること。"""
    assert GOLD_SET_PATH.exists()


def test_gold_set_has_version(gold_set: dict) -> None:
    """version フィールドが必須。"""
    assert "version" in gold_set
    assert "cases" in gold_set


def test_each_case_has_required_fields(gold_set: dict) -> None:
    """各ケースに id / query が必須。"""
    cases = gold_set["cases"]
    assert len(cases) >= 20, "最低 20 ケースは欲しい (統計的に意味のあるサンプル数)"

    seen_ids: set[int] = set()
    for case in cases:
        assert "id" in case, f"case missing id: {case}"
        assert "query" in case, f"case missing query: {case}"
        cid = case["id"]
        assert cid not in seen_ids, f"duplicate case id: {cid}"
        seen_ids.add(cid)
        assert len(case["query"]) >= 3, f"query too short in case {cid}"


def test_expect_fields_have_valid_types(gold_set: dict) -> None:
    """expect_keywords は list、expect_metadata は dict であること。"""
    for case in gold_set["cases"]:
        kws = case.get("expect_keywords")
        if kws is not None:
            assert isinstance(kws, list), f"case {case['id']}: expect_keywords must be list"
        meta = case.get("expect_metadata")
        if meta is not None:
            assert isinstance(meta, dict), f"case {case['id']}: expect_metadata must be dict"


def test_zero_hit_cases_have_no_keywords(gold_set: dict) -> None:
    """expect_zero_hits=true のケースは expect_keywords を空にすべき (ネガティブテスト)。"""
    for case in gold_set["cases"]:
        if case.get("expect_zero_hits"):
            kws = case.get("expect_keywords") or []
            assert kws == [], f"case {case['id']}: zero-hit case should not have keywords"


def test_match_hit_logic() -> None:
    """run_eval._match_hit の主要分岐を検証。"""
    # run_eval を import するために PYTHONPATH 調整
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from run_eval import _match_hit  # type: ignore[import-not-found]

    # 全 keyword 含む → match
    assert _match_hit(
        "日本ガイシ ADK中部 ケイパ 提案",
        {"source_type": "slack"},
        {"expect_keywords": ["日本ガイシ", "ADK中部"]},
    )
    # 1 つ欠ける → no match
    assert not _match_hit(
        "日本ガイシ ケイパ 提案",
        {},
        {"expect_keywords": ["日本ガイシ", "ADK中部"]},
    )
    # source_type 不一致 → no match
    assert not _match_hit(
        "日本ガイシ",
        {"source_type": "gdrive"},
        {"expect_keywords": ["日本ガイシ"], "expect_source_type": "slack"},
    )
    # client_name 部分一致 → match
    assert _match_hit(
        "content",
        {"client_name": "日本ガイシ"},
        {"expect_client_name": "日本ガイシ"},
    )
    # metadata 一致 → match
    assert _match_hit(
        "content",
        {"deal_phase": "ケイパ"},
        {"expect_metadata": {"deal_phase": "ケイパ"}},
    )
