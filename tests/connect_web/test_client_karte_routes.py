"""クライアントカルテ（GET /search/client/{client} + /api/v1/client/{client}）のテスト。

実 Google 0・実 DB 0。dashboard.auth の HMAC 署名 cookie（test_search_routes と同流儀）
+ client_karte_provider 注入で、認証ゲート / ヘッダ合成 / 時系列の降順化 /
gdrive→view 整形 / URL エンコードされたクライアント名 / 導線マーカー を検証する。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"unit-test-search-secret-32-bytes!"
_EMAIL = "s-komata@vectorinc.co.jp"


def _config() -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({_EMAIL}),
        allowed_hd=None,
        google_client_id="cid-123",
        session_secret=_SECRET,
        dev_bypass=False,
        cookie_secure=False,
    )


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


def _fb_hit(
    occurred_at: str,
    *,
    title: str = "営業FB",
    deal_phase: str | None = None,
    bant_score: str | None = None,
    **extra: Any,
) -> SearchHit:
    meta: dict[str, Any] = {
        "source_uri": f"slack://C1/{occurred_at}",
        "source_type": "slack",
        "title": title,
        "occurred_at": occurred_at,
        "is_sales_fb": True,
    }
    if deal_phase:
        meta["deal_phase"] = deal_phase
    if bant_score:
        meta["bant_score"] = bant_score
    meta.update({k: v for k, v in extra.items() if v})
    return SearchHit(chunk_id=1, content=f"{occurred_at} の商談メモ", score=1.0, metadata=meta)


def _doc_row(
    title: str,
    *,
    source_uri: str = "gdrive://F1",
    source_type: str = "gdrive",
    modified_at: str | None = "2026-06-01",
    cls_industry: str | None = None,
    cls_doc_type: str | None = "提案書",
) -> dict[str, Any]:
    return {
        "title": title,
        "source_uri": source_uri,
        "source_type": source_type,
        "modified_at": modified_at,
        "cls_industry": cls_industry,
        "cls_project": None,
        "cls_doc_type": cls_doc_type,
        "cls_solution": None,
        "cls_budget": None,
        "cls_target": None,
        "client_name": None,
        "excerpt": "抜粋テキスト",
    }


def _build(
    data: dict[str, Any] | None = None,
    *,
    provider: Any = None,
) -> tuple[TestClient, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    payload = data or {"timeline": [], "documents": []}

    def _provider(email: str, client: str) -> dict[str, Any]:
        calls.append((email, client))
        return payload

    app = create_app(
        search_config=_config(),
        client_karte_provider=provider or _provider,
    )
    return TestClient(app), calls


# ---------------- 認証ゲート ----------------


def test_karte_ui_unauthenticated_redirects_to_login() -> None:
    client, _ = _build()
    r = client.get("/search/client/出光興産", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/search/login"


def test_karte_api_requires_auth() -> None:
    client, calls = _build()
    r = client.get("/api/v1/client/出光興産")
    assert r.status_code == 401
    assert calls == []  # 未認証で provider（=DB）へ到達しない


# ---------------- カルテ UI ----------------


def test_karte_ui_renders_client_and_markers() -> None:
    client, _ = _build()
    r = client.get("/search/client/出光興産", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "クライアントカルテ" in r.text
    assert "出光興産" in r.text
    assert "/api/v1/client/" in r.text  # データは API から fetch
    assert "まだ記録がありません" in r.text  # 空状態文言（JS リテラル）
    assert "営業FB時系列" in r.text
    assert "関連資料" in r.text


def test_karte_ui_escapes_client_name_html() -> None:
    """HTML 差し込みは html.escape 済（XSS 防御・既存 _shell_page 流儀）。"""
    client, _ = _build()
    evil = "<script>alert(1)</script>"
    r = client.get(f"/search/client/{quote(evil, safe='')}", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_search_page_has_karte_link_wiring() -> None:
    """検索カード/プレビューパネルの「カルテを見る」導線が /search ページに載る。"""
    client, _ = _build()
    r = client.get("/search", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "カルテを見る" in r.text
    assert "/search/client/" in r.text


# ---------------- カルテ API ----------------


def test_karte_api_header_from_latest_and_timeline_desc() -> None:
    """ヘッダは timeline 末尾（最新）から・時系列は日付降順で返す。

    list_client_timeline_recent は【最新 N 件】を **昇順（古い順）** で返すため、
    API 側で反転する契約。
    """
    data = {
        "timeline": [
            _fb_hit("2026-05-01", deal_phase="初回接触", bant_score="C（検討）"),
            _fb_hit("2026-06-15", deal_phase="提案", bant_score="B（前向き）"),
        ],
        "documents": [
            _doc_row("提案書A", cls_industry="エネルギー"),
            _doc_row("議事録B", source_uri="gdrive://F2", cls_industry="エネルギー"),
            _doc_row("その他C", source_uri="gdrive://F3", cls_industry="食品"),
        ],
    }
    client, _ = _build(data)
    r = client.get("/api/v1/client/出光興産", cookies=_auth_cookie())
    assert r.status_code == 200
    body = r.json()
    header = body["header"]
    assert header["client"] == "出光興産"
    assert header["deal_phase"] == "提案"  # 最新（末尾）要素から
    assert header["bant_score"] == "B（前向き）"
    assert header["last_contact"] == "2026-06-15"
    assert header["industry"] == "エネルギー"  # 資料側 cls_industry の最頻値で補完
    assert header["fb_count"] == 2
    assert header["doc_count"] == 3
    dates = [t["occurred_at"] for t in body["timeline"]]
    assert dates == ["2026-06-15", "2026-05-01"]  # 日付降順


def test_karte_api_shapes_gdrive_view_url_and_doc_fields() -> None:
    data = {
        "timeline": [],
        "documents": [
            _doc_row("提案書A", source_uri="gdrive://FILE_X"),
            _doc_row("Slackメモ", source_uri="slack://C9/123", source_type="slack"),
        ],
    }
    client, _ = _build(data)
    r = client.get("/api/v1/client/出光興産", cookies=_auth_cookie())
    docs = r.json()["documents"]
    assert docs[0]["open_url"] == "https://drive.google.com/file/d/FILE_X/view"
    assert docs[0]["doc_type"] == "提案書"  # 種別 chip 用
    assert docs[0]["modified_at"] == "2026-06-01"
    assert docs[1]["open_url"] == "slack://C9/123"  # gdrive 以外は素通し


def test_karte_api_passes_url_encoded_client_to_provider() -> None:
    """%エンコードされたクライアント名（スラッシュ含む）が復号されて provider へ届く。"""
    client, calls = _build()
    r = client.get(f"/api/v1/client/{quote('A/B商事', safe='')}", cookies=_auth_cookie())
    assert r.status_code == 200
    assert calls == [(_EMAIL, "A/B商事")]


def test_karte_api_empty_data_returns_zero_counts() -> None:
    client, _ = _build({"timeline": [], "documents": []})
    r = client.get("/api/v1/client/未知クライアント", cookies=_auth_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["header"]["fb_count"] == 0
    assert body["header"]["doc_count"] == 0
    assert body["timeline"] == []
    assert body["documents"] == []


def test_karte_api_provider_failure_returns_500() -> None:
    def _boom(email: str, client: str) -> dict[str, Any]:
        raise RuntimeError("db down")

    client, _ = _build(provider=_boom)
    r = client.get("/api/v1/client/出光興産", cookies=_auth_cookie())
    assert r.status_code == 500
    assert r.json() == {"error": "karte_failed"}


def test_karte_api_production_path_uses_recent_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 非注入（本番経路）は list_client_timeline_recent（最新 N 件）を使う。

    ASC LIMIT の list_client_timeline だと FB が 50 件を超えるクライアントで
    「最古の50件」になり、最新 FB とヘッダが誤る（要修正 major の再発防止）。
    """
    from teamagent.adapters.pgvector_client import PgVectorClient

    calls: list[tuple[str, str, int]] = []

    class _StubPg:
        @contextmanager
        def connection(self, **kwargs: Any) -> Iterator[object]:
            yield object()

        def list_client_timeline_recent(
            self,
            conn: Any,
            client: str,
            limit: int = 20,
            request_id: str | None = None,
        ) -> list[SearchHit]:
            calls.append(("recent", client, limit))
            return [_fb_hit("2026-06-15", deal_phase="提案")]

        def list_documents_for_client(
            self,
            conn: Any,
            client: str,
            *,
            limit: int = 50,
            request_id: str | None = None,
        ) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(PgVectorClient, "from_env", classmethod(lambda cls: _StubPg()))
    app = create_app(search_config=_config())
    client = TestClient(app)
    r = client.get("/api/v1/client/出光興産", cookies=_auth_cookie())
    assert r.status_code == 200
    assert calls == [("recent", "出光興産", 50)]  # ASC 版 list_client_timeline を使わない
    assert r.json()["header"]["deal_phase"] == "提案"
