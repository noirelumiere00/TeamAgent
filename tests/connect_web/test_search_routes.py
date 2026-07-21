"""connect_web の資料検索 Web UI ルートのテスト（実 Google0・実 DB0・実 Bedrock0）.

dashboard.auth の HMAC 署名 cookie + fake SearchSkill + fake feedback store を注入し、
認証ゲート / skill 呼び出しの ctx / feedback 保存 / allowlist 拒否 を検証する。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web import app as connect_app
from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

_SECRET = b"unit-test-search-secret-32-bytes!"
_EMAIL = "s-komata@vectorinc.co.jp"


def _config(*, client_id: str | None = "cid-123", hd: str | None = None) -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({_EMAIL}),
        allowed_hd=hd,
        google_client_id=client_id,
        session_secret=_SECRET,
        dev_bypass=False,
        cookie_secure=False,
    )


class _FakeSearchSkill:
    """run() の呼び出しを記録し、固定の SearchOutput を返す fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[SearchInput, SkillContext]] = []

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="保存率訴求は X 社の事例が参考になります。",
            hits=[
                SearchHitOut(
                    chunk_id=42,
                    content="ある飲料メーカー向けの保存率訴求の提案。" + "詳細本文" * 40,
                    score=0.87,
                    source_uri="gdrive://FILE_42",
                    source_type="gdrive",
                    title="飲料メーカー提案 2025Q3",
                    client_name="サンプル飲料",
                )
            ],
            total_cost_usd=0.01,
        )


