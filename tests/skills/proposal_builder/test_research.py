"""Gemini v3 research のスキーマ意味論に基づく数値出典検証。"""

from __future__ import annotations

import copy
from typing import Any

from teamagent.skills.proposal_builder.research import (
    parse_gemini_research,
    sanitize_unverified_numbers,
)


def _full_gemini_v3_payload() -> dict[str, Any]:
    return {
        "research_date": "2026-08-04",
        "brand": "ACME",
        "product_meta": {
            "sector": "化粧品",
            "purpose": ["投稿5本で認知を獲得"],
            "product_state": "発売中",
            "channel": ["TikTok"],
            "regulation": False,
            "moment": "2週間の集中展開",
            "target_categories": ["30代女性"],
            "kaiwai_keywords": ["30代美容", "時短ケア"],
        },
        "A_market_data": [
            {
                "theme": "国内美容市場",
                "headline": "市場は拡大基調",
                "analysis": "市場規模は1,200億円、前年比12%増加",
                "url": "https://example.com/market",
                "source_name": "市場調査",
                "alt_data": [
                    {
                        "headline": "別調査でも伸長",
                        "analysis": "利用者は前年比8%増加",
                        "url": "https://example.com/market-alt",
                    }
                ],
            }
        ],
        "B_social_trend": [
            {
                "theme": "ショート動画",
                "headline": "美容投稿が増加",
                "analysis": "関連投稿は3万件に到達",
                "url": "https://example.com/social",
                "source_name": "SNS調査",
                "alt_data": [],
            }
        ],
        "C_tiktok": [
            {
                "related_tag": "30代美容",
                "representative_post_url": "https://www.tiktok.com/@creator/video/123",
                "search_demand_note": "検索上位10本を確認",
                "total_count": "取得不可（UI非表示）",
            }
        ],
        "D_publicity": [
            {
                "trend_word": "時短ケア",
                "article_count_500days": "直近500日で120件",
                "evidence_url": "https://example.com/publicity",
                "recommended_media": ["美容媒体"],
            }
        ],
        "E_community": [
            {
                "name": "時短美容界隈",
                "estimated_population": "推計50万人",
                "calculation": "対象人口500万人の10%として算出",
                "data_url": "https://example.com/community",
                "tiktok_tags": [
                    {
                        "tag": "30代美容",
                        "representative_post_url": ("https://www.tiktok.com/@creator/video/456"),
                    }
                ],
            }
        ],
        "F_competitor": [
            {
                "name": "競合ブランド",
                "target": "働く女性",
                "core_concept": "短時間で完了するケア",
                "features": "過去施策で投稿5本を展開",
                "positioning": "カテゴリ売上1位",
                "url": "https://example.com/competitor",
            }
        ],
        "G_insight": {
            "complaint_pattern": "朝の準備時間が足りない",
            "complaint_example": "仕事前はゆっくり準備できない",
            "desire_pattern": "無理なく継続したい",
            "desire_example": "手軽なら続けられそう",
        },
        "H_event": {
            "overview": "3日間の体験イベント",
            "scale": "全国10会場で開催",
            "sns_reality": "来場者の45%が投稿",
            "benchmark_case": "過去施策で動画20本を公開",
            "url": "https://example.com/event",
        },
    }


def test_full_v3_descriptions_and_plans_are_ready_eligible() -> None:
    research = parse_gemini_research(_full_gemini_v3_payload())

    result = sanitize_unverified_numbers(research)

    assert result.issues == []
    assert result.is_draft is False
    assert result.sanitized["product_meta"] == research.product_meta.model_dump(mode="python")
    assert result.sanitized["G_insight"] == research.g_insight.model_dump(mode="python")


def test_unverified_quantity_in_fact_field_is_masked() -> None:
    payload = copy.deepcopy(_full_gemini_v3_payload())
    market_entry = payload["A_market_data"][0]
    del market_entry["url"]

    result = sanitize_unverified_numbers(payload)

    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("$.A_market_data[0].analysis", "unverified_numeric_claim")
    ]
    assert market_entry["analysis"] == "市場規模は1,200億円、前年比12%増加"
    assert (
        result.sanitized["A_market_data"][0]["alt_data"][0]["url"]
        == "https://example.com/market-alt"
    )
    assert result.sanitized["A_market_data"][0]["analysis"] == "要確認（出典URL未取得）"


def test_numeric_insight_without_evidence_remains_a_fact_claim() -> None:
    payload = _full_gemini_v3_payload()
    payload["G_insight"]["complaint_pattern"] = "調査回答者の70%が不便を感じる"
    research = parse_gemini_research(payload)

    result = sanitize_unverified_numbers(research)

    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("$.G_insight.complaint_pattern", "unverified_numeric_claim")
    ]
    assert result.sanitized["G_insight"]["complaint_pattern"] == "要確認（出典URL未取得）"


def test_unknown_field_quantity_fails_closed_even_with_exempt_field_name() -> None:
    payload = _full_gemini_v3_payload()
    payload["product_meta"]["future_plan"] = "投稿7本を制作"
    payload["G_insight"]["purpose"] = "投稿5本を2週間で実施"

    result = sanitize_unverified_numbers(payload)

    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("$.product_meta.future_plan", "unverified_numeric_claim"),
        ("$.G_insight.purpose", "unverified_numeric_claim"),
    ]
    assert result.sanitized["product_meta"]["future_plan"] == "要確認（出典URL未取得）"
    assert result.sanitized["G_insight"]["purpose"] == "要確認（出典URL未取得）"
