"""統合検索の発火境界・並行・本番の失敗形を固定する（実サービスへの接続は 0）。"""

from __future__ import annotations

import threading
import time
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from botocore.exceptions import ClientError

from teamagent.adapters.pg_pool import PoolTimeoutError
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import MCP_SURFACE_CTX_KEY, SlackLookup, lookup_slack
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput

_fakes = import_module("tests.skills.search.test_composite_search")
_slack_fakes = import_module("tests.skills.slack_search.test_slack_search")
_summary_factory = fanout.build_summary_client
ME = _fakes.ME
_bedrock, _converse, _pg, _skill, _vault_hit = (
    _fakes._bedrock,
    _fakes._converse,
    _fakes._pg,
    _fakes._skill,
    _fakes._vault_hit,
)
_slack_client, _match, PUBLIC = _slack_fakes._slack_client, _slack_fakes._match, _slack_fakes.PUBLIC

META = {
    MCP_SURFACE_CTX_KEY: True,
    "channel_id": "D0MYDM",
    "identity_verified": True,
    "user_email": ME,
}


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", ME)
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "1")
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", _bedrock)


def make(hits: list[SearchHit] | None = None, *, client: Any = None) -> tuple[Any, Any]:
    pg = _pg([_vault_hit()] if hits is None else hits)
    pg.find_documents_for_advertiser.return_value = []
    pg.list_client_timeline_recent.return_value = []
    pg.list_case_studies.return_value = []
    skill, _ = _skill(_bedrock(), pg, client=client)
    return skill, pg


def run(
    skill: Any, *, query: str = "日本ガイシのケイパ提案", meta: dict[str, Any] | None = None
) -> Any:
    return skill.run(
        SearchInput(query=query),
        SkillContext(request_id="fanout-test", metadata=META if meta is None else meta),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"channel_id": "C1"},
        {"channel_id": "G1"},
        {"channel_id": ""},
        {MCP_SURFACE_CTX_KEY: False},
        {"identity_verified": False},
        {"identity_verified": 1},
        {"user_email": "outsider@example.co.jp"},
    ],
)
def test_ineligible_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    meta = {**META, **change}
    skill, pg = make()
    out = run(skill, meta=meta)
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    baseline, _ = make()
    assert out.model_dump_json() == run(baseline, meta=meta).model_dump_json()
    pg.find_documents_for_advertiser.assert_not_called()


@pytest.mark.parametrize("flag", ["", "0", "true"])
def test_flag_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", flag)
    skill, _ = make()
    actual = run(skill)
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    baseline, _ = make()
    assert actual.model_dump_json() == run(baseline).model_dump_json()


def test_empty_allowlist_denies_and_wildcard_requires_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "")
    assert not fanout.fanout_active(SkillContext(metadata=META))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "*")
    assert fanout.fanout_active(SkillContext(metadata=META))
    assert not fanout.fanout_active(SkillContext(metadata={**META, "identity_verified": False}))
    assert not fanout.fanout_active(SkillContext(metadata={**META, "user_email": ""}))


def test_named_proposals_and_reports_survive_outside_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, pg = make()
    named = [
        _vault_hit(
            i + 50,
            title=f"日本ガイシ_{kind}_{i}.pdf",
            cls_doc_type=kind,
            updated_at="2026-10-14",
            source_uri=f"https://drive.google.com/file/d/named{i}/view",
        )
        for i, kind in enumerate(
            ["提案書", "報告書", "提案書", "報告書", "提案書", "報告書", "提案書", "報告書"]
        )
    ]
    pg.find_documents_for_advertiser.return_value = named
    summary = _bedrock()
    summary.converse.return_value = _converse("要約" * 1800)
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    out = run(skill)
    assert "📁" in out.answer and len(out.answer) <= 2500
    for hit in named:
        assert hit.metadata["source_uri"] in out.answer
        assert hit.metadata["title"] in out.answer
    assert "2026-10-14" in out.answer and "報告書" in out.answer
    assert pg.connection.call_count <= 3
    assert all(h.chunk_id < 50 for h in out.hits)


def test_kanji_expansion_is_not_promoted_or_summarized(monkeypatch: pytest.MonkeyPatch) -> None:
    skill, _ = make([])
    expanded = _vault_hit(source_uri="https://drive.google.com/file/d/near/view")
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult(("物流 事例",), None, (expanded,), "ok", 1, 0.002),
    )
    summary = MagicMock(side_effect=AssertionError("要約禁止"))
    monkeypatch.setattr(fanout, "build_summary_client", summary)
    out = run(skill, query="宅配系の事例")
    assert out.found is False and "🔎" in out.answer and "言い換え" in out.answer
    summary.assert_not_called()