class _FakeFeedbackStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def save(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


def _verifier_ok(token: str, client_id: str) -> dict[str, Any]:
    return {"email": _EMAIL, "email_verified": True}


def _verifier_other(token: str, client_id: str) -> dict[str, Any]:
    return {"email": "intruder@example.com", "email_verified": True}


def _build(
    *,
    skill: _FakeSearchSkill | None = None,
    store: _FakeFeedbackStore | None = None,
    verifier: Any = _verifier_ok,
    config: DashboardConfig | None = None,
) -> tuple[TestClient, _FakeSearchSkill, _FakeFeedbackStore]:
    sk = skill or _FakeSearchSkill()
    st = store or _FakeFeedbackStore()
    app = create_app(
        search_skill_factory=lambda: sk,
        search_config=config or _config(),
        search_verifier=verifier,
        feedback_store=st,
    )
    return TestClient(app), sk, st


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


# ---------------- 認証ゲート ----------------


def test_search_unauthenticated_redirects_to_login() -> None:
    client, _, _ = _build()
    r = client.get("/search", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/search/login"


def test_search_login_renders_google_button() -> None:
    client, _, _ = _build()
    r = client.get("/search/login")
    assert r.status_code == 200
    assert "g_id_onload" in r.text
    assert "cid-123" in r.text


def test_search_authenticated_renders_ui() -> None:
    client, _, _ = _build()
    r = client.get("/search", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "社内ナレッジ検索" in r.text


# ---------------- ログイン（allowlist） ----------------


def test_login_verify_sets_cookie_and_redirects() -> None:
    client, _, _ = _build()
    r = client.post("/search/auth/verify", data={"credential": "tok"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app"
    assert "ta_search_session" in r.headers.get("set-cookie", "")


def test_login_rejects_email_outside_allowlist() -> None:
    client, _, _ = _build(verifier=_verifier_other)
    r = client.post("/search/auth/verify", data={"credential": "tok"}, follow_redirects=False)
    assert r.status_code == 403
    assert "set-cookie" not in {k.lower() for k in r.headers}


# ---------------- /api/v1/search ----------------


def test_api_search_calls_skill_with_user_email_ctx() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search", json={"query": "保存率の提案", "top_k": 8}, cookies=_auth_cookie()
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].startswith("保存率訴求")
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["title"] == "飲料メーカー提案 2025Q3"
    assert len(hit["excerpt"]) <= 120  # content 先頭120文字
    # gdrive:// は実ブラウザで開ける Drive view リンクへ整形（資料提出 段階1）。
    assert hit["source_uri"] == "https://drive.google.com/file/d/FILE_42/view"
    assert hit["doc_id"] == "gdrive://FILE_42"  # FB 識別子は元の gdrive:// を保つ
    assert sk.calls[0][0].query == "保存率の提案"
    # ctx に本人 email / groups / role が入っていること
    _, ctx = sk.calls[0]
    assert ctx.metadata["user_email"] == _EMAIL
    assert ctx.metadata["user_groups"] == ["vectorinc.co.jp"]
    assert ctx.metadata["user_role"] == "user"


def test_api_search_requires_auth() -> None:
    client, sk, _ = _build()
    r = client.post("/api/v1/search", json={"query": "x"})
    assert r.status_code == 401
    assert sk.calls == []


class _FakeSkillWithTags(_FakeSearchSkill):
    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約",
            hits=[
                SearchHitOut(
                    chunk_id=7,
                    content="本文",
                    score=0.9,
                    source_uri="gdrive://7",
                    source_type="gdrive",
                    title="ニチレイ提案",
                    client_name="ニチレイ",
                    industry="食品",
                    doc_type="提案書",
                    project="ニチレイ案件",
                    deal_phase="提案",
                )
            ],
            total_cost_usd=0.01,
        )


def test_api_search_includes_cls_tags() -> None:
    client, _, _ = _build(skill=_FakeSkillWithTags())
    r = client.post("/api/v1/search", json={"query": "ニチレイ"}, cookies=_auth_cookie())
    assert r.status_code == 200
    hit = r.json()["hits"][0]
    assert hit["industry"] == "食品"
    assert hit["doc_type"] == "提案書"
    assert hit["project"] == "ニチレイ案件"
    assert hit["deal_phase"] == "提案"


def test_api_search_passes_filter_industry() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_industry": "食品"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_industry == "食品"


def test_api_search_ignores_blank_filter_industry() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_industry": "  "},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_industry is None


def test_api_search_rejects_empty_query() -> None:
    client, sk, _ = _build()
    r = client.post("/api/v1/search", json={"query": "  "}, cookies=_auth_cookie())
    assert r.status_code == 400
    assert sk.calls == []


# ---------------- /api/v1/feedback ----------------


def test_api_feedback_saves_with_cookie_email() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={
            "query": "保存率の提案",
            "target_type": "chunk",
            "doc_id": "gdrive://FILE_42",
            "chunk_id": 42,
            "rating": 1,
            "note": "ぴったり",
        },
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert len(st.rows) == 1
    row = st.rows[0]
    assert row["user_email"] == _EMAIL  # クライアント入力ではなく cookie 由来
    assert row["target_type"] == "chunk"
    assert row["doc_id"] == "gdrive://FILE_42"
    assert row["chunk_id"] == 42
    assert row["rating"] == 1


def test_api_feedback_answer_target() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={"query": "保存率", "target_type": "answer", "rating": -1},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert st.rows[0]["target_type"] == "answer"
    assert st.rows[0]["rating"] == -1
    assert st.rows[0]["chunk_id"] is None


def test_api_feedback_requires_auth() -> None:
    client, _, st = _build()
    r = client.post("/api/v1/feedback", json={"query": "x", "target_type": "answer", "rating": 1})
    assert r.status_code == 401
    assert st.rows == []


def test_api_feedback_rejects_bad_rating() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={"query": "x", "target_type": "answer", "rating": 5},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 400
    assert st.rows == []


def test_api_feedback_rejects_bad_target_type() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={"query": "x", "target_type": "bogus", "rating": 1},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 400
    assert st.rows == []


@pytest.mark.parametrize(
    ("score", "expected_rating", "client_rating"),
    [(4, 1, -1), (3, 1, -1), (2, -1, 1), (1, -1, 1)],
)
def test_api_feedback_score_derives_rating_over_client_rating(
    score: int, expected_rating: int, client_rating: int
) -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={
            "query": "要約の評価",
            "target_type": "answer",
            "rating": client_rating,
            "score": score,
        },
        cookies=_auth_cookie(),
    )

    assert r.status_code == 200
    assert st.rows[0]["score"] == score
    assert st.rows[0]["rating"] == expected_rating


@pytest.mark.parametrize("score", [0, 5, "a"])
def test_api_feedback_rejects_bad_score(score: int | str) -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={"query": "x", "target_type": "answer", "score": score},
        cookies=_auth_cookie(),
    )

    assert r.status_code == 400
    assert r.json() == {"error": "bad_score"}
    assert st.rows == []


def test_api_feedback_rejects_score_for_chunk() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={"query": "x", "target_type": "chunk", "score": 4},
        cookies=_auth_cookie(),
    )

    assert r.status_code == 400
    assert r.json() == {"error": "bad_score_target"}
    assert st.rows == []


