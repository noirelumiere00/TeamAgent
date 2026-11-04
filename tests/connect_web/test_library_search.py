"""Shared search interpretation and the header-to-video search journey."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_library_catalog import case_row
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("サラヤ #PR", [1]),
        ("サラヤ　＃ＰＲ", [1]),
        ("サラヤ #pr", [1]),
        ("サラヤ ラカント", [1, 2]),
        ("#白髪ケア", [3]),
        ("＃白髪ケア", [3]),
        ("PR広告", [4]),
        ("#PR広告", [4]),
        ("サラヤ ラカント #PR", [1]),
        ("サラヤ' OR true--", []),
    ],
)
def test_search_words_execute_as_and_and_pr(query: str, expected: list[int]) -> None:
    # Execute the generated predicate; adapt only PostgreSQL syntax/functions to SQLite.
    with sqlite3.connect(":memory:") as conn:
        conn.create_function("concat_ws", -1, lambda sep, *args: sep.join(x for x in args if x))
        conn.create_function("array_to_string", 2, lambda raw, sep: sep.join(json.loads(raw)))
        conn.execute("""CREATE TABLE videos (id INTEGER, advertiser TEXT, campaign TEXT,
            caption TEXT, hashtags TEXT, is_pr BOOLEAN, is_private BOOLEAN,
            account_name TEXT, account_handle TEXT, industry TEXT, purpose TEXT)""")
        conn.executemany(
            "INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            [
                (1, "サラヤ", "ラカント", "おいしい", "[]", True, False),
                (2, "サラヤ", "ラカント", "おいしい PR", '["PR"]', False, False),
                (3, "他社", "ヘアケア", "使ってみた", '["白髪ケア", "PR"]', True, False),
                (4, "サラヤ", "別の商品", "PR広告", "[]", False, False),
                (5, "サラヤ", "ラカント", "非公開", "[]", True, True),
            ],
        )
        where, values, _, _ = library._where({"q": query}, admin=False)
        sql = "SELECT id FROM videos v WHERE " + where.replace("ILIKE", "LIKE").replace("%s", "?")
        assert [row[0] for row in conn.execute(sql, values)] == expected
        assert query not in where


def test_search_limits_and_pr_override() -> None:
    assert library.parse_search({"q": " ".join(["語"] * 10)}) == (["語"] * 10, None)
    assert library.parse_search({"q": "Ｘ" * 500}) == (["X" * 500], None)
    assert library.parse_search({"q": "#PR", "pr": "no"}) == ([], True)
    assert library.parse_search({"q": "PR広告", "pr": "no"}) == (["PR広告"], False)
    assert library.parse_search({"q": " # \t\n "}) == ([], None)
    for query in (" ".join(["語"] * 11), "x" * 501, "㍿" * 126):
        with pytest.raises(HTTPException) as error:
            library.parse_search({"q": query})
        assert error.value.status_code == 400


@pytest.mark.parametrize("path", ["/library/videos", "/library/cases", "/library/api/videos"])
def test_all_search_routes_share_parameters_and_validation(
    setup: tuple[TestClient, Pg], path: str
) -> None:
    client, pg = setup
    response = client.get(path, params={"q": "サラヤ　＃ＰＲ"}, cookies=_auth_cookie())
    assert response.status_code == 200
    for sql, values in pg.calls:
        assert "サラヤ" not in sql and "＃ＰＲ" not in sql
        assert "v.is_pr = %s" in sql
        assert values[:2] == ["%サラヤ%", True]
    assert (
        client.get(path, params={"q": " ".join(["語"] * 11)}, cookies=_auth_cookie()).status_code
        == 400
    )
    client.cookies.clear()
    assert client.get(path, params={"q": "#PR"}, follow_redirects=False).status_code in {
        302,
        303,
        307,
    }


def test_combined_results_keep_video_interactions_and_next_query(
    setup: tuple[TestClient, Pg],
) -> None:
    client, pg = setup
    original = pg.execute

    def execute(query: str, params: Any = None) -> MagicMock:
        result = original(query, params)
        if "GROUP BY v.advertiser" in query:
            result.fetchall.return_value = [case_row(pg) for _ in range(6)]
        elif "count(*) AS n" in query:
            result.fetchone.return_value = {"n": 123}
        return result

    pg.execute = execute  # type: ignore[method-assign]
    query = {"q": "サラヤ #PR", "sort": "saves", "category": "media", "pr": "no"}
    response = client.get("/library/videos", params=query, cookies=_auth_cookie())
    soup = BeautifulSoup(response.text, "html.parser")
    assert response.status_code == 200
    assert soup.select_one("form.workspace")["action"] == "/library/videos"
    assert soup.select_one(".search-summary").text == "「サラヤ」に一致するPR投稿 123件"
    assert soup.select_one('select[name="pr"] option[selected]')["value"] == "yes"
    assert len(soup.select(".campaign-grid .card-link")) == 4
    assert soup.select_one("main").text.index("一致した事例") < soup.select_one("main").text.index(
        "一致した動画"
    )
    assert soup.select_one(".video-grid .card-link")["href"] == "/library/v/tiktok/123"
    assert soup.select_one('.video-grid img[loading="lazy"]')["src"] == "/library/thumb/tiktok/123"
    assert soup.select_one(".video-grid [data-player]") is not None
    assert soup.select_one(".video-grid input[data-url]")["type"] == "checkbox"
    assert soup.select_one("button#copy") is not None
    next_url = soup.select_one('[data-autoload] a[rel="next"]')["href"]
    assert parse_qs(urlsplit(next_url).query) == {
        **{k: [v] for k, v in query.items()},
        "page": ["2"],
    }
    assert len(pg.calls) == 3  # count + videos + one case aggregate, never N+1
    assert "m.total_saves / m.total_plays" in pg.calls[1][0]
    assert all(values[:3] == ["%サラヤ%", "media", True] for _, values in pg.calls)
    assert "https://www.tiktok.com" in response.headers["content-security-policy"]
    assert all(tag.get("nonce") for tag in soup.select("script, style"))
    pg.calls.clear()
    soup = BeautifulSoup(client.get(next_url, cookies=_auth_cookie()).text, "html.parser")
    assert not soup.select("#matching-cases") and len(pg.calls) == 2


@pytest.mark.parametrize("catalog", [False, True])
def test_search_interpretation_escaping_and_empty_guidance(catalog: bool) -> None:
    soup = BeautifulSoup(
        library._search_summary({"q": "<script> ラカント ＃PR"}, 0, catalog=catalog),
        "html.parser",
    )
    assert soup.select_one("script") is None
    text = soup.text
    for expected in (
        "<script> / ラカント",
        "すべての語を含む",
        "PR投稿",
        "0件",
        "語を減らす",
        "#PR を外す",
    ):
        assert expected in text
    assert ("を含む事例" in text) == catalog
    assert "#PR を外す" not in library._search_summary({"q": "サラヤ"}, 0)
    assert "#PR を外す" not in library._search_summary({"q": "サラヤ #PR"}, 1)


@pytest.mark.parametrize("path", ["/library/videos", "/library/cases"])
def test_empty_search_page_guidance(setup: tuple[TestClient, Pg], path: str) -> None:
    client, pg = setup
    pg.execute = MagicMock()  # type: ignore[method-assign]
    pg.execute.return_value.fetchone.return_value = {"n": 0}
    pg.execute.return_value.fetchall.return_value = []
    soup = BeautifulSoup(
        client.get(path, params={"q": "サラヤ #PR"}, cookies=_auth_cookie()).text, "html.parser"
    )
    assert "0件" in soup.select_one(".search-summary").text
    assert "語を減らす" in soup.text and "#PR を外す" in soup.text
    assert not soup.select("#matching-cases")


def test_active_filter_labels_are_human_readable() -> None:
    from teamagent.connect_web import library

    html = library._workspace("", {"pr": "yes", "industry": "食品"}, current="videos", nav="")
    assert "PR投稿のみ" in html and "業種: 食品" in html
    assert "絞り込み中: yes" not in html


def test_placeholder_is_hidden_from_screen_readers_only_under_a_thumbnail() -> None:
    from teamagent.connect_web import library

    row = {"platform": "tiktok", "video_id": "1", "account_name": "a"}
    assert 'aria-hidden="true"' not in library._cover(row)
    assert 'class="placeholder" aria-hidden="true"' in library._cover(
        {**row, "thumbnail_s3_key": "video-library/thumbs/tiktok/1.jpg"}
    )