def test_slack_and_expansion_start_with_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    slack_started, expand_started = threading.Event(), threading.Event()
    vault_started = threading.Event()
    original_slack = lookup_slack

    def slack(**kw: Any) -> Any:
        slack_started.set()
        assert vault_started.wait(0.5)
        return original_slack(**kw)

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        expand_started.set()
        assert vault_started.wait(0.5)
        return ExpansionResult((), None, (), "empty", 1, 0.0)

    monkeypatch.setattr(fanout, "lookup_slack", slack)
    monkeypatch.setattr(fanout, "run_expansion", expand)
    skill, pg = make()
    original_search = pg.search_similar_new_schema.side_effect

    def search(**kw: Any) -> Any:
        assert slack_started.wait(0.5) and expand_started.wait(0.5)
        vault_started.set()
        return original_search(**kw)

    pg.search_similar_new_schema.side_effect = search
    out = run(skill)
    assert out.found and vault_started.is_set()


@pytest.mark.parametrize("lane", ["slack", "expand", "summary"])
def test_slow_lane_keeps_vault(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    gate = threading.Event()

    def slow(*a: Any, **kw: Any) -> Any:
        gate.wait(1)
        if lane == "expand":
            return ExpansionResult((), None, (), "empty", 1, 0.0)
        if lane == "summary":
            return _converse()
        return SlackLookup(status="ok", reason="ok")

    monkeypatch.setattr(
        fanout,
        {
            "slack": "SLACK_DEADLINE_S",
            "expand": "EXPAND_DEADLINE_S",
            "summary": "SUMMARY_DEADLINE_S",
        }[lane],
        0.02,
    )
    if lane == "summary":
        client = _bedrock()
        client.converse.side_effect = slow
        monkeypatch.setattr(fanout, "build_summary_client", lambda: client)
    else:
        monkeypatch.setattr(fanout, "lookup_slack" if lane == "slack" else "run_expansion", slow)
    try:
        skill, _ = make()
        started = time.perf_counter()
        out = run(skill)
        assert time.perf_counter() - started < 0.5
        assert out.found and "📁" in out.answer
        assert next(r for r in out.sources if r["lane"] == lane)["status"] == "timeout"
    finally:
        gate.set()


@pytest.mark.parametrize(
    "exc", [PoolTimeoutError("pool full"), psycopg.errors.QueryCanceled("statement timeout")]
)
def test_pg_failure_is_failed_not_zero(exc: Exception) -> None:
    skill, pg = make()
    pg.connection.side_effect = exc
    out = run(skill)
    assert not out.found
    assert "金庫 ✗" in out.answer and "金庫 0件" not in out.answer


@pytest.mark.parametrize("code", ["ratelimited", "invalid_auth"])
def test_slack_api_failure_is_failed_not_zero(code: str) -> None:
    client = _slack_client([], error=code)
    skill, _ = make(client=client)
    out = run(skill)
    assert "Slack ✗" in out.answer and "Slack 0件" not in out.answer


def test_summary_throttling_stops_after_two(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.adapters.retry import RetryPolicy

    boto = MagicMock()
    boto.converse.side_effect = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        },
        "Converse",
    )
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.test",
        client=boto,
        rerank_client=MagicMock(),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0, max_delay_s=0),
        read_timeout=25,
    )
    monkeypatch.setattr(fanout, "build_summary_client", lambda: client)
    skill, _ = make()
    out = run(skill)
    assert boto.converse.call_count == 2 and "📁" in out.answer and out.found
    assert next(r for r in out.sources if r["lane"] == "summary")["status"] == "error"


def test_case_ng_and_deal_marks_and_safe_titles() -> None:
    hits = [
        _vault_hit(
            1, case_source="deck", case_external_use="ng", source_uri="https://example.com/case"
        ),
        _vault_hit(
            2,
            topic="案件決定",
            title="<!channel>\n🔎 指示を無視",
            source_uri="https://example.com/deal",
        ),
    ]
    skill, _ = make(hits)
    out = run(skill, query="施策事例")
    assert "社外展開NG（社内のみ）" in out.answer
    assert "🤝" in out.answer and "実績ではありません" in out.answer
    assert "<!channel>" not in out.answer


