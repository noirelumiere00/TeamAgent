"""``case_records`` テーブル（migration 0028）の upsert と検索。

SQL はこのモジュールに閉じる（script / skill から生 SQL を書かない）。接続は
``PgVectorClient.connection(...)`` で取った psycopg 接続（dict_row・pgvector 登録済）を受ける。

検索の設計（設計書 §4.1）:
  1. 構造一致スコアで絞る — sector 3・purpose 2・product_state 1・traits 1（最大 7）。
     ``min_structure_score``（既定 1）未満は返さない（構造が 1 つも合わない事例を出さない）。
  2. 要約埋め込みの cosine 類似で並べる（``query_embedding`` 省略時は更新日時順）。
  3. 既定で ``reviewed = true`` のみ（未確認は draft・社内検索は ``reviewed_only=False``）。
     ``external_use = 'ng'`` は既定で除外（社外向け）。``exclude_client`` で対象社の自社事例を外す。

冪等: ``ON CONFLICT (case_id) DO UPDATE``。人が確定する ``reviewed`` は再抽出で上書きせず、
reviewed 済みの行では ``client_masked`` / ``external_use`` も既存値を保つ（sticky）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog

from teamagent.cases.schema import CASE_RECORD_VERSION, OTHER, CaseRecord

logger = structlog.get_logger(__name__)

TABLE: Final = "case_records"
#: 既存 chunks.embedding / embedding_cohere と同じ次元（migration 0001 / 0016）。
EMBEDDING_DIM: Final = 1024
STRUCTURE_WEIGHTS: Final[dict[str, int]] = {
    "sector": 3,
    "purpose": 2,
    "product_state": 1,
    "traits": 1,
}
MAX_STRUCTURE_SCORE: Final = sum(STRUCTURE_WEIGHTS.values())

_JSONB_COLUMNS: Final[tuple[str, ...]] = (
    "purpose",
    "channel",
    "traits",
    "period",
    "metrics",
    "similar_keys",
    "competitors",
    "sources",
)
_SCALAR_COLUMNS: Final[tuple[str, ...]] = (
    "case_id",
    "case_group",
    "client_internal",
    "client_masked",
    "sector",
    "product_state",
    "product",
    "scale",
    "result_masked",
    "winpattern",
    "external_use",
    "confidence",
    "reviewed",
)
RECORD_COLUMNS: Final[tuple[str, ...]] = _SCALAR_COLUMNS + _JSONB_COLUMNS

_UPSERT_SQL: Final = f"""
INSERT INTO {TABLE} (
    case_id, case_group, client_internal, client_masked, sector, product_state,
    product, scale, result_masked, winpattern, external_use, confidence, reviewed,
    purpose, channel, traits, period, metrics, similar_keys, competitors, sources,
    embedding, embedding_backend, schema_version, updated_at
) VALUES (
    %(case_id)s, %(case_group)s, %(client_internal)s, %(client_masked)s, %(sector)s,
    %(product_state)s, %(product)s, %(scale)s, %(result_masked)s, %(winpattern)s,
    %(external_use)s, %(confidence)s, %(reviewed)s,
    %(purpose)s::jsonb, %(channel)s::jsonb, %(traits)s::jsonb, %(period)s::jsonb,
    %(metrics)s::jsonb, %(similar_keys)s::jsonb, %(competitors)s::jsonb, %(sources)s::jsonb,
    %(embedding)s::vector, %(embedding_backend)s, %(schema_version)s, NOW()
)
ON CONFLICT (case_id) DO UPDATE SET
    case_group = EXCLUDED.case_group,
    client_internal = EXCLUDED.client_internal,
    -- 人が確定した行（reviewed）では masked 表記と対外可否を再抽出で上書きしない（sticky）
    client_masked = CASE WHEN {TABLE}.reviewed THEN {TABLE}.client_masked
                         ELSE EXCLUDED.client_masked END,
    external_use = CASE WHEN {TABLE}.reviewed THEN {TABLE}.external_use
                        ELSE EXCLUDED.external_use END,
    reviewed = {TABLE}.reviewed,
    sector = EXCLUDED.sector,
    product_state = EXCLUDED.product_state,
    product = EXCLUDED.product,
    scale = EXCLUDED.scale,
    result_masked = EXCLUDED.result_masked,
    winpattern = EXCLUDED.winpattern,
    confidence = EXCLUDED.confidence,
    purpose = EXCLUDED.purpose,
    channel = EXCLUDED.channel,
    traits = EXCLUDED.traits,
    period = EXCLUDED.period,
    metrics = EXCLUDED.metrics,
    similar_keys = EXCLUDED.similar_keys,
    competitors = EXCLUDED.competitors,
    sources = EXCLUDED.sources,
    embedding = COALESCE(EXCLUDED.embedding, {TABLE}.embedding),
    embedding_backend = COALESCE(EXCLUDED.embedding_backend, {TABLE}.embedding_backend),
    schema_version = EXCLUDED.schema_version,
    updated_at = NOW()
