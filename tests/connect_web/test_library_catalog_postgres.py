"""Real GROUP BY/latest-snapshot contracts in the rollback-only library DB fixture."""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.connect_web.library import featured_videos, search_campaigns, search_videos
from tests.ingest.test_video_library_postgres import _DSN, db
from tests.ingest.test_video_library_settings_postgres import prepare

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def seed(conn: Any, repository: Any) -> None:
    prepare(conn, repository)
    conn.execute("UPDATE vl_videos SET posted_at='2026-10-01', account_handle='one'")
    conn.execute("UPDATE vl_metrics SET total_plays=100000, total_saves=10000")
    conn.execute("""INSERT INTO vl_metrics
        (platform, video_id, captured_on, total_plays, total_saves, total_likes,
         total_comments, total_shares)
        VALUES ('tiktok','1','2026-10-30',1000,20,30,4,6)""")
    for vid, plays, saves, thumb, campaign, handle in [
        ("2", 2000, 100, "thumb", "test", "two"),
        ("3", 3000, 30, "thumb", "test", "three"),
        ("4", 50000, 500, None, "test", "four"),
        ("5", 100, 2, None, "other", "five"),
    ]:
        conn.execute(
            """INSERT INTO vl_videos (platform,video_id,url,advertiser,campaign,
            account_handle,is_pr,posted_at,thumbnail_s3_key)
            VALUES ('tiktok',%s,%s,'test',%s,%s,true,'2026-10-02',%s)""",
            (vid, f"https://www.tiktok.com/@{handle}/video/{vid}", campaign, handle, thumb),
        )
        conn.execute(
            """INSERT INTO vl_metrics (platform,video_id,captured_on,total_plays,
            total_saves,total_likes,total_comments,total_shares)
            VALUES ('tiktok',%s,'2026-10-30',%s,%s,30,4,6)""",
            (vid, plays, saves),
        )


def test_campaign_group_by_latest_totals_average_and_representative(db: tuple[Any, Any]) -> None:
    conn, repository = db
    seed(conn, repository)
    rows = search_campaigns(conn, {}, admin=False)
    row = rows[0]
    assert len(rows) == 2
    assert row["campaign"] == "test"
    assert row["video_count"] == 4
    assert row["total_plays"] == 56000  # latest 1000, never the old 100000
    assert row["total_saves"] == 650
    assert row["average_save_rate"] == pytest.approx(0.0225)
    assert row["total_likes"] == 120 and row["total_comments"] == 16 and row["total_shares"] == 24
    assert row["first_posted"].day == 1 and row["last_posted"].day == 2
    assert row["representative"]["video_id"] == "3"  # highest among thumbnail-bearing videos
    assert row["representative"]["has_thumbnail"] is True
    conn.execute("UPDATE vl_videos SET thumbnail_s3_key=NULL")
    assert search_campaigns(conn, {}, admin=False)[0]["representative"]["video_id"] == "4"
    conn.execute("UPDATE vl_metrics SET total_saves=NULL WHERE video_id='2'")
    assert search_campaigns(conn, {}, admin=False)[0]["total_saves"] is None
    conn.execute("UPDATE vl_metrics SET total_plays=0 WHERE video_id='3'")
    assert search_campaigns(conn, {}, admin=False)[0]["average_save_rate"] == pytest.approx(0.015)


def test_campaign_filters_privacy_and_sort(db: tuple[Any, Any]) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("""INSERT INTO vl_campaign_settings
        (advertiser,campaign,business_category,industry,purpose,is_private,updated_by)
        VALUES ('test','test','media','食品','理解',true,'admin')""")
    assert len(search_campaigns(conn, {}, admin=False)) == 1
    assert len(search_campaigns(conn, {}, admin=True)) == 2
    assert search_campaigns(conn, {"industry": "食品"}, admin=False) == []
    assert len(search_campaigns(conn, {"q": "食品", "purpose": "理解"}, admin=True)) == 1
    assert search_campaigns(conn, {"q": "' OR true--"}, admin=True) == []
    assert search_campaigns(conn, {"sort": "count"}, admin=True)[0]["campaign"] == "test"
    conn.execute("UPDATE vl_videos SET posted_at='2026-10-03' WHERE campaign='other'")
    assert search_campaigns(conn, {"sort": "new"}, admin=True)[0]["campaign"] == "other"
    assert len(featured_videos(conn, {}, admin=True)) == 3


def test_analysis_tag_filter_executes_on_latest_supported_payload(db: tuple[Any, Any]) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("""INSERT INTO vl_video_analysis (platform,video_id,kind,version,payload)
        VALUES ('tiktok','1','structure','v1',
        '{"tags":{"hook_type":["question"]},"standards":{"loop":true}}')""")
    for params in (
        {"tag_group": "hook_type", "tag": "question"},
        {"tag_group": "standards", "tag": "loop"},
    ):
        result = search_videos(conn, params, admin=False)
        assert result["total"] == 1 and result["videos"][0]["video_id"] == "1"
    assert (
        search_videos(conn, {"tag_group": "hook_type", "tag": "other"}, admin=False)["total"] == 0
    )


