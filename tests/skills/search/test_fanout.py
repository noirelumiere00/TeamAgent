"""統合検索の発火境界・並行・本番の失敗形を固定する（実サービスへの接続は 0）。"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
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
from teamagent.skills.search.not_found import NOT_FOUND_HEAD
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
    monkeypatch.delenv("SEARCH_FANOUT_LIST_FAST", raising=False)
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
    assert out.found is False and "宅配系の事例は見つかりませんでした" in out.answer
    assert "🔎" not in out.answer and "near/view" not in out.answer
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
    assert "実績ではありません" in out.answer
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
        assert len(calls) == 3
        assert [len(c.kwargs["query"].split()) for c in calls] == [3, 2, 1]


def test_fanout_next_move_never_asks_or_contains_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    skill, _ = make()
    out = run(skill)
    assert out.suggested_next is None or out.suggested_next.startswith("knowledge_deliver:")
    assert "日本ガイシ" not in (out.suggested_next or "")


def test_soul_conditional_fanout_contract() -> None:
    soul = Path("infra/openclaw/SOUL.md").read_text()
    section = soul.split("search の answer に「探した範囲（まとめて検索）:」")[1].split("## Slack")[
        0
    ]
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
    gate, entered, finished = threading.Event(), threading.Event(), threading.Event()

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        entered.set()
        # 10s は待つ実装に戻ったときに CI を止めないための上限。正常時は finally で即解放。
        gate.wait(10)
        finished.set()
        return ExpansionResult((), None, (), "empty", 1, 0.0)

    monkeypatch.setattr(fanout, "run_expansion", expand)
    skill, pg = make([], client=_slack_client([]))
    original_search = pg.search_similar_new_schema.side_effect

    def search(**kw: Any) -> Any:
        assert entered.wait(10)
        return original_search(**kw)

    pg.search_similar_new_schema.side_effect = search
    try:
        out = run(skill, query="宅配系の事例")
        # 経過秒でなく「広げるレーンが終わる前に返った」ことで待たないことを確かめる。
        assert not finished.is_set() and not out.found
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


@pytest.mark.parametrize(
    "query", ["伊藤ハムの資料を一覧で", "提案書をリストで出して", "資料は何件ある？"]
)
def test_list_fast_skips_summary_and_keeps_links(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    summary = _bedrock()
    factory = MagicMock(return_value=summary)
    monkeypatch.setattr(fanout, "build_summary_client", factory)
    hit = _vault_hit(title="提案書", source_uri="https://drive.google.com/file/d/list/view")
    skill, _ = make([hit])
    with capture_logs() as logs:
        out = run(skill, query=query)
    factory.assert_not_called()
    summary.converse.assert_not_called()
    skill._bedrock.converse.assert_not_called()
    assert out.found
    assert fanout.LIST_FAST_HEAD in out.answer
    assert "📁" in out.answer and "提案書" in out.answer
    assert hit.metadata["source_uri"] in out.answer
    assert out.answer.splitlines()[-1].startswith("探した範囲（まとめて検索）:")
    lane = next(r for r in out.sources if r["lane"] == "summary")
    assert lane["status"] == "skipped"
    row = next(r for r in logs if r["event"] == "search_latency_breakdown")
    assert row["list_fast"] is True and row["converse_ms"] == 0
    assert "伊藤ハム" not in str(logs) and query not in str(logs)


def test_list_fast_keeps_campaign_footers(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.mcp_gateway.direct_video_metrics import extract_section

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    hit = SearchHit(
        chunk_id=20,
        content=(
            "上位の投稿（再生数順）:\n"
            "1. 再生 507,000、保存 120、たべもの、ハンバーグ https://www.tiktok.com/@example/video/1"
        ),
        score=0.9,
        metadata={
            "campaign_aggregate": "true",
            "advertiser": "伊藤ハム",
            "campaign": "ハンバーグ",
            "title": "施策実績 伊藤ハム ハンバーグ",
            "related_files": [
                {
                    "title": "ハンバーグ報告書",
                    "url": "https://drive.google.com/file/d/report/view",
                    "kind": "レポート",
                }
            ],
        },
    )
    skill, _ = make([_vault_hit(), hit])
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    out = run(skill, query="伊藤ハムの資料を一覧で")
    summary.converse.assert_not_called()
    assert "📎" in out.answer and "📊 動画ごとの数字（ショート動画データベース）" in out.answer
    assert "https://drive.google.com/file/d/report/view" in out.answer
    assert "https://www.tiktok.com/@example/video/1" in out.answer
    assert "507,000" in out.answer and "120" in out.answer
    assert out.answer.splitlines()[-1].startswith("探した範囲（まとめて検索）:")
    section = extract_section(out.answer)
    assert section is not None
    prefix, metrics, suffix = section
    assert fanout.LIST_FAST_HEAD in prefix and "📎" in prefix
    assert "507,000" in metrics and "120" in metrics
    assert "📁" not in metrics and "探した範囲" not in metrics
    assert "📁" in suffix and "探した範囲（まとめて検索）:" in suffix


def test_iter4_list_fast_keeps_campaign_sheet_rows_when_answer_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_campaign_files import _video, _video_campaign

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    campaigns = [
        _video_campaign(
            20 + i,
            "伊藤ハム",
            f"施策{i}",
            [
                _video(f"https://example.test/{i}/{n}", "投稿の説明" * 8, 507_700, n)
                for n in range(1, 6)
            ],
        )
        for i in range(2)
    ]
    for hit in campaigns:
        hit.metadata["source_uri"] = "https://docs.google.com/spreadsheets/d/campaign/edit#gid=0"
        hit.metadata["client_name"] = "伊藤ハム"
    skill, pg = make([_vault_hit(), *campaigns])
    pg.list_client_names.return_value = ["伊藤ハム"]
    monkeypatch.setattr(skill, "_match_client", lambda *a, **kw: "伊藤ハム")
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    pg.find_documents_for_advertiser.return_value = [
        _vault_hit(
            50 + i,
            title="伊藤ハム_提案書_" + "長い資料名" * 9,
            source_uri=f"https://drive.google.com/file/d/{'x' * 330}{i}/view",
        )
        for i in range(3)
    ]
    out = run(skill, query="伊藤ハムの資料を一覧で出して")
    assert "🏆 事例・施策実績（2件）" in out.answer
    for hit in campaigns:
        assert hit.metadata["source_uri"] in out.answer
        assert hit.metadata["title"] in out.answer
    assert len(out.answer) <= 2500


@pytest.mark.parametrize("fanout_enabled", [True, False])
@pytest.mark.parametrize("target", ["金融", "メタバース"])
def test_iter4_zero_matching_cases_never_fill_with_unrelated_results(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, target: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    unrelated = _vault_hit(title="食品メーカーの施策", cls_industry="食品")
    skill, _ = make([unrelated])
    wrong = "見つかりませんでした。食品の事例です。銀行で検索してと言ってもらえれば探せます。"
    monkeypatch.setattr(skill, "_summarize", MagicMock(return_value=(wrong, 0.0)))
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult(("別の事例",), "食品", (unrelated,), "ok", 1, 0.0),
    )
    out = run(skill, query=f"{target}の事例を教えて")
    assert (
        out.answer
        == f"{target}の事例は見つかりませんでした（探した範囲: 社内資料・施策実績・Slack）。"
    )
    assert out.found is False
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [True, False])
@pytest.mark.parametrize("industry_evidence", ["client_industry", "content"])
def test_iter4_food_campaign_survives_missing_campaign_industry_without_false_absence(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, industry_evidence: str
) -> None:
    from tests.skills.search.test_campaign_files import _video, _video_campaign

    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    food = _video_campaign(
        20,
        "伊藤ハム",
        "ハンバーグ",
        [_video("https://example.test/food/1", "アレンジレシピ", 507_700, 261)],
    )
    food.metadata.pop("cls_industry", None)
    food.metadata["campaign_industry"] = ""
    food.metadata["source_uri"] = "https://docs.google.com/spreadsheets/d/food/edit#gid=1"
    if industry_evidence == "client_industry":
        food.metadata["client_industry"] = "食品"
    else:
        food = replace(food, content=food.content + "\n食品メーカーのショート動画施策。")
    other = _vault_hit(30, title="アパレル社の施策", cls_industry="アパレル")
    skill, _ = make([food, other])
    skill._result_guard = True
    summary = MagicMock(return_value=("伊藤ハムのハンバーグの事例です。", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query="食品メーカーのショート動画の事例ってある？", filter_industry="食品"),
        SkillContext(request_id="iter4-food", metadata=META),
    )
    assert out.found
    assert "見つかりません" not in out.answer
    assert "アパレル" not in out.answer
    if fanout_enabled:
        assert "https://docs.google.com/spreadsheets/d/food/edit#gid=1" in out.answer
        summary.assert_not_called()
    else:
        assert "https://example.test/food/1" in out.answer
        assert [h.chunk_id for h in summary.call_args.args[1]] == [20]


def test_iter4_fanout_answer_links_include_summary_slack_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.search.composite import SlackItem
    from teamagent.skills.search.schema import SlackHitOut

    items = tuple(
        SlackItem(
            hit=SlackHitOut(
                channel=f"#project-{i}",
                permalink=f"https://vectorinc.slack.com/archives/CPROJECT/p{i}",
            ),
            text=f"案件{i}の説明",
        )
        for i in range(3)
    )
    monkeypatch.setattr(
        fanout, "lookup_slack", lambda **kw: SlackLookup(status="ok", reason="ok", items=items)
    )
    skill, _ = make()
    monkeypatch.setattr(
        skill, "_summarize", lambda *a, **kw: ("案件2。施策管理シートもあります。", 0.0)
    )
    out = run(skill, query="案件2って何？うちも関わってる？")
    for item in items:
        assert item.hit.permalink in out.answer


def test_iter4_case_miss_preserves_include_answer_false() -> None:
    skill, _ = make([])
    out = skill.run(
        SearchInput(query="金融の事例を教えて", include_answer=False), SkillContext(metadata=META)
    )
    assert not out.found and out.answer == ""


def test_iter4_industry_list_excludes_other_industry_campaigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_campaign_files import _video, _video_campaign

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    food = _video_campaign(
        20,
        "伊藤ハム",
        "ハンバーグ",
        [_video("https://example.test/food/1", "レシピ", 507_700, 261)],
    )
    other = _video_campaign(
        21, "服飾会社", "夏服", [_video("https://example.test/clothes/1", "服", 500_000, 110)]
    )
    other.metadata["cls_industry"] = "アパレル"
    skill, _ = make([food, other])
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    out = run(skill, query="食品の資料を一覧で出して")
    assert out.found and "伊藤ハム" in out.answer
    assert "服飾会社" not in out.answer and "夏服" not in out.answer


@pytest.mark.parametrize("summary_kind", ["empty", "long", "error"])
@pytest.mark.parametrize("wording", ["動画ごとの結果", "投稿ごとの結果", "1本ずつの結果"])
def test_iter3_campaign_metrics_survive_empty_or_long_summary_without_related_files(
    monkeypatch: pytest.MonkeyPatch, summary_kind: str, wording: str
) -> None:
    from structlog.testing import capture_logs

    from teamagent.mcp_gateway.direct_video_metrics import extract_section

    campaign_fakes = import_module("tests.skills.search.test_campaign_files")
    hits = [
        campaign_fakes._video_campaign(
            20 + i,
            "食品メーカー",
            name,
            [
                campaign_fakes._video(f"https://example.test/{i}/{n}", "食事のアレンジ", plays, n)
                for n, plays in enumerate((507_700, 331_300, 184_100, 122_500, 49_200), 1)
            ],
        )
        for i, name in enumerate(("ハンバーグ", "クイックディナー"))
    ]
    skill, pg = make(hits)
    skill._campaign_files = True
    pg.find_drive_files_for_advertisers.return_value = [
        {"advertiser": "食品メーカー", "title": "会社紹介", "source_uri": "gdrive://OTHER1"},
        {"advertiser": "食品メーカー", "title": "別商品提案書", "source_uri": "gdrive://OTHER2"},
    ]
    summary = _bedrock()
    summary.converse.return_value = _converse("説明です。" * 600 if summary_kind == "long" else "")
    if summary_kind == "error":
        summary.converse.side_effect = RuntimeError("summary unavailable")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    with capture_logs() as logs:
        out = run(skill, query=f"食品メーカーのハンバーグの施策の{wording}と、何が効いたか教えて")
    row = next(log for log in logs if log["event"] == "search_campaign_files")
    assert (row["campaigns"], row["attached"], row["candidates"]) == (2, 0, 2)
    section = extract_section(out.answer)
    assert section is not None
    assert "331,300 再生" in section[1] and "49,200 再生" in section[1]
    assert len(out.answer) <= 2500


@pytest.mark.parametrize("industry", [None, "金融"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_iter3_industry_metrics_exclude_unconfirmed_or_other_industries(
    monkeypatch: pytest.MonkeyPatch, industry: str | None, incomplete: bool
) -> None:
    from teamagent.mcp_gateway.direct_video_metrics import extract_section

    campaign_fakes = import_module("tests.skills.search.test_campaign_files")
    food = campaign_fakes._video_campaign(
        20,
        "食品メーカー",
        "ハンバーグ",
        [campaign_fakes._video("https://example.test/food", "食事", 331_300, 10)],
    )
    card = campaign_fakes._video_campaign(
        21,
        "JCB-USJ",
        "TikTok",
        [campaign_fakes._video("https://example.test/card", "旅行", 990_000, 20)],
    )
    food.metadata["cls_industry"] = "食品"
    card.metadata.pop("cls_industry", None)
    if industry:
        card.metadata["cls_industry"] = industry
    skill, pg = make([food, card])
    skill._campaign_files = True
    if incomplete:
        retrieve = skill._retrieve

        def partial(*args: Any, **kwargs: Any) -> Any:
            hits = retrieve(*args, **kwargs)
            kwargs["fanout_probe"]["incomplete"] = True
            return hits

        monkeypatch.setattr(skill, "_retrieve", partial)
    pg.find_drive_files_for_advertisers.return_value = []
    summary = _bedrock()
    summary.converse.return_value = _converse("食品の事例です。")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    out = run(skill, query="食品メーカーのショート動画施策の事例を探して")
    section = extract_section(out.answer)
    assert section is not None and "331,300 再生" in section[1]
    assert "JCB" not in section[1] and "990,000" not in section[1]


@pytest.mark.parametrize(
    "query",
    [
        "伊藤ハムの施策で何が効いた？",
        "比較して",
        "伊藤ハムの施策で何が効いたか一覧で",
        "提案書を一覧で比較して",
        "施策の傾向を一覧で",
        "最新の提案書どこ？",
    ],
)
def test_list_fast_preserves_summary_for_analysis_and_fact(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    skill, _ = make()
    with capture_logs() as logs:
        run(skill, query=query)
    summary.converse.assert_called_once()
    assert next(r for r in logs if r["event"] == "search_latency_breakdown")["list_fast"] is False


@pytest.mark.parametrize("flag", [None, "0", "true", ""])
def test_list_fast_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch, flag: str | None) -> None:
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    monkeypatch.delenv("SEARCH_FANOUT_LIST_FAST", raising=False)
    baseline, _ = make()
    expected = run(baseline, query="提案書をリストで出して").model_dump_json()
    if flag is not None:
        monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", flag)
    skill, _ = make()
    actual = run(skill, query="提案書をリストで出して")
    assert actual.model_dump_json() == expected
    assert fanout.LIST_FAST_HEAD not in actual.answer
    assert summary.converse.call_count == 2


def test_list_fast_not_found_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    baseline, _ = make([])
    expected = run(baseline, query="伊藤ハムの資料を一覧で").model_dump_json()
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    skill, _ = make([])
    out = run(skill, query="伊藤ハムの資料を一覧で")
    assert out.model_dump_json() == expected
    assert not out.found and out.answer.startswith(NOT_FOUND_HEAD)
    summary.converse.assert_not_called()


def test_list_fast_inactive_fanout_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    baseline, _ = make()
    expected = run(baseline, query="提案書をリストで出して").model_dump_json()
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    skill, _ = make()
    assert run(skill, query="提案書をリストで出して").model_dump_json() == expected
    skill._bedrock.converse.assert_called_once()


def test_list_fast_without_answer_skips_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    skill, _ = make()
    with capture_logs() as logs:
        out = skill.run(
            SearchInput(query="資料を一覧で", include_answer=False),
            SkillContext(request_id="fanout-test", metadata=META),
        )
    assert out.found and out.answer == ""
    summary.converse.assert_not_called()
    assert next(r for r in logs if r["event"] == "search_latency_breakdown")["list_fast"] is False


def test_other_industry_case_does_not_claim_a_match_or_show_unrelated_expansion(
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
    assert not out.found and "事例は見つかりませんでした" in out.answer
    assert "🔎" not in out.answer and "飲料事例" not in out.answer
    summary.converse.assert_not_called()


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

    gate, entered, finished = threading.Event(), threading.Event(), threading.Event()
    expanded = threading.Event()
    from teamagent.skills.search.fanout_sources import case_lookup_by_industry

    original = case_lookup_by_industry

    def lookup(*a: Any, **kw: Any) -> Any:
        if kw.get("industry"):
            entered.set()
            # 10s は待つ実装に戻ったときに CI を止めないための上限。正常時は finally で即解放。
            gate.wait(10)
            finished.set()
            return SourceResult(source="case", status="zero", reason="no_records")
        return original(*a, **kw)

    def expand(*a: Any, **kw: Any) -> ExpansionResult:
        expanded.set()
        return ExpansionResult((), "飲料", (), "empty", 1, 0.0)

    monkeypatch.setattr(fanout, "case_lookup_by_industry", lookup)
    monkeypatch.setattr(fanout, "run_expansion", expand)
    # 該当なしでは回収の追加待ちは 0s。同業種 SQL の遅延も待たない。
    # 「X の事例」は対象で絞り同業種 SQL を通らないため、絞らない聞き方にする。
    skill, pg = make([], client=_slack_client([]))
    original_search = pg.search_similar_new_schema.side_effect

    def search(**kw: Any) -> Any:
        # 言い換えの業種が回収時に揃っているよう、金庫の検索は言い換えの後にする。
        assert expanded.wait(10)
        return original_search(**kw)

    pg.search_similar_new_schema.side_effect = search
    try:
        out = run(skill, query="お酒のキャンペーン")
        # 経過秒でなく「同業種 SQL が終わる前に返った」ことで待たないことを確かめる。
        assert entered.is_set() and not finished.is_set() and not out.found
        assert out.answer.startswith(NOT_FOUND_HEAD)
        assert "🔎" not in out.answer
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


def test_graph_link_flag_and_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    nid = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.newstv.co.jp")
    monkeypatch.delenv("SEARCH_FANOUT_GRAPH_LINK", raising=False)
    skill, _ = make([_vault_hit(document_id=nid)])
    baseline = run(skill).answer
    for flag in ("0", "true"):
        monkeypatch.setenv("SEARCH_FANOUT_GRAPH_LINK", flag)
        skill, _ = make([_vault_hit(document_id=nid)])
        assert run(skill).answer.encode() == baseline.encode()
    monkeypatch.setenv("SEARCH_FANOUT_GRAPH_LINK", "1")
    skill, _ = make([_vault_hit(document_id=nid)])
    answer = run(skill).answer
    assert (
        f"🕸 関係図で見る: <https://connect.newstv.co.jp/search/graph/focus?seed={nid}>\n\n探した範囲"
        in answer
    )
    assert len(answer) <= 2500
    monkeypatch.delenv("CONNECT_BASE_URL")
    skill, _ = make([_vault_hit(document_id=nid)])
    assert run(skill).answer.encode() == baseline.encode()


def test_graph_link_non_fanout_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.newstv.co.jp")
    monkeypatch.setenv("SEARCH_FANOUT_GRAPH_LINK", "1")
    skill, _ = make()
    baseline = run(skill, meta={**META, "channel_id": "C1"}).model_dump_json()
    monkeypatch.setenv("SEARCH_FANOUT_GRAPH_LINK", "0")
    skill, _ = make()
    assert run(skill, meta={**META, "channel_id": "C1"}).model_dump_json() == baseline


@pytest.mark.parametrize("with_campaign", [True, False])
@pytest.mark.parametrize("list_fast", [True, False])
def test_timeline_reproduction_summary_and_sections(
    monkeypatch: pytest.MonkeyPatch,
    with_campaign: bool,
    list_fast: bool,
) -> None:
    from tests.skills.search.test_timeline_status import campaign, hit

    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1" if list_fast else "0")
    fb = hit(client_name="日本ガイシ", source_uri="https://example.com/fb")
    deal = hit(
        3,
        client_name="日本ガイシ",
        topic="案件決定",
        is_sales_fb=False,
        source_uri="https://example.com/deal",
    )
    actual = campaign(client_name="日本ガイシ", source_uri="https://example.com/campaign")
    skill, pg = make([fb, deal, *([actual] if with_campaign else [])])
    pg.list_client_timeline_recent.return_value = [fb]
    summary = _bedrock()
    summary.converse.return_value = _converse("要約器の文章")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    out = run(skill, query="日本ガイシの過去の提案資料と施策実績一覧")
    expected = (
        "実施済み（投稿 6 本・最終投稿 2026-08-15）"
        if with_campaign
        else "2026-06-15 時点では詰めの段階"
    )
    assert out.answer.count("- 状態: 日本ガイシ / 500万視聴トライアル: " + expected) == 2
    assert "現在は" not in out.answer
    assert out.answer.index("🤝") < out.answer.index("🗂")
    if not list_fast:
        text = summary.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
        assert expected in text
        assert "投稿日: 2026-06-15" in text
        assert out.answer.index("🗂") < out.answer.index("要約器の文章")
    else:
        summary.converse.assert_not_called()
    assert len(out.answer) <= 2500


def test_timeline_off_fanout_payload_and_summary_are_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_timeline_status import campaign, hit

    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    monkeypatch.delenv("SEARCH_TIMELINE_STATUS", raising=False)
    skill, _ = make([hit(client_name="日本ガイシ"), campaign(client_name="日本ガイシ")])
    baseline = run(skill).model_dump_json()
    prompt = summary.converse.call_args.kwargs
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "0")
    skill, _ = make([hit(client_name="日本ガイシ"), campaign(client_name="日本ガイシ")])
    assert baseline == run(skill).model_dump_json()
    assert prompt == summary.converse.call_args.kwargs


def test_timeline_latest_karte_record_controls_status_when_summary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_timeline_status import hit

    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")
    old = hit(client_name="日本ガイシ", source_uri="https://example.com/old")
    recent = hit(
        4,
        client_name="日本ガイシ",
        occurred_at="2026-09-01",
        deal_phase="終了",
        source_uri="https://example.com/new",
    )
    skill, pg = make([old])
    pg.list_client_timeline_recent.return_value = [recent]
    summary = _bedrock()
    summary.converse.side_effect = RuntimeError("fake failure")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    answer = run(skill, query="日本ガイシの提案資料と施策実績").answer
    assert "状態: 日本ガイシ / 500万視聴トライアル: 2026-09-01 時点では終了" in answer
    assert "時点では詰めの段階" not in answer


def test_timeline_unknown_date_section(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.skills.search.test_timeline_status import hit

    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")
    skill, _ = make([hit(client_name="日本ガイシ", occurred_at=None)])
    answer = run(skill).answer
    assert "状態は資料からは確認できません（最新の記録: 日付不明 の営業FB）" in answer


def test_public_nonmember_is_prioritized_and_private_nonmember_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slack enforces private membership: the fake never returns that private channel.
    private = _match("1759210000.000001", "CSECRET", "joined-private", "JOINED", is_private=True)
    public = _match("1759210000.000002", "COPEN", "unjoined-public", "PUBLIC_NONMEMBER")
    public["channel"].update(is_channel=True, is_member=False)
    client = _slack_client([private, public])
    skill, _ = make(client=client)
    summary = _bedrock()
    monkeypatch.setattr(fanout, "build_summary_client", lambda: summary)
    out = run(skill)
    assert [hit.channel for hit in out.slack_hits] == ["#unjoined-public", "🔒#joined-private"]
    assert "PUBLIC_NONMEMBER" in out.answer
    assert "UNJOINED_PRIVATE" not in out.model_dump_json()
    assert "Slack: 公開チャンネル全体＋参加している非公開・DM" in out.answer
    assert "PUBLIC_NONMEMBER" in str(summary.converse.call_args)


def test_wide_pages_summary_cap_and_display_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    def batch(start: int) -> list[dict[str, Any]]:
        return [
            _match(f"1759210000.{i:06}", "COPEN", "public", f"BODY_{i:02}")
            for i in range(start, start + 20)
        ]

    first, second = batch(0), batch(20)
    # Public results on page 2 must outrank private results on page 1.
    for match in first:
        match["channel"]["is_private"] = True
    for match in second:
        match["channel"]["is_channel"] = True
    client = _slack_client([])
    client.search_messages.side_effect = [
        _slack_fakes._search_response(first, total=40),
        _slack_fakes._search_response(second, total=40),
    ]
    skill, _ = make(client=client)
    summary = MagicMock(return_value=("要約", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = run(skill, query="日本ガイシ ケイパ 動画")
    calls = client.search_messages.call_args_list
    assert [(c.kwargs["count"], c.kwargs["page"]) for c in calls] == [(20, 1), (20, 2)]
    assert calls[0].kwargs["query"] == calls[1].kwargs["query"]
    items = summary.call_args.kwargs["slack_items"]
    assert len(items) == 20 and all(i.hit.channel == "#public" for i in items)
    assert len(out.slack_hits) == 5 and out.slack_hits[0].excerpt == "BODY_20"
    assert "BODY_39" not in out.answer


@pytest.mark.parametrize("initial_count", [0, 2])
def test_relaxation_order_merges_deduplicates_and_stops_at_five(initial_count: int) -> None:
    first = [_match(f"1759210000.{i:06}", "COPEN", "public", str(i)) for i in range(initial_count)]
    final = [_match(f"1759210000.{i:06}", "COPEN", "public", str(i)) for i in range(5)]
    client = _slack_client([])
    client.search_messages.side_effect = [
        _slack_fakes._search_response(first),
        _slack_fakes._search_response(first),
        _slack_fakes._search_response(final),
    ]
    skill, _ = make(client=client)
    out = run(skill, query="日本ガイシ ケイパ 動画")
    assert [c.kwargs["query"] for c in client.search_messages.call_args_list] == [
        "日本ガイシ ケイパ 動画",
        "日本ガイシ ケイパ",
        "日本ガイシ",
    ]
    assert len(out.slack_hits) == 5


def test_wide_deadline_includes_token_lookup_and_no_call_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters.slack_user_reader import SlackSearchRead
    from teamagent.skills.search import composite

    now = [100.0]
    monkeypatch.setattr(composite.time, "perf_counter", lambda: now[0])
    reader = MagicMock()
    timeouts: list[float] = []

    def search(*a: Any, **kw: Any) -> SlackSearchRead:
        timeouts.append(kw["timeout_s"])
        now[0] += 4
        return SlackSearchRead()

    reader.search_checked.side_effect = search
    store = _slack_fakes._Store()
    result = lookup_slack(
        slack_store=store,
        reader_factory=lambda token: reader,
        query="日本ガイシ ケイパ 動画",
        metadata=META,
        request_id="deadline",
        fanout=True,
    )
    assert timeouts == [8.0, 4.0]
    assert result.status == "error" and result.reason == "timeout"
    reader.reset_mock()
    result = lookup_slack(
        slack_store=store,
        reader_factory=lambda token: reader,
        query="日本ガイシ",
        metadata=META,
        request_id="expired",
        fanout=True,
        deadline_at=108.0,
    )
    reader.search_checked.assert_not_called()
    assert result.reason == "timeout"


def test_operators_are_not_relaxed_and_off_keeps_count_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _slack_client([])
    skill, _ = make(client=client)
    run(skill, query="日本ガイシ in:general after:2026-01-01")
    assert client.search_messages.call_count == 1
    assert (
        client.search_messages.call_args.kwargs["query"] == "日本ガイシ in:general after:2026-01-01"
    )
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    client.reset_mock()
    run(skill, query="日本ガイシ ケイパ 動画")
    assert client.search_messages.call_count == 1
    assert client.search_messages.call_args.kwargs == {
        "query": "日本ガイシ ケイパ 動画",
        "count": 10,
        "sort": "timestamp",
        "page": 1,
    }


def test_explicit_client_is_used_for_last_relaxation() -> None:
    client = _slack_client([])
    reader = _slack_fakes._ReaderFactory(client)
    result = lookup_slack(
        slack_store=_slack_fakes._Store(),
        reader_factory=reader,
        query="動画 ケイパ 日本ガイシ",
        metadata=META,
        request_id="explicit-client",
        fanout=True,
        client_name="日本ガイシ",
    )
    assert result.status == "ok"
    assert [c.kwargs["query"] for c in client.search_messages.call_args_list] == [
        "動画 ケイパ 日本ガイシ",
        "動画 ケイパ",
        "日本ガイシ",
    ]
    assert reader.tokens == [_slack_fakes.XOXP]
