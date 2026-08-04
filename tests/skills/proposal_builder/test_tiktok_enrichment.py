"""proposal_builder の TikTok 実測値・証拠画像配線。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.media_job import MediaJobError
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokScrapeError,
    TikTokSearchResult,
    TikTokVideo,
)
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder import skill as builder_module
from teamagent.skills.proposal_builder.schema import ProposalBuilderInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderSkill
from teamagent.skills.proposal_campaign.adapters import Searcher
from teamagent.skills.proposal_campaign.skill import ProposalCampaignSkill
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput


def _research(
    *,
    brand: str = "ACME",
    sector: str = "食品",
    kaiwai_keywords: list[str] | None = None,
    target_categories: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "research_date": "2026-08-04",
        "brand": brand,
        "product_meta": {
            "sector": sector,
            "purpose": ["認知獲得"],
            "product_state": "発売中",
            "channel": ["TikTok"],
            "regulation": False,
            "moment": "秋",
            "target_categories": target_categories or ["学生", "作業用BGM"],
            "kaiwai_keywords": kaiwai_keywords or ["集中", "作業用BGM", "集中"],
        },
        "A_market_data": [
            {
                "theme": "市場",
                "headline": "需要がある",
                "analysis": "利用場面が広い",
                "url": "https://example.com/market",
                "source_name": "市場資料",
                "alt_data": [],
            }
        ],
        "B_social_trend": [
            {
                "theme": "潮流",
                "headline": "短尺動画が好相性",
                "analysis": "生活者が投稿を参考にする",
                "url": "https://example.com/social",
                "source_name": "潮流資料",
                "alt_data": [],
            }
        ],
        "C_tiktok": [
            {
                "related_tag": "集中",
                "representative_post_url": "https://www.tiktok.com/@seed/video/seed",
                "search_demand_note": "検索需要は実測前",
                "total_count": "取得不可（UI非表示）",
            }
        ],
        "D_publicity": [
            {
                "trend_word": "集中",
                "article_count_500days": "要確認",
                "evidence_url": "https://example.com/publicity",
                "recommended_media": ["生活情報"],
            }
        ],
        "E_community": [
            {
                "name": "集中界隈",
                "estimated_population": "要確認",
                "calculation": "要確認",
                "data_url": "https://example.com/community",
                "tiktok_tags": [
                    {
                        "tag": "集中",
                        "representative_post_url": "https://www.tiktok.com/@seed/video/community",
                    }
                ],
            }
        ],
        "F_competitor": [
            {
                "name": "競合",
                "target": "一般生活者",
                "core_concept": "日常利用",
                "features": "手軽さ",
                "positioning": "身近",
                "url": "https://example.com/competitor",
            }
        ],
        "G_insight": {
            "complaint_pattern": "続けにくい",
            "complaint_example": "習慣化したい",
            "desire_pattern": "手軽に使いたい",
            "desire_example": "日常に取り入れたい",
        },
        "H_event": {
            "overview": "体験イベント",
            "scale": "全国",
            "sns_reality": "投稿と相性が良い",
            "benchmark_case": "体験型企画",
            "url": "https://example.com/event",
        },
    }


def _video(query: str, rank: int, *, play_count: int) -> TikTokVideo:
    video_id = f"{sum((index + 1) * ord(char) for index, char in enumerate(query)) + 1}{rank:02d}"
    return TikTokVideo(
        id=f"{query}-{rank}",
        url=f"https://www.tiktok.com/@creator/video/{video_id}",
        desc="説明",
        create_time=0,
        duration=15,
        cover_url=f"https://cdn.example.com/{query}-{rank}.jpg",
        author=TikTokAuthor(unique_id="creator", nickname="投稿者", follower_count=0),
        play_count=play_count,
        digg_count=0,
        comment_count=0,
        share_count=0,
        collect_count=0,
        hashtags=(),
        music_title="",
    )


class _CapturingDeck:
    def __init__(self) -> None:
        self.inputs: list[ProposalDeckInput] = []
        self.image_paths_during_run: list[Path] = []
        self.cleaned: list[ProposalDeckOutput] = []

    def run(self, input: ProposalDeckInput, ctx: SkillContext) -> ProposalDeckOutput:
        del ctx
        self.inputs.append(input)
        self.image_paths_during_run = [
            Path(image.image_path or "")
            for images in input.evidence_images.values()
            for image in images
        ]
        assert all(path.is_file() for path in self.image_paths_during_run)
        return ProposalDeckOutput(
            pptx_path="/tmp/fake-proposal.pptx",
            version_id=f"v-test-{len(self.inputs)}",
            filled_count=95,
            skipped_count=0,
            coverage_ratio=1.0,
            skipped_ids=[],
            total_cost_usd=0.0,
        )

    def cleanup_output(self, output: ProposalDeckOutput) -> None:
        self.cleaned.append(output)


def _campaign_factory(
    fetcher: Callable[[str | None, str], bytes | None],
) -> Callable[[Searcher], ProposalCampaignSkill]:
    def factory(searcher: Searcher) -> ProposalCampaignSkill:
        return ProposalCampaignSkill(
            searcher=searcher,
            fetcher=fetcher,
            normalizer=lambda body: body,
            max_workers=1,
        )

    return factory


def _configure_builder_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    media_configured: bool,
) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_TEMPLATE_PATH", "/tmp/template.pptx")
    monkeypatch.delenv("PROPOSAL_BUILDER_DELIVER_INTERNAL_DRAFTS", raising=False)
    monkeypatch.delenv("PROPOSAL_BUILDER_PUBLISH_READY", raising=False)
    monkeypatch.setattr(builder_module, "load_and_select_accounts", lambda *_a, **_kw: [])
    monkeypatch.setattr(builder_module, "search_case_candidates", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        builder_module.MediaJobClient,
        "is_configured",
        classmethod(lambda cls: media_configured),
    )


def _input(research: dict[str, Any], **kwargs: Any) -> ProposalBuilderInput:
    return ProposalBuilderInput(
        gemini_json=research,
        posting_start_date="2026-09-01",
        **kwargs,
    )


def test_configured_search_injects_evidence_and_measured_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)
    calls: list[tuple[str, int, str | None]] = []

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        calls.append((query, max_videos, request_id))
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=100), _video(query, 2, play_count=200)),
        )

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(lambda _url, _rid: b"\xff\xd8\xffimage"),
    )

    output = skill.run(_input(_research()), SkillContext(request_id="success-request"))

    assert [(query, count) for query, count, _rid in calls] == [
        ("集中", 10),
        ("作業用BGM", 10),
        ("学生", 10),
    ]
    deck_input = deck.inputs[0]
    assert set(deck_input.evidence_images) == {58, 60, 62}
    assert all(
        image.placeholder_id == placeholder_id
        for placeholder_id, images in deck_input.evidence_images.items()
        for image in images
    )
    assert "# 実測TikTokデータ" in deck_input.research_material
    assert "上位2本合計再生数 300回" in deck_input.research_material
    first_url = _video("集中", 1, play_count=100).url
    second_url = _video("集中", 2, play_count=200).url
    assert first_url in deck_input.research_material
    assert "取得不可（UI非表示）" not in deck_input.research_material
    assert deck_input.quantitative_evidence is not None
    assert set(deck_input.quantitative_evidence["300回"]) >= {
        first_url,
        second_url,
    }
    assert deck.image_paths_during_run
    assert all(not path.exists() for path in deck.image_paths_during_run)
    assert not any("SNSキャプチャは未自動化" in warning for warning in output.warnings)


def test_unconfigured_media_skips_both_features_even_with_local_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")

    def forbidden_search(*_args: Any, **_kwargs: Any) -> TikTokSearchResult:
        raise AssertionError("TikTok search must be skipped")

    def forbidden_campaign(_searcher: Searcher) -> ProposalCampaignSkill:
        raise AssertionError("thumbnail campaign must be skipped")

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=forbidden_search,
        campaign_factory=forbidden_campaign,
    )

    skill.run(_input(_research()), SkillContext(request_id="unconfigured-request"))

    deck_input = deck.inputs[0]
    assert deck_input.evidence_images == {}
    assert "# 実測TikTokデータ" not in deck_input.research_material
    assert "取得不可（UI非表示）" in deck_input.research_material


@pytest.mark.parametrize("failure", ["timeout", "empty", "scrape_error"])
def test_search_failures_are_logged_and_fail_open(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        if failure == "timeout":
            raise TikTokScrapeError("TIKTOK_TIMEOUT: worker deadline")
        if failure == "scrape_error":
            raise TikTokScrapeError("TIKTOK_MEDIA_JOB_FAILED: MediaJobError")
        return TikTokSearchResult(query=query, search_type="keyword", videos=())

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(lambda _url, _rid: b"unused"),
    )

    with capture_logs() as logs:
        skill.run(
            _input(
                _research(
                    kaiwai_keywords=["集中"],
                    target_categories=["集中"],
                )
            ),
            SkillContext(request_id=f"failure-{failure}"),
        )

    deck_input = deck.inputs[0]
    assert deck_input.evidence_images == {}
    assert "# 実測TikTokデータ" not in deck_input.research_material
    assert "取得不可（UI非表示）" in deck_input.research_material
    failure_logs = [
        entry for entry in logs if entry.get("event") == "proposal_builder_tiktok_search_failed"
    ]
    assert failure_logs
    expected_code = {
        "timeout": "TIKTOK_TIMEOUT",
        "empty": "TIKTOK_EMPTY_RESULT",
        "scrape_error": "TIKTOK_MEDIA_JOB_FAILED",
    }[failure]
    assert failure_logs[0]["error_code"] == expected_code


@pytest.mark.parametrize("thumbnail_failure", ["timeout", "empty"])
def test_thumbnail_failure_keeps_measured_data_and_builds_without_image(
    thumbnail_failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=500),),
        )

    def timed_out_fetcher(_url: str | None, _request_id: str) -> bytes | None:
        if thumbnail_failure == "timeout":
            raise MediaJobError("MEDIA_JOB_TIMEOUT")
        return None

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(timed_out_fetcher),
    )

    with capture_logs() as logs:
        skill.run(
            _input(
                _research(
                    kaiwai_keywords=["集中"],
                    target_categories=["集中"],
                )
            ),
            SkillContext(request_id=f"thumbnail-{thumbnail_failure}"),
        )

    deck_input = deck.inputs[0]
    assert deck_input.evidence_images == {}
    assert "# 実測TikTokデータ" in deck_input.research_material
    assert "上位1本合計再生数 500回" in deck_input.research_material
    assert "取得不可（UI非表示）" not in deck_input.research_material
    assert any(
        entry.get("event") == "proposal_builder_thumbnail_failed"
        and entry.get("error_type")
        == ("MediaJobError" if thumbnail_failure == "timeout" else "no_result")
        for entry in logs
    )


def test_zero_play_counts_do_not_replace_unavailable_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=0),),
        )

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(lambda _url, _rid: b"\xff\xd8\xffimage"),
    )

    with capture_logs() as logs:
        skill.run(
            _input(
                _research(
                    kaiwai_keywords=["集中"],
                    target_categories=["集中"],
                )
            ),
            SkillContext(request_id="zero-play-count"),
        )

    deck_input = deck.inputs[0]
    assert set(deck_input.evidence_images) == {58}
    assert "# 実測TikTokデータ" not in deck_input.research_material
    assert "取得不可（UI非表示）" in deck_input.research_material
    assert any(
        entry.get("event") == "proposal_builder_tiktok_measurement_unavailable"
        and entry.get("error_code") == "no_usable_source_backed_play_data"
        for entry in logs
    )


def test_partial_search_failure_preserves_slots_and_only_updates_matching_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)
    research = _research(
        kaiwai_keywords=["成功", "失敗"],
        target_categories=["成功", "失敗"],
    )
    research["C_tiktok"] = [
        {
            "related_tag": tag,
            "representative_post_url": f"https://www.tiktok.com/@seed/video/{index}",
            "search_demand_note": "検索需要は実測前",
            "total_count": "取得不可（UI非表示）",
        }
        for index, tag in enumerate(("成功", "失敗"), start=101)
    ]

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        if query == "失敗":
            raise TikTokScrapeError("TIKTOK_TIMEOUT")
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=700),),
        )

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(lambda _url, _rid: b"\xff\xd8\xffimage"),
    )

    skill.run(_input(research), SkillContext(request_id="partial-search"))

    deck_input = deck.inputs[0]
    assert set(deck_input.evidence_images) == {58}
    assert "実測:「成功」検索上位1本合計再生数 700回" in deck_input.research_material
    assert "取得不可（UI非表示）" in deck_input.research_material
    assert "実測:「失敗」" not in deck_input.research_material


def test_campaign_pipeline_exception_is_logged_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)

    def failing_campaign(_searcher: Searcher) -> ProposalCampaignSkill:
        raise MediaJobError("MEDIA_JOB_TIMEOUT")

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("search must not run when campaign construction fails")
        ),
        campaign_factory=failing_campaign,
    )

    with capture_logs() as logs:
        skill.run(
            _input(
                _research(
                    kaiwai_keywords=["集中"],
                    target_categories=["集中"],
                )
            ),
            SkillContext(request_id="campaign-failure"),
        )

    deck_input = deck.inputs[0]
    assert deck_input.evidence_images == {}
    assert "取得不可（UI非表示）" in deck_input.research_material
    assert any(
        entry.get("event") == "proposal_builder_thumbnail_pipeline_failed"
        and entry.get("error_code") == "MEDIA_JOB_TIMEOUT"
        for entry in logs
    )


def test_pre_deck_failure_still_cleans_campaign_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)
    campaigns: list[ProposalCampaignSkill] = []

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=100),),
        )

    def factory(searcher: Searcher) -> ProposalCampaignSkill:
        campaign = _campaign_factory(lambda _url, _rid: b"\xff\xd8\xffimage")(searcher)
        campaigns.append(campaign)
        return campaign

    monkeypatch.setattr(
        builder_module,
        "build_quantitative_evidence",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("pre-deck failure")),
    )
    skill = ProposalBuilderSkill(
        search=object(),
        deck=_CapturingDeck(),  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=factory,
    )

    with pytest.raises(RuntimeError, match="pre-deck failure"):
        skill.run(
            _input(
                _research(
                    kaiwai_keywords=["集中"],
                    target_categories=["集中"],
                )
            ),
            SkillContext(request_id="pre-deck-cleanup"),
        )

    assert len(campaigns) == 1
    assert campaigns[0]._temporary_output_dirs == {}


def test_confidential_queries_use_category_and_sector_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_builder_test(monkeypatch, media_configured=True)
    brand = "極秘ブランド"
    queries: list[str] = []

    def fake_search(
        query: str,
        *,
        max_videos: int,
        request_id: str | None = None,
    ) -> TikTokSearchResult:
        del max_videos, request_id
        queries.append(query)
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(_video(query, 1, play_count=100),),
        )

    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
        tiktok_searcher=fake_search,
        campaign_factory=_campaign_factory(lambda _url, _rid: b"\xff\xd8\xffimage"),
    )

    with capture_logs() as logs:
        skill.run(
            _input(
                _research(
                    brand=brand,
                    sector="化粧品",
                    kaiwai_keywords=[f"{brand} ファン"],
                    target_categories=[f"{brand} 愛用者"],
                ),
                confidential_product_name=True,
                category_term=f"{brand} 化粧水",
            ),
            SkillContext(request_id="confidential-request"),
        )

    assert queries == ["化粧品"]
    assert all(brand not in query for query in queries)
    deck_input = deck.inputs[0]
    assert brand not in deck_input.research_material
    assert brand not in json.dumps(
        {
            str(pid): [image.model_dump(mode="json") for image in images]
            for pid, images in deck_input.evidence_images.items()
        },
        ensure_ascii=False,
    )
    assert brand not in json.dumps(deck_input.quantitative_evidence, ensure_ascii=False)
    assert brand not in json.dumps(logs, ensure_ascii=False)
