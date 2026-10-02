"""case_records store: 生 SQL を偽接続で捕まえ、upsert の冪等・sticky と検索の絞り込み/重みを固定する。

変異テスト（赤くなることを確認済み）:
- store.py の STRUCTURE_WEIGHTS を {sector: 1, ...} に変える → test_search_scores_with_design_weights が赤
- ``ON CONFLICT (case_id) DO UPDATE`` を ``DO NOTHING`` に変える → test_upsert_is_idempotent_on_case_id が赤
- ``reviewed_only`` の WHERE を外す → test_search_defaults_to_reviewed_and_non_ng が赤
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.cases.schema import OTHER, CaseRecord
from teamagent.cases.store import (
    EMBEDDING_DIM,
    MAX_STRUCTURE_SCORE,
    STRUCTURE_WEIGHTS,
    record_params,
    row_to_record,
    search_case_records,
    upsert_case_records,
)


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


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sql: list[str] = []
        self.params: list[Any] = []
        self.rows = rows or []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _record(case_id: str = "gdrive-1#1", **overrides: Any) -> CaseRecord:
    base: dict[str, Any] = {
        "case_id": case_id,
        "case_group": "kaneka|q10|2025-04",
        "client_internal": "カネカ",
        "client_masked": "機能性食品メーカー様",
        "sector": "飲料食品",
        "purpose": ["認知拡大"],
        "product_state": "新商品",
        "channel": ["店頭小売"],
        "traits": ["検証型"],
        "product": "Q10グミ",
        "scale": "",
        "period": {"start": "2025-04", "end": ""},
        "metrics": [{"name": "再生", "value": "130%", "unit": "", "source_url": "https://d/x"}],
        "result_masked": "目標比130%",
        "winpattern": "実演×テンポ",
        "competitors": [],
        "external_use": "ok",
        "sources": [{"external_id": "gdrive-1", "url": "https://d/x", "excerpt": "抜粋"}],
        "confidence": 0.7,
        "reviewed": False,
    }
    base.update(overrides)
    return CaseRecord.model_validate(base)


# ── upsert ────────────────────────────────────────────────────


def test_upsert_is_idempotent_on_case_id() -> None:
    conn = _Conn()
    record = _record()
    assert upsert_case_records(conn, [record, record]) == 2
    assert len(conn.sql) == 2
    for sql in conn.sql:
        assert "INSERT INTO case_records" in sql
        assert "ON CONFLICT (case_id) DO UPDATE" in sql
    assert conn.params[0] == conn.params[1]  # 同じレコードは同じバインド値＝再実行で差分が出ない
    assert conn.params[0]["case_id"] == "gdrive-1#1"
    assert json.loads(conn.params[0]["purpose"]) == ["認知拡大"]
    assert json.loads(conn.params[0]["metrics"])[0]["value"] == "130%"
    assert conn.params[0]["embedding"] is None
    assert conn.params[0]["schema_version"] == 1


def test_upsert_keeps_human_review_sticky() -> None:
    """reviewed 済みの行では client_masked / external_use / reviewed を再抽出で上書きしない。"""
    conn = _Conn()
    upsert_case_records(conn, [_record()])
    sql = conn.sql[0]
    assert "reviewed = case_records.reviewed" in sql
    assert "CASE WHEN case_records.reviewed THEN case_records.client_masked" in sql
    assert "CASE WHEN case_records.reviewed THEN case_records.external_use" in sql
    assert "embedding = COALESCE(EXCLUDED.embedding, case_records.embedding)" in sql


def test_upsert_binds_embedding_and_backend() -> None:
    conn = _Conn()
    vec = [0.1] * EMBEDDING_DIM
    upsert_case_records(
        conn, [_record()], embeddings={"gdrive-1#1": vec}, embedding_backend="cohere"
    )
    assert conn.params[0]["embedding"] == vec
    assert conn.params[0]["embedding_backend"] == "cohere"
    assert "%(embedding)s::vector" in conn.sql[0]


def test_upsert_rejects_wrong_dimension() -> None:
    with pytest.raises(ValueError):
        record_params(_record(), embedding=[0.1] * 3)


def test_upsert_empty_is_noop() -> None:
    conn = _Conn()
    assert upsert_case_records(conn, []) == 0
    assert conn.sql == []


# ── search ────────────────────────────────────────────────────


def _search(conn: _Conn, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "sector": "飲料食品",
        "purpose": ["認知拡大", "売上・POS"],
        "product_state": "新商品",
        "traits": ["検証型"],
        "query_embedding": None,
        "exclude_client": None,
        "limit": 5,
    }
    kwargs.update(overrides)
    return search_case_records(conn, **kwargs)


def test_search_scores_with_design_weights() -> None:
    """sector 3・purpose 2・product_state 1・traits 1（設計 §4.1）。"""
    assert STRUCTURE_WEIGHTS == {"sector": 3, "purpose": 2, "product_state": 1, "traits": 1}
    assert MAX_STRUCTURE_SCORE == 7
    conn = _Conn()
    _search(conn)
    sql = conn.sql[0]
    assert "CASE WHEN sector = %(sector)s THEN 3 ELSE 0 END" in sql
    assert "purpose ?| %(purpose)s::text[]" in sql
    assert "THEN 2 ELSE 0 END" in sql
    assert "CASE WHEN product_state = %(product_state)s" in sql
    assert "traits ?| %(traits)s::text[]" in sql
    assert "WHERE structure_score >= %(min_score)s" in sql
    assert "ORDER BY structure_score DESC, similarity DESC" in sql
    params = conn.params[0]
    assert params["sector"] == "飲料食品"
    assert params["purpose"] == ["認知拡大", "売上・POS"]
    assert params["traits"] == ["検証型"]
    assert params["min_score"] == 1
    assert params["limit"] == 5


def test_search_defaults_to_reviewed_and_non_ng() -> None:
    conn = _Conn()
    _search(conn)
    assert "reviewed = true" in conn.sql[0]
    assert "external_use <> 'ng'" in conn.sql[0]


def test_search_internal_mode_can_include_unreviewed() -> None:
    conn = _Conn()
    _search(conn, reviewed_only=False, exclude_external_ng=False)
    assert "reviewed = true" not in conn.sql[0]
    assert "external_use <> 'ng'" not in conn.sql[0]


def test_search_excludes_client_with_escaped_ilike() -> None:
    conn = _Conn()
    _search(conn, exclude_client="100%_カネカ")
    assert "client_internal NOT ILIKE %(exclude_like)s ESCAPE '\\'" in conn.sql[0]
    assert conn.params[0]["exclude_like"] == r"%100\%\_カネカ%"


def test_search_orders_by_embedding_similarity_when_given() -> None:
    conn = _Conn()
    vec = [0.5] * EMBEDDING_DIM
    _search(conn, query_embedding=vec, embedding_backend="cohere")
    sql = conn.sql[0]
    assert "embedding <=> %(query_embedding)s::vector" in sql
    assert "embedding_backend = %(embedding_backend)s" in sql
    assert conn.params[0]["query_embedding"] == vec
    assert conn.params[0]["embedding_backend"] == "cohere"


def test_search_without_embedding_uses_constant_similarity() -> None:
    conn = _Conn()
    _search(conn)
    assert "0.0::float8 AS similarity" in conn.sql[0]
    assert "query_embedding" not in conn.params[0]


def test_search_rejects_wrong_query_dimension() -> None:
    with pytest.raises(ValueError):
        _search(_Conn(), query_embedding=[0.1, 0.2])


def test_search_never_scores_other() -> None:
    """その他 は「一致」と数えない（意味の無い一致で点を付けない）。"""
    conn = _Conn()
    _search(conn, sector=OTHER, purpose=[OTHER, "採用"], product_state=OTHER, traits=[OTHER])
    params = conn.params[0]
    assert params["sector"] is None
    assert params["purpose"] == ["採用"]
    assert params["product_state"] is None
    assert params["traits"] == []


def test_search_limit_is_clamped() -> None:
    conn = _Conn()
    _search(conn, limit=500)
    assert conn.params[0]["limit"] == 50


def test_search_rows_become_hits() -> None:
    row = _record().model_dump(mode="json")
    row.update({"structure_score": 6, "similarity": 0.83, "updated_at": "2026-09-16"})
    hits = _search(_Conn(rows=[row]))
    assert len(hits) == 1
    assert hits[0].structure_score == 6
    assert hits[0].similarity == pytest.approx(0.83)
    assert hits[0].record.case_id == "gdrive-1#1"


def test_row_to_record_accepts_json_strings_for_jsonb() -> None:
    row = _record().model_dump(mode="json")
    for key in (
        "purpose",
        "channel",
        "traits",
        "period",
        "metrics",
        "similar_keys",
        "competitors",
        "sources",
    ):
        row[key] = json.dumps(row[key], ensure_ascii=False)
    record = row_to_record(row)
    assert record.purpose == ["認知拡大"]
    assert record.sources[0].url == "https://d/x"