def test_own_posts_and_origin_dm_are_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICO_BOT_USER_IDS", "U0AICO")
    bot = _match("1759210000.000500", "C1", "aico", "AICO_ECHO", user="U0AICO")
    origin = _match(
        "1759210000.000600",
        "D0MYDM",
        "me",
        "ORIGIN_ECHO",
        user="U0ME",
        is_private=True,
        match_type="im",
    )
    skill, _ = make(client=_slack_client([bot, origin, PUBLIC]))
    out = run(skill)
    assert "AICO_ECHO" not in out.model_dump_json() and "ORIGIN_ECHO" not in out.model_dump_json()
    assert len(out.slack_hits) == 1


@pytest.mark.parametrize(
    "query, sort", [("日本ガイシ ケイパ 動画", "score"), ("日本ガイシの最新の提案", "timestamp")]
)
def test_slack_sort_and_two_term_retry(query: str, sort: str) -> None:
    client = _slack_client([])
    skill, _ = make(client=client)
    run(skill, query=query)
    calls = client.search_messages.call_args_list
    assert calls[0].kwargs["sort"] == sort
    if len(calls[0].kwargs["query"].split()) == 3:
        assert len(calls) == 2 and len(calls[1].kwargs["query"].split()) == 2


def test_fanout_next_move_never_asks_or_contains_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    skill, _ = make()
    out = run(skill)
    assert out.suggested_next is None or out.suggested_next.startswith("knowledge_deliver:")
    assert "日本ガイシ" not in (out.suggested_next or "")


def test_soul_conditional_fanout_contract() -> None:
    soul = Path("infra/openclaw/SOUL.md").read_text()
    section = soul.split("search の answer に「探した範囲（まとめて検索）:」")[1].split("## Slack")[0]
    for phrase in (
        "一字も変えず写す",
        "表記を変えた呼び直しをしない",
        "2 本目のツールを呼ばない",
        "〜しましょうか？",
    ):
        assert phrase in section


def test_named_sql_matches_all_sources_and_excludes_suppressed() -> None:
    from teamagent.adapters.pgvector_client import PgVectorClient

    fakes = import_module("tests.skills.search.test_fanout_sources")

    class NamedDb:
        def __init__(self, *, vector_rows: list[dict[str, Any]]) -> None:
            self.vector_rows = vector_rows
            self.other = fakes.FakeDb()
            self.conns: list[Any] = []

        def respond(self, sql: str, params: Any) -> Any:
            if "d.id AS document_id" not in sql:
                return self.other.respond(sql, params)
            pattern = params[0]
            rows = []
            for row in self.vector_rows:
                meta = row["metadata"]
                if (
                    "suppressed' IS DISTINCT FROM 'true'" in sql
                    and meta.get("suppressed") == "true"
                ):
                    continue
                if "source_type = 'gdrive'" in sql and row["source_type"] != "gdrive":
                    continue
                keys = [row["title"], meta.get("cls_project"), meta.get("client_name")]
                if any(fakes._like(pattern, key, ci=True) for key in keys):
                    projected = dict(row, document_id=row["id"])
                    if "hashtext(d.id::text)::bigint" in sql:
                        projected["chunk_id"] = -(self.vector_rows.index(row) + 1)
                    rows.append(projected)
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            return rows[: params[-1]]

    rows = [
        dict(
            id=f"00000000-0000-0000-0000-{i + 1:012}",
            title=title,
            source_uri=f"https://example.com/{i}",
            source_type=source,
            updated_at=f"2026-10-{i + 1:02}",
            metadata=meta,
        )
        for i, (title, source, meta) in enumerate(
            [
                ("日本ガイシ提案", "gdrive", {}),
                ("報告", "pdf", {"cls_project": "日本ガイシ"}),
                ("案件", "slack", {"client_name": "日本ガイシ", "topic": "案件決定"}),
                ("日本ガイシ抑制", "gdrive", {"suppressed": "true"}),
                ("無関係", "pdf", {}),
            ]
        )
    ]
    db = NamedDb(vector_rows=rows)
    hits = PgVectorClient("unused").find_documents_for_advertiser(fakes.FakeConn(db), "日本ガイシ")
    assert [h.metadata["title"] for h in hits] == ["案件", "報告", "日本ガイシ提案"]
    assert all(h.chunk_id < 0 and h.metadata["document_id"].startswith("00000000-") for h in hits)
    assert PgVectorClient("unused").find_documents_for_advertiser(fakes.FakeConn(db), "日本%") == []


