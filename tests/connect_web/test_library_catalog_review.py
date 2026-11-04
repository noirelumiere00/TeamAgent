"""Regression contracts from the 55-case / 2,800-video UI review."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_library_catalog import case_row
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


class LargeCatalogPg(Pg):
    """Synthetic read responses, including real-sized page/case result sets."""

    def __init__(self) -> None:
        super().__init__()
        codes = [None] * 30 + ["organic"] * 18 + ["media"] * 3 + ["tto"] * 2 + ["kirinuki", "other"]
        self.videos = [
            {
                **self.row,
                "video_id": str(10000 + i),
                "url": f"https://www.tiktok.com/@kana_zubora_chie/video/{10000 + i}",
                "account_name": f"kana_zubora_chie_{i % 40:02}",
                "account_handle": f"account_{i % 40}",
                "advertiser": f"サンプル企業 {i % 55 + 1:02}",
                "campaign": f"暮らしに役立つ提案 {i % 55 + 1:02}",
                "thumbnail_s3_key": "preview" if i % 10 else None,
                "business_category": codes[i % 55],
                "total_plays": 2_800_000 - i * 500,
                "total_saves": 28000 - i * 5,
                "posted_at": self.row["posted_at"] - timedelta(days=54 - i % 55) if i % 3 else None,
                "caption": "毎日のちょっとした工夫をご紹介。" if i % 3 else "",
                "industry": "食品" if i % 2 else "日用品",
                "purpose": "認知",
            }
            for i in range(2800)
        ]
        self.cases = []
        for i in range(55):
            members = self.videos[i::55]
            self.cases.append(
                {
                    **case_row(self),
                    **{k: members[0][k] for k in ("advertiser", "campaign", "industry", "purpose")},
                    "video_count": len(members),
                    "total_plays": sum(v["total_plays"] for v in members),
                    "total_saves": sum(v["total_saves"] for v in members),
                    "categories": [codes[i]],
                    "primary_category": codes[i],
                    "first_posted": self.row["posted_at"] - timedelta(days=84 - i),
                    "last_posted": self.row["posted_at"] - timedelta(days=54 - i),
                    "representative": next(v.copy() for v in members if v["thumbnail_s3_key"]),
                }
            )
        self.row = self.videos[0]

    def execute(self, query: str, params: Any = None) -> MagicMock:
        result = super().execute(query, params)
        if "SELECT * FROM vl_business_categories" in query:
            result.fetchall.return_value = [
                {"code": code, "display_name": label}
                for code, label in library.CATEGORY_LABELS.items()
            ]
        elif "GROUP BY v.advertiser" in query:
            result.fetchall.return_value = (
                self.cases[:1] if "v.advertiser = %s" in query else self.cases
            )
        elif "ranked AS (" in query:
            result.fetchall.return_value = self.videos[:240]
        elif "SELECT count(*) AS n" in query:
            result.fetchone.return_value = {"n": len(self.videos)}
        elif "AND NOT (v.platform=%s" in query:
            result.fetchall.return_value = self.videos[55::55][:6]
        elif "SELECT v.*" in query and "v.advertiser = %s" in query:
            result.fetchall.return_value = self.videos[::55]
        elif "SELECT v.*" in query and "LIMIT %s OFFSET %s" in query:
            offset, size = params[-1], params[-2]
            result.fetchall.return_value = [v.copy() for v in self.videos[offset : offset + size]]
        return result


def css_rule(selector: str) -> str:
    match = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*\{([^}]+)\}", library._STYLE)
    assert match is not None, selector
    return match[1]


def test_case_listing_is_paginated_with_large_catalog(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    large = LargeCatalogPg()
    pg.execute = large.execute  # type: ignore[method-assign]
    for query, count in (("", 12), ("?page=5", 7), ("?q=暮らし", 12)):
        soup = BeautifulSoup(
            client.get("/library/cases" + query, cookies=_auth_cookie()).text, "html.parser"
        )
        assert len(soup.select(".campaign-grid .card")) == count
        assert "55件の事例" in soup.text
        assert soup.select_one(".pagination a") is not None
    soup = BeautifulSoup(client.get("/library/videos", cookies=_auth_cookie()).text, "html.parser")
    assert "2,800件" in soup.text and len(soup.select(".video-grid .card")) == 50
    assert soup.find("a", string="次へ") is not None


def test_campaign_and_video_cards_share_portrait_preview() -> None:
    assert "aspect-ratio:9/16" in css_rule(".preview")
    assert ".campaign-grid .preview" not in library._STYLE


def test_placeholder_is_quiet_and_clear_of_detail_play_button() -> None:
    row = LargeCatalogPg().row
    soup = BeautifulSoup(library._preview(row, detail=True), "html.parser")
    assert soup.select_one(".placeholder-account").text == row["account_name"]
    assert soup.select_one(".placeholder-account")["title"] == row["account_name"]
    font = re.search(r"font-size:(\d+)px", css_rule(".placeholder strong"))
    assert font is not None and int(font[1]) <= 32
    assert "justify-content:flex-start" in css_rule(".detail-player .placeholder")
    assert "padding-top:24px" in css_rule(".detail-player .placeholder")
    assert soup.select_one("button[data-play]") is not None
    assert soup.select_one(".placeholder button") is None


def test_account_names_are_single_line_with_escaped_full_title(
    setup: tuple[TestClient, Pg],
) -> None:
    client, pg = setup
    name = 'kana_zubora_chie_<img src=x>"'
    pg.row["account_name"] = name
    for path in ("/library/videos", "/library/v/tiktok/123"):
        soup = BeautifulSoup(client.get(path, cookies=_auth_cookie()).text, "html.parser")
        label = soup.select_one(".account-name")
        assert label.text == name and label["title"] == name
        assert label.select_one("img") is None
    for rule in ("white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis"):
        assert rule in css_rule(".account-name")


@pytest.mark.parametrize("caption", [None, "", " \n "])
def test_absent_date_and_caption_do_not_render_empty_metadata(
    setup: tuple[TestClient, Pg], caption: str | None
) -> None:
    client, pg = setup
    pg.row.update(posted_at=None, caption=caption, is_pr=False, is_private=False)
    listing = BeautifulSoup(
        client.get("/library/videos", cookies=_auth_cookie()).text, "html.parser"
    )
    assert listing.select_one(".card .period") is None
    detail = BeautifulSoup(
        client.get("/library/v/tiktok/123", cookies=_auth_cookie()).text, "html.parser"
    )
    assert "不明" not in detail.select_one(".detail-meta").text
    assert detail.select_one(".posted-date") is None
    assert detail.find("h3", string="投稿文") is None
    pg.row["is_pr"] = True
    card = BeautifulSoup(library._video_card(pg.row), "html.parser")
    assert card.select_one(".card-context").text == "MEDIA · PR · campaign"
    assert "不明" not in card.text


def test_filters_keep_search_and_sort_outside_disclosure(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    for path in ("/library/cases", "/library/videos"):
        response = client.get(
            path, params={"industry": "食品", "purpose": "認知"}, cookies=_auth_cookie()
        )
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.select_one("form.filters")
        details = form.select_one("details.sidebar")
        assert details.has_attr("open")  # Usable without JS; enhanced mobile view folds it.
        assert details.summary.text == "条件を指定"
        assert form.select_one('[name="q"]').find_parent("details") is None
        assert form.select_one('[name="sort"]').find_parent("details") is None
        assert details.select_one('[name="pr"]') is not None
        for field, value in (("industry", "食品"), ("purpose", "認知")):
            assert details.select_one(f'input[name="{field}"]')["value"] == value
            assert not details.select(f'select[name="{field}"], datalist')
        assert details.select_one('[type="submit"]') is not None
        assert "業種: 食品 / 目的: 認知" in soup.select_one(".active-filters").text
