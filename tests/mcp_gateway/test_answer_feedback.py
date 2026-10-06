"""回答評価（answer_feedback_record）の MCP 境界。

実際の build_server と SDK の CallTool ハンドラを通し、caller claim は testkit で署名する。
保存先だけフェイク（本番の失敗の形＝AnswerFeedbackStoreError／想定外の例外を再現できる）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from mcp import types

from teamagent.adapters.answer_feedback_store import (
    INSERT_SQL,
    AnswerFeedbackRow,
    AnswerFeedbackStoreError,
    PgAnswerFeedbackStore,
)
from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway import answer_feedback as afb
from teamagent.mcp_gateway.caller_claim import CallerClaimVerifier, InMemoryCallerClaimReplayStore
from teamagent.mcp_gateway.server import build_server
from teamagent.orchestrator.tools import ToolSpec
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    sign_arguments,
)
from tests.mcp_gateway.test_personal_memory_gate import _EchoSkill

TOOL = afb.ANSWER_FEEDBACK_TOOL_NAME
EMAIL = "Member@VectorInc.co.jp"
OTHER_USER = "U0BBBBBBBBB"
CHANNEL = "C0123456789"
THREAD = "1784423990.000100"
ANSWER_ID = hashlib.sha256(b"run-1").hexdigest()[:16]
QUERY = "JAL の過去提案 事例"


def _key(secret: str = TEST_CALLER_CLAIM_SECRET) -> bytes:
    return CallerClaimVerifier(
        secret=secret, expected_team_id=TEST_SLACK_TEAM_ID
    ).derive_purpose_key(afb.KEY_LABEL)


def _token(
    *,
    owner: str = TEST_SLACK_USER_ID,
    team: str = TEST_SLACK_TEAM_ID,
    query: str = QUERY,
    expires_at: int = TEST_NOW + 3600,
    secret: str = TEST_CALLER_CLAIM_SECRET,
) -> str:
    return afb.encode_feedback_token(
        key=_key(secret),
        query=query,
        answer_id=ANSWER_ID,
        slack_user_id=owner,
        slack_team_id=team,
        expires_at=expires_at,
    )


class _Store:
    """search_feedback の記録係。fail に例外を入れると本番の失敗（DB 不達・権限）を再現する。"""

    def __init__(self) -> None:
        self.rows: list[AnswerFeedbackRow] = []
        self.fail: Exception | None = None

    def insert(self, row: AnswerFeedbackRow) -> None:
        if self.fail is not None:
            raise self.fail
        self.rows.append(row)


def _resolver() -> IdentityResolver:
    people = {
        TEST_SLACK_USER_ID: ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email=EMAIL),
        OTHER_USER: ResolvedIdentity(slack_user_id=OTHER_USER, email="other@vectorinc.co.jp"),
    }

    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return people.get(slack_user_id)

    return resolve


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(afb.ANSWER_FEEDBACK_FLAG_ENV, "1")
    monkeypatch.delenv("USE_AGENT_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("USAGE_EVENTS_DISABLE", "1")
    yield


@pytest.fixture
def store() -> _Store:
    return _Store()


def _server(store: _Store, *, now: int = TEST_NOW, resolver: IdentityResolver | None = None) -> Any:
    verifier = CallerClaimVerifier(
        secret=TEST_CALLER_CLAIM_SECRET,
        expected_team_id=TEST_SLACK_TEAM_ID,
        clock=lambda: now,
        replay_store=InMemoryCallerClaimReplayStore(),
    )
    return build_server(
        [ToolSpec("echo", "echo", _EchoSkill)],
        identity_resolver=resolver or _resolver(),
        company_shared_groups=frozenset({"vectorinc.co.jp"}),
        allowed_domains=frozenset({"vectorinc.co.jp"}),
        caller_claim_verifier=verifier,
        answer_feedback_store=store,
    )


def _invocation() -> str:
    return f"aico-fb-{secrets.token_hex(16)}"


def _signed(
    business: dict[str, Any],
    *,
    user_id: str = TEST_SLACK_USER_ID,
    tool_call_id: str | None = None,
    run_id: str | None = None,
    tool: str = TOOL,
) -> dict[str, Any]:
    call_id = tool_call_id if tool_call_id is not None else _invocation()
    return sign_arguments(
        tool,
        business,
        user_id=user_id,
        channel_id=CHANNEL,
        thread_ts=THREAD,
        tool_call_id=call_id,
        run_id=run_id if run_id is not None else call_id,
    )


async def _call(server: Any, arguments: dict[str, Any], name: str = TOOL) -> dict[str, Any]:
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    content = result.root.content
    assert len(content) == 1
    return dict(json.loads(content[0].text))


def _code(payload: dict[str, Any]) -> str | None:
    return payload.get("code") if payload.get("error") == "answer_feedback_rejected" else None


# --- トークン（純関数） --------------------------------------------------------------------


def test_token_roundtrip() -> None:
    claim = afb.verify_feedback_token(
        _token(),
        key=_key(),
        now=TEST_NOW,
        presser_user_id=TEST_SLACK_USER_ID,
        team_id=TEST_SLACK_TEAM_ID,
    )
    assert claim.query == QUERY
    assert claim.answer_id == ANSWER_ID


def _tamper_payload(token: str, **changes: Any) -> str:
    segment, sig = token.split(".")
    raw = afb._b64d(segment)
    payload = json.loads(raw)
    payload.update(changes)
    new_segment = afb._b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{new_segment}.{sig}"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_tamper_payload(_token(), q="別の質問"), id="query_tampered"),
        pytest.param(_tamper_payload(_token(), u=OTHER_USER), id="owner_tampered"),
        pytest.param(_tamper_payload(_token(), e=TEST_NOW + 10**6), id="expiry_tampered"),
        pytest.param(_token(secret="another-secret-that-is-at-least-32-bytes!"), id="wrong_key"),
        pytest.param(_token()[:-2] + "AA", id="signature_changed"),
        pytest.param("not-a-token", id="shape"),
    ],
)
def test_token_tampering_rejected(token: str) -> None:
    with pytest.raises(afb.AnswerFeedbackTokenError) as excinfo:
        afb.verify_feedback_token(
            token,
            key=_key(),
            now=TEST_NOW,
            presser_user_id=TEST_SLACK_USER_ID,
            team_id=TEST_SLACK_TEAM_ID,
        )
    assert excinfo.value.code == "AFB_TOKEN_INVALID"


def test_token_expired_rejected() -> None:
    with pytest.raises(afb.AnswerFeedbackTokenError) as excinfo:
        afb.verify_feedback_token(
            _token(expires_at=TEST_NOW),
            key=_key(),
            now=TEST_NOW,
            presser_user_id=TEST_SLACK_USER_ID,
            team_id=TEST_SLACK_TEAM_ID,
        )
    assert excinfo.value.code == "AFB_TOKEN_EXPIRED"


def test_token_other_presser_rejected() -> None:
    with pytest.raises(afb.AnswerFeedbackTokenError) as excinfo:
        afb.verify_feedback_token(
            _token(),
            key=_key(),
            now=TEST_NOW,
            presser_user_id=OTHER_USER,
            team_id=TEST_SLACK_TEAM_ID,
        )
    assert excinfo.value.code == "AFB_NOT_OWNER"


def test_token_signed_with_claim_secret_itself_is_rejected() -> None:
    """claim の秘密そのもので署名したトークンは通らない（用途ラベルで鍵を分けている）。"""
    forged = afb.encode_feedback_token(
        key=TEST_CALLER_CLAIM_SECRET.encode(),
        query=QUERY,
        answer_id=ANSWER_ID,
        slack_user_id=TEST_SLACK_USER_ID,
        slack_team_id=TEST_SLACK_TEAM_ID,
        expires_at=TEST_NOW + 60,
    )
    with pytest.raises(afb.AnswerFeedbackTokenError):
        afb.verify_feedback_token(
            forged,
            key=_key(),
            now=TEST_NOW,
            presser_user_id=TEST_SLACK_USER_ID,
            team_id=TEST_SLACK_TEAM_ID,
        )


@pytest.mark.parametrize(
    ("tool_call_id", "run_id", "ok"),
    [
        ("aico-fb-" + "a" * 32, "aico-fb-" + "a" * 32, True),
        ("aico-fb-" + "a" * 32, "run-1", False),
        ("aico-pm-cmd-" + "a" * 32, "aico-pm-cmd-" + "a" * 32, False),
        ("toolu_0123456789abcdef", "toolu_0123456789abcdef", False),
    ],
)
def test_reserved_invocation(tool_call_id: str, run_id: str, ok: bool) -> None:
    assert afb.reserved_invocation_ok(tool_call_id, run_id) is ok


# --- MCP 境界 ------------------------------------------------------------------------------


async def test_hidden_from_list_tools(store: _Store) -> None:
    server = _server(store)
    result = await server.request_handlers[types.ListToolsRequest](
        types.ListToolsRequest(method="tools/list")
    )
    assert TOOL not in {tool.name for tool in result.root.tools}


def test_toolspec_named_answer_feedback_is_refused() -> None:
    with pytest.raises(RuntimeError):
        build_server([ToolSpec(TOOL, "x", _EchoSkill)], require_rls=False)


async def test_flag_off_is_unknown_tool(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    monkeypatch.setenv(afb.ANSWER_FEEDBACK_FLAG_ENV, "0")
    payload = await _call(_server(store), _signed({"feedback_token": _token(), "rating": 1}))
    assert payload == {"error": f"unknown tool: {TOOL}"}
    assert store.rows == []


async def test_records_thumb_up_and_overwrite_by_append(store: _Store) -> None:
    server = _server(store)
    first = await _call(server, _signed({"feedback_token": _token(), "rating": 1}))
    second = await _call(server, _signed({"feedback_token": _token(), "rating": -1}))
    assert first == {"ok": True, "rating": 1}
    assert second == {"ok": True, "rating": -1}
    assert [row.rating for row in store.rows] == [1, -1]
    row = store.rows[-1]
    assert row.user_email == EMAIL.lower()  # resolver が解決した本人（小文字）
    assert row.query == QUERY
    assert row.answer_id == ANSWER_ID
    assert row.search_session_id == f"slack-{ANSWER_ID}"


async def test_other_presser_rejected_and_not_saved(store: _Store) -> None:
    payload = await _call(
        _server(store), _signed({"feedback_token": _token(), "rating": 1}, user_id=OTHER_USER)
    )
    assert _code(payload) == "AFB_NOT_OWNER"
    assert store.rows == []


async def test_tampered_token_rejected(store: _Store) -> None:
    payload = await _call(
        _server(store),
        _signed({"feedback_token": _tamper_payload(_token(), q="改ざん"), "rating": 1}),
    )
    assert _code(payload) == "AFB_TOKEN_INVALID"
    assert store.rows == []


async def test_expired_token_rejected(store: _Store) -> None:
    payload = await _call(
        _server(store, now=TEST_NOW),
        _signed({"feedback_token": _token(expires_at=TEST_NOW - 1), "rating": 1}),
    )
    assert _code(payload) == "AFB_TOKEN_EXPIRED"
    assert store.rows == []


async def test_model_path_invocation_rejected(store: _Store) -> None:
    payload = await _call(
        _server(store),
        _signed(
            {"feedback_token": _token(), "rating": 1},
            tool_call_id="toolu_0123456789abcdef",
            run_id="11111111-1111-4111-8111-111111111111",
        ),
    )
    assert _code(payload) == "AFB_INVOCATION_REJECTED"
    assert store.rows == []


async def test_unsigned_call_rejected(store: _Store) -> None:
    payload = await _call(
        _server(store),
        {
            "feedback_token": _token(),
            "rating": 1,
            "_user_context": {"slack_user_id": TEST_SLACK_USER_ID},
        },
    )
    assert _code(payload) == "AFB_CALLER_REJECTED"
    assert store.rows == []


async def test_claim_for_another_tool_rejected(store: _Store) -> None:
    payload = await _call(
        _server(store), _signed({"feedback_token": _token(), "rating": 1}, tool="search")
    )
    assert _code(payload) == "AFB_CALLER_REJECTED"


@pytest.mark.parametrize(
    "business",
    [
        {"feedback_token": _token(), "rating": 0},
        {"feedback_token": _token(), "rating": True},
        {"feedback_token": _token(), "rating": "1"},
        {"feedback_token": _token()},
        {"feedback_token": _token(), "rating": 1, "query": "x"},
    ],
)
async def test_invalid_input_rejected(store: _Store, business: dict[str, Any]) -> None:
    payload = await _call(_server(store), _signed(business))
    assert _code(payload) == "AFB_INVALID_INPUT"
    assert store.rows == []


async def test_unresolved_identity_rejected(store: _Store) -> None:
    async def nobody(slack_user_id: str) -> ResolvedIdentity | None:
        return None

    payload = await _call(
        _server(store, resolver=nobody), _signed({"feedback_token": _token(), "rating": 1})
    )
    assert _code(payload) == "AFB_IDENTITY_REJECTED"
    assert store.rows == []


@pytest.mark.parametrize(
    "error",
    [
        AnswerFeedbackStoreError("insert_failed:OperationalError"),
        RuntimeError("db down: secret-ish"),
    ],
)
async def test_store_failure_returns_fixed_code(store: _Store, error: Exception) -> None:
    store.fail = error
    payload = await _call(_server(store), _signed({"feedback_token": _token(), "rating": 1}))
    assert payload == {"error": "answer_feedback_rejected", "code": "AFB_STORE_FAILED"}


async def test_replayed_claim_rejected(store: _Store) -> None:
    server = _server(store)
    arguments = _signed({"feedback_token": _token(), "rating": 1})
    assert (await _call(server, arguments)) == {"ok": True, "rating": 1}
    payload = await _call(server, arguments)
    assert _code(payload) == "AFB_CALLER_REJECTED"
    assert len(store.rows) == 1


# --- 保存先（SQL の形） ---------------------------------------------------------------------


class _Cursor:
    def __init__(self, sink: list[tuple[str, list[Any]]], fail: Exception | None) -> None:
        self._sink = sink
        self._fail = fail

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: list[Any]) -> None:
        if self._fail is not None:
            raise self._fail
        self._sink.append((sql, params))


class _Pg:
    def __init__(self, fail: Exception | None = None) -> None:
        self.executed: list[tuple[str, list[Any]]] = []
        self.connections: list[dict[str, Any]] = []
        self._fail = fail

    def connection(self, **kwargs: Any) -> Any:
        pg = self

        class _Conn:
            def __enter__(self) -> Any:
                pg.connections.append(kwargs)
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def cursor(self) -> _Cursor:
                return _Cursor(pg.executed, pg._fail)

        return _Conn()


def _row(rating: int = 1) -> AnswerFeedbackRow:
    return AnswerFeedbackRow(
        user_email="member@vectorinc.co.jp",
        query=QUERY,
        rating=rating,
        answer_id=ANSWER_ID,
        search_session_id=f"slack-{ANSWER_ID}",
    )


def test_pg_store_inserts_with_app_role_and_no_conflict_clause() -> None:
    pg = _Pg()
    PgAnswerFeedbackStore(pg).insert(_row(-1))
    assert pg.connections == [{"app_role": "teamagent_app", "user_email": "member@vectorinc.co.jp"}]
    sql, params = pg.executed[0]
    assert sql == INSERT_SQL
    assert "ON CONFLICT" not in sql.upper() and "RETURNING" not in sql.upper()
    assert params == ["member@vectorinc.co.jp", QUERY, -1, f"slack-{ANSWER_ID}", ANSWER_ID]


def test_pg_store_wraps_errors_without_row_content() -> None:
    pg = _Pg(fail=RuntimeError(f"permission denied {QUERY}"))
    with pytest.raises(AnswerFeedbackStoreError) as excinfo:
        PgAnswerFeedbackStore(pg).insert(_row())
    assert QUERY not in str(excinfo.value)
    assert excinfo.value.code == "insert_failed:RuntimeError"


@pytest.mark.parametrize(
    "changes",
    [
        {"user_email": "Upper@x.jp"},
        {"query": "  "},
        {"rating": 0},
        {"answer_id": "xyz"},
        {"search_session_id": "slack_bad id"},
    ],
)
def test_row_validation(changes: dict[str, Any]) -> None:
    base = {
        "user_email": "member@vectorinc.co.jp",
        "query": QUERY,
        "rating": 1,
        "answer_id": ANSWER_ID,
        "search_session_id": f"slack-{ANSWER_ID}",
    }
    with pytest.raises(AnswerFeedbackStoreError):
        AnswerFeedbackRow(**{**base, **changes})