def test_api_feedback_score_sanitizes_optional_ids_and_caps_text() -> None:
    client, _, st = _build()
    r = client.post(
        "/api/v1/feedback",
        json={
            "query": "要約の評価",
            "target_type": "answer",
            "score": 4,
            "search_session_id": "!" + "a" * 64,
            "answer_id": "not-hex",
            "note": "n" * 600,
            "doc_id": "d" * 600,
        },
        cookies=_auth_cookie(),
    )

    assert r.status_code == 200
    row = st.rows[0]
    assert row["search_session_id"] is None
    assert row["answer_id"] is None
    assert row["note"] == "n" * 500
    assert row["doc_id"] == "d" * 512


def test_api_feedback_uses_legacy_seven_column_insert_without_new_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, list[Any]]] = []

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, values: list[Any]) -> None:
            executed.append((sql, values))

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            return None

    class FakePg:
        def connection(self, **kwargs: Any) -> FakeConnection:
            assert kwargs == {"app_role": "teamagent_app", "user_email": _EMAIL}
            return FakeConnection()

    from teamagent.adapters.pgvector_client import PgVectorClient

    monkeypatch.setattr(PgVectorClient, "from_env", lambda: FakePg())
    app = create_app(
        search_skill_factory=_FakeSearchSkill,
        search_config=_config(),
        search_verifier=_verifier_ok,
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/feedback",
        json={"query": "x", "target_type": "answer", "rating": 1},
        cookies=_auth_cookie(),
    )

    assert r.status_code == 200
    assert executed == [
        (
            "INSERT INTO search_feedback (user_email, query, target_type, doc_id, chunk_id, rating, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [_EMAIL, "x", "answer", None, None, 1, None],
        )
    ]


def test_feedback_insert_retries_legacy_columns_after_undefined_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0022未適用DBでも、新列を落として従来評価の保存を続ける。"""
    from psycopg.errors import UndefinedColumn

    columns = [
        "user_email",
        "query",
        "target_type",
        "doc_id",
        "chunk_id",
        "rating",
        "note",
        "score",
        "search_session_id",
        "answer_id",
    ]
    values: list[Any] = [_EMAIL, "x", "answer", None, None, 1, None, 4, "session-1", "abc"]
    executed: list[tuple[str, list[Any]]] = []
    recovered: list[bool] = []
    warnings: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            warnings.append((event, kwargs))

    def execute(sql: str, params: list[Any]) -> None:
        executed.append((sql, params))
        if len(executed) == 1:
            raise UndefinedColumn('column "score" does not exist')

    monkeypatch.setattr(connect_app, "logger", FakeLogger())
    connect_app._execute_feedback_insert(
        execute, columns, values, prepare_legacy_retry=lambda: recovered.append(True)
    )

    assert [sql for sql, _ in executed] == [
        "INSERT INTO search_feedback (user_email, query, target_type, doc_id, chunk_id, rating, note, "
        "score, search_session_id, answer_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        "INSERT INTO search_feedback (user_email, query, target_type, doc_id, chunk_id, rating, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
    ]
    assert executed[1][1] == [_EMAIL, "x", "answer", None, None, 1, None]
    assert recovered == [True]
    assert warnings == [
        (
            "feedback_save_legacy_fallback",
            {"dropped_columns": ["score", "search_session_id", "answer_id"]},
        )
    ]


def test_api_feedback_rate_limit_recovers_after_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(connect_app.time, "monotonic", lambda: now[0])
    connect_app._feedback_rate_windows.clear()
    try:
        client, _, st = _build()
        payload = {"query": "x", "target_type": "answer", "score": 4}
        for _ in range(30):
            assert (
                client.post("/api/v1/feedback", json=payload, cookies=_auth_cookie()).status_code
                == 200
            )

        limited = client.post("/api/v1/feedback", json=payload, cookies=_auth_cookie())
        assert limited.status_code == 429
        assert limited.json() == {"error": "rate_limited"}
        assert len(st.rows) == 30

        now[0] += 60
        assert (
            client.post("/api/v1/feedback", json=payload, cookies=_auth_cookie()).status_code == 200
        )
        assert len(st.rows) == 31
    finally:
        connect_app._feedback_rate_windows.clear()


def test_api_feedback_legacy_rating_is_not_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(connect_app.time, "monotonic", lambda: now[0])
    connect_app._feedback_rate_windows.clear()
    try:
        client, _, st = _build()
        payload = {"query": "x", "target_type": "answer", "rating": 1}
        for _ in range(31):
            assert (
                client.post("/api/v1/feedback", json=payload, cookies=_auth_cookie()).status_code
                == 200
            )

        assert len(st.rows) == 31
        assert connect_app._feedback_rate_windows == {}
    finally:
        connect_app._feedback_rate_windows.clear()
