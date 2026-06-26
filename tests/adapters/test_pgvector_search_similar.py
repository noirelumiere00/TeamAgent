"""PgVectorClient.search_similar() の SQL ビルダー単体テスト。

`metadata_filters` を渡したとき、値が SQL リテラルに補間されず必ず
psycopg の placeholder (`%s`) として bind されることを保証する。
将来 Pydantic 側の入力検証が緩和されても SQL injection が発生しないことを
adapter 層で固定するための回帰テスト。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient


def _mock_conn() -> tuple[MagicMock, MagicMock]:
    """conn.cursor() の context manager をモックして cur を返す。

    cur.execute は呼び出し引数を後から検証するため MagicMock のまま、
    cur.fetchall は空 list を返して SearchHit ループをスキップさせる。
    """
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_search_similar_no_filters_emits_no_where() -> None:
    """metadata_filters=None なら WHERE 句は生成されない。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_col="metadata",
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" not in sql


def test_search_similar_metadata_filter_uses_placeholders() -> None:
    """metadata_filters を渡すと `metadata->>%s = %s` 形式の SQL になり、値は params 側に行く。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_filters={"industry": "エネルギー"},
        metadata_col="metadata",
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    # SQL は placeholder のみ。値そのものは含まない
    assert "metadata->>%s = %s" in sql
    assert "エネルギー" not in sql
    assert "'industry'" not in sql  # key 側もリテラル化されない

    # params: [embedding, "industry", "エネルギー", embedding, limit]
    assert "industry" in params
    assert "エネルギー" in params


def test_search_similar_injection_payload_stays_in_params() -> None:
    """悪意ある文字列を渡しても SQL 本体には混入せず、params 側に bind される。

    `'; DROP TABLE chunks; --` を industry に渡しても、生成 SQL に
    そのまま埋め込まれていないこと（=placeholder 経由でしか DB に届かないこと）を確認。
    """
    payload = "'; DROP TABLE chunks; --"
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposal_chunks",
        limit=5,
        metadata_filters={"industry": payload},
        metadata_col="metadata",
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    # 1. SQL 本体に injection payload が一切含まれていない
    assert payload not in sql
    assert "DROP TABLE" not in sql
    # 2. params 側に literal として渡っている（psycopg がエスケープ責任を持つ）
    assert payload in params


def test_search_similar_ignores_metadata_filters_when_no_metadata_col() -> None:
    """metadata 列を持たないテーブル (metadata_col=None) では filter を無視する fail-safe。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar(
        conn=conn,
        embedding=[0.1] * 1024,
        table="proposals_chunks",
        limit=5,
        metadata_filters={"industry": "飲食"},
        content_col="text",
        metadata_col=None,
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" not in sql
    assert "飲食" not in sql


def test_search_similar_new_schema_metadata_filters_uses_placeholders() -> None:
    """search_similar_new_schema も metadata_filters を placeholder にバインドする。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    payload = "'; DROP TABLE documents; --"
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        metadata_filters={"client_company": payload},
    )

    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])

    assert "d.metadata->>%s = %s" in sql
    assert payload not in sql
    assert payload in params
    assert "client_company" in params


def test_search_similar_new_schema_filter_industry_uses_placeholder() -> None:
    """本番経路の filter_industry= 引数を injection しても placeholder にバインドされる。

    filter_industry は Router の auto-detection やスラッシュコマンド
    (industry=...) 経由でユーザー入力の影響を受けるため、metadata_filters とは
    別経路として soft / strict 両分岐で値が SQL リテラルに混入しないことを固定する。
    """
    payload = "'; DROP TABLE chunks; --"

    # soft (strict_industry=False, 既定): industry=値 OR NULL の OR 句
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry=payload,
    )
    sql_soft: str = cur.execute.call_args.args[0]
    params_soft: list[Any] = list(cur.execute.call_args.args[1])
    assert "d.metadata->>'industry' = %s" in sql_soft
    assert "IS NULL" in sql_soft  # soft 経路は OR NULL を含む
    assert payload not in sql_soft
    assert "DROP TABLE" not in sql_soft
    assert payload in params_soft

    # strict (strict_industry=True): industry=値 のみ
    conn2, cur2 = _mock_conn()
    client.search_similar_new_schema(
        conn=conn2,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry=payload,
        strict_industry=True,
    )
    sql_strict: str = cur2.execute.call_args.args[0]
    params_strict: list[Any] = list(cur2.execute.call_args.args[1])
    assert "d.metadata->>'industry' = %s" in sql_strict
    assert payload not in sql_strict
    assert "DROP TABLE" not in sql_strict
    assert payload in params_strict


def test_search_similar_new_schema_default_no_boilerplate_clause() -> None:
    """exclude_boilerplate=False（既定）では boilerplate 句を一切足さない（現行一致）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(conn=conn, embedding=[0.1] * 1024, limit=5)
    sql: str = cur.execute.call_args.args[0]
    assert "boilerplate" not in sql
    # フィルタ無し既定では WHERE 句自体が生成されない（現行 SQL と完全一致）
    assert "WHERE" not in sql


def test_search_similar_new_schema_exclude_boilerplate_adds_where() -> None:
    """exclude_boilerplate=True で WHERE にテンプレ除外句を足す。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        exclude_boilerplate=True,
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" in sql
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false) = false" in sql


def test_search_similar_new_schema_exclude_boilerplate_combines_with_filters() -> None:
    """exclude_boilerplate=True は他の WHERE 条件と AND 結合し、汎用ループを壊さない。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry="飲食",
        metadata_filters={"client_company": "ACME"},
        exclude_boilerplate=True,
    )
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])
    # 全条件が共存（industry soft 句 / metadata_filters placeholder / boilerplate 句）
    assert "d.metadata->>'industry' = %s" in sql
    assert "d.metadata->>%s = %s" in sql
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false) = false" in sql
    assert " AND " in sql
    # metadata_filters の placeholder bind は維持（汎用ループ非破壊）
    assert "client_company" in params
    assert "ACME" in params


