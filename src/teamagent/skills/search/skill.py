"""検索 Skill 本体。

Sprint 1 時点の最小実装：
1. 入力クエリを embedding 化する想定の枠（実装は別タスクで差し替え）
2. pgvector で類似 chunk を取得
3. Bedrock に system prompt + chunks + query を渡して要約

CLAUDE.md 6-bis ルール準拠：
- 3層分離：本ファイルは Skill 層のみ。boto3 / psycopg は呼ばず adapters/ 経由
- Pydantic v2 で I/O 固定
- 構造化ログ：request_id をすべてのログに付与
- prompt はファイルから読み込み（コード内ハードコード禁止）
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.embeddings_client import Embedder
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.search.aggregation import extract_aggregation_filter
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

logger = structlog.get_logger(__name__)


@register
class SearchSkill(BaseSkill[SearchInput, SearchOutput]):
    """過去資料を pgvector で検索 → Claude で要約する Skill。"""

    name: ClassVar[str] = "search"
    description: ClassVar[str] = "営業16名が過去の提案書・議事録・メールを自然文クエリで検索する"
    input_schema: ClassVar[type[BaseModel]] = SearchInput
    output_schema: ClassVar[type[BaseModel]] = SearchOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        pgvector: PgVectorClient | None = None,
        embedder: Embedder | None = None,
        target_table: str = "proposals_chunks",
        *,
        content_col: str = "text",
        metadata_col: str | None = None,
        extra_cols: list[str] | None = None,
        use_contextual: bool = False,
        use_new_schema: bool = False,
        use_fb_drive_match: bool = False,
        fb_drive_match_limit: int = 3,
        use_cohere_rerank: bool = False,
        rerank_pool_size: int = 30,
        min_relevance: float = 0.0,
        use_aggregation_mode: bool = False,
        prompt_version: str = "v1",
        summary_max_tokens: int = 4096,
        app_role: str | None = "teamagent_app",
    ) -> None:
        """Adapter は外から注入する（テストでモック差し替え可能にするため）。

        デフォルトはローカル demo のスキーマ（proposals_chunks: text, no metadata）。
        本番 RDS で proposal_chunks(content, metadata JSONB) を使う際は引数で上書き。

        use_contextual=True を指定すると proposals_chunks_contextual テーブルを使い、
        Anthropic Contextual Retrieval（前置詞付き chunk + 再 embedding）で検索する。
        scripts/contextual_retrieval.py で事前にテーブルを作成しておく必要がある。

        use_new_schema=True を指定すると migration 0001 の documents + chunks JOIN を使う。
        Slack 197 件 + 将来の Drive/Gmail 全件を横断検索できるようになる。
        USE_NEW_SCHEMA=true 環境変数でも切替可能（runtime/slack_bot.py で制御）。

        app_role: Postgres ロール切替（migration 0002 で導入の `teamagent_app`）。
          - 本番では必ず `teamagent_app` を渡し、SET ROLE で RLS を効かせる
          - ローカル開発で `teamagent_app` が未作成の環境では None を渡す（旧挙動）
          - 既存 proposals_chunks 系は RLS 未適用テーブルだが、teamagent_app role に
            migration 0002 で GRANT 済なので問題なくアクセス可能
        """
        self._bedrock = bedrock or BedrockClient.from_env()
        self._pgvector = pgvector or PgVectorClient.from_env()
        self._embedder = embedder
        self._use_new_schema = use_new_schema
        if use_contextual:
            # Contextual Retrieval テーブルを優先（明示指定がなければ）
            self._target_table = (
                target_table
                if target_table != "proposals_chunks"
                else "proposals_chunks_contextual"
            )
            self._content_col = content_col if content_col != "text" else "contextualized_text"
        else:
            self._target_table = target_table
            self._content_col = content_col
        self._metadata_col = metadata_col
        self._extra_cols = list(extra_cols or ["file_name", "page_num", "drive_url"])
        self._app_role = app_role
        # Day 8 (2026-05-28) Phase 2: Slack 営業 FB がヒットしたとき、その client_name で
        # Drive 資料を裏で検索して「関連資料」として attach する機能。
        # USE_FB_DRIVE_MATCH=true 環境変数で gating (デフォルト OFF、段階ロールアウト用)。
        self._use_fb_drive_match = use_fb_drive_match
        self._fb_drive_match_limit = fb_drive_match_limit
        # Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 (Bedrock 東京)。
        # USE_COHERE_RERANK=true で有効化。top_k=rerank_pool_size 取得 → Rerank → top_k 絞り込み。
        # Anthropic 公式ベンチで dense retrieval の失敗率 5.7% → 1.9% (-67%) を実現する中核機能。
        self._use_cohere_rerank = use_cohere_rerank
        self._rerank_pool_size = rerank_pool_size
        # Sprint 5: 反ハルシネーション閾値。Rerank relevance がこの値未満の hit は
        # 「根拠として弱い」とみなし落とす。全 hit が落ちれば 0 件 = Bot は
        # 「資料に記載がありません」と返し、無い情報を捏造しない。
        # SEARCH_MIN_RELEVANCE env で制御 (既定 0.0 = OFF)。Rerank score (0-1) 前提。
        # gold set 実測: 実ヒット最低 0.50 / expect_zero 最高 0.23 → 0.4 で綺麗に分離。
        self._min_relevance = min_relevance
        # Sprint 5: 集約・一覧クエリモード。「BANT A の案件一覧」等を検出したら
        # 意味検索ではなくメタデータフィルタ列挙 (list_by_metadata) で答える。
        # USE_AGGREGATION_MODE=true で有効化 (既定 OFF)。new_schema 前提。
        self._use_aggregation_mode = use_aggregation_mode
        # Day 8 (2026-05-28) Sprint 4-B: prompt v2 (insight + actionable thinking)。
        # 「過去のチャンク要約」から「パターン抽出 + 推奨アクション」に役割を進化させる。
        # ユーザー指摘 (Day 8): "あたりまえの過去事例リサーチは不要、リサーチ＆改善の思考が欲しい"
        # PROMPT_VERSION 環境変数 (v1 / v2 / v2c) で切替可能。
        # v2c は v2 の compact 版 (104→41行)、output token 削減でレイテンシ 46s→目標 20s 以下。
        self._prompt_version = prompt_version
        # Day 8 Sprint 4-D: Bedrock Converse の max_tokens 制限。
        # SEARCH_MAX_TOKENS env で runtime 制御、v2c と組合せて latency を半減狙い。
        self._summary_max_tokens = summary_max_tokens

    def retrieve_hits(
        self,
        query: str,
        ctx: SkillContext,
        *,
        top_k: int = 5,
        filter_industry: str | None = None,
        strict_industry: bool = False,
    ) -> list[SearchHit]:
        """要約せず検索ヒットだけを返す公開メソッド (他 Skill からの再利用口)。

        embed → _retrieve のラッパ。新スキーマ + Rerank + min_relevance + 集約モード等、
        run() と同じ retrieval パイプライン (gold set top-1 88% 構成) をそのまま通す。
        ProposalDraftSkill 等が「類似過去提案の取得」に再利用する。
        """
        embedding = self._embed(query)
        retrieval_input = SearchInput(
            query=query,
            top_k=top_k,
            filter_industry=filter_industry,
            strict_industry=strict_industry,
        )
        return self._retrieve(embedding, retrieval_input, ctx)

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        """検索 Skill のメインフロー。"""
        log = ctx.bind_logger(self.name)
        log.info(
            "search_skill_start",
            query_len=len(input.query),
            top_k=input.top_k,
            filter_industry=input.filter_industry,
        )

        # 1. クエリを embedding 化
        embedding = self._embed(input.query)

        # 2. pgvector で類似 chunk を取得（RLS 評価用 user_email を ctx から取得）
        hits = self._retrieve(embedding, input, ctx)

        # 3. Bedrock で要約（chunk が 0 件のときはスキップ）
        answer, cost_usd = self._summarize(input.query, hits, ctx.request_id)

        # 4. 出力スキーマに整形
        output = SearchOutput(
            answer=answer,
            hits=[
                SearchHitOut(
                    chunk_id=h.chunk_id,
                    content=h.content,
                    score=h.score,
                    source=self._build_source(h),
                    file_name=(
                        str(h.metadata.get("file_name")) if h.metadata.get("file_name") else None
                    ),
                    page_num=self._safe_int(h.metadata.get("page_num")),
                    drive_url=(
                        str(h.metadata.get("drive_url")) if h.metadata.get("drive_url") else None
                    ),
                    source_uri=(
                        str(h.metadata["source_uri"]) if h.metadata.get("source_uri") else None
                    ),
                    source_type=(
                        str(h.metadata["source_type"]) if h.metadata.get("source_type") else None
                    ),
                    channel_name=(
                        str(h.metadata["channel_name"]) if h.metadata.get("channel_name") else None
                    ),
                    client_name=(
                        str(h.metadata["client_name"]) if h.metadata.get("client_name") else None
                    ),
                    deal_phase=(
                        str(h.metadata["deal_phase"]) if h.metadata.get("deal_phase") else None
                    ),
                    bant_score=(
                        str(h.metadata["bant_score"]) if h.metadata.get("bant_score") else None
                    ),
                    channel_type=(
                        str(h.metadata["channel_type"]) if h.metadata.get("channel_type") else None
                    ),
                )
                for h in hits
            ],
            total_cost_usd=cost_usd,
        )
        log.info(
            "search_skill_done",
            hit_count=len(hits),
            cost_usd=cost_usd,
            top_score=hits[0].score if hits else None,
        )
        return output

    def _embed(self, text: str) -> list[float]:
        """クエリを埋め込みベクトルに変換する。

        Embedder は外から注入（LocalE5Embedder / BedrockTitanEmbedder など）。
        """
        if self._embedder is None:
            raise NotImplementedError(
                "embedder が注入されていません。"
                "LocalE5Embedder または BedrockTitanEmbedder を __init__ で渡してください"
            )
        return self._embedder.embed(text)

    def _retrieve(
        self, embedding: list[float], input: SearchInput, ctx: SkillContext
    ) -> list[SearchHit]:
        """pgvector で類似検索する。

        ctx.metadata から RLS 評価用の user_email / user_groups / user_role を取得し、
        PgVectorClient.connection() に渡す。runtime/slack_bot.py の SkillDispatcher が
        Slack user_id → email 解決を行ってから metadata に詰めるのが理想形。
        現状は metadata 未設定でも動く（その場合は teamagent_app + user_email 未注入
        = RLS で何も見えない fail-safe、ただし proposals_chunks 系は RLS 未適用なので
        通常通り見える）。

        use_new_schema=True の場合は search_similar_new_schema() を使い、
        documents + chunks JOIN で横断検索する（Slack 197 件等が対象）。
        """
        # ctx.metadata から RLS GUC 用の値を取得（runtime 層が注入することを想定）
        user_email = ctx.metadata.get("user_email")
        user_groups_raw = ctx.metadata.get("user_groups")
        user_groups = list(user_groups_raw) if isinstance(user_groups_raw, (list, tuple)) else None
        user_role = ctx.metadata.get("user_role")

        with self._pgvector.connection(
            app_role=self._app_role,
            user_email=user_email,
            user_groups=user_groups,
            user_role=user_role,
        ) as conn:
            if self._use_new_schema:
                # Sprint 5: 集約・一覧クエリモード。「BANT A の案件一覧」「失注案件」等は
                # 意味検索では答えられないため、メタデータフィルタで FB を列挙する。
                # フィルタが取れたときだけ列挙経路に入り、取れなければ通常の意味検索へ。
                if self._use_aggregation_mode:
                    agg_filters = extract_aggregation_filter(input.query)
                    if agg_filters:
                        agg_hits = self._pgvector.list_by_metadata(
                            conn=conn,
                            metadata_filters=agg_filters,
                            limit=input.top_k,
                            request_id=ctx.request_id,
                        )
                        if agg_hits:
                            return agg_hits
                # Day 8 Sprint 4-A: Cohere Rerank 有効時は pool_size まで広く retrieve
                # → Rerank で top_k に絞る (dense retrieval の固有名詞弱点を補強)
                retrieve_limit = (
                    max(input.top_k, self._rerank_pool_size)
                    if self._use_cohere_rerank
                    else input.top_k
                )
                hits = self._pgvector.search_similar_new_schema(
                    conn=conn,
                    embedding=embedding,
                    limit=retrieve_limit,
                    filter_industry=input.filter_industry,
                    request_id=ctx.request_id,
                    strict_industry=input.strict_industry,
                )
                # Rerank: top_k に絞り直す (relevance_score で再ソート)
                if self._use_cohere_rerank and hits:
                    hits = self._apply_cohere_rerank(
                        query=input.query,
                        hits=hits,
                        top_k=input.top_k,
                        request_id=ctx.request_id,
                    )
                # Sprint 5: 反ハルシネーション閾値。relevance < 閾値の hit を落とす。
                # Rerank 後の relevance score に対して適用 (drive-match の固定 score=1.0
                # より前に評価し、弱い根拠しか無いクエリでは drive-match も発火させない)。
                if self._min_relevance > 0.0 and hits:
                    kept = [h for h in hits if h.score >= self._min_relevance]
                    if len(kept) != len(hits):
                        logger.info(
                            "min_relevance_filter",
                            request_id=ctx.request_id,
                            min_relevance=self._min_relevance,
                            before=len(hits),
                            after=len(kept),
                            top_score=hits[0].score,
                        )
                    hits = kept
                # Day 8 Phase 2: FB hits があれば client_name で Drive 資料を追加 retrieve
                if self._use_fb_drive_match and hits:
                    related = self._fetch_related_drive_hits(
                        conn=conn,
                        primary_hits=hits,
                        request_id=ctx.request_id,
                    )
                    if related:
                        hits = list(hits) + related
                return hits
            # メタデータ JSONB のフィルタ。値は adapter 側で placeholder にバインド
            # されるため SQL injection から保護される。metadata 列を持つテーブルで
            # のみ有効（adapter 側で metadata_col is None なら無視される fail-safe）。
            metadata_filters: dict[str, str] | None = None
            if input.filter_industry and self._metadata_col is not None:
                metadata_filters = {"industry": input.filter_industry}
            return self._pgvector.search_similar(
                conn=conn,
                embedding=embedding,
                table=self._target_table,
                limit=input.top_k,
                metadata_filters=metadata_filters,
                content_col=self._content_col,
                metadata_col=self._metadata_col,
                extra_cols=self._extra_cols,
            )

    def _apply_cohere_rerank(
        self,
        query: str,
        hits: list[SearchHit],
        top_k: int,
        request_id: str,
    ) -> list[SearchHit]:
        """Cohere Rerank v3.5 で hits を relevance score 順に並べ替え、top_k に絞る。

        Day 8 (2026-05-28) Sprint 4-A の中核処理。
        - 入力: pgvector top_pool_size hits (dense retrieval 結果)
        - 処理: Bedrock Rerank API で query との関連性を再評価
        - 出力: relevance_score 降順 top_k 件 (元 hits.score は Rerank score で上書き)

        失敗時はオリジナル hits を top_k で truncate して返す (fail-safe, 副作用最小化)。
        """
        try:
            documents = [h.content for h in hits]
            response = self._bedrock.rerank(
                query=query,
                documents=documents,
                request_id=request_id,
                top_n=top_k,
            )
        except Exception:
            # Rerank 失敗は致命傷ではない: dense retrieval 結果をそのまま使う
            logger.exception("cohere_rerank_failed_falling_back_to_dense", request_id=request_id)
            return hits[:top_k]

        # Rerank 結果に従って元 hits を並べ替え + score を Rerank score で更新
        reranked: list[SearchHit] = []
        for r in response.results:
            if r.index < 0 or r.index >= len(hits):
                continue
            original = hits[r.index]
            reranked.append(
                SearchHit(
                    chunk_id=original.chunk_id,
                    content=original.content,
                    score=r.relevance_score,  # dense score → rerank relevance score
                    metadata={**original.metadata, "dense_score": original.score},
                )
            )
        return reranked

    def _fetch_related_drive_hits(
        self,
        conn: Any,
        primary_hits: list[SearchHit],
        request_id: str,
    ) -> list[SearchHit]:
        """Slack 営業 FB がヒットしたら、その client_name で Drive 資料を追加取得する。

        Day 8 (2026-05-28) Phase 2 の中核処理。
        - primary_hits のうち metadata.is_sales_fb=True のものから client_name を集める
        - 同じ client_name の Drive doc を最大 _fb_drive_match_limit 件追加検索
        - 既存 chunk_id と重複したら除外
        - 戻り値の metadata.is_related_drive=True で「関連資料」マーカー付与

        副作用なし: FB がなければ空 list を返す。
        """
        client_names: list[str] = []
        seen_names: set[str] = set()
        for h in primary_hits:
            meta = h.metadata or {}
            if not meta.get("is_sales_fb"):
                continue
            name = meta.get("client_name")
            if not isinstance(name, str) or not name.strip():
                continue
            cleaned = name.strip()
            if cleaned not in seen_names:
                seen_names.add(cleaned)
                client_names.append(cleaned)

        if not client_names:
            return []

        primary_chunk_ids = [h.chunk_id for h in primary_hits]
        related = self._pgvector.search_drive_by_client_names(
            conn=conn,
            client_names=client_names,
            limit=self._fb_drive_match_limit,
            exclude_chunk_ids=primary_chunk_ids,
            request_id=request_id,
        )
        return related

    def _summarize(
        self,
        query: str,
        hits: list[SearchHit],
        request_id: str,
    ) -> tuple[str, float]:
        """Bedrock に system prompt + chunks + query を渡して要約させる。

        Day 8 Phase 2: is_related_drive=True の hits は「関連 Drive 資料」セクションに
        分離して渡すことで、Sonnet 4.6 が主検索結果と関連資料を区別して回答できるようにする。
        """
        if not hits:
            return ("該当する資料が見つかりませんでした。", 0.0)

        system = load_prompt("search", self._prompt_version, "system")

        # 主検索結果と関連 Drive 資料を分離 (Phase 2 新機能)
        primary_hits = [h for h in hits if not (h.metadata or {}).get("is_related_drive")]
        related_hits = [h for h in hits if (h.metadata or {}).get("is_related_drive")]

        primary_block = "\n\n".join(
            f"[chunk_id: {h.chunk_id}, score: {h.score:.3f}]\n{h.content}" for h in primary_hits
        )
        sections = [f"# 質問\n{query}\n\n# 参考資料\n{primary_block}"]
        if related_hits:
            related_block = "\n\n".join(
                f"[chunk_id: {h.chunk_id}] {(h.metadata or {}).get('title', '')}\n{h.content}"
                for h in related_hits
            )
            sections.append(
                "# 関連 Drive 資料 (営業 FB のクライアント名で自動マッチング)\n"
                "本資料は質問内容と直接マッチした FB 投稿のクライアントについて、"
                "Drive に存在する関連 PDF / Doc の冒頭抜粋です。回答中で別途紹介してください。\n\n"
                + related_block
            )
        user_message = "以下の社内資料から質問に答えてください。\n\n" + "\n\n".join(sections)

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,  # 同じ system prompt を頻繁に呼ぶのでキャッシュで input cost 1/10
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """metadata 値を安全に int 化する。文字列・None・int 混在対応。"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_source(hit: SearchHit) -> str | None:
        """SearchHit から表示用の source 文字列を組み立てる。

        優先順位:
        1. source_type='slack' → channel_name（新スキーマ）
        2. metadata "source"（旧スキーマ）
        3. file_name + page_num（ローカル proposals_chunks）
        4. None
        """
        meta = hit.metadata or {}
        source_type = meta.get("source_type")
        if source_type == "slack":
            channel = meta.get("channel_name") or "Slack"
            return str(channel)
        src = meta.get("source")
        if src:
            return str(src)
        file_name = meta.get("file_name")
        page_num = meta.get("page_num")
        if file_name:
            if page_num is not None:
                return f"{file_name} (p.{page_num})"
            return str(file_name)
        return None