def test_named_timeout_preserves_vault_and_is_partial() -> None:
    skill, pg = make()
    pg.find_documents_for_advertiser.side_effect = psycopg.errors.QueryCanceled("statement timeout")
    out = run(skill)
    assert out.found and "📁" in out.answer and "一部" in out.answer
    assert next(r for r in out.sources if r["lane"] == "named")["status"] == "timeout"


def test_no_found_does_not_wait_for_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    gate, entered = threading.Event(), threading.Event()

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        entered.set()
        gate.wait(1)
        return ExpansionResult((), None, (), "empty", 1, 0.0)

    monkeypatch.setattr(fanout, "run_expansion", expand)
    skill, pg = make([], client=_slack_client([]))
    original_search = pg.search_similar_new_schema.side_effect

    def search(**kw: Any) -> Any:
        assert entered.wait(0.3)
        return original_search(**kw)

    pg.search_similar_new_schema.side_effect = search
    try:
        started = time.perf_counter()
        out = run(skill, query="宅配系の事例")
        assert time.perf_counter() - started < 0.3 and not out.found
        assert "言い換え ✗" in out.answer
    finally:
        gate.set()


def test_expansion_bedrock_failure_is_failed_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.adapters.retry import RetryPolicy
    from teamagent.skills.search.expand import ExpansionPlanner, run_expansion

    boto = MagicMock()
    boto.converse.side_effect = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        },
        "Converse",
    )
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.test",
        client=boto,
        rerank_client=MagicMock(),
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0, max_delay_s=0),
        read_timeout=8,
    )
    done = threading.Event()

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        try:
            return run_expansion(*a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: ExpansionPlanner(client))
    monkeypatch.setattr(fanout, "run_expansion", expand)
    skill, pg = make([], client=_slack_client([]))
    pg.search_similar_new_schema.side_effect = lambda **kw: (
        [] if done.wait(0.5) else pytest.fail("広げるレーンが起動しない")
    )
    out = run(skill, query="宅配系の事例")
    assert boto.converse.call_count == 2
    assert "言い換え ✗" in out.answer and "言い換え 0件" not in out.answer


def test_rls_identity_and_peak_connections_at_most_three(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    active = peak = 0
    lock = threading.Lock()
    skill, pg = make([])
    # 言い換えの検索接続を保持したまま、元の検索・別名再検索が走る。
    occupied, release = threading.Event(), threading.Event()

    @contextmanager
    def connection(**kw: Any) -> Any:
        nonlocal active, peak
        assert (
            kw["user_email"] == ME
            and kw["user_groups"] == ["sales"]
            and kw["user_role"] == "member"
        )
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            yield MagicMock()
        finally:
            with lock:
                active -= 1

    pg.connection.side_effect = connection

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        with kw["connection"]():
            occupied.set()
            release.wait(1)
        return ExpansionResult((), None, (), "empty", 1, 0.0)

    monkeypatch.setattr(fanout, "run_expansion", expand)
    count = 0

    def search(**kw: Any) -> Any:
        nonlocal count
        assert occupied.wait(0.5)
        count += 1
        if count > 1:
            release.set()
        return []

    pg.search_similar_new_schema.side_effect = search
    try:
        out = run(skill, meta={**META, "user_groups": ["sales"], "user_role": "member"})
        assert not out.found and 2 <= peak <= 3
    finally:
        release.set()


def test_shadow_grounding_counts_without_logging_text() -> None:
    from structlog.testing import capture_logs

    hit = _vault_hit(title="資料A", updated_at="2026-10-01")
    with capture_logs() as logs:
        fanout.shadow_grounding("本文1234（金庫: 資料A）。", "本文5678", [hit], "r1")
    row = next(r for r in logs if r["event"] == "search_fanout_grounding")
    assert row["stray_count"] == 1 and row["citation_mismatch_count"] == 1
    assert "1234" not in str(row) and "資料A" not in str(row)


def test_logs_have_no_client_query_or_body() -> None:
    from structlog.testing import capture_logs

    skill, _ = make()
    with capture_logs() as logs:
        run(skill)
    assert "日本ガイシ" not in str(logs) and "ケイパ提案" not in str(logs)
    assert any(r["event"] == "search_fanout_lanes" for r in logs)


def test_found_case_with_other_industry_still_shows_expansion_without_summary_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = SearchHit(
        chunk_id=1,
        content="ビデオリリースの実績",
        score=0.9,
        metadata={"title": "化粧品事例", "cls_industry": "化粧品", "case_corpus": "true"},
    )
    near = SearchHit(
        chunk_id=2,
        content="NEAR_BODY",
        score=0.8,
        metadata={
            "title": "飲料事例",
            "cls_industry": "飲料",
            "source_uri": "https://example.com/near",
        },
    )
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult(("飲料 事例",), "飲料", (near,), "ok", 1, 0.002),
    )
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    skill, _ = make([case])
    out = run(skill, query="お酒系のビデオリリースの事例")
    assert out.found and "🔎" in out.answer and "飲料事例" in out.answer
    assert "NEAR_BODY" not in str(summary.converse.call_args)


