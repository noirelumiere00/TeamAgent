"""広げるレーン（skills/search/expand.py）の単体テスト（ネットワーク無し・課金 0）。

偽物は本番の失敗の形をそのまま使う:
  - Bedrock: botocore の ClientError（ThrottlingException 429 / ModelTimeoutException 408）と
    ReadTimeoutError。専用クライアントは本物の BedrockClient に boto3 の偽クライアントを差して、
    再試行の回数（2 回）を実際に数える。
  - DB: psycopg.errors.QueryCanceled（statement_timeout）・psycopg.OperationalError・
    adapters/pg_pool の PoolTimeoutError（接続待ち）。
検証の芯:
  - 例外を投げず、ok / empty / timeout / error を分ける（「無い」と「探せなかった」を混ぜない）
  - 接続は 1 本だけ。語が無い・LLM が失敗した回は埋め込みも DB も 0 回
  - found を持たない（格上げしない）・ログに語の本文と問いを出さない
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from psycopg import errors as pg_errors
from structlog.testing import capture_logs

from teamagent.adapters.bedrock_client import (
    BedrockClient,
    ConverseResponse,
    RerankResponse,
    RerankResult,
    TokenUsage,
)
from teamagent.adapters.pg_pool import PoolTimeoutError
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import CANONICAL_INDUSTRIES, INDUSTRY_PROMPT_LIST
from teamagent.skills.search import expand
from teamagent.skills.search.expand import (
    DEFAULT_MODEL,
    MAX_TOKENS,
    RERANK_COST_USD,
    ExpansionPlanner,
    ExpansionResult,
    bedrock_reranker,
    build_expansion_bedrock_client,
    build_expansion_planner,
    clean_terms,
    is_timeout_error,
    render_system_prompt,
    resolve_expand_model_id,
    retrieve_expanded,
    run_expansion,
)

QUERY = "宅配系の事例"


# ── 偽物（本番の形）──────────────────────────────────────────────────────


def _client_error(code: str, status: int, op: str = "Converse") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


def _read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://bedrock-runtime.ap-northeast-1.amazonaws.com")


def _resp(text: str, *, cost: float = 0.0003) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=900,
            output_tokens=60,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=cost,
        ),
        model_id=DEFAULT_MODEL,
        latency_ms=1,
        stop_reason="end_turn",
    )


def _boto_ok(text: str) -> dict[str, Any]:
    """boto3 bedrock-runtime の converse の返り値の形。"""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 900, "outputTokens": 60},
    }


class _FakeBedrock:
    """converse() が順に text を返すか例外を投げる偽物（呼び出し引数を記録する）。"""

    def __init__(self, *outcomes: str | BaseException, on_call: Any = None) -> None:
        self._outcomes = list(outcomes)
        self._on_call = on_call
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> ConverseResponse:
        self.calls.append(kwargs)
        if self._on_call is not None:
            self._on_call()
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _resp(outcome)


def _plan_json(terms: Any, industry: Any = None) -> str:
    return json.dumps({"terms": terms, "industry": industry}, ensure_ascii=False)


def _hit(cid: int, score: float = 0.5, **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=cid, content=f"本文{cid}", score=score, metadata=dict(meta))


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Lane:
    """埋め込み・接続・検索・rerank の偽物（呼んだ順番を events に残す）。"""

    def __init__(
        self,
        lists: dict[str, list[SearchHit]] | None = None,
        *,
        search_exc: BaseException | None = None,
        connect_exc: BaseException | None = None,
        embed_exc: BaseException | None = None,
        rerank_exc: BaseException | None = None,
    ) -> None:
        self.lists = lists or {}
        self.search_exc = search_exc
        self.connect_exc = connect_exc
        self.embed_exc = embed_exc
        self.rerank_exc = rerank_exc
        self.events: list[str] = []
        self.opened = 0
        self.vec_to_term: dict[float, str] = {}

    def embed(self, term: str) -> list[float]:
        self.events.append(f"embed:{term}")
        if self.embed_exc is not None:
            raise self.embed_exc
        key = float(len(self.vec_to_term) + 1)
        self.vec_to_term[key] = term
        return [key, 0.0]

    @contextmanager
    def connection(self) -> Iterator[str]:
        self.events.append("connect")
        if self.connect_exc is not None:
            raise self.connect_exc
        self.opened += 1
        try:
            yield "conn"
        finally:
            self.events.append("close")

    def search(self, conn: Any, embedding: list[float]) -> list[SearchHit]:
        assert conn == "conn"
        term = self.vec_to_term[embedding[0]]
        self.events.append(f"search:{term}")
        if self.search_exc is not None:
            raise self.search_exc
        return list(self.lists.get(term, []))

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        self.events.append(f"rerank:{query}")
        if self.rerank_exc is not None:
            raise self.rerank_exc
        # 逆順にし、関連度を付ける（rerank が効いたことが順番で分かるように）。
        return [dataclasses.replace(h, score=0.9 - i * 0.1) for i, h in enumerate(reversed(hits))]


def _run(
    planner: ExpansionPlanner, lane: _Lane, *, with_rerank: bool = True, **kw: Any
) -> ExpansionResult:
    return run_expansion(
        QUERY,
        planner=planner,
        embed=lane.embed,
        connection=lane.connection,
        search=lane.search,
        rerank=lane.rerank if with_rerank else None,
        request_id="req-x",
        **kw,
    )


# ── ExpansionPlanner ─────────────────────────────────────────────────────


def test_plan_maps_terms_and_canonical_industry() -> None:
    bedrock = _FakeBedrock(
        _plan_json(["物流 事例", "配送 事例", "食品宅配 事例", "ネットスーパー 事例"], "物流")
    )
    plan = ExpansionPlanner(bedrock).plan(QUERY, "req-1")

    assert plan.status == "ok"
    assert plan.reason == "ok"
    assert plan.terms == ("物流 事例", "配送 事例", "食品宅配 事例")  # 最大 3 本
    assert plan.industry == "運輸・交通"  # normalize_industry で正準化
    assert plan.cost_usd == pytest.approx(0.0003)
    call = bedrock.calls[0]
    assert call["max_tokens"] == MAX_TOKENS == 200
    assert INDUSTRY_PROMPT_LIST in call["system"]
    assert QUERY in call["messages"][0]["content"][0]["text"]


def test_plan_drops_restatements_duplicates_and_single_query_words() -> None:
    query = "冷凍食品の事例"
    raw = [
        "冷凍食品の事例",  # 元の問いそのもの
        "事例",  # 問いに含まれる 1 語だけ（主題が無く別業種を混ぜる）
        "冷凍食品",  # 問いに含まれる 1 語だけ（主レーンと同じ）
        "食品 事例",  # 上位語。問いの部分文字列でも 2 語なら残す（複合語の上位語）
        "食品　事例",  # 全角空白の重複
        "ＩＴ 物流 事例",  # NFKC で半角に
        "冷凍 惣菜 事例",
        "弁当 事例",  # 4 本目（上限 3 本で落ちる）
    ]
    assert clean_terms(raw, query=query) == ("食品 事例", "IT 物流 事例", "冷凍 惣菜 事例")
    assert clean_terms(["物流 事例", "宅配"], query=QUERY) == ("物流 事例",)


def test_plan_strips_slack_markup_from_terms() -> None:
    bedrock = _FakeBedrock(_plan_json(["<!channel> *物流* 事例", "配送—事例 `x`"], "運輸"))
    plan = ExpansionPlanner(bedrock).plan(QUERY, "req-1")

    assert plan.status == "ok"
    for term in plan.terms:
        for ch in "<>!*`—":
            assert ch not in term, term
    assert plan.terms == ("channel 物流 事例", "配送 事例 x")


@pytest.mark.parametrize(
    ("industry", "expected"),
    [("その他", None), ("宇宙", None), (None, None), (3, None), ("ＩＴ", "IT"), ("飲料", "飲料")],
)
def test_plan_industry_is_canonical_or_none(industry: Any, expected: str | None) -> None:
    plan = ExpansionPlanner(_FakeBedrock(_plan_json(["物流 事例"], industry))).plan(QUERY, "r")
    assert plan.industry == expected


@pytest.mark.parametrize(
    "text",
    [
        "すみません、わかりません。",
        '{"paraphrases": ["宅配の事例"], "hyde_answer": "…"}',
        '{"terms": "物流 事例", "industry": "運輸・交通"}',
        '{"terms": ["物流 事例", "配送',  # max_tokens で切れた形
    ],
)
def test_plan_bad_json_is_error_not_exception(text: str) -> None:
    plan = ExpansionPlanner(_FakeBedrock(text)).plan(QUERY, "req-1")
    assert plan.status == "error"
    assert plan.reason == "bad_json"
    assert plan.terms == ()
    assert plan.cost_usd == pytest.approx(0.0003)  # 呼んだ分の費用は数える


def test_plan_without_terms_is_empty_but_keeps_industry() -> None:
    plan = ExpansionPlanner(_FakeBedrock(_plan_json([], "飲料"))).plan(QUERY, "req-1")
    assert (plan.status, plan.reason) == ("empty", "no_terms")
    assert plan.industry == "飲料"


def test_plan_empty_query_does_not_call_bedrock() -> None:
    bedrock = _FakeBedrock()
    plan = ExpansionPlanner(bedrock).plan("  \n ", "req-1")
    assert (plan.status, plan.reason) == ("empty", "empty_query")
    assert bedrock.calls == []


def test_plan_missing_prompt_is_error_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing() -> str:
        raise FileNotFoundError("prompts/search/expand/v1/system.md")

    monkeypatch.setattr(expand, "render_system_prompt", _missing)
    bedrock = _FakeBedrock()
    plan = ExpansionPlanner(bedrock).plan(QUERY, "req-1")
    assert (plan.status, plan.reason) == ("error", "prompt_missing")
    assert bedrock.calls == []


# ── 専用クライアント（試行 2 回・read 8s）──────────────────────────────────


def _planner_with_boto(side_effect: Any) -> tuple[ExpansionPlanner, MagicMock]:
    boto = MagicMock()
    boto.converse.side_effect = side_effect
    return build_expansion_planner(client=boto, rerank_client=MagicMock()), boto


def test_throttling_stops_after_two_attempts_and_returns_error() -> None:
    planner, boto = _planner_with_boto(_client_error("ThrottlingException", 429))
    plan = planner.plan(QUERY, "req-throttle")
    assert (plan.status, plan.reason) == ("error", "bedrock_error")
    assert boto.converse.call_count == 2  # 共有クライアントの 5 回ではない


def test_throttling_then_success_uses_second_attempt() -> None:
    planner, boto = _planner_with_boto(
        [_client_error("ThrottlingException", 429), _boto_ok(_plan_json(["物流 事例"], "物流"))]
    )
    plan = planner.plan(QUERY, "req-throttle")
    assert plan.status == "ok"
    assert boto.converse.call_count == 2


def test_read_timeout_is_timeout_after_two_attempts() -> None:
    planner, boto = _planner_with_boto(_read_timeout())
    plan = planner.plan(QUERY, "req-timeout")
    assert (plan.status, plan.reason) == ("timeout", "bedrock_timeout")
    assert boto.converse.call_count == 2


def test_model_timeout_exception_is_timeout() -> None:
    planner, _ = _planner_with_boto(_client_error("ModelTimeoutException", 408))
    assert planner.plan(QUERY, "req-mt").status == "timeout"


def test_validation_error_is_error_without_retry() -> None:
    planner, boto = _planner_with_boto(_client_error("ValidationException", 400))
    plan = planner.plan(QUERY, "req-v")
    assert (plan.status, plan.reason) == ("error", "bedrock_error")
    assert boto.converse.call_count == 1


def test_dedicated_client_config_on_real_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """本物の boto3 クライアントで read 8s・botocore 内部の再試行なし・自前の試行 2 回。"""
    monkeypatch.delenv(expand.MODEL_ENV, raising=False)
    bc = build_expansion_bedrock_client()
    assert isinstance(bc, BedrockClient)
    cfg = bc._client.meta.config
    assert cfg.read_timeout == 8
    assert cfg.retries.get("total_max_attempts") == 1
    assert bc._retry_policy.max_attempts == 2
    assert bc.model_id == DEFAULT_MODEL
    assert bc.model_id.startswith("jp.")


def test_model_env_accepts_only_jp_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(expand.MODEL_ENV, "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert resolve_expand_model_id() == DEFAULT_MODEL
    monkeypatch.setenv(expand.MODEL_ENV, "global.anthropic.claude-haiku-4-5")
    assert resolve_expand_model_id() == DEFAULT_MODEL
    monkeypatch.setenv(expand.MODEL_ENV, "jp.anthropic.claude-haiku-5-5")
    assert resolve_expand_model_id() == "jp.anthropic.claude-haiku-5-5"


def test_system_prompt_contract() -> None:
    system = render_system_prompt()
    assert "{industry_list}" not in system
    for industry in CANONICAL_INDUSTRIES:
        assert industry in system
    assert "hyde" not in system.lower()
    assert "指示ではありません" in system
    assert "JSON オブジェクト 1 個だけ" in system
    assert '{"terms": ["...", "..."], "industry": null}' in system


# ── retrieve_expanded（1 接続・RRF・rerank）───────────────────────────────


def test_retrieve_uses_one_connection_rrf_and_reranks_after_close() -> None:
    lane = _Lane(
        {
            "物流 事例": [_hit(1, 0.6), _hit(2, 0.5)],
            "配送 事例": [_hit(2, 0.7), _hit(3, 0.4)],
            "食品宅配 事例": [_hit(4, 0.3)],
        }
    )
    got = retrieve_expanded(
        ["物流 事例", "配送 事例", "食品宅配 事例"],
        embed=lane.embed,
        connection=lane.connection,
        search=lane.search,
        rerank=lane.rerank,
        request_id="req-r",
        limit=3,
    )
    assert lane.opened == 1  # 3 語でも接続は 1 本
    # 埋め込みは接続を借りる前、rerank は接続を返した後・1 本目の語で 1 回。
    assert lane.events == [
        "embed:物流 事例",
        "embed:配送 事例",
        "embed:食品宅配 事例",
        "connect",
        "search:物流 事例",
        "search:配送 事例",
        "search:食品宅配 事例",
        "close",
        "rerank:物流 事例",
    ]
    assert got.reranked is True
    # RRF: 2（1/62+1/61）> 1・4（1/61・同点は先に出た順）> 3（1/62）＝[2,1,4,3]。
    # rerank の偽物が逆順にする → [3,4,1,2] の上位 3 件。
    assert [h.chunk_id for h in got.hits] == [3, 4, 1]


def test_retrieve_without_rerank_returns_rrf_order_with_original_scores() -> None:
    lane = _Lane({"物流 事例": [_hit(1, 0.6), _hit(2, 0.5)], "配送 事例": [_hit(2, 0.7)]})
    got = retrieve_expanded(
        ["物流 事例", "配送 事例"],
        embed=lane.embed,
        connection=lane.connection,
        search=lane.search,
        request_id="req-r",
    )
    assert got.reranked is False
    assert [(h.chunk_id, h.score) for h in got.hits] == [(2, 0.7), (1, 0.6)]


def test_retrieve_without_terms_touches_nothing() -> None:
    lane = _Lane()
    got = retrieve_expanded(
        ["", "  "],
        embed=lane.embed,
        connection=lane.connection,
        search=lane.search,
        rerank=lane.rerank,
        request_id="req-r",
    )
    assert got.hits == ()
    assert lane.events == []


def test_retrieve_rerank_failure_falls_back_to_rrf_order() -> None:
    lane = _Lane(
        {"物流 事例": [_hit(1, 0.6), _hit(2, 0.5)]},
        rerank_exc=_client_error("ThrottlingException", 429, op="Rerank"),
    )
    got = retrieve_expanded(
        ["物流 事例"],
        embed=lane.embed,
        connection=lane.connection,
        search=lane.search,
        rerank=lane.rerank,
        request_id="req-r",
    )
    assert got.reranked is False
    assert [h.chunk_id for h in got.hits] == [1, 2]


def test_bedrock_reranker_maps_scores_and_keeps_dense_score() -> None:
    bedrock = MagicMock()
    bedrock.rerank.return_value = RerankResponse(
        results=[
            RerankResult(index=1, relevance_score=0.8),
            RerankResult(index=9, relevance_score=0.7),
        ],
        model_arn="arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.rerank-v3-5:0",
        latency_ms=1,
        query_count=1,
    )
    rerank = bedrock_reranker(bedrock, request_id="req-rr")
    out = rerank("物流 事例", [_hit(1, 0.6, cls_industry="食品"), _hit(2, 0.5)])

    assert [(h.chunk_id, h.score) for h in out] == [(2, 0.8)]  # 範囲外の index は捨てる
    assert out[0].metadata["dense_score"] == 0.5
    kwargs = bedrock.rerank.call_args.kwargs
    assert kwargs["query"] == "物流 事例"
    assert kwargs["documents"] == ["本文1", "本文2"]


# ── run_expansion（1 レーン・例外を投げない）──────────────────────────────


def _ok_planner(terms: list[str] | None = None, industry: str | None = "運輸") -> ExpansionPlanner:
    return ExpansionPlanner(_FakeBedrock(_plan_json(terms or ["物流 事例", "配送 事例"], industry)))


def test_run_ok_returns_hits_cost_and_never_promotes_to_found() -> None:
    lane = _Lane({"物流 事例": [_hit(1), _hit(2)], "配送 事例": [_hit(3)]})
    result = _run(_ok_planner(), lane)

    assert (result.status, result.reason) == ("ok", "ok")
    assert result.terms == ("物流 事例", "配送 事例")
    assert result.industry == "運輸・交通"
    assert {h.chunk_id for h in result.hits} == {1, 2, 3}
    assert result.cost_usd == pytest.approx(0.0003 + RERANK_COST_USD)
    # 返り値は found を持たない（格上げは呼び出し側でもしない約束・形で固定する）。
    fields = {f.name for f in dataclasses.fields(ExpansionResult)}
    assert fields == {"terms", "industry", "hits", "status", "elapsed_ms", "cost_usd", "reason"}


def test_run_without_rerank_does_not_add_rerank_cost() -> None:
    lane = _Lane({"物流 事例": [_hit(1)]})
    result = _run(_ok_planner(["物流 事例"]), lane, with_rerank=False)
    assert result.status == "ok"
    assert result.cost_usd == pytest.approx(0.0003)


@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        (_read_timeout(), "timeout", "bedrock_timeout"),
        (_client_error("AccessDeniedException", 403), "error", "bedrock_error"),
        ("壊れた出力", "error", "bad_json"),
        (_plan_json([], "飲料"), "empty", "no_terms"),
    ],
)
def test_run_planner_failure_opens_no_connection(
    outcome: str | BaseException, status: str, reason: str
) -> None:
    lane = _Lane({"物流 事例": [_hit(1)]})
    result = _run(ExpansionPlanner(_FakeBedrock(outcome)), lane)
    assert (result.status, result.reason) == (status, reason)
    assert result.hits == ()
    assert lane.events == []  # 埋め込みも接続も 0 回


def test_run_keeps_industry_when_no_terms() -> None:
    result = _run(ExpansionPlanner(_FakeBedrock(_plan_json([], "飲料"))), _Lane())
    assert result.industry == "飲料"


@pytest.mark.parametrize(
    ("lane_kwargs", "status", "reason"),
    [
        (
            {"search_exc": pg_errors.QueryCanceled("canceling statement due to statement timeout")},
            "timeout",
            "retrieve_timeout",
        ),
        (
            {"connect_exc": PoolTimeoutError("接続待ちタイムアウト（10.0s, max_size=8）。")},
            "timeout",
            "retrieve_timeout",
        ),
        (
            {"embed_exc": _read_timeout()},
            "timeout",
            "retrieve_timeout",
        ),
        (
            {"search_exc": psycopg.OperationalError("server closed the connection unexpectedly")},
            "error",
            "retrieve_failed",
        ),
        (
            {"embed_exc": _client_error("ThrottlingException", 429, op="InvokeModel")},
            "error",
            "retrieve_failed",
        ),
    ],
)
def test_run_retrieve_failures_are_states_not_exceptions(
    lane_kwargs: dict[str, Any], status: str, reason: str
) -> None:
    lane = _Lane({"物流 事例": [_hit(1)]}, **lane_kwargs)
    result = _run(_ok_planner(), lane)
    assert (result.status, result.reason) == (status, reason)
    assert result.hits == ()
    assert result.terms == ("物流 事例", "配送 事例")  # 何で探したかは残す


def test_run_no_hits_is_empty_not_error() -> None:
    result = _run(_ok_planner(), _Lane({}))
    assert (result.status, result.reason) == ("empty", "no_hits")


def test_run_deadline_after_planning_skips_db() -> None:
    clock = _Clock()

    def slow() -> None:
        clock.now += 11.0  # LLM が締め切り（10s）を越えて返ってきた

    planner = ExpansionPlanner(
        _FakeBedrock(_plan_json(["物流 事例"], "運輸"), on_call=slow), clock=clock
    )
    lane = _Lane({"物流 事例": [_hit(1)]})
    result = _run(planner, lane, clock=clock)
    assert (result.status, result.reason) == ("timeout", "deadline")
    assert lane.events == []  # 埋め込みも接続も打たない
    assert result.elapsed_ms == 11000


def test_run_deadline_before_rerank_skips_rerank() -> None:
    clock = _Clock()
    lane = _Lane({"物流 事例": [_hit(1)]})
    original_search = lane.search

    def slow_search(conn: Any, embedding: list[float]) -> list[SearchHit]:
        clock.now += 10.5
        return original_search(conn, embedding)

    result = run_expansion(
        QUERY,
        planner=_ok_planner(["物流 事例"]),
        embed=lane.embed,
        connection=lane.connection,
        search=slow_search,
        rerank=lane.rerank,
        request_id="req-x",
        clock=clock,
    )
    assert (result.status, result.reason) == ("timeout", "deadline")
    assert not any(e.startswith("rerank:") for e in lane.events)


def test_run_never_raises_even_if_planner_raises() -> None:
    planner = MagicMock(spec=ExpansionPlanner)
    planner.plan.side_effect = RuntimeError("boom")
    lane = _Lane()
    result = _run(planner, lane)
    assert (result.status, result.reason) == ("error", "planner_failed")
    assert lane.events == []


def test_logs_have_counts_and_industry_but_no_terms_or_query() -> None:
    lane = _Lane({"物流 事例": [_hit(1)]})
    with capture_logs() as logs:
        _run(_ok_planner(["物流 事例", "配送 事例"]), lane)
        _run(ExpansionPlanner(_FakeBedrock(_read_timeout(), _read_timeout())), _Lane())

    lane_logs = [e for e in logs if e["event"] == "search_expand_lane"]
    assert lane_logs[0]["terms"] == 2
    assert lane_logs[0]["industry"] == "運輸・交通"
    assert lane_logs[0]["hits"] == 1
    assert any(e["event"] == "search_expand_plan_failed" for e in logs)
    dumped = json.dumps(logs, ensure_ascii=False, default=str)
    for secret in ("物流", "配送", "宅配", QUERY):
        assert secret not in dumped


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_read_timeout(), True),
        (_client_error("ModelTimeoutException", 408), True),
        (pg_errors.QueryCanceled("canceling statement due to statement timeout"), True),
        (PoolTimeoutError("接続待ちタイムアウト"), True),
        (TimeoutError(), True),
        (_client_error("ThrottlingException", 429), False),
        (psycopg.OperationalError("server closed the connection unexpectedly"), False),
        (ValueError("x"), False),
    ],
)
def test_is_timeout_error(exc: BaseException, expected: bool) -> None:
    assert is_timeout_error(exc) is expected
