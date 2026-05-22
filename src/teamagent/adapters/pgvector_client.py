"""pgvector / PostgreSQL の薄いラッパー。

3層分離の Adapter 層。Skill からは PgVectorClient.search_similar() などを使う。
psycopg / pgvector への直叩きは禁止（CLAUDE.md 6-bis Don't）。

Usage:
    client = PgVectorClient.from_env()
    with client.connection() as conn:
        rows = client.search_similar(conn, embedding=vec, table="proposal_chunks", limit=5)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
import structlog
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]
from psycopg.rows import dict_row

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SearchHit:
    """ベクトル検索の1ヒット。

    score は cosine 類似度（1 - cosine distance）で、1.0 に近いほど類似。
    """

    chunk_id: int
    content: str
    score: float
    metadata: dict[str, Any]


class PgVectorClient:
    """pgvector への薄いラッパー。

    Skill 層からは psycopg / pgvector を直接見せない。
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @classmethod
    def from_env(cls) -> PgVectorClient:
        """環境変数 DATABASE_URL から接続情報を取得する。

        ローカル: postgresql://teamagent:teamagent@localhost:5432/teamagent
        本番: 踏み台経由・Secrets Manager 経由で組み立てた URL を入れる
        """
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "DATABASE_URL が未設定です。.env を読み込んでから起動してください"
            )
        return cls(dsn=dsn)

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """コンテキストマネージャで接続を取得する。

        終了時に必ず close する。例外時もロールバックされる。
        """
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            register_vector(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def search_similar(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        embedding: list[float],
        table: str,
        limit: int = 5,
        where: str | None = None,
        request_id: str | None = None,
        *,
        id_col: str = "id",
        content_col: str = "content",
        embedding_col: str = "embedding",
        metadata_col: str | None = "metadata",
        extra_cols: list[str] | None = None,
    ) -> list[SearchHit]:
        """cosine 類似度上位 limit 件を返す。

        Args:
            conn: connection() で取得した接続
            embedding: クエリベクトル（1024 次元想定）
            table: 検索対象テーブル名（外部入力禁止、コード内固定で渡すこと）
            limit: 上位件数
            where: 追加の WHERE 句（外部入力禁止、コード内固定の文字列）
            request_id: トレース ID
            id_col: 主キー列名（デフォルト "id"）
            content_col: 本文列名（デフォルト "content"、proposals_chunks では "text"）
            embedding_col: ベクトル列名（デフォルト "embedding"）
            metadata_col: JSONB メタデータ列名。None なら読まない（無いテーブル向け）
            extra_cols: metadata 列が無い場合に metadata dict として格納したい追加列名
        """
        # SQL インジェクション対策: 列名・テーブル名はコード内固定値のみ受け付ける
        where_clause = f"WHERE {where}" if where else ""
        select_cols: list[str] = [
            f"{id_col} AS chunk_id",
            f"{content_col} AS content",
            f"1 - ({embedding_col} <=> %s::vector) AS score",
        ]
        if metadata_col is not None:
            select_cols.append(f"{metadata_col} AS metadata")
        extras = list(extra_cols or [])
        for col in extras:
            select_cols.append(col)

        sql = f"""
            SELECT {", ".join(select_cols)}
            FROM {table}
            {where_clause}
            ORDER BY {embedding_col} <=> %s::vector
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (embedding, embedding, limit))
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            if metadata_col is not None:
                meta: dict[str, Any] = dict(r.get("metadata") or {})
            else:
                meta = {col: r.get(col) for col in extras}
            hits.append(
                SearchHit(
                    chunk_id=int(r["chunk_id"]),
                    content=str(r["content"]),
                    score=float(r["score"]),
                    metadata=meta,
                )
            )

        logger.info(
            "pgvector_search",
            request_id=request_id,
            table=table,
            limit=limit,
            hit_count=len(hits),
            top_score=hits[0].score if hits else None,
        )
        return hits
