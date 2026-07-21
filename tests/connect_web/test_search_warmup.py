"""SearchSkill startup warmup のテスト（実 Google・DB・Bedrock は使わない）。"""

from __future__ import annotations

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


class _RecordingEmbedder:
    """warmup で渡された入力を記録する embedder fake。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.called = threading.Event()

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        self.called.set()
        return [0.0]


class _FakeSearchSkill:
    """warmup と API 検索の双方に使う最小 fake。"""

    def __init__(self, *, embedder: _RecordingEmbedder | None = None) -> None:
        if embedder is not None:
            self.embedder = embedder
        self.run_calls: list[SearchInput] = []

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.run_calls.append(input)
        return _output()


class _FakeFeedbackStore:
    def save(self, row: dict[str, Any]) -> None:  # pragma: no cover - 本テストでは未使用
        pass


def _verifier_ok(token: str, client_id: str) -> dict[str, Any]:
    return {"email": _EMAIL, "email_verified": True}


def _build_app(factory: Any) -> Any:
    return create_app(
        search_skill_factory=factory,
        search_config=_config(),
        search_verifier=_verifier_ok,
        feedback_store=_FakeFeedbackStore(),
    )


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


def _join_warmup(app: Any) -> None:
    """テストが起動した daemon を回収し、次のテストへ持ち越さない。"""
    thread = app.state.search_warmup_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_warmup_builds_skill_and_runs_dummy_embed() -> None:
    """検索リクエストが無くても factory と dummy embed を各1回だけ実行する。"""
    embedder = _RecordingEmbedder()
    skill = _FakeSearchSkill(embedder=embedder)
    factory_called = threading.Event()
    factory_calls = 0

    def factory() -> _FakeSearchSkill:
        nonlocal factory_calls
        factory_calls += 1
        factory_called.set()
        return skill

    app = _build_app(factory)
    with TestClient(app):
        try:
            assert factory_called.wait(timeout=5)
            assert embedder.called.wait(timeout=5)
        finally:
            _join_warmup(app)
    assert factory_calls == 1
    assert embedder.calls == ["ウォームアップ"]


def test_warmup_does_not_block_healthz() -> None:
    """factory がモデルロード中でも startup と healthz は待たされない。"""
    entered = threading.Event()
    release = threading.Event()

    def factory() -> _FakeSearchSkill:
        entered.set()
        assert release.wait(timeout=5)
        return _FakeSearchSkill()

    app = _build_app(factory)
    with TestClient(app) as client:
        try:
            assert entered.wait(timeout=5)
            started_at = time.perf_counter()
            response = client.get("/healthz")
            elapsed_s = time.perf_counter() - started_at
            assert response.status_code == 200
            assert elapsed_s < 1
        finally:
            release.set()
            _join_warmup(app)


def test_warmup_failure_is_open_and_next_search_rebuilds() -> None:
    """warmup 失敗は起動を止めず、通常検索が lazy 経路で再構築できる。"""
    skill = _FakeSearchSkill()
    factory_calls = 0

    def factory() -> _FakeSearchSkill:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise RuntimeError("warmup test failure")
        return skill

    app = _build_app(factory)
    with TestClient(app) as client:
        _join_warmup(app)
        assert client.get("/healthz").status_code == 200
        response = client.post(
            "/api/v1/search",
            json={"query": "保存率"},
            cookies=_auth_cookie(),
        )
    assert response.status_code == 200
    assert factory_calls == 2
    assert len(skill.run_calls) == 1


def test_search_warmup_zero_disables_startup_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_WARMUP=0 では factory を呼ばず、thread state も None のままにする。"""
    monkeypatch.setenv("SEARCH_WARMUP", "0")
    factory_calls = 0

    def factory() -> _FakeSearchSkill:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeSearchSkill()

    app = _build_app(factory)
    with TestClient(app):
        assert app.state.search_warmup_thread is None
    assert factory_calls == 0


def test_warmup_reuses_singleton_for_search() -> None:
    """warmup 済みの skill は後続 API 検索でそのまま再利用する。"""
    skill = _FakeSearchSkill()
    factory_calls = 0

    def factory() -> _FakeSearchSkill:
        nonlocal factory_calls
        factory_calls += 1
        return skill

    app = _build_app(factory)
    with TestClient(app) as client:
        _join_warmup(app)
        response = client.post(
            "/api/v1/search",
            json={"query": "保存率"},
            cookies=_auth_cookie(),
        )
    assert response.status_code == 200
    assert factory_calls == 1
    assert len(skill.run_calls) == 1


def test_warmup_allows_skill_without_embedder() -> None:
    """embedder 属性のない skill でも warmup は成功として完走する。"""
    skill = _FakeSearchSkill()
    factory_calls = 0

    def factory() -> _FakeSearchSkill:
        nonlocal factory_calls
        factory_calls += 1
        return skill

    app = _build_app(factory)
    with TestClient(app):
        _join_warmup(app)
    assert factory_calls == 1
    assert skill.run_calls == []