"""

_SELECT_COLUMNS: Final = ", ".join(RECORD_COLUMNS)


@dataclass(frozen=True)
class CaseSearchHit:
    """検索 1 件。``structure_score`` は 0〜7、``similarity`` は cosine 類似（埋め込み無しは 0）。

    dataclass なので、CaseCandidate への変換は selectors 側で行う。
    """

    record: CaseRecord
    structure_score: int
    similarity: float


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def record_params(
    record: CaseRecord,
    *,
    embedding: Sequence[float] | None = None,
    embedding_backend: str | None = None,
) -> dict[str, Any]:
    """upsert のバインド値（jsonb 列は JSON 文字列・embedding は list）。"""
    if embedding is not None and len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"embedding は {EMBEDDING_DIM} 次元 (got {len(embedding)})")
    payload = record.model_dump(mode="json")
    params: dict[str, Any] = {column: payload[column] for column in _SCALAR_COLUMNS}
    for column in _JSONB_COLUMNS:
        params[column] = _dumps(payload[column])
    params["embedding"] = list(embedding) if embedding is not None else None
    params["embedding_backend"] = embedding_backend if embedding is not None else None
    params["schema_version"] = CASE_RECORD_VERSION
    return params


def upsert_case_records(
    conn: Any,
    records: Sequence[CaseRecord],
    *,
    embeddings: dict[str, Sequence[float]] | None = None,
    embedding_backend: str | None = None,
    request_id: str | None = None,
) -> int:
    """レコードを冪等に書く（同じ case_id は UPDATE）。書いた件数を返す。

    ``embeddings`` は ``{case_id: vector}``。無い case_id は embedding を NULL で入れる
    （既存行の embedding は COALESCE で保持）。commit は呼び出し側（connection() が担う）。
    """
    if not records:
        return 0
    vectors = embeddings or {}
    written = 0
    with conn.cursor() as cur:
        for record in records:
            cur.execute(
                _UPSERT_SQL,
                record_params(
                    record,
                    embedding=vectors.get(record.case_id),
                    embedding_backend=embedding_backend,
                ),
            )
            written += 1
    logger.info("case_records_upserted", request_id=request_id, count=written)
    return written


def _match_values(values: Sequence[str] | None) -> list[str]:
    """照合に使う値。``その他`` は「一致」と数えない（意味の無い一致で点を付けない）。"""
    return [value for value in (values or []) if value and value != OTHER]


def search_case_records(
    conn: Any,
    *,
    sector: str | None,
    purpose: Sequence[str] | None,
    product_state: str | None,
    traits: Sequence[str] | None,
    query_embedding: Sequence[float] | None = None,
    exclude_client: str | None = None,
    limit: int = 5,
    reviewed_only: bool = True,
    exclude_external_ng: bool = True,
    min_structure_score: int = 1,
    embedding_backend: str | None = None,
    request_id: str | None = None,
) -> list[CaseSearchHit]:
    """構造一致スコアで絞り、埋め込み類似で並べる。"""
    limit = max(1, min(50, int(limit)))
    if query_embedding is not None and len(query_embedding) != EMBEDDING_DIM:
        raise ValueError(f"query_embedding は {EMBEDDING_DIM} 次元 (got {len(query_embedding)})")
    params: dict[str, Any] = {
        "sector": sector if sector and sector != OTHER else None,
        "purpose": _match_values(purpose),
        "product_state": product_state if product_state and product_state != OTHER else None,
        "traits": _match_values(traits),
        "min_score": max(0, int(min_structure_score)),
        "limit": limit,
    }
    where: list[str] = []
    if reviewed_only:
        where.append("reviewed = true")
    if exclude_external_ng:
        where.append("external_use <> 'ng'")
    if exclude_client and exclude_client.strip():
        where.append("client_internal NOT ILIKE %(exclude_like)s ESCAPE '\\'")
        params["exclude_like"] = f"%{_escape_like(exclude_client.strip())}%"
    if embedding_backend:
        where.append("embedding_backend = %(embedding_backend)s")
        params["embedding_backend"] = embedding_backend
    if query_embedding is not None:
        similarity = "COALESCE(1 - (embedding <=> %(query_embedding)s::vector), 0.0)"
        params["query_embedding"] = list(query_embedding)
    else:
        similarity = "0.0::float8"
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT * FROM (
            SELECT
                {_SELECT_COLUMNS},
                updated_at,
                (CASE WHEN sector = %(sector)s THEN {STRUCTURE_WEIGHTS["sector"]} ELSE 0 END)
                + (CASE WHEN purpose ?| %(purpose)s::text[]
                        THEN {STRUCTURE_WEIGHTS["purpose"]} ELSE 0 END)
                + (CASE WHEN product_state = %(product_state)s
                        THEN {STRUCTURE_WEIGHTS["product_state"]} ELSE 0 END)
                + (CASE WHEN traits ?| %(traits)s::text[]
                        THEN {STRUCTURE_WEIGHTS["traits"]} ELSE 0 END) AS structure_score,
                {similarity} AS similarity
            FROM {TABLE}{where_sql}
        ) scored
        WHERE structure_score >= %(min_score)s
        ORDER BY structure_score DESC, similarity DESC, updated_at DESC NULLS LAST
        LIMIT %(limit)s
    """  # nosec B608 - 列・重みは定数、条件値はすべてバインド
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    hits = [row_to_hit(dict(row)) for row in rows]
    logger.info(
        "case_records_searched",
        request_id=request_id,
        hit_count=len(hits),
        reviewed_only=reviewed_only,
        has_embedding=query_embedding is not None,
    )
    return hits


def _load_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def row_to_record(row: dict[str, Any]) -> CaseRecord:
    """SELECT 行（jsonb は dict/list または JSON 文字列）から ``CaseRecord`` を復元する。"""
    data: dict[str, Any] = {column: row[column] for column in _SCALAR_COLUMNS}
    for column in _JSONB_COLUMNS:
        data[column] = _load_json(row.get(column))
    data["confidence"] = float(data["confidence"])
    return CaseRecord.model_validate(data)


def row_to_hit(row: dict[str, Any]) -> CaseSearchHit:
    return CaseSearchHit(
        record=row_to_record(row),
        structure_score=int(row.get("structure_score") or 0),
        similarity=float(row.get("similarity") or 0.0),
    )


__all__ = [
    "EMBEDDING_DIM",
    "MAX_STRUCTURE_SCORE",
    "RECORD_COLUMNS",
    "STRUCTURE_WEIGHTS",
    "TABLE",
    "CaseSearchHit",
    "record_params",
    "row_to_hit",
    "row_to_record",
    "search_case_records",
    "upsert_case_records",
]
