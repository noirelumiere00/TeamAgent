"""External intake, employee edits, shared shelf and additive API fields, offline."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

from teamagent.connect_web.library import (
    _campaign_totals_cte,
    _post_badges,
    _where,
    archive_home,
    recent_imports,
    search_campaigns,
)
from tests.connect_web.test_library import Pg
from tests.connect_web.test_library_import import ORIGIN, ImportPg, intake, post
from tests.connect_web.test_search_routes import _EMAIL
from tests.ingest.test_video_library_url_import import URL

__all__ = ["intake"]


def test_intake_default_external_and_own_choice_survives_retry(
    intake: tuple[Any, ImportPg],
) -> None:
    client, pg = intake
    soup = BeautifulSoup(client.get("/library/import").text, "html.parser")
    assert (
        soup.select_one('select[name="requested_origin"] option[selected]')["value"] == "external"
    )
    assert all(el.has_attr("hidden") for el in soup.select("[data-own-campaign]"))
    assert post(client, action="submit", requested_origin="bad").status_code == 400
    response = post(client, action="submit", requested_origin="own")
    assert response.status_code == 303
    batch = next(iter(pg.batches.values()))
    assert batch["requested_origin"] == "own"
    assert post(client, action="submit", requested_origin="external").status_code == 409
    pg.rows[batch["batch_id"]][0]["status"] = "failed"
    response = client.post(
        response.headers["location"] + "/retry",
        headers=ORIGIN,
        data={"idempotency_key": "retry"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert list(pg.batches.values())[-1]["requested_origin"] == "own"


def test_external_intake_ignores_campaign_and_own_reveals_it(intake: tuple[Any, ImportPg]) -> None:
    client, pg = intake
    assert post(client, action="submit", case='["forged","pair"]').status_code == 303
    assert next(iter(pg.batches.values()))["advertiser"] is None
    soup = BeautifulSoup(post(client, requested_origin="own").text, "html.parser")
    assert all(not el.has_attr("hidden") for el in soup.select("[data-own-campaign]"))


@pytest.mark.parametrize(
    "pr,ad,external_case,label",
    [
        (True, False, True, "他社事例"),
        (False, True, True, "他社事例"),
        (False, False, False, "一般投稿"),
    ],
)
def test_external_badges_and_filter(pr: bool, ad: bool, external_case: bool, label: str) -> None:
    badges = _post_badges(
        dict(
            origin="external",
            is_pr=pr,
            page_is_ad=ad,
            is_external_case=external_case,
            advertiser="brand",
            campaign="legacy",
        )
    )
    assert label in badges and "案件未設定" not in badges
    assert "他社事例" not in _post_badges(dict(origin="own", is_pr=True))
    assert "v.is_external_case" in _where({"external_case": "yes"}, False)[0]
    assert "NOT v.is_external_case" in _where({"general": "yes"}, False)[0]


def test_own_case_queries_exclude_external_even_with_campaign() -> None:
    conn = MagicMock()
    search_campaigns(conn, {}, admin=False)
    assert "v.origin='own'" in conn.execute.call_args.args[0]
    assert "v.origin='own'" in _campaign_totals_cte(False)


def test_recent_imports_shared_order_limit_and_empty_shelf() -> None:
    conn = MagicMock()
    recent_imports(conn, admin=False)
    sql, values = conn.execute.call_args.args
    assert "requested_by" not in sql
    assert "ORDER BY recent.imported_at DESC" in sql and "LIMIT 12" in sql
    assert "NOT v.is_private" in sql and values == []
    assert "最近取り込んだ動画" not in archive_home([], [], imported=[])
    row = ImportPg().row
    html = archive_home([], [], imported=[row] * 13)
    soup = BeautifulSoup(html, "html.parser")
    shelf = soup.select_one("#recent-imports")
    assert len(shelf.select(".card")) == 12
    assert shelf.select_one('a[href="/library/videos?url_import=yes"]')


def test_employee_switch_and_advertiser_edit_require_origin_and_record_actor(
    intake: tuple[Any, ImportPg],
) -> None:
    client, pg = intake
    pg.execute = lambda query, params=None: Pg.execute(pg, query, params)
    pg.row.update(origin="external", external_advertiser="MOFT", is_external_case=True)
    html = client.get("/library/v/tiktok/123").text
    assert "他社の広告主: MOFT" in html and "自社の案件にする" in html
    assert "の事例を見る" not in html
    for suffix, data in [
        ("origin", {"origin": "own", "decision_id": str(uuid.uuid4())}),
        ("advertiser", {"advertiser": "変更後"}),
    ]:
        path = f"/library/v/tiktok/123/{suffix}"
        for headers in ({}, {"origin": "https://evil.test"}):
            before = len(pg.calls)
            assert client.post(path, data=data, headers=headers).status_code == 403
            assert len(pg.calls) == before
        response = client.post(path, data=data, headers=ORIGIN, follow_redirects=False)
        assert response.status_code == 303
        _query, params = next((q, p) for q, p in reversed(pg.calls) if "INSERT INTO" in q)
        assert params[-1] == _EMAIL
    assert any("vl_video_origin_decisions" in q for q, _ in pg.calls)
    assert any("external-v1" in q for q, _ in pg.calls)
    assert (
        client.post(
            "/library/v/tiktok/123/origin", headers=ORIGIN, data={"origin": "invalid"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/library/v/tiktok/123/advertiser", headers=ORIGIN, data={"advertiser": "a" * 501}
        ).status_code
        == 400
    )
    pg.row["origin"] = "own"
    assert (
        client.post(
            "/library/v/tiktok/123/advertiser", headers=ORIGIN, data={"advertiser": "brand"}
        ).status_code
        == 409
    )


def test_api_adds_origin_external_case_and_advertiser(intake: tuple[Any, ImportPg]) -> None:
    client, pg = intake
    pg.execute = lambda query, params=None: Pg.execute(pg, query, params)
    pg.row.update(origin="external", is_external_case=True, external_advertiser="MOFT")
    result = client.get("/library/api/videos").json()["videos"][0]
    assert result["origin"] == "external" and result["is_external_case"] is True
    assert result["external_advertiser"] == "MOFT"
    assert result["url"] == URL and result["advertiser"] == "advertiser"
    assert "video_s3_key" not in result and "campaign_total_plays" not in result