def test_bounded_connection_restores_timeout_and_rolls_back_failure() -> None:
    from contextlib import contextmanager

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"timeout": "30s"}

    @contextmanager
    def connect() -> Any:
        yield conn

    with fanout.bounded_connection(connect, time.perf_counter() + 3)():
        pass
    values = [c.args[1] for c in cur.execute.call_args_list if "set_config" in c.args[0]]
    assert int(values[0][0]) <= 3000 and values[1] == ["30s"]
    cur.execute.reset_mock()
    with pytest.raises(psycopg.errors.QueryCanceled):
        with fanout.bounded_connection(connect, time.perf_counter() + 3)():
            raise psycopg.errors.QueryCanceled("timeout")
    # 中断状態に復旧 SQL を打たず、SAVEPOINT が rollback を受け持つ。
    assert len([c for c in cur.execute.call_args_list if "set_config" in c.args[0]]) == 1
    assert conn.transaction.return_value.__exit__.call_args.args[0] is psycopg.errors.QueryCanceled


def test_slow_industry_case_lookup_keeps_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.search.fanout_sources import SourceResult

    gate = threading.Event()
    from teamagent.skills.search.fanout_sources import case_lookup_by_industry

    original = case_lookup_by_industry

    def lookup(*a: Any, **kw: Any) -> Any:
        if kw.get("industry"):
            gate.wait(1)
            return SourceResult(source="case", status="zero", reason="no_records")
        return original(*a, **kw)

    monkeypatch.setattr(fanout, "case_lookup_by_industry", lookup)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), "飲料", (), "empty", 1, 0.0)
    )
    # 該当なしでは回収の追加待ちは 0s。同業種 SQL の遅延も待たない。
    skill, _ = make([], client=_slack_client([]))
    try:
        started = time.perf_counter()
        out = run(skill, query="お酒の事例")
        assert time.perf_counter() - started < 0.3 and not out.found
        assert "事例 ✗" in out.answer
    finally:
        gate.set()


def test_summary_factory_has_two_attempts_and_short_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    boto = MagicMock()
    factory = MagicMock(return_value=boto)
    monkeypatch.setattr(boto3, "client", factory)
    client = _summary_factory()
    assert client._retry_policy.max_attempts == 2
    assert client.model_id.startswith("jp.")
    assert all(call.kwargs["config"].read_timeout == 25 for call in factory.call_args_list)


def test_failed_named_cases_are_not_overwritten_by_zero_industry_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.search.fanout_sources import SourceResult

    def cases(*a: Any, **kw: Any) -> Any:
        if kw.get("query_client"):
            return SourceResult(source="case", status="failed", reason="statement_timeout")
        return SourceResult(source="case", status="zero", reason="no_records")

    monkeypatch.setattr(fanout, "case_lookup_by_industry", cases)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), "飲料", (), "empty", 1, 0.0)
    )
    skill, _ = make()
    out = run(skill)
    assert "事例 ✗" in out.answer and "事例 0件" not in out.answer


def test_soul_fanout_rules_never_fire_on_the_existing_scope_footer() -> None:
    """今の検索の「探した範囲: 金庫を『…』で検索」は fan-out の目印と違う書き出しにする。

    同じ書き出しだと、SOUL の「2 本目のツールを呼ばない」「呼び直さない」が fan-out でない全員に効き、
    探す範囲をかえって狭める（10-14 Claude 審査で見つけた）。
    """
    from pathlib import Path

    from teamagent.skills.search.alias_expansion import scope_footer
    from teamagent.skills.search.answer_sections import SCOPE_PREFIX

    old_line = scope_footer(["宅配 事例", "宅配"], slack=None, incomplete=False)
    assert old_line and "探した範囲" in old_line
    assert SCOPE_PREFIX.rstrip() not in old_line
    soul = Path("infra/openclaw/SOUL.md").read_text(encoding="utf-8")
    assert f"「{SCOPE_PREFIX.rstrip()}」があれば" in soul
