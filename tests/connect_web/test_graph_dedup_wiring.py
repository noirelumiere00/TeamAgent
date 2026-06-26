"""L5: 資料グラフ route が ``DOC_DEDUP_EXCLUDE_SEARCH`` を読み、suppressed 除外を検索と連動させる。

検索側（skills/search）は ``DOC_DEDUP_EXCLUDE_SEARCH`` が ON のときだけ重複（suppressed）資料を
除外する（既定 OFF・後方互換）。グラフ側（/api/v1/graph）も同じ env を読み、検索とグラフで
「重複資料を隠す/見せる」を連動させる。app.py は env を読んで pgvector へ ``exclude_duplicates``
として渡す（pgvector が当該引数を**サポートしている場合**）。

pgvector_client.py は本タスクで編集禁止のため、ここでは ``PgVectorClient`` をフェイクに差し替えて
app.py 側の env 読み取り・受け渡しのみを検証する:
  - フェイクが ``exclude_duplicates`` 引数を持つ場合 → env 値がそのまま渡る（連動）。
  - フェイクが当該引数を持たない場合（= 現状の本番 pgvector）→ TypeError にならず従来どおり呼ぶ。

graph_docs_provider は注入しない（= 実 DB 経路を通す）ことで、env 読み取りが効く分岐に入る。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import teamagent.adapters.pgvector_client as pgmod
from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"unit-test-search-secret-32-bytes!"
_EMAIL = "s-komata@vectorinc.co.jp"

_SAMPLE_DOCS: list[dict[str, Any]] = [
    {
        "node_id": 1,
        "title": "ニチレイ 提案書",
        "source_uri": "gdrive://1",
        "source_type": "gdrive",
        "cls_industry": "食品",
        "cls_project": "ニチレイ",
        "cls_doc_type": "提案書",
        "client_name": "ニチレイ",
    },
]


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


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakePgSupportsExclude:
    """``exclude_duplicates`` 引数を**サポートする**フェイク（pgvector 引数追加後の想定）。"""

    calls: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def from_env(cls) -> _FakePgSupportsExclude:
        return cls()

    def connection(self, **_kwargs: Any) -> _FakeConn:
        return _FakeConn()

    def list_documents_for_graph(
        self,
        conn: Any,
        *,
        limit: int = 600,
        request_id: str | None = None,
        with_embeddings: bool = False,
        exclude_duplicates: bool = False,
    ) -> list[dict[str, Any]]:
        type(self).calls.append({"exclude_duplicates": exclude_duplicates})
        return _SAMPLE_DOCS


class _FakePgNoExclude:
    """``exclude_duplicates`` 引数を**持たない**フェイク（= 現状の本番 pgvector）。"""

    calls: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def from_env(cls) -> _FakePgNoExclude:
        return cls()

    def connection(self, **_kwargs: Any) -> _FakeConn:
        return _FakeConn()

    def list_documents_for_graph(
        self,
        conn: Any,
        *,
        limit: int = 600,
        request_id: str | None = None,
        with_embeddings: bool = False,
    ) -> list[dict[str, Any]]:
        type(self).calls.append({"with_embeddings": with_embeddings})
        return _SAMPLE_DOCS


def _client() -> TestClient:
    # graph_docs_provider を注入しない＝実 DB 経路（env 読み取り分岐）を通す。
    return TestClient(create_app(search_config=_config()))


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("1", True), ("true", True), ("yes", True), ("1 ", True), (None, False), ("0", False)],
)
def test_graph_forwards_dedup_flag_when_pgvector_supports_it(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: bool
) -> None:
    _FakePgSupportsExclude.calls = []
    monkeypatch.setattr(pgmod, "PgVectorClient", _FakePgSupportsExclude)
    if env_value is None:
        monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)
    else:
        monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", env_value)

    r = _client().get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    assert {n["id"] for n in r.json()["nodes"]} == {1}
    # env 値が exclude_duplicates としてそのまま pgvector に渡る（検索と連動）。
    assert _FakePgSupportsExclude.calls == [{"exclude_duplicates": expected}]


def test_graph_falls_back_when_pgvector_lacks_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 現状の本番 pgvector（引数なし）でも、env ON で TypeError にならず従来どおり描画できる。
    _FakePgNoExclude.calls = []
    monkeypatch.setattr(pgmod, "PgVectorClient", _FakePgNoExclude)
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "1")

    r = _client().get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    assert {n["id"] for n in r.json()["nodes"]} == {1}
    # 引数なしで 1 回だけ呼ばれた（exclude_duplicates は渡していない）。
    assert _FakePgNoExclude.calls == [{"with_embeddings": False}]


def test_graph_env_off_default_does_not_break(monkeypatch: pytest.MonkeyPatch) -> None:
    # 既定（env 未設定）でも実 DB 経路が壊れない後方互換の担保。
    _FakePgSupportsExclude.calls = []
    monkeypatch.setattr(pgmod, "PgVectorClient", _FakePgSupportsExclude)
    monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)

    r = _client().get("/api/v1/graph", cookies=_auth_cookie())
    assert r.status_code == 200
    assert _FakePgSupportsExclude.calls == [{"exclude_duplicates": False}]