@pytest.mark.parametrize("admin,total", [(False, 6000), (True, 56000)])
@pytest.mark.parametrize(
    "filters", [{"q": "needle"}, {"industry": "食品"}, {"account": "one", "sort": "plays"}]
)
def test_video_campaign_total_ignores_search_but_respects_visibility(
    db: tuple[Any, Any], admin: bool, total: int, filters: dict[str, str]
) -> None:
    conn, repository = db
    seed(conn, repository)
    # The current view sets privacy per campaign; exercise mixed visibility at its read boundary.
    conn.execute("CREATE TEMP TABLE vl_videos_effective AS SELECT * FROM vl_videos_effective")
    conn.execute("UPDATE vl_videos_effective SET is_private=true WHERE video_id='4'")
    conn.execute(
        "UPDATE vl_videos_effective SET caption='needle', industry='食品' WHERE video_id='1'"
    )
    # Identical campaign names belonging to different advertisers must not be combined.
    conn.execute(
        "UPDATE vl_videos_effective SET campaign='test', advertiser='another' WHERE video_id='5'"
    )
    case = next(r for r in search_campaigns(conn, {}, admin=admin) if r["advertiser"] == "test")
    assert case["total_plays"] == total
    result = search_videos(conn, filters, admin=admin)
    assert result["total"] == 1
    assert result["videos"][0]["total_plays"] == 1000
    assert result["videos"][0]["campaign_total_plays"] == total
    featured = featured_videos(conn, filters, admin=admin)
    assert len(featured) == 1 and featured[0]["campaign_total_plays"] == total


@pytest.mark.parametrize("missing_snapshot", [False, True])
@pytest.mark.parametrize("private", [False, True])
def test_video_campaign_total_keeps_unknown_metrics_unknown(
    db: tuple[Any, Any], missing_snapshot: bool, private: bool
) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("CREATE TEMP TABLE vl_videos_effective AS SELECT * FROM vl_videos_effective")
    conn.execute("UPDATE vl_videos_effective SET is_private=%s WHERE video_id='4'", (private,))
    if missing_snapshot:
        # teamagent_app has no DELETE grant (as in production); only the fixture setup needs it.
        conn.execute("RESET ROLE")
        conn.execute("DELETE FROM vl_metrics WHERE video_id='4'")
        conn.execute("SET ROLE teamagent_app")
    else:
        conn.execute("UPDATE vl_metrics SET total_plays=NULL WHERE video_id='4'")
    for admin in (False, True):
        expected = 6000 if private and not admin else None
        case = next(r for r in search_campaigns(conn, {}, admin=admin) if r["campaign"] == "test")
        assert case["total_plays"] == expected
        row = search_videos(conn, {"account": "one"}, admin=admin)["videos"][0]
        assert row["campaign_total_plays"] == expected


def test_video_campaign_total_is_zero_or_absent_for_unassigned(db: tuple[Any, Any]) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("UPDATE vl_metrics SET total_plays=0")
    assert search_videos(conn, {}, admin=False)["videos"][0]["campaign_total_plays"] == 0
    conn.execute("UPDATE vl_videos SET campaign=NULL WHERE video_id='1'")
    row = search_videos(conn, {"account": "one"}, admin=False)["videos"][0]
    assert row["campaign_total_plays"] is None


@pytest.mark.parametrize("winner", ["media", "tto", "kirinuki", "other", "organic", None])
def test_primary_category_majority_and_tie_priority(
    db: tuple[Any, Any], winner: str | None
) -> None:
    conn, repository = db
    seed(conn, repository)
    # Shadow only the view in this rollback-only connection to exercise every future mixed category.
    conn.execute(
        "CREATE TEMP TABLE vl_videos_effective AS SELECT * FROM vl_videos_effective LIMIT 0"
    )
    codes = ["media", "tto", "kirinuki", "other", "organic", None]
    for i, code in enumerate(codes):
        conn.execute(
            """INSERT INTO vl_videos_effective
            (platform,video_id,advertiser,campaign,business_category,is_private,is_pr)
            VALUES ('tiktok',%s,'test','mixed',%s,false,true)""",
            (str(i), code),
        )
    # All tied, then remove each winner: proves the complete priority including NULL last.
    for code in codes:
        assert search_campaigns(conn, {}, admin=False)[0]["primary_category"] == code
        conn.execute(
            "DELETE FROM vl_videos_effective WHERE business_category IS NOT DISTINCT FROM %s",
            (code,),
        )
    for i, code in enumerate([*codes, winner]):
        conn.execute(
            """INSERT INTO vl_videos_effective
            (platform,video_id,advertiser,campaign,business_category,is_private,is_pr)
            VALUES ('tiktok',%s,'test','mixed',%s,false,true)""",
            (str(i), code),
        )
    assert search_campaigns(conn, {}, admin=False)[0]["primary_category"] == winner
