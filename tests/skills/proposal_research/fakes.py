"""自動調査専用フェイク。既存のテスト土台から独立し外部に接続しない。"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from teamagent.adapters.gemini_client import (
    GeminiGroundedResponse,
    GeminiResponse,
    GroundingSource,
    GroundingSupport,
)
from teamagent.adapters.source_url_check import UrlCheckResult
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokSearchResult,
    TikTokVideo,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "grounded_markdown.json").read_text())
GOOD_URL = FIXTURE["sources"][0]["resolved_url"]
BAD_URL = FIXTURE["sources"][1]["resolved_url"]
UNREGISTERED_URL = "https://unregistered.example/invented-study"
REAL_TIKTOK_URL = "https://www.tiktok.com/@fake_creator/video/7412345678901234567"
SECTION_NAMES = tuple(FIXTURE["sections"])


def intermediate_payload() -> dict[str, Any]:
    """URL 欄には S1、G 本文には [S1] のみを入れた中間 JSON。"""
    return {
        "research_date": "2000-01-01",
        "brand": "モデルの別商材",
        "product_meta": {
            "sector": "食品",
            "purpose": ["認知"],
            "product_state": "発売中",
            "channel": ["TikTok"],
            "regulation": False,
            "moment": "冬",
            "target_categories": ["甘いものが好きな人"],
            "kaiwai_keywords": [
                "ご褒美チョコ",
                "冬限定",
                "チョコ食べ比べ",
                "コンビニおやつ",
                "ひとりおやつ",
                "仕事終わりのおやつ",
                "自分へのご褒美",
                "濃厚チョコ",
            ],
        },
        "A_market_data": [
            {
                "theme": theme,
                "headline": "ご褒美需要がある",
                "analysis": "生活者はご褒美としておやつを選ぶ",
                "url": "S1",
                "source_name": "ロイヤリティ マーケティング",
                "alt_data": [
                    {
                        "headline": f"補足{index}",
                        "analysis": "気分転換になるおやつへの関心",
                        "url": "S1",
                    }
                    for index in range(3)
                ],
            }
            for theme in ("ご褒美", "冬のおやつ", "手軽さ")
        ],
        "B_social_trend": [
            {
                "theme": theme,
                "headline": "楽しみ方を投稿する",
                "analysis": "食べ比べが会話のきっかけになる",
                "url": "S1",
                "source_name": "生活者調査",
                "alt_data": [],
            }
            for theme in ("食べ比べ", "おやつ時間", "限定品")
        ],
        # これらはモデルの捏造。段 C は必ずスクレイパの実データで上書きする。
        "C_tiktok": [
            {
                "related_tag": "モデル捏造タグ",
                "representative_post_url": "https://unregistered.example/model-video",
                "search_demand_note": "モデル捏造300億回",
                "total_count": "300億件",
            }
        ],
        "D_publicity": [
            {
                "trend_word": word,
                "article_count_500days": "取得不可（出典に件数記載なし）",
                "evidence_url": "S1",
                "recommended_media": ["食品媒体"],
            }
            for word in ("ご褒美", "冬限定", "濃厚", "新食感", "食べ比べ", "おやつ")
        ],
        "E_community": [
            {
                "name": name,
                "estimated_population": "取得不可（推計根拠なし）",
                "calculation": "公開データで推計できない",
                "data_url": "S1",
                "tiktok_tags": [
                    {
                        "tag": "ご褒美チョコ",
                        "representative_post_url": "https://unregistered.example/model-tag",
                    }
                ],
            }
            for name in ("おやつ好き", "限定商品を楽しむ人", "食べ比べ好き")
        ],
        "F_competitor": [
            {
                "name": name,
                "target": "おやつを楽しむ人",
                "core_concept": "気軽に楽しむ",
                "features": "持ち運びしやすい",
                "positioning": "身近なおやつ",
                "url": "S1",
            }
            for name in ("競合アルファ", "競合ベータ", "競合ガンマ")
        ],
        "G_insight": {
            "complaint_pattern": "リラックスしにくい [S1]",
            "complaint_example": "仕事のあとに気分を切り替えたい [S1]",
            "desire_pattern": "小さなご褒美が欲しい [S1]",
            "desire_example": "手軽なおやつがあるとうれしい [S1]",
        },
        "H_event": {
            "overview": "冬のおやつ時間",
            "scale": "公開根拠のない規模は記載しない",
            "sns_reality": "季節のおやつを紹介する",
            "benchmark_case": "ご褒美を楽しむ生活者の声",
            "url": "S1",
        },
    }


class FakeGemini:
    def __init__(
        self,
        payloads: list[dict[str, Any] | str] | None = None,
        *,
        grounding_failures: dict[str, int] | None = None,
        pause: float = 0.0,
        unsupported_source: bool = False,
    ) -> None:
        self.payloads = copy.deepcopy(payloads or [intermediate_payload()])
        self.grounding_failures = grounding_failures or {}
        self.grounding_calls: Counter[str] = Counter()
        self.grounding_prompts: list[str] = []
        self.text_calls: list[dict[str, Any]] = []
        self.pause = pause
        self.unsupported_source = unsupported_source
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def generate_with_google_search(
        self, prompt: str, request_id: str, **kwargs: Any
    ) -> GeminiGroundedResponse:
        match = re.search(r"SECTION:\s*([^\n]+)", prompt)
        assert match, "区分を識別できる SECTION が必要"
        section = match[1].strip()
        assert section in SECTION_NAMES
        with self._lock:
            self.grounding_calls[section] += 1
            call_number = self.grounding_calls[section]
            self.grounding_prompts.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.pause:
                time.sleep(self.pause)
            sources = [
                GroundingSource(title=source["title"], uri=source["uri"], domain=source["domain"])
                for source in FIXTURE["sources"]
            ]
            if self.unsupported_source:
                sources.append(
                    GroundingSource(
                        title="本文に支持のない元データ",
                        uri="https://unsupported.example/unused",
                        domain="unsupported.example",
                    )
                )
            return GeminiGroundedResponse(
                text=FIXTURE["sections"][section],
                sources=tuple(sources),
                supports=tuple(
                    GroundingSupport(
                        text=support["text"], source_indices=tuple(support["source_indices"])
                    )
                    for support in FIXTURE["supports"]
                ),
                search_queries=tuple(FIXTURE["search_queries"]),
                grounded=call_number > self.grounding_failures.get(section, 0),
                input_tokens=FIXTURE["input_tokens"],
                output_tokens=FIXTURE["output_tokens"],
                cost_usd=FIXTURE["cost_usd"],
                model_id=FIXTURE["model_id"],
                latency_ms=FIXTURE["latency_ms"],
                thoughts_tokens=FIXTURE["thoughts_tokens"],
            )
        finally:
            with self._lock:
                self.active -= 1

    def generate_text(self, prompt: str, request_id: str, **kwargs: Any) -> GeminiResponse:
        self.text_calls.append({"prompt": prompt, "request_id": request_id, **kwargs})
        payload = self.payloads[min(len(self.text_calls) - 1, len(self.payloads) - 1)]
        return GeminiResponse(
            text=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
            input_tokens=200,
            output_tokens=300,
            cost_usd=0.01,
            model_id=FIXTURE["model_id"],
            latency_ms=2,
        )


class FakeUrlChecker:
    def __init__(self, *, all_unavailable: bool = False) -> None:
        self.all_unavailable = all_unavailable
        self.resolve_calls: list[str] = []
        self.verify_calls: list[str] = []
        self._lock = threading.Lock()

    def resolve_grounding_redirect(self, uri: str) -> str:
        with self._lock:
            self.resolve_calls.append(uri)
        for source in FIXTURE["sources"]:
            if uri == source["uri"]:
                return str(source["resolved_url"])
        return uri

    def verify_public_url(self, url: str) -> UrlCheckResult:
        with self._lock:
            self.verify_calls.append(url)
        unavailable = self.all_unavailable or url == BAD_URL
        return UrlCheckResult(
            url=url,
            final_url=url,
            ok=not unavailable,
            status_code=404 if unavailable else 200,
            reason="not_found" if unavailable else "",
        )


class FakeTikTokSearcher:
    def __init__(self, *, empty_hashtag: bool = False, all_failed: bool = False) -> None:
        self.empty_hashtag = empty_hashtag
        self.all_failed = all_failed
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(
        self, query: str, *, search_type: str = "keyword", **kwargs: Any
    ) -> TikTokSearchResult:
        with self._lock:
            self.calls.append({"query": query, "search_type": search_type, **kwargs})
        if self.all_failed or (self.empty_hashtag and search_type == "hashtag"):
            return TikTokSearchResult(query=query, search_type=search_type, videos=())
        video = TikTokVideo(
            id="7412345678901234567",
            url=REAL_TIKTOK_URL,
            desc="ご褒美チョコを食べてみた",
            create_time=1780000000,
            duration=19,
            cover_url="https://cover.example/fake.jpg",
            author=TikTokAuthor(unique_id="fake_creator", nickname="投稿者", follower_count=2300),
            play_count=12345,
            digg_count=400,
            comment_count=12,
            share_count=3,
            collect_count=9,
            hashtags=("ご褒美チョコ",),
            music_title="",
        )
        return TikTokSearchResult(query=query, search_type=search_type, videos=(video,))
