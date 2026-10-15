"""本人確認済みの MCP・DM だけで動く統合検索（既定 OFF・読み取りだけ）。

締め切りは t=0 から数える。待ち切れないスレッドの終了は待たず、短い専用クライアントで
取り残しを抑える。広げた結果は found 判定・要約入力には混ぜない。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, TypeVar, cast

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.adapters.retry import RetryPolicy
from teamagent.mcp_gateway.allowlist import email_allowed
from teamagent.skills.base import SkillContext
from teamagent.skills.search import timeline_status
from teamagent.skills.search.answer_mode import classify_answer_mode
from teamagent.skills.search.answer_sections import (
    LaneStatus,
    Sections,
    build_sections,
    compose_answer,
    entry_from_hit,
    lane_from_slack,
)
from teamagent.skills.search.campaign_files import is_campaign_hit, scope_by_industry
from teamagent.skills.search.client_match import hit_entities
from teamagent.skills.search.composite import (
    MCP_SURFACE_CTX_KEY,
    SLACK_TOP_N,
    SlackLookup,
    lookup_slack,
)
from teamagent.skills.search.dates import resolve_date_basis
from teamagent.skills.search.expand import (
    ExpansionResult,
    bedrock_reranker,
    build_expansion_planner,
    is_timeout_error,
    run_expansion,
)
from teamagent.skills.search.fanout_sources import (
    SourceResult,
    case_lookup_by_industry,
    classify_hits,
    karte_lookup,
    reuse_connection,
    rls_connection,
)
from teamagent.skills.search.next_move import suggest_next_move
from teamagent.skills.search.not_found import NOT_FOUND_HEAD
from teamagent.skills.search.result_guard import build_result_header, prefix_header
from teamagent.skills.search.schema import RelatedFileOut, SearchHitOut, SearchInput, SearchOutput

SLACK_DEADLINE_S = 8.0
EXPAND_DEADLINE_S = 10.0
SUMMARY_DEADLINE_S = 30.0
LIST_FAST_HEAD = "検索で見つかった範囲の資料を一覧にしました（要約は省いています）。"
# 既存の分類器が拾わない比較・効果の問いも、一覧語との併記で要約を落とさない。
_LIST_FAST_ANALYSIS_RE = re.compile(r"比較|何が効|どれが効")
T = TypeVar("T")
LaneState = Literal["ok", "empty", "skipped", "not_connected", "timeout", "error"]


@dataclass(frozen=True)
class LaneResult:
    """ログと構造化結果の材料。本文・検索語・社名を持たない。"""

    lane: str
    status: LaneState
    count: int = 0
    elapsed_ms: int = 0
    cost_usd: float = 0.0
    reason: str = "ok"


def fanout_active(ctx: SkillContext) -> bool:
    """1 つでも欠けたら従来の経路へ戻す（空の許可リストは全員拒否）。"""
    meta = ctx.metadata
    allowed = {
        s.strip().lower()
        for s in os.environ.get("SEARCH_FANOUT_ALLOWED_EMAILS", "").split(",")
        if s.strip()
    }
    return (
        os.environ.get("USE_SEARCH_FANOUT", "") == "1"
        and bool(meta.get(MCP_SURFACE_CTX_KEY))
        and str(meta.get("channel_id") or "").startswith("D")
        and meta.get("identity_verified") is True
        and email_allowed(meta.get("user_email"), allowed)
    )


def _submit(fn: Callable[[], T], name: str) -> Future[T]:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"search-{name}")
    try:
        return executor.submit(fn)
    finally:
        executor.shutdown(wait=False)


def _collect(future: Future[T], started: float, deadline: float, fallback: T) -> T:
    try:
        return future.result(timeout=max(0.0, started + deadline - time.perf_counter()))
    except Exception:
        future.cancel()
        return fallback


def build_summary_client() -> BedrockClient:
    """共有クライアントを変えず、要約だけ試行 2 回・read 25s にする。"""
    return BedrockClient(
        region=os.environ.get("AWS_REGION", "ap-northeast-1"),
        model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0.25, max_delay_s=1.0),
        read_timeout=25,
    )


def bounded_connection(connect: Callable[[], Any], deadline_at: float) -> Callable[[], Any]:
    """借りた接続の SQL だけを残り時間内に制限し、元の設定を必ず戻す。"""

    @contextmanager
    def open_connection() -> Iterator[Any]:
        remaining_ms = int((deadline_at - time.perf_counter()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("lane_deadline")
        with connect() as conn:
            remaining_ms = int((deadline_at - time.perf_counter()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError("lane_deadline")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT current_setting('statement_timeout') AS timeout")
                    previous = cur.fetchone()["timeout"]
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)", [str(remaining_ms)]
                    )
                # 失敗時は外側 SAVEPOINT の rollback が設定も戻す。中断中に復旧 SQL を打たない。
                yield conn
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)", [previous])

    return open_connection


def collect_named_sources(
    skill: Any, conn: Any, client: str | None, ctx: SkillContext, probe: dict[str, Any]
) -> None:
    """金庫の接続で名指し資料・カルテ・事例を読む。SAVEPOINT で本体を守る。"""
    connect = bounded_connection(reuse_connection(conn), time.perf_counter() + 3.0)
    probe["karte"] = karte_lookup(
        skill._pgvector, connect, query_client=client, request_id=ctx.request_id
    )
    probe["case"] = case_lookup_by_industry(
        skill._pgvector,
        connect,
        industry=None,
        main_hits=(),
        query_client=client,
        request_id=ctx.request_id,
    )
    started = time.perf_counter()
    status: LaneState = "skipped"
    named: list[SearchHit] = []
    if client:
        try:
            with connect() as named_conn:
                named = skill._pgvector.find_documents_for_advertiser(named_conn, client, limit=8)
            status = "ok" if named else "empty"
        except Exception as exc:
            status = "timeout" if is_timeout_error(exc) else "error"
    probe["named"] = named
    probe["named_lane"] = LaneResult(
        "named", status, len(named), int((time.perf_counter() - started) * 1000)
    )


def _source_lane(result: SourceResult[Any]) -> LaneStatus:
    if result.status == "failed":
        return LaneStatus.failed(result.reason)
    if result.status == "skipped":
        return LaneStatus.skipped(result.reason)
    return LaneStatus.found(len(result.items), partial=result.incomplete)


def _case_rows(result: SourceResult[Any]) -> list[dict[str, Any]]:
    return [
        {
            "title": r.title,
            "source_uri": r.source_uri,
            "case_client": r.client,
            "case_brand": r.brand,
            "case_product": r.product,
            "case_effect": r.effect,
            "case_source": r.source,
            "case_external_use": r.external_use,
            "case_industry": r.industry,
            "updated_at": r.updated_at,
        }
        for r in result.items
    ]


def run_fanout(skill: Any, input: SearchInput, ctx: SkillContext) -> SearchOutput:
    """並行取得→従来の found 判定→専用要約→コードの節と探した範囲。"""
    started = time.perf_counter()
    log = ctx.bind_logger(skill.name)
    log.info(
        "search_skill_start",
        query_len=len(input.query),
        top_k=input.top_k,
        filter_industry=input.filter_industry,
    )
    timings: dict[str, float] = {}
    probe: dict[str, str] = {}
    extra: dict[str, Any] = {}
    connect = rls_connection(skill._pgvector, ctx.metadata, app_role=skill._app_role)

    def expand() -> ExpansionResult:
        planner = build_expansion_planner()
        return run_expansion(
            input.query,
            planner=planner,
            embed=skill._embed,
            connection=bounded_connection(connect, started + EXPAND_DEADLINE_S),
            search=lambda conn, vector: skill._pool_search(
                conn=conn,
                embedding=vector,
                limit=30,
                filter_industry=None,
                strict_industry=False,
                metadata_filters=None,
                request_id=ctx.request_id,
            ),
            rerank=bedrock_reranker(planner.bedrock, request_id=ctx.request_id),
            request_id=ctx.request_id,
            deadline_s=EXPAND_DEADLINE_S,
        )

    slack_ms = [0]

    def slack_lookup() -> SlackLookup:
        lane_started = time.perf_counter()
        try:
            return lookup_slack(
                slack_store=skill._slack_store,
                reader_factory=skill._slack_reader_factory,
                query=input.query,
                metadata=dict(ctx.metadata),
                request_id=ctx.request_id,
                fanout=True,
                deadline_at=started + SLACK_DEADLINE_S,
                client_name=input.filter_client,
            )
        finally:
            slack_ms[0] = int((time.perf_counter() - lane_started) * 1000)

    slack_future = _submit(slack_lookup, "slack")
    expand_future = _submit(expand, "expand")
    vault_status: LaneState = "ok"
    vault_started = time.perf_counter()
    try:
        t = time.perf_counter()
        embedding = skill._embed(input.query)
        timings["embed_ms"] = (time.perf_counter() - t) * 1000
        hits = skill._retrieve(
            embedding, input, ctx, timings=timings, probe=probe, fanout_probe=extra
        )
        decision = skill._judge_found(input.query, hits, probe)
        if skill._alias_expansion and not decision.found:
            hits, _, incomplete = skill._retry_aliases(input, ctx, hits, probe)
            extra["incomplete"] = incomplete
            extra["retried"] = True
        found = skill._judge_found(input.query, hits, probe).found
    except Exception as exc:
        vault_status = "timeout" if is_timeout_error(exc) else "error"
        hits, found = [], False
    vault_ms = int((time.perf_counter() - vault_started) * 1000)
    # 時間切れの別名再検索が接続を持つ間は、補助接続を足さない（最大 3 本）。
    file_urls = {} if extra.get("incomplete") else skill._resolve_file_urls(hits, ctx)
    summary_hits = hits
    from teamagent.ingest.industry_taxonomy import normalize_industry
    from teamagent.skills.search.knowledge_query import extract_query_industry

    asked_industry = normalize_industry(input.filter_industry) or extract_query_industry(
        input.query
    )
    if (
        not extra.get("incomplete")
        and skill._campaign_files
        and any(is_campaign_hit(h) for h in hits)
    ):
        skill._attach_campaign_files(hits, ctx)
    if skill._campaign_files:
        summary_hits = scope_by_industry(hits, asked_industry)
        if not summary_hits:
            found = False
    client = skill._asked_client(probe)
    if found and skill._summary_client_scope and client:
        summary_hits = skill._scope_to_client(summary_hits, client) or summary_hits
    slack = _collect(
        slack_future, started, SLACK_DEADLINE_S, SlackLookup(status="error", reason="timeout")
    )
    karte = extra.get("karte", SourceResult(source="karte", status="skipped", reason="no_client"))
    cases = extra.get("case", SourceResult(source="case", status="skipped", reason="no_industry"))
    statuses = (
        timeline_status.resolve_statuses([*summary_hits, *extra.get("named", []), *karte.items])
        if timeline_status.enabled()
        else None
    )
    head, summary_cost = ("" if found else NOT_FOUND_HEAD), 0.0
    summary_state: LaneState = "skipped"
    summary_ms = 0
    list_fast = (
        found
        and input.include_answer
        and os.environ.get("SEARCH_FANOUT_LIST_FAST", "") == "1"
        and classify_answer_mode(input.query) == "list"
        and not _LIST_FAST_ANALYSIS_RE.search(input.query)
    )
    if list_fast:
        head = LIST_FAST_HEAD
    elif found and input.include_answer:
        summary_started = time.perf_counter()

        def summarize() -> tuple[str, float, LaneState]:
            try:
                answer, cost = skill._summarize(
                    input.query,
                    summary_hits,
                    ctx.request_id,
                    slack_items=slack.items,
                    bedrock=build_summary_client(),
                    karte_hits=karte.items,
                    asked_industry=asked_industry,
                    **({"timeline_statuses": statuses} if statuses is not None else {}),
                )
                return answer, cost, "ok"
            except Exception as exc:
                return "", 0.0, "timeout" if is_timeout_error(exc) else "error"

        summary_future = _submit(summarize, "summary")
        head, summary_cost, summary_state = _collect(
            summary_future, summary_started, SUMMARY_DEADLINE_S, ("", 0.0, "timeout")
        )
        summary_ms = int((time.perf_counter() - summary_started) * 1000)
    if found and input.include_answer:
        head = skill._append_campaign_footer(head, summary_hits, asked_industry=asked_industry)
    # 広げる処理は 10s 以内。回収の追加待ちは該当ありで 1s、該当なしで 0s に抑える。
    collection_deadline = time.perf_counter() + (1.0 if found else 0.0)
    expanded = _collect(
        expand_future,
        started,
        min(EXPAND_DEADLINE_S, collection_deadline - started),
        ExpansionResult((), None, (), "timeout", int(EXPAND_DEADLINE_S * 1000), 0.0, "timeout"),
    )
    candidates = extra.get("candidates", hits)
    if extra.get("retried"):
        candidates = hits
    show_expanded = (
        not found
        or not classify_hits(hits).has_case_or_campaign
        or (
            expanded.industry is not None
            and not any((h.metadata or {}).get("cls_industry") == expanded.industry for h in hits)
        )
    )
    near: SourceResult[Any] = SourceResult(source="case", status="skipped", reason="main_has_cases")
    if (
        expanded.industry
        and not classify_hits(hits).has_case_or_campaign
        and not cases.items
        and not extra.get("incomplete")
    ):
        # 段 4 も残りの回収予算だけ待つ。プール混雑・遅い SQL を回答へ直列に足さない。
        case_started = time.perf_counter()
        case_future = _submit(
            lambda: case_lookup_by_industry(
                skill._pgvector,
                bounded_connection(connect, case_started + 3.0),
                industry=expanded.industry,
                main_hits=hits,
                request_id=ctx.request_id,
            ),
            "case",
        )
        near = _collect(
            case_future,
            case_started,
            max(0.0, collection_deadline - case_started),
            SourceResult(source="case", status="failed", reason="timeout"),
        )
        # 名指しの検索失敗を、段 4 の 0 件で上書きしない。
        if cases.status == "failed":
            if near.items:
                cases = replace(near, incomplete=True)
        else:
            cases = near
    named = extra.get("named", [])
    sections = build_sections(
        vault_hits=candidates if found else (),
        expand_hits=expanded.hits if show_expanded else (),
        karte_hits=karte.items,
        case_rows=_case_rows(cases) if cases.stage != 4 else (),
        near_case_rows=_case_rows(near),
        slack_hits=[i.hit for i in slack.items[:SLACK_TOP_N]],
        min_score=skill._weak_result_threshold,
        karte_client=client or "",
        expand_subject=input.query,
        url_of=lambda h: skill._hit_url(h, file_urls=file_urls),
    )
    # 名指し一覧は関連度で落とさず先頭へ。資料種別はメタデータの値だけを書く。
    grouped = {key: list(value) for key, value in sections.entries.items()}
    for hit in reversed(named):
        entry = entry_from_hit(hit, url=skill._hit_url(hit, file_urls=file_urls))
        entry = replace(entry, detail=str((hit.metadata or {}).get("cls_doc_type") or entry.detail))
        grouped.setdefault(entry.kind, []).insert(0, entry)
    sections = Sections(
        entries=grouped,
        karte_client=client or "",
        expand_subject=input.query,
        timeline_lines={
            key: tuple(status.line for status in statuses if key in status.sections)
            for key in ("deal", "karte")
        }
        if statuses is not None
        else {},
        required_counts={
            key: max(1, count)
            for key in grouped
            if (count := sum(entry_from_hit(h, url=None).kind == key for h in named)) > 0
            or (statuses is not None and any(key in status.sections for status in statuses))
        },
    )
    vault_count = len(hits) + len(named)
    vault_lane = (
        LaneStatus.failed("timeout" if vault_status == "timeout" else "search_failed")
        if vault_status in ("error", "timeout")
        else LaneStatus.found(vault_count, partial=bool(extra.get("incomplete")))
    )
    lanes = {
        "vault": vault_lane,
        "slack": lane_from_slack(slack) or LaneStatus.failed("internal_error"),
        "karte": _source_lane(karte),
        "case": _source_lane(cases),
        "expand": LaneStatus.failed(expanded.reason)
        if expanded.status in ("error", "timeout")
        else LaneStatus.found(len(expanded.hits), queries=expanded.terms),
        "sns": LaneStatus.skipped("not_auto"),
    }
    if extra.get("named_lane") and extra["named_lane"].status in ("error", "timeout"):
        lanes["vault"] = (
            LaneStatus.found(len(hits), partial=True)
            if hits
            else LaneStatus.failed("search_failed")
        )
    if found and skill._result_guard:
        head = prefix_header(
            build_result_header(
                query=input.query,
                hits=hits,
                weak_threshold=skill._weak_result_threshold,
                query_client=client,
                asked_industry=input.filter_industry,
                use_entities=skill._client_guard_entities,
            ),
            head,
        )
    main_kinds = classify_hits([*candidates, *named])
    if cases.status != "failed" and (main_kinds.case or main_kinds.campaign):
        lanes["case"] = LaneStatus.found(
            len(cases.items) + len(main_kinds.case) + len(main_kinds.campaign)
        )
    if karte.status != "failed" and main_kinds.sales_fb:
        lanes["karte"] = LaneStatus.found(len(karte.items) + len(main_kinds.sales_fb))
    answer = (
        compose_answer(
            head=head,
            sections=sections,
            lanes=lanes,
            graph_base_url=os.environ.get("CONNECT_BASE_URL", "").strip()
            if os.environ.get("SEARCH_FANOUT_GRAPH_LINK") == "1"
            else "",
        )
        if input.include_answer
        else ""
    )
    vault_cost = 0.002 if timings.get("rerank_ms", 0.0) > 0.0 else 0.0
    results = [
        LaneResult(
            "vault",
            vault_status if vault_count or vault_status != "ok" else "empty",
            vault_count,
            vault_ms,
            cost_usd=vault_cost,
        ),
        LaneResult(
            "slack",
            "ok"
            if slack.status == "ok"
            else "not_connected"
            if slack.status == "not_connected"
            else "timeout"
            if slack.reason == "timeout"
            else "error",
            len(slack.items),
            slack_ms[0] if slack_future.done() else int(SLACK_DEADLINE_S * 1000),
            reason=slack.reason,
        ),
        LaneResult(
            "expand",
            "empty" if expanded.status == "empty" else expanded.status,
            len(expanded.hits),
            expanded.elapsed_ms,
            expanded.cost_usd,
            expanded.reason,
        ),
        LaneResult("summary", summary_state, elapsed_ms=summary_ms, cost_usd=summary_cost),
    ]
    if extra.get("named_lane"):
        results.append(extra["named_lane"])
    for source in (karte, cases):
        results.append(
            LaneResult(
                source.source,
                cast(
                    LaneState,
                    {"found": "ok", "zero": "empty", "failed": "error", "skipped": "skipped"}[
                        lanes[source.source].kind
                    ],
                ),
                lanes[source.source].count,
                source.elapsed_ms,
                reason=source.reason,
            )
        )
    log.info("search_fanout_lanes", lanes=[asdict(r) for r in results])
    log.info(
        "search_skill_done",
        hit_count=len(hits),
        cost_usd=summary_cost + expanded.cost_usd + vault_cost,
        top_score=hits[0].score if hits else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    log.info(
        "search_latency_breakdown",
        fanout=True,
        list_fast=list_fast,
        embed_ms=int(timings.get("embed_ms", 0)),
        retrieve_ms=vault_ms,
        converse_ms=summary_ms,
        total_ms=int((time.perf_counter() - started) * 1000),
        hit_count=len(hits),
        deferred=False,
    )
    return SearchOutput(
        answer=answer,
        hits=_output_hits(skill, hits, file_urls),
        found=found,
        total_cost_usd=summary_cost + expanded.cost_usd + vault_cost,
        slack_hits=[i.hit for i in slack.items[:SLACK_TOP_N]],
        slack_status=slack.status,
        query_client=client,
        sources=[
            {"lane": r.lane, "status": r.status, "reason": r.reason, "count": r.count}
            for r in results
        ],
        suggested_next=suggest_next_move(
            query=input.query,
            found=found,
            slack_status=slack.status,
            slack_shown=len(slack.items),
            delivery_offered=False,
            query_client=client,
            requester=ctx.metadata.get("user_email"),
            fanout=True,
        ),
    )


def _output_hits(
    skill: Any, hits: list[SearchHit], file_urls: dict[str, str]
) -> list[SearchHitOut]:
    """通常のリンク・配信情報を保ち、本文だけ 200 字へ抑える。"""
    search_hits: list[SearchHitOut] = []
    for h in hits:
        meta = h.metadata or {}
        resolved_file_ref = skill._resolved_file_ref(meta, h.content or "", file_urls)
        url = skill._doc_url({"drive_url": meta.get("drive_url")})
        if not url and resolved_file_ref:
            url = resolved_file_ref[1]
        if not url:
            url = skill._doc_url({"source_uri": meta.get("source_uri")})
        # 便A-3: 日付は tool が根拠を持つ値だけを返す。
        #   updated_at = documents.modified_at（adapter が JST 文字列化済み）
        #   title_date = 資料名（title / 実ファイル名 / file_name）に埋まった日付
        #   date_basis = title_date 優先 → modified_at → none（両方 None）
        updated_at = str(meta["updated_at"]) if meta.get("updated_at") else None
        title_date = skill._title_date_of(meta, resolved_file_ref)

        search_hits.append(
            SearchHitOut(
                chunk_id=h.chunk_id,
                content=h.content[:200],
                score=h.score,
                source=skill._build_source(h),
                file_name=(str(meta.get("file_name")) if meta.get("file_name") else None),
                page_num=skill._safe_int(meta.get("page_num")),
                drive_url=(str(meta.get("drive_url")) if meta.get("drive_url") else None),
                url=url,
                source_uri=(str(meta["source_uri"]) if meta.get("source_uri") else None),
                resolved_file_name=(resolved_file_ref[0] if resolved_file_ref else None),
                source_type=(str(meta["source_type"]) if meta.get("source_type") else None),
                channel_name=(str(meta["channel_name"]) if meta.get("channel_name") else None),
                client_name=(str(meta["client_name"]) if meta.get("client_name") else None),
                deal_phase=(str(meta["deal_phase"]) if meta.get("deal_phase") else None),
                bant_score=(str(meta["bant_score"]) if meta.get("bant_score") else None),
                channel_type=(str(meta["channel_type"]) if meta.get("channel_type") else None),
                title=(str(meta["title"]) if meta.get("title") else None),
                project=(str(meta["cls_project"]) if meta.get("cls_project") else None),
                # 取引先判定の口（search の警告と同じ SEARCH_CLIENT_GUARD_ENTITIES で切る）。
                entities=(hit_entities(h) or None) if skill._client_guard_entities else None,
                industry=(
                    str(meta.get("cls_industry") or meta.get("campaign_industry"))
                    if (meta.get("cls_industry") or meta.get("campaign_industry"))
                    else None
                ),
                doc_type=(str(meta["cls_doc_type"]) if meta.get("cls_doc_type") else None),
                budget=(str(meta["cls_budget"]) if meta.get("cls_budget") else None),
                is_low_confidence=bool(meta.get("is_low_confidence", False)),
                related_files=(
                    [RelatedFileOut(**f) for f in meta["related_files"]]
                    if isinstance(meta.get("related_files"), list)
                    else None
                ),
                updated_at=updated_at,
                title_date=title_date,
                date_basis=resolve_date_basis(updated_at, title_date),
            )
        )
    return search_hits


def shadow_grounding(text: str, inputs: str, hits: list[SearchHit], request_id: str) -> None:
    """実際の要約入力と文ごとの出典を照合する（shadow・件数だけを記録）。"""
    import re

    import structlog

    from teamagent.skills._shared.grounding import NumberGrounder

    stray = NumberGrounder.from_inputs(inputs).stray(text)
    mismatch = unresolved = 0
    for sentence in re.split(r"[。\n]", text):
        for match in re.finditer(r"[（(](金庫|カルテ|事例):\s*([^）)]+)[）)]", sentence):
            label = match.group(2).strip()
            cited = [
                h
                for h in hits
                if any(
                    str((h.metadata or {}).get(key) or "") in label
                    for key in ("title", "client_name")
                    if (h.metadata or {}).get(key)
                )
            ]
            if not cited:
                unresolved += 1
            elif NumberGrounder.from_inputs(
                "\n".join(
                    h.content
                    + " "
                    + str((h.metadata or {}).get("title") or "")
                    + " "
                    + str((h.metadata or {}).get("updated_at") or "")
                    for h in cited
                )
            ).stray(sentence):
                mismatch += 1
    structlog.get_logger(__name__).info(
        "search_fanout_grounding",
        request_id=request_id,
        mode="shadow",
        stray_count=len(stray),
        citation_mismatch_count=mismatch,
        citation_unresolved_count=unresolved,
    )
