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
from pgvector.psycopg import register_vector
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
            raise RuntimeError("DATABASE_URL が未設定です。.env を読み込んでから起動してください")
        return cls(dsn=dsn)

    @contextmanager
    def connection(
        self,
        *,
        app_role: str | None = None,
        user_email: str | None = None,
        user_groups: list[str] | None = None,
        user_role: str | None = None,
    ) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """コンテキストマネージャで接続を取得する。

        Sprint 3 / migration 0002 で RLS 連携を導入：
        - `app_role` を指定すると `SET ROLE` で RLS を効かせるロール
          （例 "teamagent_app"）に切り替わる。本番 documents/chunks 検索時は
          必ず "teamagent_app" を渡すこと。
          （default は None = SET ROLE しない＝旧挙動互換 + ローカル開発で
          migration 0002 を流していなくても動く）
        - `user_email` / `user_groups` / `user_role` を渡すと、対応する
          `app.user_*` GUC を SET LOCAL で注入し、RLS policy 評価に使われる
        - これらは transaction 単位で適用、commit 後は破棄

        終了時に必ず close する。例外時もロールバックされる。
        """
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            register_vector(conn)
            with conn.cursor() as cur:
                if app_role:
                    # SET ROLE は session レベル（commit を跨いで持続）
                    # psycopg は parameterized identifier をサポートしないので
                    # app_role はコード内固定値のみ受け付ける
                    if not app_role.replace("_", "").isalnum():
                        raise ValueError(f"invalid app_role: {app_role!r}")
                    cur.execute(f"SET ROLE {app_role}")  # nosec B608
                # RLS policy 評価用 GUC（transaction-local）
                # SET LOCAL は parameterize 不可なので set_config(name, value, is_local=true) を使う
                if user_role:
                    cur.execute("SELECT set_config('app.user_role', %s, true)", (user_role,))
                if user_email:
                    cur.execute("SELECT set_config('app.user_email', %s, true)", (user_email,))
                if user_groups:
                    cur.execute(
                        "SELECT set_config('app.user_groups', %s, true)",
                        (",".join(user_groups),),
                    )
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
        metadata_filters: dict[str, str] | None = None,
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
            metadata_filters: JSONB メタデータの等価フィルタ。例: ``{"industry": "エネルギー"}``。
                key / value はどちらも ``metadata_col->>%s = %s`` の placeholder に
                bind され、SQL injection から保護される。``metadata_col is None`` の
                テーブル（旧 proposals_chunks 等）では無視される（fail-safe）。
            request_id: トレース ID
            id_col: 主キー列名（デフォルト "id"）
            content_col: 本文列名（デフォルト "content"、proposals_chunks では "text"）
            embedding_col: ベクトル列名（デフォルト "embedding"）
            metadata_col: JSONB メタデータ列名。None なら読まない（無いテーブル向け）
            extra_cols: metadata 列が無い場合に metadata dict として格納したい追加列名
        """
        # SQL インジェクション対策: 列名・テーブル名はコード内固定値のみ受け付ける
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

        # WHERE: metadata_filters の (key, value) を placeholder にバインド。
        # metadata_col が None のテーブルでは JSONB 取得不可なのでフィルタを無視する。
        where_clauses: list[str] = []
        filter_params: list[Any] = []
        if metadata_filters and metadata_col is not None:
            for key, value in metadata_filters.items():
                where_clauses.append(f"{metadata_col}->>%s = %s")
                filter_params.extend([key, value])
        where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # bandit B608: 列名・テーブル名はコード内固定値、metadata_filters の値は placeholder
        sql = f"""
            SELECT {", ".join(select_cols)}
            FROM {table}
            {where_clause}
            ORDER BY {embedding_col} <=> %s::vector
            LIMIT %s
        """  # nosec B608
        params: list[Any] = [embedding, *filter_params, embedding, limit]
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            if metadata_col is not None:
                meta: dict[str, Any] = dict(r.get("metadata") or {})
            else:
                meta = {}
            # extra_cols があれば値を metadata に merge（metadata 列の存在に依らず）
            for col in extras:
                if col in r and col not in meta:
                    meta[col] = r.get(col)
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

    def search_similar_new_schema(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        embedding: list[float],
        limit: int = 5,
        filter_industry: str | None = None,
        request_id: str | None = None,
        *,
        strict_industry: bool = False,
        metadata_filters: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """documents + chunks JOIN で cosine 類似度上位 limit 件を返す。

        migration 0001 で作成した新スキーマ（documents / chunks）専用。
        RLS は connection(app_role='teamagent_app', user_email=...) で有効化済の前提。

        contextualized 列があれば優先して返す（Contextual Retrieval との互換性維持）。
        chunk_id は UUID から hashtext で安定した整数に変換する（int 型の後方互換性維持）。
        source_uri / source_type / title / channel_name は metadata に詰めて返す。

        filter_industry のセマンティクス:
        - strict_industry=False（既定）: soft filter。industry=指定値 OR industry IS NULL を許容。
          Router の自動付与で Slack docs (industry メタ無し) が全件除外されるのを防ぐ。
        - strict_industry=True: 厳密一致。明示的に user が「飲食だけ」と指定したい場合用。

        metadata_filters:
            ``d.metadata->>%s = %s`` の追加 AND 条件として bind される（厳密一致のみ）。
            filter_industry とは独立に AND 結合する。key / value は placeholder 化される
            ため SQL injection から保護される。
        """
        where_parts: list[str] = []
        params: list[Any] = [embedding]  # score 算出の 1st %s

        if filter_industry is not None:
            if strict_industry:
                # 厳密: industry=指定値 のみ
                where_parts.append("d.metadata->>'industry' = %s")
                params.append(filter_industry)
            else:
                # soft: industry=指定値 OR NULL (industry メタを持たない docs も含める)
                # Slack docs などは industry メタが無いので、auto-filter で全件除外を防ぐ
                where_parts.append(
                    "(d.metadata->>'industry' = %s OR d.metadata->>'industry' IS NULL)"
                )
                params.append(filter_industry)

        if metadata_filters:
            for key, value in metadata_filters.items():
                where_parts.append("d.metadata->>%s = %s")
                params.extend([key, value])

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        # chunk_id: hashtext は int4 だが bigint キャストして abs で INT_MIN overflow を防ぐ
        # Day 8 (2026-05-28): Slack 営業 FB の is_sales_fb / client_name を expose。
        # Phase 2 で Drive 自動マッチングに使う (SearchSkill._find_related_drive_docs)。
        sql = f"""
            SELECT
                abs(hashtext(c.id::text)::bigint) AS chunk_id,
                COALESCE(c.contextualized, c.content) AS content,
                1 - (c.embedding <=> %s::vector) AS score,
                c.page_num,
                d.source_uri,
                d.source_type::text AS source_type,
                d.title,
                d.metadata->>'channel_name' AS channel_name,
                d.metadata->>'is_sales_fb' AS is_sales_fb,
                d.metadata->>'client_name' AS client_name,
                d.metadata->>'deal_phase' AS deal_phase,
                d.metadata->>'bant_score' AS bant_score,
                d.metadata->>'channel_type' AS channel_type
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            {where_clause}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
        """  # nosec B608

        # ORDER BY + LIMIT 用パラメータを追加
        params.extend([embedding, limit])

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            meta: dict[str, Any] = {
                "source_uri": r.get("source_uri"),
                "source_type": r.get("source_type"),
                "title": r.get("title"),
                "channel_name": r.get("channel_name"),
            }
            if r.get("page_num") is not None:
                meta["page_num"] = r["page_num"]
            # Day 8: 営業 FB metadata (Phase 2 Drive 自動マッチング用)
            if r.get("is_sales_fb") == "true":
                meta["is_sales_fb"] = True
                if r.get("client_name"):
                    meta["client_name"] = r["client_name"]
                if r.get("deal_phase"):
                    meta["deal_phase"] = r["deal_phase"]
                # Sprint 5: BANT 評価 / チャネル種別 (代理店/直販)。検索結果での
                # フィルタ・表示・eval 判定 (expect_metadata) に使う。
                if r.get("bant_score"):
                    meta["bant_score"] = r["bant_score"]
                if r.get("channel_type"):
                    meta["channel_type"] = r["channel_type"]
            hits.append(
                SearchHit(
                    chunk_id=int(r["chunk_id"]),
                    content=str(r["content"]),
                    score=float(r["score"]),
                    metadata=meta,
                )
            )

        logger.info(
            "pgvector_search_new_schema",
            request_id=request_id,
            limit=limit,
            hit_count=len(hits),
            top_score=hits[0].score if hits else None,
        )
        return hits

    def list_by_metadata(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        metadata_filters: dict[str, str],
        limit: int = 5,
        request_id: str | None = None,
    ) -> list[SearchHit]:
        """メタデータフィルタに一致する FB chunk を新しい順に列挙する (集約・一覧クエリ用)。

        「BANT A の案件一覧」「失注案件」のような列挙系クエリは意味検索では
        答えられないため、``WHERE metadata->>'bant_score' = 'A'`` 等で
        構造化フィルタ一致を modified_at 降順 (新しい案件優先) で返す。

        embedding を使わないため score は recency proxy ではなく 1.0 固定
        (フィルタ完全一致 = 高信頼)。min_relevance 閾値より上に置き、落とされない。
        key / value は placeholder 化され SQL injection から保護される。
        """
        if not metadata_filters:
            return []

        where_parts = ["d.metadata->>'is_sales_fb' = 'true'"]
        params: list[Any] = []
        for key, value in metadata_filters.items():
            where_parts.append("d.metadata->>%s = %s")
            params.extend([key, value])
        where_clause = " AND ".join(where_parts)

        sql = f"""
            SELECT
                abs(hashtext(c.id::text)::bigint) AS chunk_id,
                COALESCE(c.contextualized, c.content) AS content,
                c.page_num,
                d.source_uri,
                d.source_type::text AS source_type,
                d.title,
                d.metadata->>'channel_name' AS channel_name,
                d.metadata->>'client_name' AS client_name,
                d.metadata->>'deal_phase' AS deal_phase,
                d.metadata->>'bant_score' AS bant_score,
                d.metadata->>'channel_type' AS channel_type
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where_clause}
            ORDER BY d.modified_at DESC NULLS LAST, c.chunk_idx ASC
            LIMIT %s
        """  # nosec B608
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            meta: dict[str, Any] = {
                "source_uri": r.get("source_uri"),
                "source_type": r.get("source_type"),
                "title": r.get("title"),
                "channel_name": r.get("channel_name"),
                "is_sales_fb": True,
            }
            if r.get("page_num") is not None:
                meta["page_num"] = r["page_num"]
            for k in ("client_name", "deal_phase", "bant_score", "channel_type"):
                if r.get(k):
                    meta[k] = r[k]
            hits.append(
                SearchHit(
                    chunk_id=int(r["chunk_id"]),
                    content=str(r["content"]),
                    score=1.0,
                    metadata=meta,
                )
            )

        logger.info(
            "pgvector_list_by_metadata",
            request_id=request_id,
            filters=metadata_filters,
            limit=limit,
            hit_count=len(hits),
        )
        return hits

    def search_drive_by_client_names(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        client_names: list[str],
        *,
        limit: int = 3,
        exclude_chunk_ids: list[int] | None = None,
        request_id: str | None = None,
    ) -> list[SearchHit]:
        """指定クライアント名にマッチする Drive ドキュメントを取得する。

        Day 8 (2026-05-28) Phase 2 で追加。Slack 営業 FB がヒットしたとき、
        その client_name で Drive 資料を裏で検索して「関連資料」として attach する用途。

        - 検索条件: source_type='gdrive' AND (title ILIKE OR content ILIKE) で client_name 部分一致
        - 重複: exclude_chunk_ids にメイン検索ヒット済の chunk_id を渡して除外
        - 順位: modified_at DESC (新しい資料優先)、なければ title の作成順
        - RLS: conn は事前に connection(app_role='teamagent_app', user_email=...) で設定済前提
        - score は 1.0 固定 (関連 attach 用途、ranking には使わない)
        """
        if not client_names:
            return []

        # NULL や空文字を除外、user 入力を直接 ILIKE に流すので escape
        clean_names = [n.strip() for n in client_names if n and n.strip()]
        if not clean_names:
            return []

        # 各 client_name について (title ILIKE %s OR content ILIKE %s) を OR で連結
        like_clauses: list[str] = []
        params: list[Any] = []
        for name in clean_names:
            like_pattern = f"%{name}%"
            like_clauses.append("(d.title ILIKE %s OR c.content ILIKE %s)")
            params.extend([like_pattern, like_pattern])

        where = "d.source_type = 'gdrive' AND (" + " OR ".join(like_clauses) + ")"

        # 重複除外用 (chunk_id は hashtext なので元 UUID に戻すのは困難 → content hash で除外)
        # 既存ヒットの chunk_id 完全一致は SearchSkill 側で post-filter する方が安全
        sql = f"""
            SELECT
                abs(hashtext(c.id::text)::bigint) AS chunk_id,
                COALESCE(c.contextualized, c.content) AS content,
                c.page_num,
                d.source_uri,
                d.source_type::text AS source_type,
                d.title,
                d.modified_at
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where} AND c.chunk_idx = 0
            ORDER BY d.modified_at DESC NULLS LAST, d.ingested_at DESC
            LIMIT %s
        """  # nosec B608
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        excluded = set(exclude_chunk_ids or [])
        hits: list[SearchHit] = []
        for r in rows:
            cid = int(r["chunk_id"])
            if cid in excluded:
                continue
            meta: dict[str, Any] = {
                "source_uri": r.get("source_uri"),
                "source_type": r.get("source_type"),
                "title": r.get("title"),
                "is_related_drive": True,  # SearchSkill / Slack Block Kit でこの flag を見て分類
            }
            if r.get("page_num") is not None:
                meta["page_num"] = r["page_num"]
            hits.append(
                SearchHit(
                    chunk_id=cid,
                    content=str(r["content"]),
                    score=1.0,  # ranking 用ではなく attach 用、固定値
                    metadata=meta,
                )
            )

        logger.info(
            "pgvector_search_drive_by_clients",
            request_id=request_id,
            client_names=clean_names,
            limit=limit,
            hit_count=len(hits),
        )
        return hits
