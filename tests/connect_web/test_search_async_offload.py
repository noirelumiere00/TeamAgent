"""#5 イベントループ非ブロッキング化 + #4 検索 UI 改善（app.py 内完結分）のテスト.

- api_search の skill.run がイベントループスレッド外（to_thread worker）で実行されること
- クライアント切断済みリクエストは skill.run 前に 499 で破棄されること（Bedrock 課金回避）
- SearchSkill lazy-singleton が並行初期化でも factory 1 回だけ（double-checked locking）
- SEARCH_CONCURRENCY=1 で同時実行が直列化されること（semaphore）
- /search の JS/CSS に AbortController・スケルトン・ボタン disable のマーカーがあること

実 Google 0・実 DB 0・実 Bedrock 0（test_search_routes.py と同じ注入作法）。
並行/切断シナリオは TestClient がリクエストごとに新イベントループを作る制約を避けるため、
ASGI アプリを直接 await して単一ループ上で再現する（asyncio_mode=auto で async test 可）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
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


def _output() -> SearchOutput:
    return SearchOutput(
        answer="要約",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="本文",
                score=0.9,
                source_uri="gdrive://F1",
                source_type="gdrive",
                title="資料",
                client_name="A社",
            )
        ],
        total_cost_usd=0.01,
    )


class _RecordingSkill:
    """run() 実行時のスレッド/イベントループ状況・同時実行数を記録する fake。"""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.calls: list[SearchInput] = []
        self.on_event_loop: list[bool] = []
        self.threads: list[int] = []
        self.active = 0
        self.max_active = 0
        self._mu = threading.Lock()
        self._delay_s = delay_s

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append(input)
        self.threads.append(threading.get_ident())
        try:
            asyncio.get_running_loop()
            self.on_event_loop.append(True)  # イベントループスレッド上＝ブロッキング実行
        except RuntimeError:
            self.on_event_loop.append(False)  # worker スレッド＝オフロード成功
        with self._mu:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self._delay_s:
                time.sleep(self._delay_s)
        finally:
            with self._mu:
                self.active -= 1
        return _output()


class _FakeFeedbackStore:
    def save(self, row: dict[str, Any]) -> None:  # pragma: no cover - 本テストでは未使用
        pass


def _verifier_ok(token: str, client_id: str) -> dict[str, Any]:
    return {"email": _EMAIL, "email_verified": True}


def _build_app(skill: Any, *, factory: Any = None) -> Any:
    return create_app(
        search_skill_factory=factory or (lambda: skill),
        search_config=_config(),
        search_verifier=_verifier_ok,
        feedback_store=_FakeFeedbackStore(),
    )


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


async def _post_search(
    app: Any, payload: dict[str, Any], *, disconnect: bool = False
) -> tuple[int, dict[str, Any]]:
    """ASGI を直接叩いて /api/v1/search を呼ぶ（単一イベントループ上で並行/切断を再現）。

    disconnect=True はリクエストボディの直後に http.disconnect を積む＝
    「ハンドラ到達時点でクライアントが既に abort 済み」のサーバー視点を再現する。
    """
    body = json.dumps(payload).encode()
    cookie = f"ta_search_session={make_session(_EMAIL, _SECRET, ttl_s=3600)}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/search",
        "raw_path": b"/api/v1/search",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cookie", cookie.encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    if disconnect:
        messages.append({"type": "http.disconnect"})

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        # 実サーバー同様、次のメッセージ（切断）が来るまで receive は返らない。
        # is_disconnected() 側のタイムアウト付き待機がこれを cancel して False を返す。
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m for m in sent if m["type"] == "http.response.start")["status"]
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(status), dict(json.loads(raw)) if raw else {}


# ---------------- #5: to_thread オフロード ----------------


def test_api_search_runs_skill_off_event_loop() -> None:
    """skill.run はイベントループスレッド外で実行される（レスポンス互換のまま）。"""
    sk = _RecordingSkill()
    client = TestClient(_build_app(sk))
    r = client.post("/api/v1/search", json={"query": "保存率の提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "要約"
    assert len(body["hits"]) == 1
    assert body["hits"][0]["source_uri"] == "https://drive.google.com/file/d/F1/view"
    # run() 内で asyncio.get_running_loop() が失敗した＝ループスレッド外（worker）で実行
    assert sk.on_event_loop == [False]


def test_api_search_filters_still_reach_skill_through_offload() -> None:
    """オフロード後も SearchInput への filter 受け渡しが従来どおり効く（回帰）。"""
    sk = _RecordingSkill()
    client = TestClient(_build_app(sk))
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_industry": "食品", "filter_budget": "〜100万"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0].filter_industry == "食品"
    assert sk.calls[0].filter_budget == "〜100万"


async def test_api_search_skips_run_when_client_disconnected() -> None:
    """abort/切断済みリクエストは skill.run（embed+Bedrock）前に 499 で破棄される。"""
    sk = _RecordingSkill()
    app = _build_app(sk)
    status, body = await _post_search(app, {"query": "保存率"}, disconnect=True)
    assert status == 499
    assert body == {"error": "client_closed_request"}
    assert sk.calls == []  # 課金経路（embed/Bedrock）に一切入らない


async def test_api_search_normal_request_not_treated_as_disconnected() -> None:
    """切断していない ASGI 直叩きリクエストは従来どおり 200（誤 499 の回帰防止）。"""
    sk = _RecordingSkill()
    app = _build_app(sk)
    status, body = await _post_search(app, {"query": "保存率"})
    assert status == 200
    assert body["answer"] == "要約"
    assert len(sk.calls) == 1


# ---------------- #5: lazy-singleton の並行初期化 ----------------


async def test_lazy_singleton_initialized_once_under_concurrency() -> None:
    """初回検索が同時に2本来ても factory（重い embedder 構築）は1回だけ。"""
    sk = _RecordingSkill()
    factory_calls: list[int] = []

    def factory() -> _RecordingSkill:
        factory_calls.append(threading.get_ident())
        time.sleep(0.3)  # 重い構築を模擬（この間に2本目の worker が到達する）
        return sk

    app = _build_app(sk, factory=factory)
    results = await asyncio.gather(
        _post_search(app, {"query": "a"}),
        _post_search(app, {"query": "b"}),
    )
    assert [s for s, _ in results] == [200, 200]
    assert len(factory_calls) == 1  # double-checked locking で二重ロードなし
    assert len(sk.calls) == 2  # 両リクエストとも同一 singleton で実行された


# ---------------- #5: SEARCH_CONCURRENCY セマフォ ----------------


async def test_search_concurrency_env_serializes_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_CONCURRENCY=1 なら同時2リクエストでも run は直列（max_active=1）。"""
    monkeypatch.setenv("SEARCH_CONCURRENCY", "1")
    sk = _RecordingSkill(delay_s=0.15)
    app = _build_app(sk)
    results = await asyncio.gather(
        _post_search(app, {"query": "a"}),
        _post_search(app, {"query": "b"}),
    )
    assert [s for s, _ in results] == [200, 200]
    assert len(sk.calls) == 2
    assert sk.max_active == 1  # semaphore(1) が CPU 推論の並走を防いだ


