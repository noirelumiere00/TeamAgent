"""Campaign totals include every member, independent of the top-k retrieval score."""

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
                [
                    post(0, campaign=None, tags=["旅行", "TikTokGO"]),
                    post(1, campaign="キャンペーン", tags=["PR"]),
                ]
            )
        )
        == 2
    )
    assert len(aggregate_cases([post(0), post(1, brand="別会社")])) == 2


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
