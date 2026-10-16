"""反応合計は施策実績の集計行だけを使い、上位動画や関連資料から作り直さない。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.campaign_aggregate import (
    CampaignAggregate,
    CampaignVideo,
    format_campaign_document,
)


def _campaign_hit() -> SearchHit:
    videos = tuple(
        CampaignVideo(
            url=f"https://www.tiktok.com/@example/video/{index}",
            account="投稿者",
            text="食事のアレンジ",
            plays=1000 - index,
            likes=likes,
            shares=None,
            comments=None,
            saves=saves,
            cost=None,
            impressions=None,
            ctr=None,
            ad_name="",
        )
        for index, (likes, saves) in enumerate(
            [(2150, 120), (694, 80), (None, 50), (None, 30), (None, 20), (None, 10)],
            start=1,
        )
    )
    return SearchHit(
        chunk_id=1,
        content=format_campaign_document(
            CampaignAggregate(videos=videos, advertiser="食品", campaign="ハンバーグ")
        ),
        score=0.9,
        metadata={"campaign_aggregate": "true", "advertiser": "食品", "campaign": "ハンバーグ"},
    )


def test_campaign_totals_use_full_campaign_row_for_every_answer() -> None:
    """10-16本番: 同じ施策のいいねが2150/2844に揺れた。上位5本の保存合計も全6本と違う。"""
    from teamagent.skills.search.campaign_files import campaign_totals_block

    campaign = _campaign_hit()
    account = replace(
        campaign,
        chunk_id=2,
        metadata={"account_aggregate": "true"},
        content="投稿アカウント実績: 投稿者\n反応: いいね 合計 2,150、保存 合計 300",
    )
    related = replace(
        account,
        chunk_id=3,
        metadata={"source_type": "gdrive", "is_related_drive": True},
        content="レポート: いいね 合計 2,150、保存 合計 300",
    )
    expected = (
        "施策の反応合計（ショート動画データベース）\n"
        "- 食品 / ハンバーグ: いいね 合計 2,844・保存 合計 310"
    )
    assert campaign_totals_block([account, campaign, related, campaign]) == expected
    assert campaign_totals_block([related, campaign, account]) == expected
    unformatted = replace(campaign, content=campaign.content.replace("2,844", "2844"))
    assert campaign_totals_block([unformatted, campaign]) == expected


def test_campaign_totals_keep_missing_and_reject_partial_numbers() -> None:
    from teamagent.skills.search.campaign_files import campaign_totals_block

    campaign = _campaign_hit()
    missing = replace(
        campaign, content="施策実績: 食品 / ハンバーグ\n反応: いいね 合計 不明、保存 合計 0"
    )
    assert campaign_totals_block([missing]).endswith("いいね 合計 不明・保存 合計 0")
    broken = replace(campaign, content="反応: いいね 合計 2,84、保存 合計 4万")
    assert campaign_totals_block([broken]) == ""
    assert campaign_totals_block([]) == ""


def test_campaign_totals_conflicting_chunks_never_depend_on_hit_order() -> None:
    from teamagent.skills.search.campaign_files import campaign_totals_block

    campaign = _campaign_hit()
    conflicting = replace(
        campaign,
        chunk_id=4,
        content=campaign.content.replace("いいね 合計 2,844", "いいね 合計 2,150"),
    )
    expected = (
        "施策の反応合計（ショート動画データベース）\n"
        "- 食品 / ハンバーグ: いいね 合計 不明（集計行が不一致）・保存 合計 310"
    )
    assert campaign_totals_block([campaign, conflicting]) == expected
    assert campaign_totals_block([conflicting, campaign]) == expected


def test_summarizer_receives_same_canonical_totals_as_code_answer() -> None:
    from teamagent.skills.search.campaign_files import campaign_totals_block
    from tests.skills.search.test_campaign_files import _message, _Pg, _skill

    campaign = _campaign_hit()
    related = replace(
        campaign,
        chunk_id=8,
        content="レポートの一部期間: いいね 合計 2,150、保存 合計 300",
        metadata={"source_type": "gdrive", "is_related_drive": True, "title": "施策レポート"},
    )
    hits = [campaign, related]
    totals = campaign_totals_block(hits)
    skill = _skill(_Pg(hits, []), answer="食事のアレンジを紹介しました。")
    skill._summarize("食品の施策", hits, "iter5-totals", canonical_campaign_totals=totals)
    message = _message(skill)
    assert message.count(totals) == 1
    assert "反応合計はこの集計値だけを使う" in message
    assert "いいね 合計 2,844・保存 合計 310" in totals
    assert "2,150" not in totals and "300" not in totals


def test_direct_campaign_answers_keep_identical_totals_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.base import SkillContext
    from teamagent.skills.search.schema import SearchInput
    from tests.skills.search.test_campaign_files import ME, _Pg, _skill

    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setenv("SEARCH_CAMPAIGN_FILES", "true")
    campaign = _campaign_hit()
    related = replace(
        campaign,
        chunk_id=8,
        content="レポートの一部期間: いいね 合計 2,150、保存 合計 300",
        metadata={"source_type": "gdrive", "is_related_drive": True, "title": "施策レポート"},
    )
    answers = []
    for model_answer in ("いいね合計は2,150です。", "いいね合計は9,999です。"):
        skill = _skill(_Pg([campaign, related], []), answer=model_answer)
        result = skill.run(
            SearchInput(query="施策実績"),
            SkillContext(metadata={"user_email": ME, "_search_direct_answer": True}),
        )
        answers.append(result.answer)
        skill._bedrock.converse.assert_not_called()
        assert "いいね 合計 2,844・保存 合計 310" in result.answer
        assert "2,150" not in result.answer and "9,999" not in result.answer
    assert answers[0] == answers[1]
