"""Progressive HTML pagination: no-JS links, bounded grids and full case copy."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_library_catalog_review import LargeCatalogPg
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


class AutoloadPg(LargeCatalogPg):
    def __init__(self) -> None:
        super().__init__()
        self.case_videos = [
            {**v, "advertiser": self.row["advertiser"], "campaign": self.row["campaign"]}
            for v in self.videos[:150]
        ]
        self.cases[0]["video_count"] = len(self.case_videos)

    def execute(self, query: str, params: Any = None) -> MagicMock:
        result = super().execute(query, params)
        if (
            "SELECT v.*" in query
            and "v.advertiser = %s" in query
            and query.endswith("v.platform, v.video_id")
        ):
            result.fetchall.return_value = self.case_videos
        return result


@pytest.mark.parametrize(
    "path,size,last,last_size",
    [
        ("/library/videos", 50, 56, 50),
        ("/library/cases", 12, 5, 7),
    ],
)
def test_next_links_preserve_all_filters_and_end(
    setup: tuple[TestClient, Pg], path: str, size: int, last: int, last_size: int
) -> None:
    client, pg = setup
    pg.execute = AutoloadPg().execute  # type: ignore[method-assign]
    params = {
        "category": "organic",
        "q": 'a&b "<script>',
        "industry": "食品",
        "purpose": "理解",
        "pr": "no",
        "sort": "plays",
        "account": "account_1",
        "from": "2026-01-01",
    }
    first = client.get(path, params=params, cookies=_auth_cookie())
    soup = BeautifulSoup(first.text, "html.parser")
    assert first.status_code == 200
    assert len(soup.select("[data-items] > .card")) == size
    link = soup.select_one('a[rel="next"]')
    assert urlsplit(link["href"]).path == path
    assert parse_qs(urlsplit(link["href"]).query) == {
        k: [v] for k, v in {**params, "page": "2"}.items()
    }
    assert not soup.select_one('a[rel="prev"]')
    assert not soup.select_one(".pagination").has_attr("hidden")
    assert soup.select_one("[data-load-more]").has_attr("hidden")
    assert soup.select_one("[data-load-status]")["aria-live"] == "polite"
    assert all(card.select_one("a.card-link") for card in soup.select("[data-items] > .card"))
    final = BeautifulSoup(
        client.get(path, params={**params, "page": last}, cookies=_auth_cookie()).text,
        "html.parser",
    )
    assert len(final.select("[data-items] > .card")) == last_size
    assert not final.select('a[rel="next"]')
    assert parse_qs(urlsplit(final.select_one('a[rel="prev"]')["href"]).query)["page"] == [
        str(last - 1)
    ]
    if path.endswith("videos"):
        second = BeautifulSoup(
            client.get(path, params={**params, "page": 2}, cookies=_auth_cookie()).text,
            "html.parser",
        )
        assert soup.select_one(".card-link")["href"] != second.select_one(".card-link")["href"]


def test_case_grid_pages_fifty_but_copy_includes_every_video(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    large = AutoloadPg()
    pg.execute = large.execute  # type: ignore[method-assign]
    params = {"advertiser": 'a & " <script>', "campaign": "case / +"}
    for number in (1, 2, 3):
        response = client.get(
            "/library/c?" + urlencode({**params, "page": number}), cookies=_auth_cookie()
        )
        soup = BeautifulSoup(response.text, "html.parser")
        assert response.status_code == 200
        cards = soup.select("[data-items] > .card")
        assert len(cards) == 50
        assert cards[0].a["href"].endswith(str(10000 + (number - 1) * 50))
        next_link = soup.select_one('a[rel="next"]')
        if number < 3:
            assert parse_qs(urlsplit(next_link["href"]).query) == {
                k: [v] for k, v in {**params, "page": str(number + 1)}.items()
            }
        else:
            assert next_link is None
        assert len(soup.select_one("#campaign-urls").text.splitlines()) == 150
        summary = soup.select_one("#campaign-copy").text
        assert "動画: 150本" in summary and large.case_videos[-1]["url"] in summary
        assert soup.select_one("[data-autoload]")["data-total"] == "150"
    assert len(large.calls) == 6  # Existing aggregate + full copy query per request.


@pytest.mark.parametrize(
    "path", ["/library/videos", "/library/cases", "/library/c?advertiser=a&campaign=b"]
)
@pytest.mark.parametrize("number", ["0", "-1", "1000001", "bad"])
def test_autoload_page_limit_stays_validated(
    setup: tuple[TestClient, Pg], path: str, number: str
) -> None:
    client, _ = setup
    separator = "&" if "?" in path else "?"
    assert (
        client.get(path + separator + "page=" + number, cookies=_auth_cookie()).status_code == 400
    )
