"""Campaign totals include every member, independent of the top-k retrieval score."""

import pytest

from teamagent.skills.media_case_search.campaigns import aggregate_cases, render_hits
from teamagent.skills.media_case_search.schema import MediaCaseInput
from teamagent.skills.media_case_search.skill import query_filters


def post(number, *, brand="楽天トラベル", campaign="リリースキャンペーン × TikTok GO", tags=None):
    return {
        "post_id": str(number),
        "score": 0.8 if number == 0 else 0.1,
        "post_url": f"https://www.tiktok.com/@trendnavi_official/video/{number}",
        "account_handle": f"account{number % 5}",
        "posted_on": "2025-09-01" if number < 30 else "2025-10-01",
        "pr_status": "disclosed",
        "has_hash_pr": True,
        "isSponsored": False,
        "metrics_observed_at": "2026-10-28T00:00:00Z",
        "metrics": {
            "playCount": 310000 if number == 0 else 19833 + (number <= 20),
            "collectCount": 150 if number == 0 else 77,
        },
        "items": [
            {
                "brand": brand,
                "campaign": campaign,
                "product": "旅行予約",
                "campaign_tags": tags if tags is not None else ["楽天トラベルリリースキャンペーン"],
                "facility": [
                    "東京グリーンパレス",
                    "the b お茶の水",
                    "施設C",
                    "施設D",
                    "施設E",
                    "施設F",
                    "施設G",
                ][number % 7],
                "category": "旅行",
                "campaign_types": ["campaign"],
                "geography": "domestic",
            }
        ],
    }


def test_61_posts_one_campaign_across_five_accounts_code_computes_all_metrics():
    rows = [
        post(i, campaign="リリースキャンペーン" if i % 2 else "リリースキャンペーン × TikTok GO")
        for i in range(61)
    ]
    rows.append(rows[0])  # duplicate retrieval or multi-item join must not double-count
    groups = aggregate_cases(rows)
    assert len(groups) == 1
    case = groups[0]
    assert case["post_count"] == 61 and case["account_count"] == 5
    assert case["play_count"] == 1_500_000 and case["save_count"] == 4_770
    assert case["top"]["metrics"]["playCount"] == 310000
    _, filters = query_filters(MediaCaseInput(query="国内 旅行 CP"))
    message = render_hits(rows, filters)
    assert "5 アカウントで 61 本" in message
    assert "1,500,000再生・保存 4,770件" in message and "最大は310,000再生" in message
    assert "#PR 表記あり" in message and "提供表示あり" not in message
    assert "2025年9〜10月" in message and "7施設" in message
    assert message.count("**") == 2 and rows[0]["post_url"] in message


def test_same_brand_and_campaign_without_tag_merge_but_generic_labels_do_not():
    assert len(aggregate_cases([post(0, tags=[]), post(1, tags=[])])) == 1
    assert (
        len(
            aggregate_cases(
                [post(0, campaign=None), post(1, campaign="リリースキャンペーン", tags=[])]
            )
        )
        == 1
    )
    assert (
        len(
            aggregate_cases(
                [
                    post(0, campaign=None, tags=["旅行", "TikTokGO"]),
                    post(1, campaign="キャンペーン", tags=["PR"]),
                ]
            )
        )
        == 2
    )
    assert len(aggregate_cases([post(0, tags=[]), post(1, brand="別会社", tags=[])])) == 2


def test_specific_hashtag_merges_missing_known_and_different_brands_with_exact_totals():
    rows = [
        post(0, brand=None, campaign="tiktokgo", tags=["#PR", "#tiktokgo"]),
        post(1, brand="TikTokGO", campaign=None, tags=["＃ＴｉｋＴｏｋＧＯ"]),
        post(2, brand="別ブランド", campaign="宿泊紹介", tags=["#TikTok_GO"]),
    ]
    rows[0]["metrics"] = {"playCount": 9374, "collectCount": 44}
    rows[1]["metrics"] = {"playCount": 10500, "collectCount": 61}
    rows[2]["metrics"] = {"playCount": 12000, "collectCount": 70}
    groups = aggregate_cases([*rows, rows[0]])
    assert len(groups) == 1
    assert groups[0]["post_count"] == 3 and groups[0]["account_count"] == 3
    assert groups[0]["play_count"] == 31874 and groups[0]["save_count"] == 175
    assert groups[0]["top"]["post_id"] == "2"
    message = render_hits(rows, {})
    assert "**TikTok GO の宿泊紹介（2025年9月）**" in message
    assert "未特定" not in message
    assert "31,874再生・保存 175件。最大は12,000再生" in message
    assert "〔いちばん伸びた投稿〕" + rows[2]["post_url"] in message


def test_rakuten_campaign_and_tiktok_go_stays_separate_without_splitting_facilities():
    rakuten = [post(i, tags=["#TikTokGO", "#楽天トラベルリリースキャンペーン"]) for i in range(5)]
    stays = [
        post(
            i + 5,
            brand=f"ホテル{i % 2}",
            campaign="宿泊紹介",
            tags=[f"#施設{i % 2}", "＃ＴｉｋＴｏｋＧＯ", "#旅行"],
        )
        for i in range(6)
    ]
    for i, row in enumerate(rakuten):
        row["account_handle"] = f"rakuten{i % 3}"
    for i, row in enumerate(stays):
        row["account_handle"] = f"stay{i % 4}"
    groups = aggregate_cases([*rakuten, *stays])
    assert len(groups) == 2
    assert set(groups[0]["posts"]) == {row["post_id"] for row in rakuten}
    assert groups[0]["post_count"] == 5 and groups[0]["account_count"] == 3
    assert set(groups[1]["posts"]) == {row["post_id"] for row in stays}
    assert groups[1]["post_count"] == 6 and groups[1]["account_count"] == 4
    for group, rows in zip(groups, [rakuten, stays], strict=True):
        assert group["play_count"] == sum(row["metrics"]["playCount"] for row in rows)
        assert group["save_count"] == sum(row["metrics"]["collectCount"] for row in rows)


