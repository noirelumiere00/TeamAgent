"""検索ロードマップ #1 二段レスポンス + #2 クエリ語ハイライトの connect_web テスト。

- /api/v1/search が payload.include_answer を SearchInput へ bool 透過すること
  （未指定は True＝完全後方互換）
- /search の埋め込み JS に 並行2フェッチ / AI要約プレースホルダ差し込み / 再試行 /
  クエリ語ハイライト（span.hl・createElement のみ）のマーカーがあること

実 Google 0・実 DB 0・実 Bedrock 0（test_search_routes.py と同じ注入作法）。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

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


class _FakeSearchSkill:
    """run() の呼び出しを記録し、input.include_answer に応じた SearchOutput を返す fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[SearchInput, SkillContext]] = []

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約" if input.include_answer else "",
            hits=[
                SearchHitOut(
                    chunk_id=1,
                    content="本文" * 100,
                    score=0.9,
                    source_uri="gdrive://F1",
                    source_type="gdrive",
                    title="資料",
                )
            ],
            total_cost_usd=0.01 if input.include_answer else 0.0,
        )


class _FakeFeedbackStore:
    def save(self, row: dict[str, Any]) -> None:  # pragma: no cover - 本テストでは未使用
        pass


def _verifier_ok(token: str, client_id: str) -> dict[str, Any]:
    return {"email": _EMAIL, "email_verified": True}


def _build() -> tuple[TestClient, _FakeSearchSkill]:
    sk = _FakeSearchSkill()
    app = create_app(
        search_skill_factory=lambda: sk,
        search_config=_config(),
        search_verifier=_verifier_ok,
        feedback_store=_FakeFeedbackStore(),
    )
    return TestClient(app), sk


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


# ---------------- /api/v1/search: include_answer 透過 ----------------


def test_api_search_passes_include_answer_false() -> None:
    """payload の include_answer:false が SearchInput へ透過し、answer 空で返る（fast path）。"""
    client, sk = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "保存率", "include_answer": False},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].include_answer is False
    body = r.json()
    assert body["answer"] == ""
    assert len(body["hits"]) == 1  # hits は fast path でも通常どおり


def test_api_search_include_answer_defaults_true() -> None:
    """include_answer 未指定は True（旧フロント・API 直叩きと完全後方互換）。"""
    client, sk = _build()
    r = client.post("/api/v1/search", json={"query": "保存率"}, cookies=_auth_cookie())
    assert r.status_code == 200
    assert sk.calls[0][0].include_answer is True
    assert r.json()["answer"] == "要約"


def test_api_search_include_answer_true_explicit() -> None:
    """include_answer:true 明示でも従来どおり要約が返る。"""
    client, sk = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "保存率", "include_answer": True},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].include_answer is True
    assert r.json()["answer"] == "要約"


# ---------------- /search: JS マーカー（文字列レベルの最小検証） ----------------


def test_search_ui_has_two_phase_fetch_markers() -> None:
    """並行2フェッチ: (a) include_answer:false 即描画と (b) true 後着差し込みの配線。"""
    client, _ = _build()
    r = client.get("/search", cookies=_auth_cookie())
    assert r.status_code == 200
    # include_answer が fetch body に乗る（withAnswer が (a)false/(b)true を切替える）
    assert "include_answer:withAnswer" in r.text
    # (b) 要約フェッチが先に発射され、(a) fast フェッチと ctl（AbortController）を共有
    assert "fetchSearch(body,true,ctl)" in r.text
    assert "fetchSearch(body,false,ctl)" in r.text
    assert "signal:ctl.signal" in r.text
    # プレースホルダ → 到着後 .abody 差し替え。(b) の hits は使わない（ちらつき防止）
    assert "AI要約を生成中…" in r.text
    assert "renderAnswerPending" in r.text
    assert "attachAnswer" in r.text
    # (b) 失敗時は要約カード内で再試行（hits の描画は維持される）
    assert "要約の生成に失敗しました" in r.text
    assert "再試行" in r.text


def test_search_ui_has_highlight_markers() -> None:
    """クエリ語ハイライト: 2文字以上の語で分割し span.hl を createElement で挿す。"""
    client, _ = _build()
    r = client.get("/search", cookies=_auth_cookie())
    assert r.status_code == 200
    # 語分割（空白区切り・2文字以上のみ）とハイライト関数
    assert "hlTerms" in r.text
    assert "w.length>=2" in r.text
    assert "appendHighlighted" in r.text
    # span.hl は createElement + textContent で構築（innerHTML への代入は無い＝XSS 安全）
    assert "sp.className='hl'" in r.text
    assert "document.createTextNode" in r.text
    assert ".innerHTML" not in r.text
    # CSS とシェル側（右プレビュー/hover ポップ）用の公開フック
    assert ".hl{" in r.text
    assert "window.searchHighlight" in r.text