def test_search_similar_new_schema_default_no_duplicates_clause() -> None:
    """exclude_duplicates=False（既定）では suppressed 句を一切足さない（現行一致）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(conn=conn, embedding=[0.1] * 1024, limit=5)
    sql: str = cur.execute.call_args.args[0]
    assert "suppressed" not in sql
    # フィルタ無し既定では WHERE 句自体が生成されない（現行 SQL と完全一致）
    assert "WHERE" not in sql


def test_search_similar_new_schema_exclude_duplicates_adds_where() -> None:
    """exclude_duplicates=True で WHERE に「正本可視時だけ非正本を除外」句を足す（H3）。

    旧実装は ``suppressed IS DISTINCT FROM 'true'`` で非正本を無条件除外していたが、
    正本が狭 ACL でこの RLS conn から不可視だとクラスタごと検索消失する事故があった。
    H3 で「suppressed=true かつ duplicate_of の正本が現 conn で可視（EXISTS 真）」の
    ときだけ除外する NOT(...) 句に変更した。EXISTS が RLS 適用 conn の documents を
    引くため、正本不可視→EXISTS 偽→非正本を残す（救済）。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        exclude_duplicates=True,
    )
    sql: str = cur.execute.call_args.args[0]
    assert "WHERE" in sql
    # 旧来の無条件除外句は使わない（クラスタ消失バグの原因）
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" not in sql
    # H3 句: suppressed=true AND（正本が現 conn で可視）のときだけ除外、を NOT で包む
    assert "NOT (" in sql
    assert "COALESCE((d.metadata->>'suppressed')::bool, false)" in sql
    # 正本 (duplicate_of) を RLS 適用 conn 上の documents から EXISTS で可視性チェック
    assert "EXISTS (" in sql
    assert "FROM documents dc" in sql
    assert "dc.id = (d.metadata->>'duplicate_of')::uuid" in sql
    # uuid 形式チェックで無効 duplicate_of のキャスト例外を防ぐ（除外しない側に倒す）
    assert "d.metadata->>'duplicate_of' ~ " in sql