async def test_search_concurrency_bad_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEARCH_CONCURRENCY が不正値でも既定(4)で動作し 200 を返す（起動失敗しない）。"""
    monkeypatch.setenv("SEARCH_CONCURRENCY", "zero")
    sk = _RecordingSkill()
    app = _build_app(sk)
    status, body = await _post_search(app, {"query": "a"})
    assert status == 200
    assert body["answer"] == "要約"


# ---------------- #4: フロント JS/CSS のマーカー ----------------


def test_search_ui_has_abort_and_skeleton_markers() -> None:
    """/search の埋め込み JS/CSS に #4 の実装マーカーが含まれる（文字列レベルの最小検証）。"""
    sk = _RecordingSkill()
    client = TestClient(_build_app(sk))
    r = client.get("/search", cookies=_auth_cookie())
    assert r.status_code == 200
    # 連打レース対策: AbortController + fetch への signal 配線 + 世代カウンタ
    assert "AbortController" in r.text
    assert "signal:ctl.signal" in r.text
    assert "searchGen" in r.text
    # 検索中 UI: ボタン disable + スケルトンカード + shimmer + reduced-motion 対応
    assert "go.disabled=on" in r.text
    assert "renderSkeleton" in r.text
    assert "@keyframes skshine" in r.text
    assert "prefers-reduced-motion" in r.text
    # 旧実装のテキスト1行ローディングは撤去済み（textContent='検索中…' は JS に残さない。
    # ボタンラベルの '検索中…' は setSearching 側なので results への直書きだけを見る）
    assert "results.textContent='検索中…'" not in r.text
