"""統合検索 fan-out の探し先（カルテ・事例・案件の取り出し）のテスト。

偽物は本番の形と失敗の形を再現する:
  - DB は **実 adapter**（PgVectorClient の list_client_timeline_recent / list_case_studies /
    search_similar_new_schema）を、psycopg の接続の最小の偽物につなぐ。偽物は adapter が
    発行した生 SQL と bind 値で行を返す（LIKE / ILIKE は ``\\`` のエスケープまで再現）。
  - 失敗は psycopg の例外の型そのまま（QueryCanceled＝statement timeout・SQLSTATE 57014、
    OperationalError＝接続断）。失敗した後の同じトランザクションの SQL は
    InFailedSqlTransaction になる（本番の PostgreSQL と同じ）。
  - 接続待ちの時間切れは、実 ConnectionPool（max_size=1）の枠を塞いで実際に起こす
    （teamagent.adapters.pg_pool.PoolTimeoutError がそのまま上がる）。
実 DB 0・実 Slack 0・実 Bedrock 0。
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from teamagent.adapters.pg_pool import ConnectionPool
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.skills.search import client_match
from teamagent.skills.search import fanout_sources as fs
from teamagent.skills.search.fanout_sources import (
    CASE_LIMIT,
    DEAL_NOTE,
    EXTERNAL_USE_LABELS,
    KARTE_MAX_ITEMS,
    KARTE_MAX_NAMES,
    KARTE_PER_NAME_LIMIT,
    case_lookup_by_industry,
    classify_hits,
    karte_lookup,
    reuse_connection,
    rls_connection,
)

META = {
    "user_email": "sales@example.co.jp",
    "user_groups": ["sales@example.co.jp", "all@example.co.jp"],
    "user_role": "member",
}


# ── 偽の DB（adapter の生 SQL に応える）────────────────────────────────────────


def _like(pattern: str, value: str | None, *, ci: bool) -> bool:
    r"""PostgreSQL の LIKE / ILIKE（既定のエスケープ文字 ``\``）を正規表現で再現する。"""
    if value is None:
        return False
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    flags = re.DOTALL | (re.IGNORECASE if ci else 0)
    return re.fullmatch("".join(out), value, flags) is not None


Failure = Callable[[str, Any], BaseException | None]


class FakeDb:
    """documents/chunks の代わり。行は adapter の SELECT の列名で持つ（射影済みの形）。"""

    def __init__(
        self,
        *,
        sales_fb: list[dict[str, Any]] | None = None,
        cases: list[dict[str, Any]] | None = None,
        vector_rows: list[dict[str, Any]] | None = None,
        fail: Failure | None = None,
    ) -> None:
        self.sales_fb = sales_fb or []
        self.cases = cases or []
        self.vector_rows = vector_rows or []
        self.fail = fail
        self.conns: list[FakeConn] = []

    def respond(self, sql: str, params: Any) -> list[dict[str, Any]] | BaseException:
        if self.fail is not None and (exc := self.fail(sql, params)) is not None:
            return exc
        if "d.metadata->>'client_name' LIKE %s" in sql:
            pattern, limit = params
            rows = [r for r in self.sales_fb if _like(pattern, r.get("client_name"), ci=False)]
            # ORDER BY modified_at DESC NULLS LAST, chunk_idx DESC
            rows.sort(
                key=lambda r: (r.get("occurred_at") or "", r.get("chunk_idx", 0)), reverse=True
            )
            return rows[:limit]
        if "case_corpus" in sql:
            p = dict(params)
            rows = list(self.cases)
            if "client" in p:
                rows = [
                    r for r in rows if p["client"] in (r.get("case_client"), r.get("case_brand"))
                ]
            if "client_like" in p:
                rows = [
                    r
                    for r in rows
                    if _like(p["client_like"], r.get("case_client"), ci=True)
                    or _like(p["client_like"], r.get("case_brand"), ci=True)
                ]
            if "industry" in p:
                rows = [r for r in rows if r.get("case_industry") == p["industry"]]
            rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
            return rows[: p["limit"]]
        if "<=>" in sql:
            return list(self.vector_rows)
        return []  # SET ROLE / set_config / RESET ROLE


class _Cursor:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._rows = self._conn.run(sql, params)

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows]

    def fetchone(self) -> dict[str, Any] | None:
        return dict(self._rows[0]) if self._rows else None


def _aborted() -> psycopg.errors.InFailedSqlTransaction:
    return psycopg.errors.InFailedSqlTransaction(
        "current transaction is aborted, commands ignored until end of transaction block"
    )


class FakeConn:
    """psycopg.Connection の最小の偽物。失敗した文の後はトランザクションが中断状態になる。"""

    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.sql: list[tuple[str, Any]] = []
        self.events: list[str] = []
        self.aborted = False
        self.closed = False
        db.conns.append(self)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def run(self, sql: str, params: Any) -> list[dict[str, Any]]:
        if self.aborted:
            raise _aborted()
        self.sql.append((sql, params))
        result = self.db.respond(sql, params)
        if isinstance(result, BaseException):
            self.aborted = True
            raise result
        return result

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # psycopg 3: トランザクション中の transaction() は SAVEPOINT。例外なら巻き戻して再送出。
        if self.aborted:
            raise _aborted()
        self.events.append("SAVEPOINT")
        try:
            yield None
        except BaseException:
            self.events.append("ROLLBACK TO SAVEPOINT")
            self.aborted = False
            raise
        self.events.append("RELEASE SAVEPOINT")

    def commit(self) -> None:
        self.events.append("COMMIT")
        self.aborted = False

    def rollback(self) -> None:
        self.events.append("ROLLBACK")
        self.aborted = False

    def close(self) -> None:
        self.closed = True

    def selects(self) -> list[tuple[str, Any]]:
        """adapter が打った読み取りの SQL（RLS の SET ROLE / set_config を除く）。"""
        return [(s, p) for s, p in self.sql if "FROM" in s]


def _pooled(db: FakeDb, *, timeout: float = 2.0) -> tuple[PgVectorClient, ConnectionPool[Any]]:
    """本番と同じ ``PgVectorClient(pool=ConnectionPool)``。接続だけ偽物。"""
    pool: ConnectionPool[Any] = ConnectionPool(
        connect=lambda: FakeConn(db), max_size=1, timeout=timeout
    )
    return PgVectorClient("postgresql://stub", pool=pool), pool


def _plain() -> PgVectorClient:
    return PgVectorClient("postgresql://stub")


def _never_connect() -> Any:
    raise AssertionError("SQL を打たない条件なのに接続を借りた")


class _OnlyReads:
    """読み取りの API 以外に触れたら赤にする包み（書き込み・要約・送信の口を塞ぐ）。"""

    _ALLOWED = frozenset({"list_client_timeline_recent", "list_case_studies", "connection"})

    def __init__(self, inner: PgVectorClient) -> None:
        self._inner = inner
        self.used: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self._ALLOWED:
            raise AssertionError(f"読み取り以外の API に触れた: {name}")
        self.used.append(name)
        return getattr(self._inner, name)


def _fb(
    chunk_id: int,
    client: str,
    occurred_at: str | None,
    *,
    uri: str | None = None,
    chunk_idx: int = 0,
    phase: str = "提案中",
    next_action: str = "見積の再提出",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "content": f"{client} の営業FB {chunk_id}",
        "occurred_at": occurred_at,
        "source_uri": uri or f"https://example.slack.com/archives/C1/p{chunk_id}",
        "title": f"{client} 営業FB",
        "client_name": client,
        "deal_phase": phase,
        "next_action": next_action,
        "chunk_idx": chunk_idx,
    }


def _case(
    title: str,
    client: str,
    *,
    brand: str = "",
    industry: str | None = None,
    use: str | None = "ng",
    source: str = "deck",
    uri: str = "https://docs.google.com/presentation/d/DECK/edit",
    updated_at: str = "2026-10-06",
) -> dict[str, Any]:
    return {
        "title": title,
        "source_uri": uri,
        "owner_email": "ingest@example.co.jp",
        "case_client": client,
        "case_product": "フルーツ酢",
        "case_effect": "合計 120 万回再生",
        "case_owner": "",
        "case_external_use": use,
        "case_external_use_note": "クライアント展開NG（社内のみ）" if use == "ng" else None,
        "case_source": source,
        "case_brand": brand,
        "case_industry": industry,
        "updated_at": updated_at,
    }


#: 営業 FB の時系列（list_client_timeline_recent）の SQL にだけある条件。主検索の SQL も
#: is_sales_fb を射影するので、そちらと区別できる文字列で見分ける。
_TIMELINE = "d.metadata->>'client_name' LIKE %s"


def _queries_with(conn: FakeConn, needle: str) -> list[tuple[str, Any]]:
    return [(s, p) for s, p in conn.selects() if needle in s]


# ── classify_hits ──────────────────────────────────────────────────────────


def _vector_row(chunk_id: int, **cols: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "chunk_id": chunk_id,
        "content": f"本文 {chunk_id}",
        "score": 0.5,
        "page_num": None,
        "document_id": f"00000000-0000-0000-0000-{chunk_id:012d}",
        "source_uri": f"https://drive.google.com/file/d/F{chunk_id}/view",
        "source_type": "gdrive",
        "title": f"資料 {chunk_id}",
        "updated_at": "2026-09-01",
    }
    base.update(cols)
    return base


def test_classify_main_search_projection_shapes() -> None:
    """主検索（search_similar_new_schema）が実際に返す metadata の形で分ける。

    射影には case_source・case_external_use・topic が無い（pgvector_client.py:626-653）。
    事例集 PPTX は資料種別「事例」で事例に、#proj-01 はチャンネル名で案件に入り、
    社外利用は読めないので unknown（要確認）になる。ok とは言わない。
    """
    db = FakeDb(
        vector_rows=[
            _vector_row(1, cls_doc_type="提案書", cls_industry="食品", cls_project="みなと食品"),
            _vector_row(
                2,
                source_type="other",
                title="みなと食品様：フルーツ酢",
                cls_doc_type="事例",
            ),
            _vector_row(
                3,
                campaign_aggregate="true",
                advertiser="みなと食品",
                campaign="フルーツ酢",
                cls_doc_type="施策実績",
            ),
            _vector_row(4, source_type="slack", is_sales_fb="true", client_name="みなと食品"),
            _vector_row(
                5, source_type="slack", channel_name="#proj-01案件決定-同行依頼", title="案件決定"
            ),
        ]
    )
    conn = FakeConn(db)
    hits = _plain().search_similar_new_schema(conn, [0.0, 0.0, 0.0], limit=10)  # type: ignore[arg-type]
    got = classify_hits(hits)

    assert [c.hit.chunk_id for c in got.vault] == [1]
    assert [c.hit.chunk_id for c in got.case] == [2]
    assert [c.hit.chunk_id for c in got.campaign] == [3]
    assert [c.hit.chunk_id for c in got.sales_fb] == [4]
    assert [c.hit.chunk_id for c in got.deal] == [5]
    assert got.case[0].external_use == "unknown"
    assert got.campaign[0].external_use == "unknown"
    assert got.vault[0].external_use is None
    # 業種は metadata の値だけ（無ければ None＝業種不明・推測しない）
    assert got.vault[0].industry == "食品"
    assert got.case[0].industry is None
    assert got.has_case_or_campaign


def _hit(chunk_id: int, **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, content="本文", score=0.5, metadata=meta)


def test_classify_full_document_metadata_shapes() -> None:
    """文書の metadata そのまま（案件の床の文字検索・search_topic_by_terms）の形でも分ける。

    施策実績の文書は case_source='campaign_db'・case_corpus='true' も持つ
    （campaign_aggregate.py:442-461）ので、事例より先に施策実績と判定する。
    """
    campaign_doc = _hit(
        1,
        campaign_aggregate="true",
        case_corpus="true",
        case_source="campaign_db",
        case_external_use="unknown",
        cls_doc_type="施策実績",
    )
    deck_case = _hit(
        2,
        case_corpus="true",
        case_source="deck",
        case_external_use="ng",
        cls_doc_type="事例",
    )
    sheet_ok = _hit(3, case_corpus="true", case_source="sheet", case_external_use="OK")
    deal_v2 = _hit(4, topic="案件決定", structure="form-response")
    fb_raw = _hit(5, is_sales_fb="true", topic="営業 FB")
    fb_projected = _hit(6, is_sales_fb=True)
    plain = _hit(7, cls_doc_type="議事録", cls_industry="旅行・観光")

    got = classify_hits([campaign_doc, deck_case, sheet_ok, deal_v2, fb_raw, fb_projected, plain])

    assert [c.hit.chunk_id for c in got.campaign] == [1]
    assert [c.hit.chunk_id for c in got.case] == [2, 3]
    assert [c.hit.chunk_id for c in got.deal] == [4]
    assert [c.hit.chunk_id for c in got.sales_fb] == [5, 6]
    assert [c.hit.chunk_id for c in got.vault] == [7]
    assert got.case[0].external_use == "ng"
    assert got.case[1].external_use == "ok"
    assert got.vault[0].industry == "旅行・観光"
    assert got.counts() == {"campaign": 1, "case": 2, "deal": 1, "sales_fb": 2, "vault": 1}


@pytest.mark.parametrize("value", [None, "", "unknown", "yes", "true", "可", "OKAY"])
def test_external_use_is_never_ok_unless_explicit(value: Any) -> None:
    """社外利用の可否は明示の ok だけが ok。値が無い・知らない値は unknown（要確認）。"""
    meta: dict[str, Any] = {"case_source": "deck"}
    if value is not None:
        meta["case_external_use"] = value
    got = classify_hits([_hit(1, **meta)])
    assert got.case[0].external_use == "unknown"
    assert EXTERNAL_USE_LABELS["unknown"] == "社外利用は要確認"
    assert EXTERNAL_USE_LABELS["ng"] == "社外展開NG（社内のみ）"


def test_classify_keeps_rerank_order_and_does_not_mutate() -> None:
    hits = [_hit(i, cls_doc_type="提案書") for i in (9, 3, 7)]
    before = [dict(h.metadata) for h in hits]
    got = classify_hits(hits)
    assert [c.hit.chunk_id for c in got.vault] == [9, 3, 7]
    assert [dict(h.metadata) for h in hits] == before
    assert classify_hits([]).has_case_or_campaign is False
    assert "実績ではありません" in DEAL_NOTE


# ── karte_lookup ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query_client", "reason"),
    [
        (None, "no_client"),
        ("", "no_client"),
        ("ベクトル", "self_org"),
        ("Aico", "self_org"),
        ("の", "not_a_client_name"),
        ("今週の空き時間", "not_a_client_name"),
    ],
)
def test_karte_without_named_client_issues_no_sql(query_client: str | None, reason: str) -> None:
    """名指しが無い（自社・依頼文の切れ端も）なら SQL を 1 本も打たず、接続も借りない。"""
    pg = _OnlyReads(_plain())
    res = karte_lookup(pg, _never_connect, query_client=query_client)  # type: ignore[arg-type]
    assert (res.status, res.reason) == ("skipped", reason)
    assert pg.used == []
    assert res.items == ()


def test_karte_reads_latest_feedback_with_same_rls_as_search() -> None:
    """検索と同じ RLS（ロール・email・groups・role）の接続で、最新の営業 FB を新しい順に返す。"""
    db = FakeDb(
        sales_fb=[
            _fb(1, "NGK（日本ガイシ）", "2026-08-01"),
            _fb(2, "NGK（日本ガイシ）", "2026-09-15", uri="https://x/p2", chunk_idx=0),
            _fb(3, "NGK（日本ガイシ）", "2026-09-15", uri="https://x/p2", chunk_idx=1),
            _fb(4, "日本ガイシ", "2026-10-01"),
        ]
    )
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="日本ガイシ株式会社")

    assert res.status == "ok"
    assert res.reason == "ok"
    # 1 FB 1 行（同じ FB の 2 チャンクは 1 行）・新しい順
    assert [h.chunk_id for h in res.items] == [4, 2, 1]
    assert res.items[0].metadata["next_action"] == "見積の再提出"
    assert res.searched == ("日本ガイシ",)
    conn = db.conns[0]
    # 法人格を剥いだ名前で LIKE（営業 FB の client_name には法人格が無いことが多い）
    timeline = _queries_with(conn, _TIMELINE)
    assert [p for _, p in timeline] == [["%日本ガイシ%", KARTE_PER_NAME_LIMIT]]
    executed = [s for s, _ in conn.sql]
    assert "SET ROLE teamagent_app" in executed
    gucs = {p[0] for s, p in conn.sql if "set_config('app.user_" in s}
    assert {"sales@example.co.jp", "member", "sales@example.co.jp,all@example.co.jp"} <= gucs


def test_karte_excludes_other_company_matched_by_like() -> None:
    """LIKE '%イオン%' に当たる「ライオン株式会社」は別会社として除く。

    clients_match（名寄せの部分一致）では除けない（'イオン' は 'ライオン' に含まれる）。
    変異: 語境界の照合を外すと、ライオンの営業 FB がイオンのカルテに混ざって赤。
    """
    db = FakeDb(
        sales_fb=[
            _fb(1, "イオン", "2026-09-01"),
            _fb(2, "ライオン株式会社", "2026-10-01"),
        ]
    )
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="イオン")
    assert res.status == "ok"
    assert [h.chunk_id for h in res.items] == [1]
    assert res.excluded == 1
    assert client_match.clients_match("イオン", "ライオン株式会社") is True  # 前提の確認


def test_karte_other_company_only_is_zero_not_ok() -> None:
    db = FakeDb(sales_fb=[_fb(2, "ライオン株式会社", "2026-10-01")])
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="イオン")
    assert (res.status, res.reason) == ("zero", "other_clients_only")
    assert res.items == ()


def test_karte_zero_records() -> None:
    pg, _ = _pooled(FakeDb())
    res = karte_lookup(pg, rls_connection(pg, META), query_client="日本ガイシ")
    assert (res.status, res.reason) == ("zero", "no_records")
    assert res.searched == ("日本ガイシ",)


def test_karte_uses_aliases_but_at_most_three_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """別名ごとに 1 本・最大 3 本（変異: 上限を外すと 5 本打って赤）。"""
    pairs = tuple(("サンプル物産", f"サンプルブランド{c}") for c in "ABCDE")
    monkeypatch.setattr(client_match, "alias_pairs", lambda: pairs)
    db = FakeDb(sales_fb=[_fb(1, "サンプルブランドA", "2026-10-01")])
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="サンプル物産")
    timeline = _queries_with(db.conns[0], _TIMELINE)
    assert len(timeline) == KARTE_MAX_NAMES == 3
    assert [p[0] for _, p in timeline] == [
        "%サンプル物産%",
        "%サンプルブランドA%",
        "%サンプルブランドB%",
    ]
    assert all(p[1] == KARTE_PER_NAME_LIMIT == 5 for _, p in timeline)
    assert res.status == "ok"
    assert [h.chunk_id for h in res.items] == [1]


def test_karte_alias_rows_are_kept_and_capped_newest_first() -> None:
    """別名（ブランド↔法人）で引いた行も同じ取引先として残す。合計は新しい順に 5 件まで。"""
    db = FakeDb(
        sales_fb=[_fb(i, "トップバリュ", f"2026-09-{i:02d}") for i in range(1, 5)]
        + [_fb(10 + i, "イオントップバリュ株式会社", f"2026-10-{i:02d}") for i in range(1, 4)]
    )
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="トップバリュ")
    assert res.status == "ok"
    assert len(res.items) == KARTE_MAX_ITEMS
    dates = [h.metadata["occurred_at"] for h in res.items]
    assert dates == sorted(dates, reverse=True)
    assert res.items[0].chunk_id == 13


def test_karte_escapes_like_metacharacters() -> None:
    r"""名前の ``%`` を LIKE のワイルドカードとして効かせない（``\%`` で渡す）。

    変異: エスケープを外すと、bind 値が ``%100%ユニット%`` になって赤。
    """
    db = FakeDb(
        sales_fb=[_fb(1, "100%ユニット", "2026-10-01"), _fb(2, "1000ユニット", "2026-10-02")]
    )
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="100%ユニット")
    params = [p for _, p in _queries_with(db.conns[0], _TIMELINE)]
    assert params[0][0] == "%100\\%ユニット%"
    assert [h.chunk_id for h in res.items] == [1]
    assert res.excluded == 0


def _cancel_on(needle: str) -> Failure:
    def fail(sql: str, params: Any) -> BaseException | None:
        if needle in str(params) or needle in sql:
            return psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return None

    return fail


def test_karte_statement_timeout_is_failed_not_zero() -> None:
    """statement timeout（QueryCanceled）は「探せなかった」。0 件と取り違えない。"""
    pg, _ = _pooled(FakeDb(fail=_cancel_on(_TIMELINE)))
    res = karte_lookup(pg, rls_connection(pg, META), query_client="日本ガイシ")
    assert (res.status, res.reason) == ("failed", "statement_timeout")
    assert res.items == ()
    assert res.incomplete is True


def test_karte_pool_timeout_is_failed_not_zero() -> None:
    """接続待ちの時間切れ（実 ConnectionPool の PoolTimeoutError）も「探せなかった」。"""
    db = FakeDb(sales_fb=[_fb(1, "日本ガイシ", "2026-10-01")])
    pg, pool = _pooled(db, timeout=0.05)
    with pool.connection():  # 別の検索が最後の 1 本を持ったまま
        res = karte_lookup(pg, rls_connection(pg, META), query_client="日本ガイシ")
    assert (res.status, res.reason) == ("failed", "pool_timeout")
    assert pool.stats().timeouts == 1
    assert res.searched == ()


def test_karte_connection_lost_is_failed() -> None:
    def lost(sql: str, params: Any) -> BaseException | None:
        if _TIMELINE in sql:
            return psycopg.OperationalError("server closed the connection unexpectedly")
        return None

    pg, _ = _pooled(FakeDb(fail=lost))
    res = karte_lookup(pg, rls_connection(pg, META), query_client="日本ガイシ")
    assert (res.status, res.reason) == ("failed", "db_unavailable")


def test_karte_unexpected_error_does_not_raise() -> None:
    class Broken:
        def list_client_timeline_recent(self, **_: Any) -> list[SearchHit]:
            raise RuntimeError("boom")

    conn = FakeConn(FakeDb())
    res = karte_lookup(Broken(), reuse_connection(conn), query_client="日本ガイシ")  # type: ignore[arg-type,arg-type]
    assert (res.status, res.reason) == ("failed", "internal_error")


def test_karte_partial_failure_keeps_found_rows_and_marks_incomplete() -> None:
    """2 本目で時間切れ: 1 本目の分は返し、incomplete を立てる。3 本目は打たない（速さ）。"""
    db = FakeDb(
        sales_fb=[_fb(1, "トップバリュ", "2026-10-01")],
        fail=_cancel_on("'%イオン%'"),
    )
    pg, _ = _pooled(db)
    res = karte_lookup(pg, rls_connection(pg, META), query_client="トップバリュ")
    assert res.status == "ok"
    assert res.incomplete is True
    assert [h.chunk_id for h in res.items] == [1]
    assert res.searched == ("トップバリュ",)
    timeline = _queries_with(db.conns[0], _TIMELINE)
    assert [p[0] for _, p in timeline] == ["%トップバリュ%", "%イオン%"]


def test_karte_failure_on_shared_connection_keeps_search_transaction_usable() -> None:
    """検索本体の接続を借りて失敗しても、本体はその接続で続きの SQL を打てる（SAVEPOINT）。

    変異: SAVEPOINT（conn.transaction()）を外すと、本体の次の SQL が
    InFailedSqlTransaction になって赤（本番の PostgreSQL と同じ中断状態）。
    """
    db = FakeDb(
        fail=_cancel_on(_TIMELINE),
        vector_rows=[_vector_row(1, cls_doc_type="提案書")],
    )
    conn = FakeConn(db)
    res = karte_lookup(_plain(), reuse_connection(conn), query_client="日本ガイシ")  # type: ignore[arg-type]
    assert (res.status, res.reason) == ("failed", "statement_timeout")
    assert conn.events == ["SAVEPOINT", "ROLLBACK TO SAVEPOINT"]
    # 本体の続き（rerank 前の検索など）が同じ接続で通る
    hits = _plain().search_similar_new_schema(conn, [0.0, 0.0, 0.0], limit=5)  # type: ignore[arg-type]
    assert [h.chunk_id for h in hits] == [1]
    assert conn.closed is False
    assert "COMMIT" not in conn.events  # 借りた接続を確定しない


def test_karte_success_on_shared_connection_releases_savepoint_only() -> None:
    db = FakeDb(sales_fb=[_fb(1, "日本ガイシ", "2026-10-01")])
    conn = FakeConn(db)
    res = karte_lookup(_plain(), reuse_connection(conn), query_client="日本ガイシ")  # type: ignore[arg-type]
    assert res.status == "ok"
    assert conn.events == ["SAVEPOINT", "RELEASE SAVEPOINT"]


# ── case_lookup_by_industry ────────────────────────────────────────────────


def _food_cases(n: int) -> list[dict[str, Any]]:
    return [
        _case(f"事例{i}様：ブランド{i}", f"事例{i}", industry="食品", updated_at=f"2026-09-{i:02d}")
        for i in range(1, n + 1)
    ]


def test_case_stage4_when_industry_known_and_no_case_in_main_hits() -> None:
    """正準業種が取れて主ヒットに事例・施策実績が無いときだけ、段 4 を limit 3 で引く。"""
    db = FakeDb(cases=[*_food_cases(5), _case("別業種様：X", "別業種", industry="飲料")])
    pg, _ = _pooled(db)
    main = [_hit(1, cls_doc_type="提案書", cls_industry="食品")]
    res = case_lookup_by_industry(pg, rls_connection(pg, META), industry="食品", main_hits=main)

    assert (res.status, res.reason, res.stage) == ("ok", "ok", 4)
    assert len(res.items) == CASE_LIMIT == 3
    assert all(r.industry == "食品" and r.stage == 4 for r in res.items)
    assert all(r.external_use == "ng" for r in res.items)
    assert res.searched == ("食品",)
    queries = _queries_with(db.conns[0], "case_corpus")
    assert len(queries) == 1
    assert queries[0][1] == {"limit": 3, "industry": "食品"}


def test_case_skipped_when_main_already_has_campaign_or_case() -> None:
    """主ヒットに事例・施策実績があれば段 4 は引かない（変異: 条件を外すと接続を借りて赤）。"""
    pg = _OnlyReads(_plain())
    for main in (
        [_hit(1, campaign_aggregate="true")],
        [_hit(2, cls_doc_type="事例")],
    ):
        res = case_lookup_by_industry(pg, _never_connect, industry="食品", main_hits=main)  # type: ignore[arg-type]
        assert (res.status, res.reason) == ("skipped", "main_has_cases")
    assert pg.used == []


@pytest.mark.parametrize("industry", [None, "", "ウィッグ", "宅配", "その他"])
def test_case_skipped_without_canonical_industry(industry: str | None) -> None:
    """業種を推測しない: 正準業種でない（その他も）なら SQL を打たない。"""
    pg = _OnlyReads(_plain())
    res = case_lookup_by_industry(pg, _never_connect, industry=industry, main_hits=[])  # type: ignore[arg-type]
    assert (res.status, res.reason) == ("skipped", "no_industry")
    assert pg.used == []


def test_case_named_client_exact_match_stops_at_stage1() -> None:
    """名指し（法人格・敬称・括弧つきでも）なら段 1 の完全一致。当たれば下の段は打たない。"""
    db = FakeDb(
        cases=[
            _case("みなと食品様：フルーツ酢", "みなと食品", brand="フルーツ酢", industry="食品"),
            *_food_cases(3),
        ]
    )
    pg, _ = _pooled(db)
    res = case_lookup_by_industry(
        pg,
        rls_connection(pg, META),
        industry="食品",
        main_hits=[],
        query_client="（みなと食品株式会社）",
    )
    assert (res.status, res.stage) == ("ok", 1)
    assert [r.client for r in res.items] == ["みなと食品"]
    assert res.items[0].external_use == "ng"
    assert res.items[0].kind == "case"
    queries = _queries_with(db.conns[0], "case_corpus")
    assert [q[1] for q in queries] == [{"limit": 3, "client": "みなと食品"}]


def test_case_named_client_partial_match_excludes_other_company() -> None:
    """段 2（部分一致）は語境界つきで別会社を除く（ILIKE '%イオン%' の「ライオン」）。"""
    db = FakeDb(
        cases=[
            _case("ライオン様：歯みがき", "ライオン", industry="日用品"),
            _case("青葉レコード販売様：北斗", "青葉レコード販売", industry="メディア・広告"),
        ]
    )
    pg, _ = _pooled(db)
    res = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry=None, main_hits=[], query_client="イオン"
    )
    assert (res.status, res.reason) == ("zero", "other_clients_only")
    assert res.excluded == 1

    res2 = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry=None, main_hits=[], query_client="青葉レコード"
    )
    assert (res2.status, res2.stage) == ("ok", 2)
    assert [r.client for r in res2.items] == ["青葉レコード販売"]


def test_case_two_char_client_does_not_partial_match() -> None:
    """2 文字の社名は段 1 だけ（段 2 の部分一致は 3 文字以上：pre_meeting_brief と同じ）。"""
    db = FakeDb(cases=[_case("花王グループ様：X", "花王グループ")])
    pg, _ = _pooled(db)
    res = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry=None, main_hits=[], query_client="花王"
    )
    assert (res.status, res.reason) == ("zero", "no_records")
    assert len(_queries_with(db.conns[0], "case_corpus")) == 1


def test_case_named_client_miss_falls_back_to_same_industry() -> None:
    """名指しの事例が無ければ、同業種（段 4）の事例を「その社そのものではない」と分かる段で返す。"""
    db = FakeDb(cases=_food_cases(2))
    pg, _ = _pooled(db)
    res = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry="食品", main_hits=[], query_client="みなと食品"
    )
    assert (res.status, res.stage) == ("ok", 4)
    assert res.searched == ("みなと食品", "食品")
    stages = [q[1] for q in _queries_with(db.conns[0], "case_corpus")]
    assert stages == [
        {"limit": 3, "client": "みなと食品"},
        {"limit": 3, "client_like": "%みなと食品%"},
        {"limit": 3, "industry": "食品"},
    ]


def test_case_marks_rows_already_in_main_hits() -> None:
    """事例集 PPTX は全事例で source_uri が同じなので、題名と組で「主ヒットに既にある」を判定。"""
    db = FakeDb(
        cases=[
            _case("みなと食品様：フルーツ酢", "みなと食品", updated_at="2026-10-02"),
            _case("みなと食品様：黒酢", "みなと食品", updated_at="2026-10-01"),
        ]
    )
    pg, _ = _pooled(db)
    main = [
        _hit(
            1,
            source_uri="https://docs.google.com/presentation/d/DECK/edit",
            title="みなと食品様：フルーツ酢",
            cls_doc_type="事例",
        )
    ]
    res = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry=None, main_hits=main, query_client="みなと食品"
    )
    assert [(r.title, r.in_main) for r in res.items] == [
        ("みなと食品様：フルーツ酢", True),
        ("みなと食品様：黒酢", False),
    ]


def test_case_campaign_rows_are_marked_campaign_with_unknown_use() -> None:
    db = FakeDb(
        cases=[_case("みなと食品 フルーツ酢", "みなと食品", use="unknown", source="campaign_db")]
    )
    pg, _ = _pooled(db)
    res = case_lookup_by_industry(
        pg, rls_connection(pg, META), industry=None, main_hits=[], query_client="みなと食品"
    )
    assert res.items[0].kind == "campaign"
    assert res.items[0].external_use == "unknown"


def test_case_statement_timeout_is_failed_not_zero() -> None:
    pg, _ = _pooled(FakeDb(cases=_food_cases(3), fail=_cancel_on("case_corpus")))
    res = case_lookup_by_industry(pg, rls_connection(pg, META), industry="食品", main_hits=[])
    assert (res.status, res.reason) == ("failed", "statement_timeout")
    assert res.items == ()


def test_case_pool_timeout_is_failed_not_zero() -> None:
    pg, pool = _pooled(FakeDb(cases=_food_cases(3)), timeout=0.05)
    with pool.connection():
        res = case_lookup_by_industry(pg, rls_connection(pg, META), industry="食品", main_hits=[])
    assert (res.status, res.reason) == ("failed", "pool_timeout")


def test_case_failure_on_shared_connection_keeps_search_transaction_usable() -> None:
    db = FakeDb(fail=_cancel_on("case_corpus"), vector_rows=[_vector_row(1)])
    conn = FakeConn(db)
    res = case_lookup_by_industry(
        _plain(),
        reuse_connection(conn),
        industry="食品",
        main_hits=[],  # type: ignore[arg-type]
    )
    assert (res.status, res.reason) == ("failed", "statement_timeout")
    hits = _plain().search_similar_new_schema(conn, [0.0, 0.0, 0.0], limit=5)  # type: ignore[arg-type]
    assert [h.chunk_id for h in hits] == [1]


# ── 読み取りだけ（書き込み・要約・送信の口を持たない）──────────────────────────


_WRITE_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|TRUNCATE|ALTER|CREATE|DROP|GRANT)\b", re.IGNORECASE
)


def test_lookups_issue_only_reads() -> None:
    """実際に打った SQL は読み取り（＋adapter の RLS 設定）だけ。adapter の読み取り API だけを使う。"""
    db = FakeDb(
        sales_fb=[_fb(1, "みなと食品", "2026-10-01")],
        cases=[_case("みなと食品様：フルーツ酢", "みなと食品", industry="食品")],
    )
    inner, _ = _pooled(db)
    pg = _OnlyReads(inner)
    karte_lookup(pg, rls_connection(pg, META), query_client="みなと食品")  # type: ignore[arg-type]
    case_lookup_by_industry(
        pg,  # type: ignore[arg-type]
        rls_connection(pg, META),  # type: ignore[arg-type]
        industry="食品",
        main_hits=[],
        query_client="みなと食品",
    )
    statements = [s for conn in db.conns for s, _ in conn.sql]
    assert statements
    assert not [s for s in statements if _WRITE_SQL_RE.search(s)]
    reads = [s for s in statements if "FROM" in s]
    assert reads and all(s.lstrip().upper().startswith("SELECT") for s in reads)
    assert set(pg.used) <= _OnlyReads._ALLOWED


def test_module_has_no_write_summary_or_send_api() -> None:
    """モジュールは書き込み・要約（Bedrock）・送信（Slack）・clientkarte の run() を参照しない。"""
    tree = ast.parse(inspect.getsource(fs))
    imported: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
    for module in imported:
        for banned in ("clientkarte", "slack", "bedrock", "gemini", "gdrive", "ingest.repository"):
            assert banned not in module.lower(), module
    banned_attrs = {
        "execute",
        "commit",
        "run",
        "converse",
        "chat_postMessage",
        "files_upload_v2",
        "upsert_document_with_chunks",
        "list_documents_for_client",
    }
    assert not (attrs & banned_attrs), attrs & banned_attrs
    pg_methods = {"list_client_timeline_recent", "list_case_studies", "connection"}
    assert pg_methods <= attrs  # 使う読み取りの口はこの 3 つ（他は _OnlyReads が赤にする）