def test_search_similar_new_schema_exclude_duplicates_and_boilerplate_combine() -> None:
    """exclude_duplicates と exclude_boilerplate は AND 併用でき、双方の句が共存する。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        filter_industry="飲食",
        metadata_filters={"client_company": "ACME"},
        exclude_boilerplate=True,
        exclude_duplicates=True,
    )
    sql: str = cur.execute.call_args.args[0]
    params: list[Any] = list(cur.execute.call_args.args[1])
    # 全条件が共存（industry soft 句 / metadata_filters placeholder /
    # boilerplate 句 / H3 の suppressed-NOT 句）
    assert "d.metadata->>'industry' = %s" in sql
    assert "d.metadata->>%s = %s" in sql
    assert "COALESCE((c.metadata->>'boilerplate')::bool, false) = false" in sql
    # H3: 旧無条件除外句は使わず、正本可視時だけ除外する NOT(...) 句
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" not in sql
    assert "NOT (" in sql
    assert "COALESCE((d.metadata->>'suppressed')::bool, false)" in sql
    assert "FROM documents dc" in sql
    assert " AND " in sql
    # metadata_filters の placeholder bind は維持（汎用ループ非破壊）
    assert "client_company" in params
    assert "ACME" in params


def _mock_conn_with_rows(rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    """fetchall が指定 rows を返す cursor モック（hit のメタデータ写像を検証する用）。"""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_search_similar_new_schema_selects_document_id() -> None:
    """L1: SELECT に ``d.id AS document_id`` を射影する（cap_per_document が使えるように）。"""
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(conn=conn, embedding=[0.1] * 1024, limit=5)
    sql: str = cur.execute.call_args.args[0]
    assert "d.id AS document_id" in sql


def test_search_similar_new_schema_document_id_lands_in_metadata() -> None:
    """L1: 行の document_id が SearchHit.metadata['document_id'] に str で詰まる。

    dedup.cap_per_document が source_uri フォールバックでなく document_id を使える。
    """
    doc_uuid = "11111111-2222-3333-4444-555555555555"
    rows = [
        {
            "chunk_id": 42,
            "content": "本文",
            "score": 0.9,
            "page_num": None,
            "document_id": doc_uuid,
            "source_uri": "gdrive://doc",
            "source_type": "gdrive",
            "title": "提案書",
            "channel_name": None,
            "is_sales_fb": None,
            "client_name": None,
            "deal_phase": None,
            "bant_score": None,
            "channel_type": None,
            "cls_project": None,
            "cls_industry": None,
            "cls_doc_type": None,
            "cls_phase": None,
            "cls_solution": None,
            "cls_budget": None,
            "cls_target": None,
        }
    ]
    client = PgVectorClient(dsn="postgresql://stub")
    conn, _cur = _mock_conn_with_rows(rows)
    hits = client.search_similar_new_schema(conn=conn, embedding=[0.1] * 1024, limit=5)
    assert len(hits) == 1
    assert hits[0].metadata.get("document_id") == doc_uuid
    assert isinstance(hits[0].metadata["document_id"], str)


def test_search_similar_new_schema_h3_only_excludes_when_canonical_visible() -> None:
    """H3: suppressed 非正本は「正本が現 conn で EXISTS 可視のときだけ」除外される。

    意味: WHERE 句が NOT(suppressed AND uuid形式 AND EXISTS(正本)) なので、
    - 正本が RLS で不可視 → EXISTS 偽 → NOT(...) 真 → 非正本を残す（救済）
    - 正本が可視 → EXISTS 真 → NOT(...) 偽 → 非正本を除外
    SQL 文字列上で、除外を EXISTS(documents dc) の可視性に**条件付け**していることを固定。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        exclude_duplicates=True,
    )
    sql: str = cur.execute.call_args.args[0]
    # 除外は EXISTS(正本可視) を AND 条件に持ち、NOT で包まれている
    assert "NOT (" in sql
    assert "EXISTS (" in sql
    assert "SELECT 1 FROM documents dc" in sql
    assert "WHERE dc.id = (d.metadata->>'duplicate_of')::uuid" in sql
    # suppressed の真偽も AND の一部（suppressed=false の doc は除外対象にならない）
    assert "COALESCE((d.metadata->>'suppressed')::bool, false)" in sql


def test_search_similar_new_schema_h3_guards_invalid_duplicate_of_uuid() -> None:
    """H3: duplicate_of が無効 UUID でもキャスト例外を出さず、除外しない側に倒す。

    uuid 形式の正規表現マッチ（``~``）を EXISTS の前段 AND に置くことで、
    ``::uuid`` キャストは形式合格時のみ評価され、無効値では NOT(...) が真→残す。
    """
    client = PgVectorClient(dsn="postgresql://stub")
    conn, cur = _mock_conn()
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        exclude_duplicates=True,
    )
    sql: str = cur.execute.call_args.args[0]
    # uuid 形式チェックがキャストより前（同じ AND チェーン内）にある
    assert "d.metadata->>'duplicate_of' ~ " in sql
    # 正規表現に uuid 形状（8-4-4-4-12 の hex）が含まれる
    assert "[0-9a-fA-F]{8}-" in sql
    assert "[0-9a-fA-F]{12}$" in sql
