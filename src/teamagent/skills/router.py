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
from typing import Any, ClassVar

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


_LLM_ROUTER_INSTRUCTION = (
    "あなたは社内営業向け検索 Bot のクエリ判定器です。\n"
    "以下のクエリを 4 種類のいずれかに分類し、業界フィルタを抽出してください。\n\n"
    "分類:\n"
    "- meta: 集計・カウント系（「業界は？」「何件ある？」「全部教えて」）\n"
    "- conditional: 特定の業界・顧客・予算で絞り込みたい（「飲食業の事例」）\n"
    "- compare: 比較系（「A と B の違い」「どっちが良い」）\n"
    "- content: 通常の意味検索（上記に当てはまらないもの。デフォルト）\n\n"
    "業界カテゴリ（リストにあるものから選ぶ、無ければ null）:\n"
    "飲食 / 化粧品 / エネルギー / 不動産 / 自治体 / 製造業 /\n"
    "教育 / 医療 / IT / 小売 / 金融 / 旅行 / メディア\n\n"
    "クエリ:\n"
    "{query}\n\n"
    "JSON だけを返してください（説明やコードブロック禁止）:\n"
    '{{"query_type": "meta|conditional|compare|content", '
    '"industry": "飲食" or null, "reason": "短い説明"}}\n'
)


class SkillRouter:
    """ハイブリッド Skill Router。

    rule-based 判定を試し、confidence が低い（< 0.6）場合のみ
    Haiku 4.5 で LLM 判定を呼ぶハイブリッド設計。

    - rule-based のみ：bedrock=None で初期化、現状の挙動を維持
    - LLM フォールバック：bedrock を注入すると低 confidence 時のみ Haiku 呼び出し

    Sprint 2 で完全 LLM に切り替えるかは PoC で判断。
    """

    LLM_FALLBACK_THRESHOLD: ClassVar[float] = 0.6

    def __init__(self, bedrock: Any | None = None) -> None:
        """bedrock client を渡すと低 confidence 時に LLM 判定を行う。"""
        self._bedrock = bedrock

    def route(self, query: str, request_id: str | None = None) -> RoutingDecision:
        """クエリを判定して RoutingDecision を返す。"""
        rule_decision = self._route_rule_based(query)
        if rule_decision.confidence >= self.LLM_FALLBACK_THRESHOLD or self._bedrock is None:
            return rule_decision

        # rule-based が低 confidence + LLM 利用可
        llm_decision = self._route_llm(query, request_id=request_id or "rt-fallback")
        if llm_decision is None:
            return rule_decision  # LLM 失敗時は rule-based を採用
        return llm_decision

    def _route_rule_based(self, query: str) -> RoutingDecision:
        """rule-based の判定（外部依存なし）。"""
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

        # 4. デフォルト：content（confidence 低）
        return RoutingDecision(
            query_type=QueryType.CONTENT,
            confidence=0.5,
            extracted_filter={},
            reason="no specific pattern matched (rule-based)",
        )

    def _route_llm(self, query: str, request_id: str) -> RoutingDecision | None:
        """Haiku 4.5 で JSON 判定。失敗時 None。"""
        if self._bedrock is None:
            return None
        prompt = _LLM_ROUTER_INSTRUCTION.format(query=query)
        try:
            resp = self._bedrock.converse(
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                request_id=request_id,
                temperature=0.0,
                max_tokens=200,
            )
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = raw.lstrip("`").lstrip("json").strip()
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
            import json

            data = json.loads(raw)
        except Exception as e:
            logger.warning(
                "llm_router_failed",
                request_id=request_id,
                error=str(e),
            )
            return None

        try:
            qt = QueryType(data.get("query_type", "content"))
        except ValueError:
            qt = QueryType.CONTENT

        industry = data.get("industry")
        extracted: dict[str, str] = {}
        if industry and industry != "null":
            extracted["industry"] = str(industry)
        reason = f"LLM router: {data.get('reason', '')[:120]}"
        return RoutingDecision(
            query_type=qt,
            confidence=0.85,
            extracted_filter=extracted,
            reason=reason,
        )
