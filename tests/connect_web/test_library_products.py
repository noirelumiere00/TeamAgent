"""Product semantics, admin confirmation and safe MCP projections."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from teamagent.connect_web import library
from teamagent.connect_web import library_products as products
from teamagent.connect_web.library_mcp_tools import CaseSearch, VideoSearch, _video
from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_library_mcp_chatgpt import _chatgpt_token, _rpc
from tests.connect_web.test_library_mcp_chatgpt import setup as mcp_setup
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["mcp_setup", "setup"]


@pytest.mark.parametrize(
    "query", ["化粧品", "#コスメ", "ｃｏｓｍｅｔｉｃｓ", "cosmetics", "COSMETICS"]
)
def test_cosmetics_is_product_filter_not_industry(query: str) -> None:
    where, values, _, _ = library._where({"q": query + " #PR", "_product_ids": "haircare"}, False)
    assert "NOT v.is_private" in where and "p.payload->>'source'='human'" in where
    assert sorted(products.GROUPS["cosmetics"]) in values
    assert not any(isinstance(value, str) and query in value for value in values)
    assert True in values


def test_intersection_never_broadens_contradictory_product_filters() -> None:
    _, values, _, _ = library._where(
        {"q": "化粧品", "product_category": "haircare", "industry": "化粧品・美容"}, False
    )
    assert [] in values and "化粧品・美容" in values
    with pytest.raises(HTTPException):
        library._where({"product_category": "unknown-category"}, False)


def test_classification_requires_human_evidence_and_strict_fields() -> None:
    for payload in [
        {"product_category": "makeup"},
        {"product_category": "makeup", "evidence": " "},
        {"product_category": "makeup", "evidence": "checked", "admin": True},
        {"product_category": "haircare", "evidence": "\x00"},
        {"brand": "a" * 201},
    ]:
        with pytest.raises(ValidationError):
            products.Product.model_validate(payload)
    assert products.Product().product_category is None
    assert products.Product(product_category="haircare", evidence="商品パッケージを確認")


def test_metadata_rates_and_reasons_do_not_expose_review_or_cost() -> None:
    row = Pg().row
    row.update(
        product_metadata={
            "brand": "ブランド",
            "product_name": "商品",
            "product_category": "makeup",
            "classification_status": "verified",
            "reviewed_by": "secret@example.test",
            "evidence": "secret evidence",
        },
        total_likes=10,
        total_comments=2,
        total_shares=3,
    )
    video = _video(row, "https://example.test", {"q": "化粧品"})
    assert video.product_category == "makeup"
    assert video.save_rate == pytest.approx(0.01)
    assert video.engagement_rate == pytest.approx(193 / 17800)
    assert video.match_reasons == ["確認済み商品分類: メイク"]
    text = video.model_dump_json()
    assert "secret" not in text and "ad_cost" not in text
    row["total_plays"] = 0
    assert _video(row, "https://example.test").save_rate is None
    row["total_plays"], row["total_likes"] = 100, None
    assert _video(row, "https://example.test").engagement_rate is None
    row["product_metadata"] = None
    assert _video(row, "https://example.test").classification_status == "unclassified"


def test_product_form_is_admin_only_and_checks_origin(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = setup
    path = "/library/v/tiktok/123/product"
    form = {"product_category": "haircare", "evidence": "商品情報を確認", "brand": "サクセス"}
    assert client.post(path, data=form, cookies=_auth_cookie()).status_code == 403
    response = client.post(
        path,
        data=form,
        cookies=_auth_cookie(),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    query, values = pg.calls[-1]
    assert "ON CONFLICT" in query
    payload = json.loads(values[-1])
    assert payload["status"] == "verified" and payload["source"] == "human"
    assert payload["product_category"] == "haircare"
    assert not any("UPDATE vl_campaign" in query for query, _ in pg.calls)
    pg.calls.clear()
    form["evidence"] = ""
    assert (
        client.post(
            path, data=form, cookies=_auth_cookie(), headers={"Origin": "http://testserver"}
        ).status_code
        == 400
    )
    assert not pg.calls
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    assert (
        client.post(
            path, data=form, cookies=_auth_cookie(), headers={"Origin": "http://testserver"}
        ).status_code
        == 403
    )
    assert not pg.calls


def test_editor_escapes_product_values_and_ui_explains_categories(setup: Any) -> None:
    client, _ = setup
    html = products.editor("tiktok", "123", {"brand": '<script>alert("x")</script>'})
    assert "<script>" not in html and "&lt;script&gt;" in html
    response = client.get("/library/videos?product_category=cosmetics", cookies=_auth_cookie())
    assert response.status_code == 200
    assert "商品分類（確認済み）" in response.text and "未分類は含めません" in response.text
    assert "ヘアケアは別" in response.text


def test_options_and_invalid_filters_through_transport(mcp_setup: Any) -> None:
    client, pg = mcp_setup
    token = _chatgpt_token(client)
    response = _rpc(
        client,
        token,
        "tools/call",
        {"name": "get_search_options", "arguments": {"field": "product_category"}},
    )
    result = response.json()["result"]
    assert not result.get("isError"), response.text
    options = result["structuredContent"]
    assert {item["value"] for item in options["options"]} == {*products.CATEGORIES, "cosmetics"}
    pg.calls.clear()
    for params in [{"product_category": "bad"}, {"industry": "存在しない業種"}]:
        response = _rpc(
            client,
            token,
            "tools/call",
            {"name": "search_videos", "arguments": {"params": params}},
        )
        assert response.json()["result"]["isError"]
    assert all("NOT v.is_private" in sql for sql, _ in pg.calls)
    assert VideoSearch().sort == "relevance"
    with pytest.raises(ValidationError):
        CaseSearch(sort="saves")


def test_case_transport_projects_only_public_fields(
    mcp_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = mcp_setup
    token = _chatgpt_token(client)
    case = {
        "advertiser": "花王",
        "campaign": "example",
        "origin": "own",
        "matched_video_count": 1,
        "case_video_count": 30,
        "matched_total_plays": 100,
        "case_total_plays": 3000,
        "product_categories": ["makeup"],
        "brands": ["brand"],
        "last_posted": None,
        "metrics_captured_from": None,
        "metrics_captured_to": None,
        "ad_cost": "must-not-leak",
    }

    def cases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["admin"] is False
        return {"cases": [case], "total": 1, "unassigned_video_count": 2}

    monkeypatch.setattr(products, "search_cases", cases)
    response = _rpc(
        client,
        token,
        "tools/call",
        {"name": "search_cases", "arguments": {"params": {}}},
    )
    result = response.json()["result"]
    assert not result.get("isError"), response.text
    data = result["structuredContent"]
    assert data["cases"][0]["case_video_count"] == 30
    assert data["total"] == 1 and data["unassigned_video_count"] == 2
    assert "ad_cost" not in response.text and "must-not-leak" not in response.text
    assert pg.sessions[-1]["user_role"] == "user"
    pg.calls.clear()
    for name in ("get_search_options", "search_cases"):
        args = {} if name == "get_search_options" else {"params": {}}
        denied = _rpc(client, "invalid", "tools/call", {"name": name, "arguments": args})
        assert denied.status_code == 401
    assert not pg.calls
