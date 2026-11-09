"""Exercise ChatGPT OAuth and read-only tools through the real MCP transport."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from teamagent.connect_web.app import create_app
from teamagent.connect_web.library_mcp_tools import VideoSearch, _video
from tests.connect_web.mcp_store_fake import patch_store
from tests.connect_web.test_library import Pg
from tests.connect_web.test_library_mcp_spike import (
    _BASE,
    _consent_form,
    _params,
    _registration,
    _token_data,
    _verifier_mcp,
)
from tests.connect_web.test_search_routes import _EMAIL, _config

_CHATGPT = "https://chatgpt.com/connector/oauth/test-callback"


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CONNECT_MCP_ENABLED", "true")
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", _BASE)
    monkeypatch.setenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", _CHATGPT)
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    patch_store(monkeypatch, resource=_BASE + "/mcp")
    pg = Pg()
    pg.row.update(ad_cost=987654321, private_payload="must-not-leak")
    with TestClient(
        create_app(search_config=_config(), search_verifier=_verifier_mcp, admin_pg=pg),
        base_url=_BASE,
        follow_redirects=False,
    ) as client:
        yield client, pg


def _rpc(client: TestClient, token: str, method: str, params: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Origin": "https://chatgpt.com",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def _chatgpt_token(client: TestClient) -> str:
    response = client.post("/register", json=_registration(_CHATGPT))
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    response = client.get("/authorize", params=_params(client_id, redirect_uri=_CHATGPT))
    assert response.status_code == 302
    target = response.headers["location"]
    csrf = _consent_form(client, target, service="ChatGPT")
    response = client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE})
    callback = urlsplit(response.headers["location"])
    assert callback.hostname == "chatgpt.com" and callback.path == "/connector/oauth/test-callback"
    code = parse_qs(callback.query)["code"][0]
    response = client.post("/token", data=_token_data(client_id, code, redirect_uri=_CHATGPT))
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def test_chatgpt_oauth_catalog_search_detail(setup: Any) -> None:
    client, pg = setup
    token = _chatgpt_token(client)
    initialized = _rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "ChatGPT", "version": "test"},
        },
    )
    assert initialized.status_code == 200, initialized.text
    tools = _rpc(client, token, "tools/list", {}).json()["result"]["tools"]
    assert [v["name"] for v in tools] == ["ping", "search_videos", "get_video"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert all(not t["annotations"]["destructiveHint"] for t in tools)
    response = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "search_videos",
            "arguments": {"params": {"q": "化粧品 #PR", "limit": 3, "page": 2}},
        },
    )
    assert response.status_code == 200 and not response.json()["result"].get("isError"), (
        response.text
    )
    result = response.json()["result"]["structuredContent"]
    assert result["limit"] == 3 and result["page"] == 2
    assert result["videos"][0]["video_plays"] == 17800
    assert result["videos"][0]["library_url"] == _BASE + "/library/v/tiktok/123"
    sql, values = pg.calls[-1]
    assert "NOT v.is_private" in sql and values[-2:] == [3, 3]
    assert "%化粧品%" in values and True in values
    assert pg.sessions[-1] == {
        "app_role": "teamagent_app",
        "user_email": _EMAIL,
        "user_groups": ["vectorinc.co.jp"],
        "user_role": "user",
    }
    for forbidden in [
        "ad_cost",
        "987654321",
        "secret-key",
        "private_payload",
        "must-not-leak",
        "caption",
    ]:
        assert forbidden not in response.text
    detail = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "get_video",
            "arguments": {"platform": "tiktok", "video_id": "123"},
        },
    )
    assert not detail.json()["result"].get("isError"), detail.text
    assert pg.calls[-1][1] == ("tiktok", "123", False)
    assert "(%s OR NOT v.is_private)" in pg.calls[-1][0]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 21},
        {"page": 101},
        {"limit": True},
        {"page": "1"},
        {"q": "x" * 501},
        {"q": 10},
        {"sort": "ad_cost"},
        {"category": "bad"},
        {"admin": True},
        {"pr": "yes"},
    ],
)
def test_search_boundary(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        VideoSearch.model_validate(params)


@pytest.mark.parametrize(
    "callback",
    [
        _CHATGPT + "x",
        _CHATGPT + "?next=evil",
        _CHATGPT + "#fragment",
        "https://chatgpt.com.evil/connector/oauth/test-callback",
    ],
)
def test_chatgpt_callback_remains_exact(setup: Any, callback: str) -> None:
    client, _ = setup
    response = client.post("/register", json=_registration(callback))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_invalid_search_and_id_never_read_db(setup: Any) -> None:
    client, pg = setup
    token = _chatgpt_token(client)
    for name, args in [
        ("search_videos", {"params": {"q": " ".join(["a"] * 11)}}),
        ("search_videos", {"params": {"limit": True}}),
        ("get_video", {"platform": "tiktok", "video_id": "../secret"}),
        ("get_video", {"platform": "youtube", "video_id": "short"}),
    ]:
        response = _rpc(client, token, "tools/call", {"name": name, "arguments": args})
        assert response.json()["result"]["isError"], response.text
    assert not pg.calls
    no_auth = _rpc(
        client, "invalid", "tools/call", {"name": "search_videos", "arguments": {"params": {}}}
    )
    assert no_auth.status_code == 401 and not pg.calls


def test_database_errors_do_not_expose_details(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, pg = setup
    token = _chatgpt_token(client)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("password=private tenant=customer")

    monkeypatch.setattr(pg, "execute", fail)
    response = _rpc(
        client, token, "tools/call", {"name": "search_videos", "arguments": {"params": {}}}
    )
    assert response.json()["result"]["isError"]
    assert "password" not in response.text and "customer" not in response.text


def test_projection_rejects_forged_post_url(setup: Any) -> None:
    _, pg = setup
    pg.row["url"] = "https://evil.example/private"
    assert _video(pg.row, _BASE).post_url is None


def test_admin_changes_take_effect_with_the_same_token(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, pg = setup
    token = _chatgpt_token(client)
    args = {"name": "get_video", "arguments": {"platform": "tiktok", "video_id": "123"}}
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL)
    assert not _rpc(client, token, "tools/call", args).json()["result"].get("isError")
    assert pg.calls[-1][1] == ("tiktok", "123", True)
    assert pg.sessions[-1]["user_role"] == "admin"
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    assert not _rpc(client, token, "tools/call", args).json()["result"].get("isError")
    assert pg.calls[-1][1] == ("tiktok", "123", False)
    assert pg.sessions[-1]["user_role"] == "user"


def test_missing_or_invisible_video_has_no_details(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    client, pg = setup
    token = _chatgpt_token(client)
    result = MagicMock()
    result.fetchone.return_value = None
    monkeypatch.setattr(pg, "execute", lambda *a, **k: result)
    response = _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "get_video",
            "arguments": {"platform": "tiktok", "video_id": "123"},
        },
    )
    assert response.json()["result"]["isError"]
    assert "動画が見つかりません" in response.text
    assert "is_private" not in response.text


def test_title_is_bounded_and_does_not_return_full_caption(setup: Any) -> None:
    _, pg = setup
    pg.row["caption"] = "a" * 500 + "\nnever-return-this-paragraph"
    video = _video(pg.row, _BASE)
    assert video.title == "a" * 200
    assert "never-return" not in video.model_dump_json()
