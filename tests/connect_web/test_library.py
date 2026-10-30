"""Library uses real cookie verification and response headers, without external I/O."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from teamagent.connect_web.app import _safe_next, create_app
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config, _verifier_ok


class Pg:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.sessions: list[Any] = []
        self.row: dict[str, Any] = {
            "platform": "tiktok",
            "video_id": "123",
            "url": "https://www.tiktok.com/@sample/video/123",
            "account_name": '<script>alert("account")</script>',
            "account_handle": "sample",
            "caption": "<img src=x onerror=alert(1)> #PR",
            "hashtags": ["PR"],
            "advertiser": "advertiser",
            "campaign": "campaign",
            "business_category": "media",
            "industry": None,
            "purpose": None,
            "is_pr": True,
            "is_private": False,
            "posted_at": datetime(2026, 10, 30, tzinfo=UTC),
            "organic_plays": 4000,
            "organic_saves": 40,
            "organic_likes": None,
            "organic_comments": 2,
            "organic_shares": 3,
            "total_plays": 17800,
            "total_saves": 178,
            "thumbnail_s3_key": "video-library/thumbs/tiktok/123.jpg",
            "video_s3_key": "secret-key",
        }

    @contextmanager
    def connection(self, **kwargs: Any) -> Any:
        self.sessions.append(kwargs)
        yield self

    def execute(self, query: str, params: Any = None) -> MagicMock:
        self.calls.append((query, params))
        result = MagicMock()
        result.fetchone.return_value = {"n": 1}
        result.fetchall.return_value = []
        if "SELECT v.*" in query:
            result.fetchone.return_value = self.row.copy()
            result.fetchall.return_value = [self.row.copy()]
        elif "SELECT * FROM vl_business_categories" in query:
            result.fetchall.return_value = [
                {"code": "media", "display_name": "メディア"},
                {"code": "organic", "display_name": "オーガニック"},
            ]
        elif "FROM vl_campaigns c" in query:
            result.fetchall.return_value = [self.row.copy()]
        elif "SELECT * FROM vl_account_settings" in query:
            result.fetchall.return_value = [
                {
                    "platform": "tiktok",
                    "handle": "sample",
                    "display_name": "<script>bad</script>",
                    "note": "<b>note</b>",
                }
            ]
        return result


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Pg]:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL)
    pg = Pg()
    return TestClient(
        create_app(search_config=_config(), search_verifier=_verifier_ok, admin_pg=pg)
    ), pg


@pytest.mark.parametrize(
    "path",
    [
        "/library",
        "/library/api/videos",
        "/library/v/tiktok/123",
        "/library/thumb/tiktok/123",
        "/library/admin/accounts",
        "/library/admin/campaigns",
    ],
)
def test_all_routes_require_cookie(setup: tuple[TestClient, Pg], path: str) -> None:
    client, pg = setup
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/search/login?next=%2Flibrary")
    assert not pg.calls


@pytest.mark.parametrize("path", ["/library/admin/campaigns", "/library/admin/accounts"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_admin_gate(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch, path: str, method: str
) -> None:
    client, pg = setup
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "other@example.test")
    response = getattr(client, method)(path, cookies=_auth_cookie())
    assert response.status_code == 403
    assert not pg.calls


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/library",
        "//evil.test/library",
        "///library",
        "///library/admin/accounts",
        "/libraryevil",
        "/library/../app",
        "/library/%2e%2e/app",
        "/library/\\evil",
        "/library?x=%0d%0aLocation:evil",
        "/library//evil",
        "/library#evil",
    ],
)
def test_next_rejects_redirect_tricks(target: str) -> None:
    assert library.library_target(target) is None
    assert _safe_next(target) == "/app"


def test_login_returns_to_library_with_query(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    target = "/library/v/tiktok/123?from=2026-01-01"
    page = client.get("/search/login", params={"next": target})
    assert f'name="next" value="{target}"' in page.text
    response = client.post(
        "/search/auth/verify", data={"credential": "ok", "next": target}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == target
    assert "ta_search_session=" in response.headers["set-cookie"]


def test_listing_csp_escaping_copy_and_single_player(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    response = client.get("/library", cookies=_auth_cookie())
    assert response.status_code == 200
    assert "https://www.tiktok.com" in response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert (
        "https://www.tiktok.com"
        not in client.get("/search/login").headers["content-security-policy"]
    )
    assert '<script>alert("account")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    assert 'data-url="https://www.tiktok.com/@sample/video/123"' in response.text
    assert "input[data-url]:checked" in response.text
    assert "active.remove()" in response.text and "mouseleave" in response.text
    assert (
        'data-player="https://www.tiktok.com/player/v1/123?autoplay=1&amp;muted=1' in response.text
    )
    assert "再生 17,800（オーガニック 4,000）" in response.text
    assert pg.sessions[0]["app_role"] == "teamagent_app"
    assert not any("FROM vl_videos " in query for query, _ in pg.calls)


def test_detail_metrics_are_separate_and_caption_escaped(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert response.status_code == 200
    assert "<img src=x onerror" not in response.text
    assert "&lt;img src=x onerror" in response.text
    for text in ("オーガニック", "総計", "広告", "近日公開", "算出不可", "参考値", "17,800"):
        assert text in response.text


def test_search_api_matches_listing_and_hides_s3_keys(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = setup
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "other@example.test")
    query = {
        "q": "案件' OR true--",
        "category": "media",
        "pr": "yes",
        "from": "2026-10-01",
        "to": "2026-10-30",
        "account": "sample",
        "sort": "saves",
        "page": "2",
    }
    response = client.get("/library/api/videos", params=query, cookies=_auth_cookie())
    assert response.status_code == 200
    payload = response.json()
    assert payload["page_size"] == 50
    row = payload["videos"][0]
    assert row["rates"] == {"save_rate": 0.01, "engagement_rate": None, "is_reference": False}
    assert "thumbnail_s3_key" not in row and "video_s3_key" not in row
    assert row["has_thumbnail"] is True
    assert "m.total_plays" in pg.calls[1][0] and "m.total_saves" in pg.calls[1][0]
    api_calls = pg.calls.copy()
    client.get("/library", params=query, cookies=_auth_cookie())
    assert pg.calls[len(api_calls) : len(api_calls) + 2] == api_calls[:2]
    sql, params = api_calls[1]
    assert "NOT v.is_private" in sql and "vl_videos_effective" in sql
    assert "OR true--" not in sql
    assert params[-2:] == [50, 50]
    assert params[0] == "%案件' OR true--%"
    assert params[4].isoformat() == "2026-10-31T00:00:00+09:00"


@pytest.mark.parametrize(
    "query",
    [
        {"sort": "injected"},
        {"page": "-1"},
        {"from": "bad"},
        {"to": "9999-12-31"},
        {"pr": "bad"},
        {"q": "x" * 501},
    ],
)
def test_invalid_search_is_400(setup: tuple[TestClient, Pg], query: dict[str, str]) -> None:
    client, _ = setup
    assert (
        client.get("/library/api/videos", params=query, cookies=_auth_cookie()).status_code == 400
    )


def test_thumbnail_cache_and_authenticated_304(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = setup
    storage = MagicMock()
    storage.get.return_value = (b"jpeg", '"etag-1"')
    monkeypatch.setattr(library, "VideoLibraryStorage", lambda: storage)
    response = client.get("/library/thumb/tiktok/123", cookies=_auth_cookie())
    assert response.content == b"jpeg"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["etag"] == '"etag-1"'
    cached = client.get(
        "/library/thumb/tiktok/123", cookies=_auth_cookie(), headers={"If-None-Match": '"etag-1"'}
    )
    assert cached.status_code == 304 and cached.content == b""
    assert (
        client.get(
            "/library/thumb/tiktok/123",
            headers={"If-None-Match": '"etag-1"'},
            follow_redirects=False,
        ).status_code
        == 303
    )
    storage.get.return_value = None
    assert client.get("/library/thumb/tiktok/123", cookies=_auth_cookie()).status_code == 404


@pytest.mark.parametrize(
    "path,form",
    [
        (
            "campaigns",
            {"advertiser": "a", "campaign": "b", "business_category": "media", "is_private": "1"},
        ),
        (
            "accounts",
            {"handle": "https://www.tiktok.com/@Example", "display_name": "sample", "note": "memo"},
        ),
    ],
)
def test_admin_forms_require_origin_and_save_as_admin(
    setup: tuple[TestClient, Pg], path: str, form: dict[str, str]
) -> None:
    client, pg = setup
    url = f"/library/admin/{path}"
    assert client.post(url, data=form, cookies=_auth_cookie()).status_code == 403
    assert (
        client.post(
            url, data=form, cookies=_auth_cookie(), headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(url, data=form, cookies=_auth_cookie(), headers={"Origin": "null"}).status_code
        == 403
    )
    assert not pg.calls
    response = client.post(
        url,
        data=form,
        cookies=_auth_cookie(),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert pg.sessions[-1]["user_role"] == "admin"
    sql, params = pg.calls[-1]
    assert "INSERT INTO vl_" in sql and "ON CONFLICT" in sql
    if path == "accounts":
        assert params[0] == "example" and "fetch_enabled" not in sql
    else:
        assert params[-2:] == (True, _EMAIL)


def test_admin_pages_escape_and_count_orphan_pr(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    response = client.get("/library/admin/campaigns", cookies=_auth_cookie())
    assert "#PR ありで案件の無い動画: 1本" in response.text
    assert response.text.index("#PR ありで案件の無い動画:") < response.text.index("<section>")
    assert 'value="organic"' not in response.text
    assert 'value="media" selected' in response.text
    response = client.get("/library/admin/accounts", cookies=_auth_cookie())
    assert "&lt;script&gt;bad" in response.text and "&lt;b&gt;note" in response.text


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.test/@name",
        "https://www.tiktok.com.evil/@name",
        "https://u:p@www.tiktok.com/@name",
        "https://www.tiktok.com/@name/video/123",
        "@bad/name",
        "a" * 25,
    ],
)
def test_bad_account_url(value: str) -> None:
    with pytest.raises(HTTPException):
        library.parse_handle(value)


def test_copy_excludes_untrusted_url(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.row["url"] = "javascript:alert(1)"
    response = client.get("/library", cookies=_auth_cookie())
    assert 'data-url="javascript:' not in response.text
    assert 'data-player="' not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/library",
        "/library/v/tiktok/123",
        "/library/admin/campaigns",
        "/library/admin/accounts",
        "/library/api/videos",
    ],
)
def test_library_referrer_policy_is_same_origin_only(
    setup: tuple[TestClient, Pg], path: str
) -> None:
    client, _ = setup
    assert client.get(path, cookies=_auth_cookie()).headers["referrer-policy"] == "same-origin"
    for other in ("/search/login", "/search", "/library-other"):
        assert client.get(other, cookies=_auth_cookie()).headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("organic_plays", 17800.0, "17,800"),
        ("total_likes", 0.0, "0"),
        # sorted: frozenset の順はプロセスごとに変わり、xdist の worker 間で収集がずれる
        *[(field, 0.685, "68.5%") for field in sorted(library.RATE_METRICS)],
        ("ad_cost", 1185.0, "¥1,185"),
        ("ad_average_watch_seconds", 9.05, "9.05秒"),
        *[(field, None, "算出不可") for field in library.METRIC_COLUMNS],
    ],
)
def test_metric_display_units(field: str, value: float | None, expected: str) -> None:
    assert library._metric(value, field) == expected


def test_detail_uses_formatted_metrics_and_paid_labels(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.row.update(
        ad_rate_2s=0.685,
        ad_ctr=0.012,
        ad_cost=1185.0,
        ad_average_watch_seconds=9.05,
        ad_likes=1234.0,
        ad_shares=12.0,
        ad_comments=3.0,
        ad_saves=45.0,
    )
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    for label, value in (
        ("2秒視聴率", "68.5%"),
        (library.METRIC_COLUMNS["ad_ctr"], "1.2%"),
        (library.METRIC_COLUMNS["ad_cost"], "¥1,185"),
        (library.METRIC_COLUMNS["ad_average_watch_seconds"], "9.05秒"),
        ("有料いいね", "1,234"),
        ("有料シェア", "12"),
        ("有料コメント", "3"),
        ("有料保存", "45"),
    ):
        assert f"<th>{label}</th><td>{value}</td>" in response.text
    assert "有料のお気に入り" not in response.text


@pytest.mark.parametrize("organic", [5535.0, None])
def test_card_uses_total_plays_and_save_rate(
    setup: tuple[TestClient, Pg], organic: float | None
) -> None:
    client, pg = setup
    pg.row.update(organic_plays=organic, total_plays=17800.0, total_saves=356.0)
    response = client.get("/library", cookies=_auth_cookie())
    expected = "再生 17,800" + ("（オーガニック 5,535）" if organic is not None else "")
    assert expected + " · 保存率 2.00%" in response.text
    assert "再生・保存率・ENG率と並び順は総計の最新数値" in response.text
    if organic is None:
        assert "（オーガニック " not in response.text
    assert "17,800.0" not in response.text


@pytest.mark.parametrize("present", [False, True])
def test_missing_thumbnail_has_only_gray_frame(setup: tuple[TestClient, Pg], present: bool) -> None:
    client, pg = setup
    pg.row["thumbnail_s3_key"] = "video-library/thumbs/tiktok/123.jpg" if present else None
    response = client.get("/library", cookies=_auth_cookie())
    assert ('<img loading="lazy"' in response.text) is present
    assert 'alt="サムネイル"' not in response.text
    assert 'class="preview"' in response.text
    assert 'aria-label="投稿を開く"' in response.text
    assert ('src="/library/thumb/tiktok/123"' in response.text) is present


def test_campaign_rejects_organic_even_if_category_exists(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    response = client.post(
        "/library/admin/campaigns",
        data={"advertiser": "a", "campaign": "b", "business_category": "organic"},
        cookies=_auth_cookie(),
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 400
    assert not any("INSERT" in query for query, _ in pg.calls)


@pytest.mark.parametrize("count", [0, 389])
def test_orphan_pr_count_is_visible_without_campaign_rows(
    setup: tuple[TestClient, Pg], count: int
) -> None:
    client, pg = setup
    original = pg.execute

    def execute(query: str, params: Any = None) -> MagicMock:
        result = original(query, params)
        if "FROM vl_campaigns c" in query:
            result.fetchall.return_value = []
        if "count(*)" in query:
            result.fetchone.return_value = {"n": count}
        return result

    pg.execute = execute  # type: ignore[method-assign]
    response = client.get("/library/admin/campaigns", cookies=_auth_cookie())
    assert f"#PR ありで案件の無い動画: {count}本" in response.text
