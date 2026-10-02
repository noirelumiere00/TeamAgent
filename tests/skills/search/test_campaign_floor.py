"""施策実績のリコール床（_apply_campaign_floor）と意図判定（is_campaign_results_intent）の回帰テスト。

**再現する本番の失敗（2026-09-29 調査）**

「サラヤのラカントの過去のショート動画施策を教えて。伸びた動画も見たい」のような聞き方で、
施策実績の文書（ショート動画DBを案件ごとに 1 文書にしたもの・ingest.campaign_aggregate。
metadata は campaign_aggregate="true"・cls_doc_type=施策実績・cls_project=広告主で
**cls_solution は無い**。1 文書 1 チャンク）に検索が届かない。

- knowledge_query._SOLUTION_KEYWORDS が「ショート動画」を cls_solution=動画広告 に写す
- pgvector の metadata_filters / sticky は SQL の AND（``d.metadata->>key = value``）で、
  キーが無い文書は NULL = '動画広告' が偽になり除外される
- _pool_search がフィルタを外すのは 0 件のときだけ（提案 PDF が当たれば外れない）
- 加えて、枚数の多い提案 PDF が最初の 30 件の枠を埋めて施策実績を押し出す

**フェイクの要件（本番の失敗モードを再現すること）**

既存テストの MagicMock はフィルタを無視するので、この除外を見つけられない。本ファイルの
``_SqlLikePg`` は ``search_similar_new_schema`` の引数を SQL と同じ意味で適用する
in-memory コーパスで、さらに**抽出した行を本物のアダプタに通して** SearchHit を作る
（SELECT 句の列を本物の SQL から読み、その列だけを行に詰める）。これにより
「アダプタが射影していないメタデータはヒットに載らない」という本番の性質も再現する。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.bedrock_client import (
    ConverseResponse,
    RerankResponse,
    RerankResult,
    TokenUsage,
)
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.knowledge_query import is_campaign_results_intent
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

Q_SARAYA = "サラヤのラカントの過去のショート動画施策を教えて。伸びた動画も見たい"
SARAYA_CAMPAIGN_MARK = "施策実績: サラヤ / ラカント"
KAO_CAMPAIGN_MARK = "施策実績: 花王 / ビオレ"


# ---------------------------------------------------------------------------
# SQL を模した偽物 DB
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Chunk:
    chunk_id: int
    content: str
    score: float
    page_num: int | None = None
    boilerplate: bool = False


@dataclass
class _Doc:
    document_id: str
    source_type: str
    source_uri: str
    title: str
    metadata: dict[str, str]
    chunks: list[_Chunk] = field(default_factory=list)


# SELECT 句の 1 行（``expr AS alias`` または ``c.page_num`` のような素の列）。
_SELECT_ALIAS_RE = re.compile(r"^\s*(?P<expr>.+?)\s+AS\s+(?P<alias>\w+),?\s*$")
_SELECT_BARE_RE = re.compile(r"^\s*[cd]\.(?P<col>\w+),?\s*$")
_META_EXPR_RE = re.compile(r"^d\.metadata->>'(?P<key>\w+)'$")

# メタデータ以外の列の値の出どころ。本物の SQL に知らない列が増えたら AssertionError で
# 落とす（フェイクを黙って古いままにしない）。
_FIXED_COLUMNS: dict[str, Callable[[_Doc, _Chunk], Any]] = {
    "chunk_id": lambda d, c: c.chunk_id,
    "content": lambda d, c: c.content,
    "score": lambda d, c: c.score,
    "page_num": lambda d, c: c.page_num,
    "document_id": lambda d, c: d.document_id,
    "source_uri": lambda d, c: d.source_uri,
    "source_type": lambda d, c: d.source_type,
    "title": lambda d, c: d.title,
    "updated_at": lambda d, c: None,
}


class _RowsCursor:
    """本物のアダプタが発行した SQL の SELECT 列だけを、抽出済みの行に詰めて返す。"""

    def __init__(self, picked: list[tuple[_Doc, _Chunk]]) -> None:
        self._picked = picked
        self._columns: list[tuple[str, str]] = []

    def __enter__(self) -> _RowsCursor:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, sql: str, params: list[Any]) -> None:
        select_block = sql.split("SELECT", 1)[1].split("FROM chunks c", 1)[0]
        columns: list[tuple[str, str]] = []
        for line in select_block.splitlines():
            m = _SELECT_ALIAS_RE.match(line)
            if m:
                columns.append((m["expr"].strip(), m["alias"]))
                continue
            b = _SELECT_BARE_RE.match(line)
            if b:
                columns.append((line.strip().rstrip(","), b["col"]))
        assert columns, "SELECT 句の列を読めない（アダプタの SQL の形が変わった）"
        self._columns = columns

    def fetchall(self) -> list[dict[str, Any]]:
        return [self._row(doc, chunk) for doc, chunk in self._picked]

    def _row(self, doc: _Doc, chunk: _Chunk) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for expr, alias in self._columns:
            meta = _META_EXPR_RE.match(expr)
            if meta:
                row[alias] = doc.metadata.get(meta["key"])
                continue
            if alias not in _FIXED_COLUMNS:
                raise AssertionError(f"フェイクが知らない列 {alias!r}（{expr}）")
            row[alias] = _FIXED_COLUMNS[alias](doc, chunk)
        return row


def _like(needle: str, haystack: str | None) -> bool:
    """ILIKE '%needle%' と同じ（大文字小文字を区別しない部分一致）。"""
    return haystack is not None and needle.casefold() in haystack.casefold()


class _SqlLikePg:
    """``search_similar_new_schema`` の引数を SQL と同じ意味で適用する in-memory コーパス。

    - metadata_filters / sticky_filters は AND（キーが無い文書は NULL = 値 が偽＝除外）
    - ``__budget_or_unknown__`` は (cls_budget = 値 OR cls_budget = '不明')
    - ``__client__`` は cls_project / client_name / title の OR ILIKE
    - filter_industry は industry（soft は NULL も通す）、filter_source_types は許可リスト
    - score 降順に並べて limit で切り、**本物のアダプタ**で SearchHit にする
    """

    def __init__(
        self,
        docs: list[_Doc],
        *,
        raise_when: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self._docs = docs
        self._raise_when = raise_when
        self._real = PgVectorClient(dsn="postgresql://stub")
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def connection(self, **_: Any) -> Iterator[object]:
        yield object()

    def list_client_names(
        self, conn: Any, request_id: str | None = None, *, limit: int = 1000
    ) -> list[str]:
        names = {
            v
            for d in self._docs
            for v in (d.metadata.get("cls_project"), d.metadata.get("client_name"))
            if v
        }
        return sorted(names)

    def resolve_file_urls_by_titles(
        self, conn: Any, titles: list[str], *, request_id: str | None = None
    ) -> dict[str, str]:
        return {}

    def search_drive_by_client_names(self, **_: Any) -> list[SearchHit]:
        return []

    def list_by_metadata(self, **_: Any) -> list[SearchHit]:
        return []

    def _doc_matches(self, doc: _Doc, call: dict[str, Any]) -> bool:
        meta = doc.metadata
        types = [t.strip() for t in (call["filter_source_types"] or []) if t and t.strip()]
        if types and doc.source_type not in types:
            return False
        industry = call["filter_industry"]
        if industry is not None:
            if call["strict_industry"]:
                if meta.get("industry") != industry:
                    return False
            elif meta.get("industry") is not None and meta.get("industry") != industry:
                return False
        for key, value in (call["metadata_filters"] or {}).items():
            # 本物の SQL と同じ: soft なら「指定値 OR キー無し（未分類）」。
            if call["metadata_filters_allow_missing"] and meta.get(key) is None:
                continue
            if meta.get(key) != value:
                return False
        for key, value in (call["sticky_filters"] or {}).items():
            if key == "__budget_or_unknown__":
                if meta.get("cls_budget") not in (value, "不明"):
                    return False
            elif meta.get(key) != value:
                return False
        for key, value in (call["metadata_contains"] or {}).items():
            if key == "__client__":
                fields = (meta.get("cls_project"), meta.get("client_name"), doc.title)
            else:
                fields = (meta.get(key),)
            if not any(_like(value, f) for f in fields):
                return False
        if call["exclude_templates"] and meta.get("cls_is_template") == "true":
            return False
        if call["exclude_recurring"] and meta.get("cls_is_recurring") == "true":
            return False
        if call["exclude_duplicates"] and meta.get("suppressed") == "true":
            canonical = meta.get("duplicate_of")
            if any(d.document_id == canonical for d in self._docs):
                return False
        return True

    def search_similar_new_schema(
        self,
        conn: Any,
        embedding: list[float],
        limit: int = 5,
        filter_industry: str | None = None,
        request_id: str | None = None,
        *,
        strict_industry: bool = False,
        metadata_filters: dict[str, str] | None = None,
        metadata_filters_allow_missing: bool = False,
        sticky_filters: dict[str, str] | None = None,
        metadata_contains: dict[str, str] | None = None,
        exclude_boilerplate: bool = False,
        exclude_duplicates: bool = False,
        exclude_templates: bool = False,
        exclude_recurring: bool = False,
        embedding_col: str = "embedding",
        filter_source_types: list[str] | None = None,
    ) -> list[SearchHit]:
        call: dict[str, Any] = {
            "limit": limit,
            "filter_industry": filter_industry,
            "strict_industry": strict_industry,
            "metadata_filters": dict(metadata_filters) if metadata_filters else None,
            "metadata_filters_allow_missing": metadata_filters_allow_missing,
            "sticky_filters": dict(sticky_filters) if sticky_filters else None,
            "metadata_contains": dict(metadata_contains) if metadata_contains else None,
            "exclude_boilerplate": exclude_boilerplate,
            "exclude_duplicates": exclude_duplicates,
            "exclude_templates": exclude_templates,
            "exclude_recurring": exclude_recurring,
            "filter_source_types": list(filter_source_types) if filter_source_types else None,
        }
        self.calls.append(call)
        if self._raise_when is not None and self._raise_when(call):
            raise RuntimeError("pgvector down")
        picked = [
            (doc, chunk)
            for doc in self._docs
            if self._doc_matches(doc, call)
            for chunk in doc.chunks
            if not (exclude_boilerplate and chunk.boilerplate)
        ]
        picked.sort(key=lambda p: (-p[1].score, p[1].chunk_id))
        picked = picked[:limit]
        stub_conn = MagicMock()
        stub_conn.cursor.return_value = _RowsCursor(picked)
        # 行 → SearchHit の写像は本物のアダプタに任せる（射影していない列はヒットに載らない）。
        return self._real.search_similar_new_schema(
            conn=stub_conn,
            embedding=embedding,
            limit=limit,
            filter_industry=filter_industry,
            request_id=request_id,
            strict_industry=strict_industry,
            metadata_filters=metadata_filters,
            sticky_filters=sticky_filters,
            metadata_contains=metadata_contains,
            exclude_boilerplate=exclude_boilerplate,
            exclude_duplicates=exclude_duplicates,
            exclude_templates=exclude_templates,
            exclude_recurring=exclude_recurring,
            embedding_col=embedding_col,
            filter_source_types=filter_source_types,
        )


def _is_campaign_call(call: dict[str, Any]) -> bool:
    for key in ("metadata_filters", "sticky_filters"):
        if (call[key] or {}).get("campaign_aggregate") == "true":
            return True
    return False


def _campaign_calls(pg: _SqlLikePg) -> list[dict[str, Any]]:
    return [c for c in pg.calls if _is_campaign_call(c)]


# ---------------------------------------------------------------------------
# コーパス
# ---------------------------------------------------------------------------


def _proposal_pdf(
    *, doc_id: str, client: str, n: int, top: float, step: float, base_id: int
) -> _Doc:
    return _Doc(
        document_id=doc_id,
        source_type="gdrive",
        source_uri=f"gdrive://{doc_id}",
        title=f"{client}様_ショート動画施策ご提案.pdf",
        metadata={
            "cls_project": client,
            "cls_doc_type": "提案書",
            "cls_solution": "動画広告",
            "industry": "食品",
        },
        chunks=[
            _Chunk(
                chunk_id=base_id + i,
                content=f"{client}様 ショート動画施策のご提案 p{i + 1}: 企画骨子・体制・スケジュール",
                score=round(top - i * step, 6),
                page_num=i + 1,
            )
            for i in range(n)
        ],
    )


def _campaign_doc(
    *, doc_id: str, advertiser: str, campaign: str, score: float, chunk_id: int
) -> _Doc:
    """ingest.campaign_aggregate.campaign_metadata と同じ形（cls_solution・industry は無い）。"""
    return _Doc(
        document_id=doc_id,
        source_type="gsheets",
        source_uri="https://docs.google.com/spreadsheets/d/SHORTDB/edit#gid=0",
        title=f"施策実績 {advertiser} {campaign}",
        metadata={
            "campaign_aggregate": "true",
            "advertiser": advertiser,
            "campaign": campaign,
            "video_count": "12",
            "plays_total": "1234567",
            "cls_doc_type": "施策実績",
            "cls_project": advertiser,
            "sheet_id": "SHORTDB",
        },
        chunks=[
            _Chunk(
                chunk_id=chunk_id,
                content=(
                    f"施策実績: {advertiser} / {campaign}\n"
                    "投稿本数: 12 本（アカウント: acc_a、acc_b）。広告配信あり: 3 本。\n"
                    "再生数: 合計 1,234,567、中央値 45,678、平均 102,880、最大 456,789\n"
                    "上位の投稿（再生数順）:\n"
                    "1. 再生 456,789、保存 1,234、acc_a、ラカントで糖質オフ https://www.tiktok.com/@acc_a/video/1"
                ),
                score=score,
            )
        ],
    )


def _corpus() -> list[_Doc]:
    return [
        # サラヤ提案 PDF 40 チャンク（0.90〜0.861）。これだけで 30 件の枠が埋まる。
        _proposal_pdf(
            doc_id="saraya-pdf", client="サラヤ", n=40, top=0.90, step=0.001, base_id=100
        ),
        # サラヤ施策実績 1 チャンク（0.80・cls_solution 無し）。
        _campaign_doc(
            doc_id="saraya-campaign",
            advertiser="サラヤ",
            campaign="ラカント",
            score=0.80,
            chunk_id=900,
        ),
        # 花王提案 PDF 数チャンク・花王施策実績 1 チャンク。
        _proposal_pdf(doc_id="kao-pdf", client="花王", n=5, top=0.85, step=0.002, base_id=500),
        _campaign_doc(
            doc_id="kao-campaign", advertiser="花王", campaign="ビオレ", score=0.78, chunk_id=901
        ),
    ]


# ---------------------------------------------------------------------------
# Bedrock（要約と rerank）
# ---------------------------------------------------------------------------


def _relevance(doc: str) -> float:
    """cross-encoder を模した関連度。**入力順の passthrough ではない**。

    「伸びた動画も見たい」に答えられるのは再生数・上位の投稿を本文に持つ文書だけなので
    それを上へ。重要なのは**候補に入っていない文書は何位にもなれない**という性質で、
    これが今回の失敗（施策実績がプールに 1 件も無い）を再現する軸になる。
    """
    if "上位の投稿" in doc:
        return 0.92
    if "ショート動画" in doc:
        return 0.55
    return 0.30


def _fake_bedrock(pools: list[list[str]]) -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )

    def _do_rerank(
        *, query: str, documents: list[str], request_id: str, top_n: int
    ) -> RerankResponse:
        pools.append(list(documents))
        ranked = sorted(enumerate(documents), key=lambda p: (-_relevance(p[1]), p[0]))
        results = [
            RerankResult(index=idx, relevance_score=_relevance(doc) - rank * 0.0001)
            for rank, (idx, doc) in enumerate(ranked[: min(top_n, len(documents))])
        ]
        return RerankResponse(results=results, model_arn="arn:stub", latency_ms=1, query_count=1)

    mock.rerank.side_effect = _do_rerank
    return mock


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _build(
    pg: _SqlLikePg,
    *,
    campaign_pool_floor: int = 3,
    use_cohere_rerank: bool = True,
    use_knowledge_filters: bool = True,
    use_client_boost: bool = True,
) -> tuple[SearchSkill, list[list[str]]]:
    """本番の既定に寄せる（USE_KNOWLEDGE_FILTERS / USE_CLIENT_BOOST / rerank は本番 ON）。"""
    pools: list[list[str]] = []
    skill = SearchSkill(
        bedrock=_fake_bedrock(pools),
        pgvector=pg,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        use_new_schema=True,
        use_cohere_rerank=use_cohere_rerank,
        rerank_pool_size=30,
        drive_pool_floor=15,
        campaign_pool_floor=campaign_pool_floor,
        use_client_boost=use_client_boost,
        use_knowledge_filters=use_knowledge_filters,
    )
    return skill, pools


def _pool_has(pools: list[list[str]], mark: str) -> bool:
    return any(mark in doc for pool in pools for doc in pool)


# ---------------------------------------------------------------------------
# 中核: 赤（床なし＝今の挙動）→ 緑（床あり）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_knowledge_filters", [True, False])
def test_campaign_results_reach_rerank_only_with_floor(use_knowledge_filters: bool) -> None:
    """本件の直接再現。床 0（今の挙動）では施策実績がプールにも結果にも無く、床 3 で届く。

    use_knowledge_filters=True は「cls_solution=動画広告 の AND で除外」、False は
    「提案 PDF 40 チャンクが 30 件の枠を埋めて押し出す」の再現。どちらも床で直る。
    """
    inp = SearchInput(query=Q_SARAYA, top_k=5, filter_client="サラヤ")

    # 赤: 床なし（＝今の挙動）。
    pg_red = _SqlLikePg(_corpus())
    skill_red, pools_red = _build(
        pg_red, campaign_pool_floor=0, use_knowledge_filters=use_knowledge_filters
    )
    out_red = skill_red.run(input=inp, ctx=SkillContext())
    assert pools_red, "rerank が呼ばれていない（テストの前提が壊れている）"
    assert not _pool_has(pools_red, SARAYA_CAMPAIGN_MARK), (
        "床なしで届いてしまう＝失敗を再現できていない"
    )
    assert all(h.doc_type != "施策実績" for h in out_red.hits)
    if use_knowledge_filters:
        # 原因の固定: 本検索に cls_solution=動画広告 が AND で載っている。
        assert pg_red.calls[0]["metadata_filters"] == {"cls_solution": "動画広告"}
    assert _campaign_calls(pg_red) == []

    # 緑: 床 3。
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg, campaign_pool_floor=3, use_knowledge_filters=use_knowledge_filters)
    out = skill.run(input=inp, ctx=SkillContext())
    assert _pool_has(pools, SARAYA_CAMPAIGN_MARK), "施策実績が rerank プールに入っていない"
    campaign_hits = [h for h in out.hits if h.doc_type == "施策実績"]
    assert [h.project for h in campaign_hits] == ["サラヤ"]
    assert campaign_hits[0].title == "施策実績 サラヤ ラカント"
    # 取引先の絞りは保つ（花王の実績を混ぜない）。
    assert not _pool_has(pools, KAO_CAMPAIGN_MARK)

    floor_calls = _campaign_calls(pg)
    assert len(floor_calls) == 1
    call = floor_calls[0]
    assert call["sticky_filters"] == {"campaign_aggregate": "true"}
    assert call["metadata_filters"] is None  # 自動抽出の cls_solution は渡さない
    assert call["metadata_contains"] == {"__client__": "サラヤ"}
    assert call["filter_industry"] is None
    assert call["limit"] == 3


def test_campaign_floor_without_client_uses_query_words() -> None:
    """取引先の指定が無くても、実績を聞く語があれば床を張る（取引先で絞らない）。"""
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(input=SearchInput(query=Q_SARAYA, top_k=5), ctx=SkillContext())

    assert _pool_has(pools, SARAYA_CAMPAIGN_MARK)
    call = _campaign_calls(pg)[0]
    assert call["metadata_contains"] is None


def test_campaign_floor_with_short_rewritten_query_and_client() -> None:
    """Aico が query を短く書き換えて実績の語が消えても、取引先があれば床を張る。"""
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(query="ラカント", top_k=5, filter_client="サラヤ"), ctx=SkillContext()
    )

    assert _pool_has(pools, SARAYA_CAMPAIGN_MARK)
    assert len(_campaign_calls(pg)) == 1


def test_campaign_floor_drops_explicit_solution_from_sticky() -> None:
    """LLM が filter_solution=動画広告 を明示しても、床の補助検索では cls_solution を外す。

    外さないと施策実績（cls_solution 無し）は sticky の AND で必ず 0 件になり、床が効かない。
    """
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(
            query=Q_SARAYA, top_k=5, filter_client="サラヤ", filter_solution="動画広告"
        ),
        ctx=SkillContext(),
    )

    assert _pool_has(pools, SARAYA_CAMPAIGN_MARK)
    assert _campaign_calls(pg)[0]["sticky_filters"] == {"campaign_aggregate": "true"}


def test_campaign_floor_keeps_budget_sticky() -> None:
    """明示の予算は床の補助検索でも保つ（明示フィルタは全再検索で保持する既存設計）。"""
    pg = _SqlLikePg(_corpus())
    skill, _pools = _build(pg)
    skill.run(
        input=SearchInput(
            query=Q_SARAYA,
            top_k=5,
            filter_client="サラヤ",
            filter_budget="100〜500万",
            include_unknown_budget=True,
            filter_solution="動画広告",
        ),
        ctx=SkillContext(),
    )

    assert _campaign_calls(pg)[0]["sticky_filters"] == {
        "__budget_or_unknown__": "100〜500万",
        "campaign_aggregate": "true",
    }


def test_campaign_floor_ignores_strict_industry() -> None:
    """施策実績には industry が無い。明示の業界（strict）で本検索から落ちても床は拾う。"""
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(
            query=Q_SARAYA,
            top_k=5,
            filter_client="サラヤ",
            filter_industry="食品",
            strict_industry=True,
        ),
        ctx=SkillContext(),
    )

    assert pg.calls[0]["filter_industry"] == "食品"
    assert _pool_has(pools, SARAYA_CAMPAIGN_MARK)
    assert _campaign_calls(pg)[0]["filter_industry"] is None


def test_campaign_floor_never_adds_non_campaign_docs() -> None:
    """その取引先に施策実績が 1 件も無いとき、施策実績でない文書を足さない。

    campaign_aggregate を metadata_filters に置くと、0 件のとき _pool_search の fail-open が
    条件ごと外して再検索し、ただの提案 PDF を「床」として足してしまう。sticky に置くので
    再検索は起きず、補助検索は 1 回で終わる。
    """
    docs = [d for d in _corpus() if d.document_id != "kao-campaign"]
    inp = SearchInput(query="花王のショート動画、結果どうだった？", top_k=5, filter_client="花王")

    pg0 = _SqlLikePg(docs)
    skill0, pools0 = _build(pg0, campaign_pool_floor=0)
    skill0.run(input=inp, ctx=SkillContext())

    pg = _SqlLikePg(docs)
    skill, pools = _build(pg)
    with capture_logs() as logs:
        skill.run(input=inp, ctx=SkillContext())

    assert len(_campaign_calls(pg)) == 1
    assert len(pg.calls) == len(pg0.calls) + 1  # fail-open の再検索が無い
    assert all(_is_campaign_call(c) for c in pg.calls[len(pg0.calls) :])
    assert pools == pools0  # プールは 1 件も増えない
    # 発火したことは added=0 で必ず残す（HNSW の候補内で 0 件になった本番を見分けるため）
    applied = [e for e in logs if e.get("event") == "search_campaign_floor_applied"]
    assert [(e["added"], e["top_score"]) for e in applied] == [(0, None)]


def test_campaign_floor_does_not_duplicate_chunks() -> None:
    """本検索に既に入っている施策実績を補助検索が返しても二重に積まない。"""
    docs = [
        _proposal_pdf(
            doc_id="saraya-pdf", client="サラヤ", n=40, top=0.90, step=0.001, base_id=100
        ),
        # 本検索でも上位に入る施策実績（1 件＝床 3 未満なので床は発火する）。
        _campaign_doc(
            doc_id="saraya-campaign",
            advertiser="サラヤ",
            campaign="ラカント",
            score=0.95,
            chunk_id=900,
        ),
    ]
    pg = _SqlLikePg(docs)
    skill, pools = _build(pg, use_knowledge_filters=False)
    skill.run(
        input=SearchInput(query=Q_SARAYA, top_k=30, filter_client="サラヤ"), ctx=SkillContext()
    )

    assert len(_campaign_calls(pg)) == 1
    pool = pools[0]
    assert len(pool) == len(set(pool)), "同じチャンクが二重にプールへ入っている"
    assert sum(SARAYA_CAMPAIGN_MARK in doc for doc in pool) == 1


# ---------------------------------------------------------------------------
# 発火しない条件（床なしと SQL の呼び出し回数が同じ）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "client"),
    [
        ("花王の提案書ある？", None),
        ("花王の提案書ある？", "花王"),
        ("ハンドクリームの提案事例を教えて", None),
        ("サラヤのショート動画の提案資料を出して", None),
        ("サラヤのショート動画の提案資料を出して", "サラヤ"),
    ],
)
def test_material_seeking_queries_do_not_fire(query: str, client: str | None) -> None:
    """資料そのものを探す聞き方では床を張らない（SQL の呼び出し回数も床なしと同じ）。"""
    inp = SearchInput(query=query, top_k=5, filter_client=client)
    pg0 = _SqlLikePg(_corpus())
    skill0, _ = _build(pg0, campaign_pool_floor=0)
    skill0.run(input=inp, ctx=SkillContext())

    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    skill.run(input=inp, ctx=SkillContext())

    assert _campaign_calls(pg) == []
    assert len(pg.calls) == len(pg0.calls)


def test_not_fired_when_pool_already_has_enough_campaigns() -> None:
    """本検索のプールに施策実績が床の数だけあれば補助検索を一切走らせない。

    プール内の件数はヒットの metadata.campaign_aggregate で数える。アダプタが射影して
    いなければ件数は常に 0 になり、ここが赤くなる（フェイクは本物のアダプタを通す）。
    """
    docs = [
        _proposal_pdf(
            doc_id="saraya-pdf", client="サラヤ", n=40, top=0.90, step=0.001, base_id=100
        ),
        *[
            _campaign_doc(
                doc_id=f"saraya-campaign-{i}",
                advertiser="サラヤ",
                campaign=f"ラカント{i}",
                score=0.95 - i * 0.01,
                chunk_id=900 + i,
            )
            for i in range(3)
        ],
    ]
    pg = _SqlLikePg(docs)
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(
            query="ラカントって何本投稿して何回再生された？", top_k=5, filter_client="サラヤ"
        ),
        ctx=SkillContext(),
    )

    assert sum("施策実績: サラヤ" in doc for doc in pools[0]) == 3
    assert _campaign_calls(pg) == []


def test_not_fired_without_rerank() -> None:
    """rerank 無効時はプール概念が無い（retrieve_limit=top_k）ので発火しない。"""
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg, use_cohere_rerank=False)
    skill.run(
        input=SearchInput(query=Q_SARAYA, top_k=5, filter_client="サラヤ"), ctx=SkillContext()
    )

    assert _campaign_calls(pg) == []


def test_not_fired_with_explicit_proposal_doc_type() -> None:
    """明示の filter_doc_type=提案書 は利用者の絞り込みを優先し、発火しない。"""
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    skill.run(
        input=SearchInput(
            query=Q_SARAYA, top_k=5, filter_client="サラヤ", filter_doc_type="提案書"
        ),
        ctx=SkillContext(),
    )

    assert _campaign_calls(pg) == []


def test_campaign_floor_failure_is_fail_open() -> None:
    """補助検索が落ちても検索本体は結果を返す（fail-open）。"""
    pg = _SqlLikePg(_corpus(), raise_when=_is_campaign_call)
    skill, _ = _build(pg)
    with capture_logs() as logs:
        out = skill.run(
            input=SearchInput(query=Q_SARAYA, top_k=5, filter_client="サラヤ"), ctx=SkillContext()
        )

    assert len(_campaign_calls(pg)) == 1
    assert len(out.hits) == 5
    failed = [e for e in logs if e.get("event") == "search_campaign_floor_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 観測（ログ）: クエリ原文を出さない（G8）
# ---------------------------------------------------------------------------


def test_campaign_floor_applied_log_has_counts_only() -> None:
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    with capture_logs() as logs:
        skill.run(
            input=SearchInput(query=Q_SARAYA, top_k=5, filter_client="サラヤ"),
            ctx=SkillContext(request_id="req-campaign-1"),
        )

    applied = [e for e in logs if e.get("event") == "search_campaign_floor_applied"]
    assert len(applied) == 1
    e = applied[0]
    assert set(e) - {"event", "log_level"} == {
        "request_id",
        "floor",
        "present",
        "added",
        "pool_before",
        "top_score",
    }
    assert e["request_id"] == "req-campaign-1"
    assert (e["present"], e["added"], e["pool_before"]) == (0, 1, 30)
    assert Q_SARAYA not in repr(e) and "サラヤ" not in repr(e)


def test_auto_filters_log_only_when_filters_and_without_query() -> None:
    """単一クエリ経路の自動抽出フィルタを 1 行で出す。値は固定語彙・クエリ原文は出さない。"""
    query = "サラヤのショート動画の提案資料を出して"
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    with capture_logs() as logs:
        skill.run(input=SearchInput(query=query, top_k=5), ctx=SkillContext(request_id="req-af-1"))
        skill.run(
            input=SearchInput(query="ラカントって何本投稿して何回再生された？", top_k=5),
            ctx=SkillContext(request_id="req-af-2"),
        )

    events = [e for e in logs if e.get("event") == "search_auto_filters"]
    assert [e["request_id"] for e in events] == ["req-af-1"]  # フィルタが無いときは出さない
    e = events[0]
    assert set(e) - {"event", "log_level"} == {"request_id", "keys", "values", "allow_unclassified"}
    assert e["allow_unclassified"] is True  # 既定 ON（固定語彙の真偽値だけ・クエリ原文は出さない）
    assert e["keys"] == ["cls_doc_type", "cls_solution"]
    assert e["values"] == ["提案書", "動画広告"]
    assert query not in repr(e) and "サラヤ" not in repr(e)


def test_auto_filters_log_excludes_keys_overridden_by_explicit() -> None:
    """明示フィルタで外した自動抽出キーは出さない（本検索に実際に載ったものだけ）。"""
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    with capture_logs() as logs:
        skill.run(
            input=SearchInput(
                query="サラヤのショート動画の提案資料を出して",
                top_k=5,
                filter_doc_type="提案書",
                filter_solution="動画広告",
            ),
            ctx=SkillContext(),
        )

    assert not [e for e in logs if e.get("event") == "search_auto_filters"]


def test_auto_filters_log_off_when_knowledge_filters_disabled() -> None:
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg, use_knowledge_filters=False)
    with capture_logs() as logs:
        skill.run(
            input=SearchInput(query="サラヤのショート動画の提案資料を出して", top_k=5),
            ctx=SkillContext(),
        )

    assert not [e for e in logs if e.get("event") == "search_auto_filters"]


# ---------------------------------------------------------------------------
# 意図判定（純関数）
# ---------------------------------------------------------------------------

# 調査で作った判定表（#10 は境界。実績も候補に入れて rerank に任せる）。
_INTENT_TABLE: list[tuple[str, bool]] = [
    ("サラヤのラカントの過去のショート動画施策を教えて。伸びた動画も見たい", True),
    ("ラカントのショート動画、結果どうだった？", True),
    ("ラカントのリールとTikTokの再生数を知りたい", True),
    ("サラヤでやったインフルエンサー施策の結果を見せて", True),
    ("ラカントで一番伸びた投稿のURLちょうだい", True),
    ("ラカントって何本投稿して何回再生された？", True),
    ("花王の提案書ある？", False),
    ("ハンドクリームの提案事例を教えて", False),
    ("サラヤのショート動画の提案資料を出して", False),
    ("食品メーカーのショート動画事例", True),
]


@pytest.mark.parametrize(("query", "expected"), _INTENT_TABLE)
@pytest.mark.parametrize("has_client", [False, True])
def test_intent_table(query: str, expected: bool, has_client: bool) -> None:
    assert (
        is_campaign_results_intent(query, explicit_doc_type=None, has_client=has_client) is expected
    )


@pytest.mark.parametrize("doc_type", ["提案書", "議事録", "報告書", "価格表", "契約"])
def test_intent_false_when_explicit_other_doc_type(doc_type: str) -> None:
    assert not is_campaign_results_intent(Q_SARAYA, explicit_doc_type=doc_type, has_client=True)


def test_intent_explicit_campaign_doc_type_is_not_blocked() -> None:
    assert is_campaign_results_intent(Q_SARAYA, explicit_doc_type="施策実績", has_client=False)


def test_intent_client_only_fallback() -> None:
    """実績の語が無くても、取引先があり資料種別の明示が無ければ True（短い書き換えへの備え）。"""
    assert is_campaign_results_intent("ラカント", explicit_doc_type=None, has_client=True)
    assert not is_campaign_results_intent("ラカント", explicit_doc_type=None, has_client=False)
    assert not is_campaign_results_intent("ラカント", explicit_doc_type="議事録", has_client=True)


@pytest.mark.parametrize("query", ["ＴｉｋＴｏｋの数字を見たい", "tiktokの数字を見たい"])
def test_intent_normalizes_width_and_case(query: str) -> None:
    assert is_campaign_results_intent(query, explicit_doc_type=None, has_client=False)


# ---------------------------------------------------------------------------
# アダプタ側の射影とフェイクの忠実性
# ---------------------------------------------------------------------------


def test_pgvector_projects_campaign_aggregate_into_metadata() -> None:
    """campaign_aggregate を SELECT し、"true" のときだけ metadata に詰める。

    射影しないと SearchSkill がプール内の施策実績を数えられず（常に 0）、床が毎回発火する。
    """
    corpus = _corpus()
    campaign = next(d for d in corpus if d.document_id == "saraya-campaign")
    pdf = next(d for d in corpus if d.document_id == "saraya-pdf")
    cursor = _RowsCursor([(campaign, campaign.chunks[0]), (pdf, pdf.chunks[0])])
    conn = MagicMock()
    conn.cursor.return_value = cursor

    hits = PgVectorClient(dsn="postgresql://stub").search_similar_new_schema(
        conn=conn, embedding=[0.1] * 1024, limit=5
    )

    assert any(alias == "campaign_aggregate" for _expr, alias in cursor._columns)
    assert hits[0].metadata["campaign_aggregate"] == "true"
    assert hits[0].metadata["cls_doc_type"] == "施策実績"
    assert "cls_solution" not in hits[0].metadata
    assert "campaign_aggregate" not in hits[1].metadata


def test_fake_signature_matches_real_adapter() -> None:
    """フェイクの引数が本物と一致すること（本物に増えた絞り込みをフェイクが黙って無視しない）。"""
    real = inspect.signature(PgVectorClient.search_similar_new_schema)
    fake = inspect.signature(_SqlLikePg.search_similar_new_schema)
    assert [(p.name, p.kind, p.default) for p in real.parameters.values()] == [
        (p.name, p.kind, p.default) for p in fake.parameters.values()
    ]