@pytest.mark.parametrize(
    "name", ["新春キャンペーン", "SpringCampaign", "ＳｐｒｉｎｇＣＡＭＰＡＩＧＮ", "春ＣＰ", "春cp"]
)
@pytest.mark.parametrize("source", ["tag", "campaign"])
def test_explicit_campaign_takes_priority_over_more_frequent_shared_tag(name, source):
    rows = [post(i, campaign=None, tags=["#TikTokGO"]) for i in range(3)]
    for row in rows[:2]:
        item = row["items"][0]
        if source == "tag":
            item["campaign_tags"].append("#" + name)
        else:
            item["campaign"] = name
    groups = aggregate_cases(rows)
    assert {frozenset(group["posts"]) for group in groups} == {
        frozenset({"0", "1"}),
        frozenset({"2"}),
    }


def test_shared_tags_require_multiple_accounts_and_count_unique_posts():
    rows = [
        post(0, campaign=None, tags=["#施設名", "#TikTokGO", "＃ＴｉｋＴｏｋＧＯ"]),
        post(1, campaign=None, tags=["#施設名", "#TikTokGO"]),
        post(2, campaign=None, tags=["#TikTokGO"]),
        post(3, campaign=None, tags=["#施設名"]),
    ]
    rows[3]["account_handle"] = rows[0]["account_handle"]
    groups = aggregate_cases([*rows, *([rows[3]] * 5)])
    assert {frozenset(group["posts"]) for group in groups} == {
        frozenset({"0", "1", "2"}),
        frozenset({"3"}),
    }
    for row in rows:
        row["account_handle"] = "same_account"
    assert len(aggregate_cases(rows)) == 4


def test_multiple_items_in_one_post_choose_one_campaign_without_bridging():
    rows = [post(0), post(1), post(2, tags=["#別キャンペーン"])]
    rows[0]["items"].append(rows[2]["items"][0])
    groups = aggregate_cases(rows)
    assert len(groups) == 2
    assert sum(group["post_count"] for group in groups) == 3
    assert next(group for group in groups if "1" in group["posts"]) is not next(
        group for group in groups if "2" in group["posts"]
    )
    assert aggregate_cases([{**rows[0], "items": []}]) == []


@pytest.mark.parametrize("tag", ["#旅行", "＃ＰＲ", "#お出かけ", "#ホテル", "#おすすめ"])
@pytest.mark.parametrize("brand", [None, "旅行社"])
def test_generic_hashtags_alone_do_not_merge_posts(tag, brand):
    assert (
        len(aggregate_cases([post(i, brand=brand, campaign=None, tags=[tag]) for i in range(2)]))
        == 2
    )


@pytest.mark.parametrize("brand", [None, "ブランド未特定", "不明"])
def test_missing_brand_uses_readable_campaign_tag_without_unknown_label(brand):
    message = render_hits(
        [post(0, brand=brand, campaign="宿泊紹介", tags=["#PR", "#tiktokgo"])], {}
    )
    assert "**TikTok GO の宿泊紹介（2025年9月）**" in message
    assert "未特定" not in message and "不明" not in message


def test_brand_from_later_member_and_top_three_facilities_or_products():
    rows = [post(i, brand=None, campaign=None) for i in range(7)]
    rows[1]["items"][0]["brand"] = "楽天トラベル"
    rows[1]["items"][0]["campaign"] = "リリースキャンペーン"
    for row in rows:
        row["items"][0]["facility"] = (
            "人気ホテル" if int(row["post_id"]) >= 3 else f"施設{row['post_id']}"
        )
    message = render_hits(rows, {})
    assert "**楽天トラベル リリースキャンペーン" in message
    assert "（人気ホテル、施設0、施設1 など4施設）" in message
    assert "施設2" not in message
    for row in rows:
        row["items"][0]["product"] = row["items"][0].pop("facility")
    assert "（人気ホテル、施設0、施設1 など4商品）" in render_hits(rows, {})


def test_max_three_campaigns_plus_one_related_line_and_no_invented_metrics():
    rows = [post(0, brand=f"会社{i}", campaign=f"固有施策{i}", tags=[]) for i in range(5)]
    for i, row in enumerate(rows):
        row["post_id"] = str(i)
    related = post(90, brand="浅草六区", campaign="SAKURA STREET", tags=[])
    related["score"] = 0.7
    related["items"][0]["campaign_types"] = ["event"]
    related["items"][0]["category"] = "娯楽"
    related["metrics"] = {"playCount": 590000, "collectCount": None}
    rows.append(related)
    _, filters = query_filters(MediaCaseInput(query="国内 旅行 CP"))
    message = render_hits(rows, filters)
    assert message.count("〔いちばん伸びた投稿〕") == 3
    assert "関連では、浅草六区 SAKURA STREET" in message and "590,000再生" in message
    row = post(0)
    row.update(
        metrics={"playCount": None, "collectCount": None}, pr_status="possible", has_hash_pr=False
    )
    message = render_hits([row], filters)
    assert "再生未取得" in message and "件未取得" in message
    assert "PR 不明（参考）" in message and "〔いちばん伸びた投稿〕" not in message
    assert "〔参考投稿〕" in message
