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

from teamagent.adapters.pg_pool import ConnectionPool, PoolStats

logger = structlog.get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    """env を int として読む（空・不正値は default）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """env を float として読む（空・不正値は default）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _connect_kwargs() -> dict[str, Any]:
    """psycopg.connect に渡す堅牢化 kwargs（接続/文/ロックのタイムアウト＋keepalive）。

    RDS 再起動・AZ障害時に、新規接続が OS の TCP タイムアウト（分単位）まで成立せず
    ワーカースレッドを占有し executor 枯渇→全機能ハングするのを防ぐ（connect_timeout）。
    さらに statement/lock/idle-in-tx のサーバ側タイムアウトで、ハングしたクエリが
    プールスロットを無期限占有してプール枯渇連鎖を起こすのを防ぐ。すべて env で上書き可能。
    """
    statement_ms = _env_int("PG_STATEMENT_TIMEOUT_MS", 30000)
    lock_ms = _env_int("PG_LOCK_TIMEOUT_MS", 5000)
    idle_tx_ms = _env_int("PG_IDLE_TX_TIMEOUT_MS", 30000)
    # 注意: DSN(DATABASE_URL)側に `options=` を付けると libpq 仕様で本 kwarg が上書きする
    # （連結しない）。search_path 等を将来注入するなら、この文字列側に合流させること。
    options = (
        f"-c statement_timeout={statement_ms} "
        f"-c lock_timeout={lock_ms} "
        f"-c idle_in_transaction_session_timeout={idle_tx_ms}"
    )
    return {
        "connect_timeout": _env_int("PG_CONNECT_TIMEOUT_S", 5),
        "options": options,
        # 死んだ接続を検知して落とす（NAT/RDS フェイルオーバー後のゾンビ接続対策）。
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


def _connect_pg(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    """物理接続ファクトリ。タイムアウト/keepalive 付き + dict_row + pgvector 型登録まで済ます。"""
    conn = psycopg.connect(dsn, row_factory=dict_row, **_connect_kwargs())
    try:
        register_vector(conn)
    except Exception:
        conn.close()  # 型登録に失敗したらソケットをリークさせず確実に閉じる
        raise
    return conn


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

    def __init__(
        self, dsn: str, *, pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] | None = None
    ) -> None:
        self.dsn = dsn
        # pool 指定時は connection() がプールから借用・返却（RESET ROLE で RLS リセット）。
        # None 時は従来どおり毎回 connect→close（テストや単発スクリプト向けの後方互換）。
        self._pool = pool

    @classmethod
    def from_env(cls) -> PgVectorClient:
        """環境変数 DATABASE_URL から接続情報を取得する（既定でコネクションプール有効）。

        ローカル: postgresql://teamagent:teamagent@localhost:5432/teamagent
        本番: 踏み台経由・Secrets Manager 経由で組み立てた URL を入れる

        プール設定（任意）:
        - ``PGVECTOR_POOL_MAX``: 総接続上限（既定 8）。``0`` で**プール無効**（直結・旧挙動）。
        - ``PGVECTOR_POOL_MIN``: 起動時ウォームアップ数（既定 0）。
        - ``PGVECTOR_POOL_TIMEOUT_S``: 空き接続の待ち上限秒（既定 10）。
        """
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL が未設定です。.env を読み込んでから起動してください")
        max_size = _env_int("PGVECTOR_POOL_MAX", 8)
        if max_size <= 0:
            return cls(dsn=dsn)  # プール無効（直結）
        pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            connect=lambda: _connect_pg(dsn),
            max_size=max_size,
            min_size=_env_int("PGVECTOR_POOL_MIN", 0),
            timeout=_env_float("PGVECTOR_POOL_TIMEOUT_S", 10.0),
        )
        return cls(dsn=dsn, pool=pool)

    def close(self) -> None:
        """プールを閉じる（保有接続を解放）。直結モードでは no-op。シャットダウン時に呼ぶ。"""
        if self._pool is not None:
            self._pool.close()

    def pool_stats(self) -> PoolStats | None:
        """接続プールの観測値（PoolStats）を返す。直結モード（プール無効）では None。

        管理画面の runtime_metrics 定期スナップショット用。in_use/idle/timeouts 等を読む。
        """
        if self._pool is None:
            return None
        return self._pool.stats()

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

        終了時に必ず close（プール時は RESET ROLE で reset し返却）する。例外時もロールバック。

        ⚠️ これは **同期・ブロッキング API**（プール取得は ``threading.Semaphore.acquire`` で
        最大 ``PGVECTOR_POOL_TIMEOUT_S`` 秒ブロックし得る）。**async 文脈から直接呼ばない**こと
        （イベントループが固まり Slack bot 全体が無応答になる）。Skill は必ず
        ``loop.run_in_executor(...)`` 等でワーカースレッド上で実行し、その中で本 API を使う。
        完全 async 化する場合は psycopg_pool.AsyncConnectionPool を検討。
        """
        if self._pool is not None:
            # --- プール経路: 借用→設定→commit/rollback→返却（RESET ROLE は pool 側） ---
            with self._pool.connection() as conn:
                try:
                    self._apply_session(conn, app_role, user_email, user_groups, user_role)
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return

        # --- 直結経路（プール無効・後方互換）: 毎回 connect→close ---
        # _connect_pg でタイムアウト/keepalive と pgvector 型登録を適用する（プール経路と同条件）。
        conn = _connect_pg(self.dsn)
        try:
            self._apply_session(conn, app_role, user_email, user_groups, user_role)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _apply_session(
        conn: psycopg.Connection[dict[str, Any]],
        app_role: str | None,
        user_email: str | None,
        user_groups: list[str] | None,
        user_role: str | None,
    ) -> None:
        """RLS 用のロール/GUC を接続に設定する（プール経路・直結経路で共通）。

        - ``SET ROLE`` は session 持続（プールでは返却時に RESET ROLE で必ず戻す）。
          psycopg は識別子の parameterize 不可なので app_role はコード内固定の英数/_ のみ許可。
        - ``app.user_*`` GUC は ``set_config(name, value, is_local=true)`` で transaction-local。
        - QW-5: ``hnsw.ef_search`` も ``set_config(..., is_local=true)`` で transaction-local に
          注入する（当該トランザクションの全クエリ＝multi-query RRF の各サブクエリにも効く）。
          LIMIT30＋HNSW 後に RLS+cls_*+boilerplate+duplicate の多段 post-filter が AND で候補を
          枯らし上位可視 chunk を取りこぼす「post-filter recall 崖」を緩和する。
          env ``SEARCH_HNSW_EF_SEARCH`` が未設定（or 0 以下）のときは **発行しない**＝DB 既定
          （HNSW ef_search=40）のまま＝完全後方互換。本番は 100 を設定して候補を広げる。
        """
        ef_search = _env_int("SEARCH_HNSW_EF_SEARCH", 0)
        with conn.cursor() as cur:
            if app_role:
                if not app_role.replace("_", "").isalnum():
                    raise ValueError(f"invalid app_role: {app_role!r}")
                cur.execute(f"SET ROLE {app_role}")  # nosec B608
            if user_role:
                cur.execute("SELECT set_config('app.user_role', %s, true)", (user_role,))
            if user_email:
                cur.execute("SELECT set_config('app.user_email', %s, true)", (user_email,))
            if user_groups:
                cur.execute(
                    "SELECT set_config('app.user_groups', %s, true)",
                    (",".join(user_groups),),
                )
            if ef_search > 0:
                # set_config の value は文字列。is_local=true で transaction 終了時に自動破棄。
                cur.execute(
                    "SELECT set_config('hnsw.ef_search', %s, true)",
                    (str(ef_search),),
                )

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
        sticky_filters: dict[str, str] | None = None,
        metadata_contains: dict[str, str] | None = None,
        exclude_boilerplate: bool = False,
        exclude_duplicates: bool = False,
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

        sticky_filters:
            metadata_filters と同じ ``d.metadata->>%s = %s`` 等価 AND だが、**呼び側
            (_pool_search) が fail-open / exclusion_rescue の再検索でも必ず再注入する**点
            だけが約束として違う（adapter 自身は普通に AND するだけ）。ユーザーが明示指定した
            予算バンド（cls_budget）など「0 件は 0 件で返す」べきフィルタを載せる。
            特別キー ``__budget_or_unknown__`` のときだけ soft 化し、value を band として
            ``(cls_budget = %s OR cls_budget = '不明')`` の OR 句に展開する（予算「不明も
            含める」用。等価1値では OR を表現できないため専用扱い）。
            key / value は placeholder 化されるため SQL injection から保護される。

        metadata_contains:
            部分一致（ILIKE）フィルタ。value の LIKE メタ文字（パーセント / アンダースコア /
            バックスラッシュ）は bind 前にエスケープし ``ESCAPE`` 句を付ける。
            特別キー ``__client__`` は
            ``cls_project``（全資料に付く取引先）/ ``client_name``（FB）/ ``d.title`` の OR
            グループへ展開する（client_name 単独だと NULL 行を暗黙除外し FB 投稿だけに痩せる
            二次被害を避けるため）。それ以外のキーは ``d.metadata->>%s ILIKE %s`` 単独。
            pattern / key とも placeholder 化されるため SQL injection から保護される。

        exclude_boilerplate:
            True のとき WHERE に
            ``AND COALESCE((c.metadata->>'boilerplate')::bool, false) = false`` を足し、
            テンプレ判定された chunk（表紙/会社紹介など複数資料に共通する定型）を
            検索対象から外す。既定 False = 句を一切足さず現行 SQL と完全一致。
            フラグが無ければ COALESCE(...,false)=false が全 chunk で真になり影響しない。

        exclude_duplicates:
            True のとき WHERE に **「非正本でも、その正本が現 RLS 接続で不可視なら残す」**
            条件を足す（H3 修正）。dedup は admin で全テナント横断クラスタ化し
            content_len 最大を正本にするため、正本が狭 ACL（個人共有）だと、会社共有版が
            suppressed=true で WHERE 除外され、かつ正本は RLS で見えず→クラスタごと検索消失
            し得る。これを防ぐため、単純な ``suppressed IS DISTINCT FROM 'true'`` ではなく
            ``AND NOT (suppressed=true AND EXISTS(正本 dc が現 conn で可視))`` を足す：
            正本が**この RLS conn で見えるときだけ**非正本を除外し、見えなければ
            非正本（会社共有版）を救済して残す。EXISTS は RLS 適用 conn 上の
            ``documents`` を引くので、正本が不可視なら EXISTS 偽→除外しない側に倒れる。
            ``duplicate_of`` が無効 UUID（キャスト失敗）でも EXISTS 偽になるよう、
            uuid 形式チェック（``~`` 正規表現）でガードしてからキャストする（除外しない側）。
            既定 False = 句を一切足さず現行 SQL と完全一致。何も suppressed されて
            いなければ NOT(...) 全体が真になり無影響（後方互換）。exclude_boilerplate と
            AND 併用可。
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

        # sticky（ユーザー明示・等価）— metadata_filters と同じ AND だが、呼び側
        # _pool_search が fail-open / exclusion_rescue 再検索でも必ず再注入する点だけが違う。
        # 特別キー __budget_or_unknown__ は soft 化（cls_budget=値 OR cls_budget='不明'）。
        # 等価1値では OR を表現できないため、予算「不明も含める」だけ専用に OR 句へ展開する。
        if sticky_filters:
            for key, value in sticky_filters.items():
                if key == "__budget_or_unknown__":
                    where_parts.append(
                        "(d.metadata->>'cls_budget' = %s OR d.metadata->>'cls_budget' = '不明')"
                    )
                    params.append(value)
                else:
                    where_parts.append("d.metadata->>%s = %s")
                    params.extend([key, value])

        # metadata_contains（部分一致 ILIKE・__client__ は cls_project/client_name/title の OR）。
        if metadata_contains:
            for key, value in metadata_contains.items():
                # LIKE メタ文字をエスケープしてから %wrap（injection は placeholder で別途防御）。
                safe = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pat = f"%{safe}%"
                if key == "__client__":
                    # 取引先は cls_project（全資料）/ client_name（FB）/ title の OR グループ。
                    where_parts.append(
                        "(d.metadata->>'cls_project' ILIKE %s ESCAPE '\\' "
                        "OR d.metadata->>'client_name' ILIKE %s ESCAPE '\\' "
                        "OR d.title ILIKE %s ESCAPE '\\')"
                    )
                    params.extend([pat, pat, pat])
                else:
                    where_parts.append("d.metadata->>%s ILIKE %s ESCAPE '\\'")
                    params.extend([key, pat])

        if exclude_boilerplate:
            # テンプレ chunk を検索対象から除外（フラグ無し chunk は影響しない）。
            where_parts.append("COALESCE((c.metadata->>'boilerplate')::bool, false) = false")

        if exclude_duplicates:
            # H3: 「非正本（suppressed=true）かつ、その正本（duplicate_of）が現 RLS 接続で
            # 可視のときだけ」除外する。正本が現 conn で不可視（狭 ACL の個人共有など）なら
            # EXISTS が偽になり、会社共有版（非正本）をクラスタごと検索消失させず救済する。
            # EXISTS は RLS 適用 conn 上の documents を引くため、正本の可視性は接続の
            # ロール/ユーザ GUC で自然に評価される。duplicate_of が無効 UUID でも
            # ``~`` の uuid 形式チェックで弾き、キャスト例外を出さず除外しない側に倒す。
            # suppressed が無い doc は NOT(false AND ...) = true で常に残る（後方互換）。
            where_parts.append(
                "NOT ("
                "COALESCE((d.metadata->>'suppressed')::bool, false) "
                "AND d.metadata->>'duplicate_of' ~ "
                "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' "
                "AND EXISTS ("
                "SELECT 1 FROM documents dc "
                "WHERE dc.id = (d.metadata->>'duplicate_of')::uuid"
                ")"
                ")"
            )

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
                d.id AS document_id,
                d.source_uri,
                d.source_type::text AS source_type,
                d.title,
                d.metadata->>'channel_name' AS channel_name,
                d.metadata->>'is_sales_fb' AS is_sales_fb,
                d.metadata->>'client_name' AS client_name,
                d.metadata->>'deal_phase' AS deal_phase,
                d.metadata->>'bant_score' AS bant_score,
                d.metadata->>'channel_type' AS channel_type,
                d.metadata->>'cls_project' AS cls_project,
                d.metadata->>'cls_industry' AS cls_industry,
                d.metadata->>'cls_doc_type' AS cls_doc_type,
                d.metadata->>'cls_phase' AS cls_phase,
                d.metadata->>'cls_solution' AS cls_solution,
                d.metadata->>'cls_budget' AS cls_budget,
                d.metadata->>'cls_target' AS cls_target
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
            # L1: document_id（資料単位の安定キー）を metadata に詰める。
            # dedup.cap_per_document が source_uri フォールバックに頼らず正確に
            # 同一資料を畳めるようにする。UUID は str 化して JSON 親和にする。
            if r.get("document_id") is not None:
                meta["document_id"] = str(r["document_id"])
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
            # ナレッジ自動分類タグ（ingest.classify が付与・案件/業界/種別/フェーズ、
            # および第2世代の解決策/予算/ターゲット軸）。
            # is_sales_fb と独立に常に拾う（Drive 資料は FB ではないが分類対象）。
            for cls_key in (
                "cls_project",
                "cls_industry",
                "cls_doc_type",
                "cls_phase",
                "cls_solution",
                "cls_budget",
                "cls_target",
            ):
                if r.get(cls_key):
                    meta[cls_key] = r[cls_key]
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

    def list_documents_for_graph(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        *,
        limit: int = 600,
        request_id: str | None = None,
        with_embeddings: bool = False,
    ) -> list[dict[str, Any]]:
        """グラフ表示用に documents を新しい順で列挙する（RLS 適用済 conn 前提）。

        ノード=資料・エッジ=共有タグの Obsidian 風グラフを
        ``connect_web.graph.build_graph`` で組むためのフィールドを返す。
        excerpt はホバープレビュー用に先頭 chunk の本文を LATERAL で 1 つだけ拾う
        （chunks の RLS は documents 連動なので本人可視分のみ）。RLS は
        ``connection(app_role='teamagent_app', user_email=...)`` で有効化済の前提なので、
        本人 ACL（個人 + 会社共有）に見えるドキュメントのみが返る。
        列名・テーブル名は固定リテラル、limit は placeholder bind（bandit B608 安全）。

        分類タグは旧軸（industry/project/doc_type）に加えて第2世代の
        ``cls_solution`` / ``cls_budget`` / ``cls_target`` も射影する（行 dict に乗る）。

        with_embeddings=True のときは、各 doc の**代表埋め込みベクトル**（= その資料の
        全チャンク embedding の平均 ``AVG(c.embedding)``）を ``embedding`` として float の
        list で行に乗せる。これは ``connect_web.graph.concept_edges``（意味クラスタ・エッジ）
        用で重いので、グラフ route が明示要求したときだけ取得する（既定 False = 旧挙動・形は不変）。
        chunks の RLS は documents 連動なので、本人可視分のチャンクのみが平均に入る。

        ※先頭チャンク（chunk_idx=0）は営業資料では表紙＝テンプレなので代表に使わない。
        全チャンクの平均にすることで共通テンプレ部分が施策チャンクで希釈され、「表紙が同じ
        デッキ同士」を意味的に近いと誤判定する（＝新種のハリネズミ）のを防ぐ。pgvector 0.5.0+
        は ``avg(vector)`` 集約を提供する（本番 RDS は pgvector 0.8.2）。

        重複排除: ``d.metadata->>'suppressed' IS DISTINCT FROM 'true'`` を**無条件**で
        WHERE に置き、dedup で非正本（隠す方）と印された document をグラフのノードに
        出さない。何も suppressed されていなければ全 doc で真になり no-op（後方互換）。
        """
        # 代表ベクトル = 全チャンク embedding の平均（テンプレ希釈・資料全体の意味）。
        embedding_select = (
            ",\n                emb.embedding AS embedding" if with_embeddings else ""
        )
        # テンプレ chunk（metadata.boilerplate=true）を平均から除外し、表紙/会社紹介
        # などの共通テンプレで concept edges がつながるのを防ぐ。フラグが無ければ
        # COALESCE(...,false)=false が全 chunk で真になり旧挙動と同一（後方互換）。
        embedding_join = (
            """
            LEFT JOIN LATERAL (
                SELECT AVG(c.embedding) AS embedding
                FROM chunks c
                WHERE c.document_id = d.id
                  AND COALESCE((c.metadata->>'boilerplate')::bool, false) = false
            ) emb ON true"""
            if with_embeddings
            else ""
        )
        sql = f"""
            SELECT
                abs(hashtext(d.id::text)::bigint) AS node_id,
                d.title,
                d.source_uri,
                d.source_type::text AS source_type,
                d.metadata->>'cls_industry' AS cls_industry,
                d.metadata->>'cls_project' AS cls_project,
                d.metadata->>'cls_doc_type' AS cls_doc_type,
                d.metadata->>'cls_solution' AS cls_solution,
                d.metadata->>'cls_budget' AS cls_budget,
                d.metadata->>'cls_target' AS cls_target,
                d.metadata->>'client_name' AS client_name,
                ex.excerpt AS excerpt{embedding_select}
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT left(COALESCE(c.contextualized, c.content), 160) AS excerpt
                FROM chunks c
                WHERE c.document_id = d.id
                ORDER BY c.chunk_idx ASC
                LIMIT 1
            ) ex ON true{embedding_join}
            WHERE d.metadata->>'suppressed' IS DISTINCT FROM 'true'
            ORDER BY d.modified_at DESC NULLS LAST
            LIMIT %s
        """  # nosec B608
        with conn.cursor() as cur:
            cur.execute(sql, [limit])
            rows = cur.fetchall()
        docs: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if with_embeddings and d.get("embedding") is not None:
                # pgvector の Vector / numpy 配列を素の float list に正規化する
                # （build_graph / concept_edges は純 Python の list を期待する）。
                d["embedding"] = [float(x) for x in d["embedding"]]
            docs.append(d)
        logger.info(
            "pgvector_list_documents_for_graph",
            request_id=request_id,
            doc_count=len(docs),
            limit=limit,
            with_embeddings=with_embeddings,
        )
        return docs

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
            # 完全一致でなく部分一致 (LIKE)。bant_score は "C（検討）" のように
            # 評価記号 + 注釈で格納され、複数評価 "B（前向き）, C（検討）" もあるため、
            # 「C」で "C（検討）" を拾えるようにする。channel_type は完全値だが部分一致でも安全。
            where_parts.append("d.metadata->>%s LIKE %s")
            params.extend([key, f"%{value}%"])
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

    def list_client_names(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        request_id: str | None = None,
        *,
        limit: int = 1000,
    ) -> list[str]:
        """既知のクライアント名（distinct）を返す（read-only）。

        クエリ中の固有名詞（例「ユニーの2回目提案」）を既知クライアント名の語彙へ
        substring 照合し、client_name で絞った検索を追加する「クライアント名ブースト」
        （SearchSkill use_client_boost）の語彙に使う。RLS は connection() 側で有効化済の前提。
        """
        sql = """
            SELECT DISTINCT d.metadata->>'client_name' AS client_name
            FROM documents d
            WHERE d.metadata->>'client_name' IS NOT NULL
              AND d.metadata->>'client_name' <> ''
            LIMIT %s
        """  # nosec B608
        with conn.cursor() as cur:
            cur.execute(sql, [limit])
            rows = cur.fetchall()
        return [str(r["client_name"]) for r in rows if r.get("client_name")]

    def list_client_timeline(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        client_name: str,
        limit: int = 20,
        request_id: str | None = None,
    ) -> list[SearchHit]:
        """指定クライアントの営業 FB を時系列 (古い順) に束ねる (ClientKarte 用)。

        client_name 部分一致 (「日本ガイシ」で「NGK（日本ガイシ）」も拾う) の営業 FB を
        modified_at 昇順で返す。各 FB の構造化メタ (deal_phase / bant_score /
        positive_reaction / negative_reaction / next_action / proposed_menu) を
        metadata に詰めて返し、Skill 側で温度感推移の合成に使う。

        client_name は placeholder 化され SQL injection から保護される。
        """
        if not client_name.strip():
            return []

        sql = """
            SELECT
                abs(hashtext(c.id::text)::bigint) AS chunk_id,
                COALESCE(c.contextualized, c.content) AS content,
                to_char(d.modified_at, 'YYYY-MM-DD') AS occurred_at,
                d.source_uri,
                d.title,
                d.metadata->>'client_name' AS client_name,
                d.metadata->>'deal_phase' AS deal_phase,
                d.metadata->>'bant_score' AS bant_score,
                d.metadata->>'channel_type' AS channel_type,
                d.metadata->>'positive_reaction' AS positive_reaction,
                d.metadata->>'negative_reaction' AS negative_reaction,
                d.metadata->>'next_action' AS next_action,
                d.metadata->>'proposed_menu' AS proposed_menu
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.metadata->>'is_sales_fb' = 'true'
              AND d.metadata->>'client_name' LIKE %s
            ORDER BY d.modified_at ASC NULLS LAST, c.chunk_idx ASC
            LIMIT %s
        """  # nosec B608
        params: list[Any] = [f"%{client_name}%", limit]

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        hits: list[SearchHit] = []
        for r in rows:
            meta: dict[str, Any] = {
                "source_uri": r.get("source_uri"),
                "source_type": "slack",
                "title": r.get("title"),
                "occurred_at": r.get("occurred_at"),
                "is_sales_fb": True,
            }
            for k in (
                "client_name",
                "deal_phase",
                "bant_score",
                "channel_type",
                "positive_reaction",
                "negative_reaction",
                "next_action",
                "proposed_menu",
            ):
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
            "pgvector_list_client_timeline",
            request_id=request_id,
            client_name=client_name,
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
