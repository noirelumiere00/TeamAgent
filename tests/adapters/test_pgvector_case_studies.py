"""``list_case_studies`` / ``get_industry_for_client`` の SQL 契約。

skill 側テストのフェイク DB は「母集団の絞り込み」を自前で再現するため、**実際に
発行される SQL が case_corpus で絞っているか**は検出できない。ここで生 SQL を捕まえ、
母集団固定・ILIKE エスケープ・段の分岐・段3/4 の業種必須を直接固定する。
"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.pgvector_client import PgVectorClient


class _Cursor:
    def __init__(self, owner: _Conn) -> None:
        self._owner = owner

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._owner.sql.append(sql)
        self._owner.params.append(params)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._owner.rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._owner.rows[0] if self._owner.rows else None


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sql: list[str] = []
        self.params: list[Any] = []
        self.rows = rows or []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _client() -> PgVectorClient:
    return PgVectorClient("postgresql://user:pw@localhost/db")


def test_population_is_pinned_to_case_corpus_in_every_stage() -> None:
    """変異: WHERE から ``case_corpus = 'true'`` を外すと全社資料が返って赤。"""
    client = _client()
    for stage, kwargs in (
        (1, {"client_name": "花王"}),
        (2, {"client_name": "初田製作所"}),
        (3, {"industry": "食品", "product": "クイックディナー"}),
        (4, {"industry": "食品"}),
    ):
        conn = _Conn()
        client.list_case_studies(conn, stage=stage, **kwargs)  # type: ignore[arg-type]
        assert conn.sql, f"stage {stage} が SQL を発行していない"
        assert "d.metadata->>'case_corpus' = 'true'" in conn.sql[0], stage


def test_exact_match_stage_uses_equality_not_like() -> None:
    """段1 は等価。「花王」（2 文字）でも長さで落とさない。"""
    conn = _Conn()
    _client().list_case_studies(conn, client_name="花王", stage=1)  # type: ignore[arg-type]
    assert "d.metadata->>'client_name' = %(client)s" in conn.sql[0]
    assert "ILIKE" not in conn.sql[0]
    assert conn.params[0]["client"] == "花王"


def test_partial_match_escapes_like_metacharacters() -> None:
    r"""``%`` ``_`` ``\`` を含む社名がワイルドカードとして効かない。

    変異: ``_escape_like`` を素通しにすると、``%`` を含む社名で全件が返る。
    """
    conn = _Conn()
    _client().list_case_studies(conn, client_name=r"100%_電\通", stage=2)  # type: ignore[arg-type]
    assert "ESCAPE '\\'" in conn.sql[0]
    assert conn.params[0]["client_like"] == r"%100\%\_電\\通%"


def test_escape_order_backslash_first() -> None:
    r"""``\`` を先に置換しないと ``%`` のエスケープが二重に壊れる。"""
    assert PgVectorClient._escape_like("\\") == "\\\\"
    assert PgVectorClient._escape_like("a%b_c") == "a\\%b\\_c"


def test_stage3_and_4_require_industry_and_emit_nothing_without_it() -> None:
    """業種が無ければ SQL を 1 本も発行しない（業種を推測しない）。"""
    client = _client()
    for stage in (3, 4):
        conn = _Conn()
        assert client.list_case_studies(conn, industry=None, stage=stage) == []  # type: ignore[arg-type]
        assert conn.sql == []


def test_stage3_requires_product_too() -> None:
    conn = _Conn()
    assert _client().list_case_studies(conn, industry="食品", product=None, stage=3) == []  # type: ignore[arg-type]
    assert conn.sql == []


def test_stage1_and_2_require_client_name() -> None:
    client = _client()
    for stage in (1, 2):
        conn = _Conn()
        assert client.list_case_studies(conn, client_name="  ", stage=stage) == []  # type: ignore[arg-type]
        assert conn.sql == []


def test_unknown_stage_is_rejected() -> None:
    conn = _Conn()
    assert _client().list_case_studies(conn, client_name="X", stage=9) == []  # type: ignore[arg-type]
    assert conn.sql == []


def test_limit_is_bound_and_clamped() -> None:
    conn = _Conn()
    _client().list_case_studies(conn, client_name="花王", stage=1, limit=99)  # type: ignore[arg-type]
    assert conn.params[0]["limit"] == 5
    assert "LIMIT %(limit)s" in conn.sql[0]


def test_projection_never_selects_chunk_content() -> None:
    """pptx の先頭 chunk（表紙）を効果として出さない＝chunks を触らない。"""
    conn = _Conn()
    _client().list_case_studies(conn, client_name="花王", stage=1)  # type: ignore[arg-type]
    sql = conn.sql[0]
    assert "chunks" not in sql
    assert "c.content" not in sql
    for column in ("case_effect", "case_product", "case_owner", "case_external_use"):
        assert column in sql


def test_corpus_availability_probe_is_limited_to_case_corpus() -> None:
    conn = _Conn(rows=[{"?column?": 1}])
    assert _client().case_corpus_available(conn) is True  # type: ignore[arg-type]
    assert "d.metadata->>'case_corpus' = 'true'" in conn.sql[0]
    assert _client().case_corpus_available(_Conn()) is False  # type: ignore[arg-type]


def test_industry_lookup_returns_none_when_absent() -> None:
    client = _client()
    assert client.get_industry_for_client(_Conn(), "花王") is None  # type: ignore[arg-type]
    assert client.get_industry_for_client(_Conn(rows=[{"industry": "  "}]), "花王") is None  # type: ignore[arg-type]
    assert client.get_industry_for_client(_Conn(rows=[{"industry": "化粧品"}]), "花王") == "化粧品"  # type: ignore[arg-type]


def test_industry_lookup_binds_the_name() -> None:
    conn = _Conn(rows=[{"industry": "化粧品"}])
    _client().get_industry_for_client(conn, "花王")  # type: ignore[arg-type]
    assert conn.params[0] == {"name": "花王"}
    assert "花王" not in conn.sql[0]  # 文字列連結していない
