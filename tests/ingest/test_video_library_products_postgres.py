"""Real product filtering, case aggregation and RLS in a disposable schema."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from teamagent.connect_web import library
from teamagent.connect_web import library_products as products
from tests.ingest.test_video_library_postgres import _DSN, db
from tests.ingest.test_video_library_settings_postgres import prepare

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def seed(conn: Any, repository: Any) -> None:
    prepare(conn, repository)
    conn.execute("UPDATE vl_videos SET advertiser='花王',campaign='サクセス',caption='care'")
    for video_id, campaign, caption, plays, origin in [
        ("2", "makeup", "red makeup", 100, "own"),
        ("3", "unknown", "未分類", 10000000, "own"),
        ("4", "private", "red secret", 9000000, "own"),
        ("5", "makeup", "blue makeup", 50, "own"),
        ("6", "makeup", "red external", 200, "external"),
        ("7", "", "red unassigned", 10, "own"),
    ]:
        conn.execute(
            """INSERT INTO vl_videos(platform,video_id,url,advertiser,campaign,caption,
               is_pr,origin,ingest_sources) VALUES ('tiktok',%s,%s,'花王',%s,%s,true,%s,%s)""",
            (
                video_id,
                f"https://www.tiktok.com/@test/video/{video_id}",
                campaign,
                caption,
                origin,
                ["sheet"] if origin == "own" else ["url_manual"],
            ),
        )
        conn.execute(
            """INSERT INTO vl_metrics(platform,video_id,captured_on,total_plays,total_saves)
               VALUES ('tiktok',%s,'2026-11-09',%s,1)""",
            (video_id, plays),
        )
    conn.execute("UPDATE vl_metrics SET total_plays=99999999 WHERE video_id='1'")
    for campaign in ("サクセス", "makeup", "unknown", "private"):
        conn.execute(
            """INSERT INTO vl_campaign_settings(advertiser,campaign,industry,is_private,updated_by)
               VALUES ('花王',%s,'化粧品・美容',%s,'admin')""",
            (campaign, campaign == "private"),
        )
    for video_id, category in [
        ("1", "haircare"), ("2", "makeup"), ("4", "skincare"),
        ("5", "makeup"), ("6", "makeup"), ("7", "makeup"),
    ]:
        products.save(
            conn, "tiktok", video_id,
            products.Product(product_category=category, brand="brand", evidence="商品パッケージ確認"),
            "admin@example.test",
        )


def test_cosmetics_excludes_haircare_unknown_and_private_independent_of_plays(db: Any) -> None:
    conn, repository = db
    seed(conn, repository)
    params = {"q": "化粧品", "sort": "relevance"}
    data = library.search_videos(conn, params, admin=False)
    assert {row["video_id"] for row in data["videos"]} == {"2", "5", "6", "7"}
    assert data["total"] == 4
    assert products.pending_count(conn, params, admin=False) == 1
    assert products.pending_count(conn, params, admin=True) == 1
    assert library.search_videos(conn, params, admin=True)["total"] == 5
    broad = library.search_videos(conn, {"industry": "化粧品・美容"}, admin=False)
    assert "1" in {row["video_id"] for row in broad["videos"]}
    detail = library.read_video(conn, "tiktok", "1", admin=False)
    assert detail["industry"] == "化粧品・美容"
    assert products.metadata(detail)["product_category"] == "haircare"
    assert not products.metadata(detail).get("evidence")


def test_case_counts_scope_missing_metrics_and_unassigned_videos(db: Any) -> None:
    conn, repository = db
    seed(conn, repository)
    params = {"q": "化粧品 red", "sort": "relevance"}
    data = products.search_cases(conn, params, admin=False, page_size=20)
    assert data["total"] == 2 and data["unassigned_video_count"] == 1
    own = next(row for row in data["cases"] if row["origin"] == "own")
    external = next(row for row in data["cases"] if row["origin"] == "external")
    assert own["matched_video_count"] == 1 and own["case_video_count"] == 2
    assert own["matched_total_plays"] == 100 and own["case_total_plays"] == 150
    assert external["matched_video_count"] == external["case_video_count"] == 1
    assert external["case_total_plays"] == 200
    assert own["product_categories"] == ["makeup"] and own["brands"] == ["brand"]
    conn.execute("UPDATE vl_metrics SET total_plays=NULL WHERE video_id='5'")
    own = next(
        row for row in products.search_cases(conn, params, admin=False, page_size=20)["cases"]
        if row["origin"] == "own"
    )
    assert own["matched_total_plays"] == 100 and own["case_total_plays"] is None
    conn.execute("UPDATE vl_metrics SET total_plays=NULL WHERE video_id='2'")
    own = next(
        row for row in products.search_cases(conn, params, admin=False, page_size=20)["cases"]
        if row["origin"] == "own"
    )
    assert own["matched_total_plays"] is None


def test_options_validation_and_private_values_do_not_leak(db: Any) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("UPDATE vl_campaign_settings SET purpose='secret-purpose' WHERE campaign='private'")
    assert products.options(conn, "purpose", "", admin=False)["options"] == []
    assert products.options(conn, "purpose", "", admin=True)["options"] == [
        {"value": "secret-purpose", "label": "secret-purpose"}
    ]
    products.validate_options(conn, {"industry": "化粧品・美容"}, admin=False)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        products.validate_options(conn, {"industry": "化粧品"}, admin=False)
    with pytest.raises(HTTPException):
        products.validate_options(conn, {"purpose": "secret-purpose"}, admin=False)


def test_review_can_be_cleared_and_employee_cannot_write(db: Any) -> None:
    conn, repository = db
    seed(conn, repository)
    products.save(conn, "tiktok", "2", products.Product(), "admin@example.test")
    data = library.search_videos(conn, {"q": "化粧品"}, admin=False)
    assert "2" not in {row["video_id"] for row in data["videos"]}
    assert products.pending_count(conn, {"q": "化粧品"}, admin=False) == 2
    conn.execute("SELECT set_config('app.user_role','viewer',true)")
    assert conn.execute(
        "UPDATE vl_video_analysis SET payload='{}' WHERE kind=%s", (products.KIND,)
    ).rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        products.save(
            conn, "tiktok", "3",
            products.Product(product_category="makeup", evidence="unauthorized"),
            "member@example.test",
        )


def test_relevance_precedes_popularity_and_explicit_plays_still_works(db: Any) -> None:
    conn, repository = db
    seed(conn, repository)
    conn.execute("UPDATE vl_videos SET campaign='red' WHERE video_id='2'")
    ranked = library.search_videos(conn, {"q": "red", "sort": "relevance"}, admin=False)
    assert ranked["videos"][0]["video_id"] == "2"
    plays = library.search_videos(conn, {"q": "red", "sort": "plays"}, admin=False)
    assert plays["videos"][0]["video_id"] == "6"
