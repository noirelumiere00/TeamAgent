"""Skill Router（ルールベース、Sprint 2 で Haiku ベースに差替予定）。

mention テキストから「どの検索戦略を使うか」を判定する。
検索 Skill 設計書 (docs/v3.1/teamagent_search_skill_design_v1.md) の Section 3 を実装。

QueryType:
- meta:        集計・カウント系（「業界は？」「何件ある？」）
- conditional: 絞り込み付き検索（「○○業界の提案」）
- compare:     比較系（「A と B の違い」）
- content:     通常の意味検索（デフォルト）

Sprint 2 で Haiku 4.5 ベースの判定に置き換える。
今は正規表現/キーワード判定の rule-based 実装。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class QueryType(StrEnum):
    META = "meta"
    CONDITIONAL = "conditional"
    COMPARE = "compare"
    CONTENT = "content"


@dataclass(frozen=True)
class RoutingDecision:
    """Router の判定結果。"""

    query_type: QueryType
    confidence: float  # 0.0 〜 1.0
    extracted_filter: dict[str, str]  # 例 {"industry": "飲食"}
    reason: str  # ログ用


# 業界キーワード（メタデータ抽出と整合）
_INDUSTRY_KEYWORDS = {
    "飲食": ["飲食", "レストラン", "居酒屋", "カフェ"],
    "化粧品": ["化粧品", "コスメ", "美容", "スキンケア"],
    "エネルギー": ["エネルギー", "電力", "ガス", "INPEX", "石油"],
    "不動産": ["不動産", "森ビル", "マンション", "賃貸", "展覧会"],
    "自治体": ["自治体", "市役所", "県", "市"],
    "製造業": ["製造業", "メーカー", "工場"],
    "教育": ["教育", "学校", "大学", "塾"],
    "医療": ["医療", "病院", "クリニック", "薬"],
    "IT": ["IT", "システム", "ソフトウェア", "SaaS"],
    "小売": ["小売", "EC", "通販", "店舗"],
    "金融": ["金融", "銀行", "保険", "証券"],
    "旅行": ["旅行", "観光", "ホテル"],
    "メディア": ["メディア", "出版", "テレビ", "新聞"],
}

# meta クエリ判定パターン
_META_PATTERNS = [
    re.compile(r"何件|いくつ|総数|合計|全部で"),
    re.compile(r"(業界|担当|顧客|案件)は[?？]"),
    re.compile(r"一覧|リスト|まとめて"),
]

# compare クエリ判定パターン
_COMPARE_PATTERNS = [
    re.compile(r"(比較|違い|どちら|どっち)"),
    re.compile(r"\S+\s*(と|vs|VS)\s*\S+\s*の?\s*(違い|比較|どっち|どちら)"),
]


class SkillRouter:
    """ルールベース Skill Router（Sprint 1 末版）。

    Sprint 2 で Haiku 4.5 ベースの自然言語判定に置き換え予定。
    """

    def route(self, query: str) -> RoutingDecision:
        """クエリを判定して RoutingDecision を返す。"""
        # 1. compare 判定（最優先、誤検出が一番痛い）
        for pat in _COMPARE_PATTERNS:
            if pat.search(query):
                return RoutingDecision(
                    query_type=QueryType.COMPARE,
                    confidence=0.7,
                    extracted_filter={},
                    reason=f"compare pattern matched: {pat.pattern}",
                )

        # 2. meta 判定
        for pat in _META_PATTERNS:
            if pat.search(query):
                return RoutingDecision(
                    query_type=QueryType.META,
                    confidence=0.6,
                    extracted_filter={},
                    reason=f"meta pattern matched: {pat.pattern}",
                )

        # 3. conditional 判定（業界キーワードあり）
        for industry, keywords in _INDUSTRY_KEYWORDS.items():
            for kw in keywords:
                if kw in query:
                    return RoutingDecision(
                        query_type=QueryType.CONDITIONAL,
                        confidence=0.8,
                        extracted_filter={"industry": industry},
                        reason=f"industry keyword matched: {kw} → {industry}",
                    )

        # 4. デフォルト：content
        return RoutingDecision(
            query_type=QueryType.CONTENT,
            confidence=0.5,
            extracted_filter={},
            reason="no specific pattern matched",
        )
