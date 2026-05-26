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

from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.embeddings_client import Embedder
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput


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
        app_role: str | None = "teamagent_app",
    ) -> None:
        """Adapter は外から注入する（テストでモック差し替え可能にするため）。

        デフォルトはローカル demo のスキーマ（proposals_chunks: text, no metadata）。
        本番 RDS で proposal_chunks(content, metadata JSONB) を使う際は引数で上書き。

        use_contextual=True を指定すると proposals_chunks_contextual テーブルを使い、
        Anthropic Contextual Retrieval（前置詞付き chunk + 再 embedding）で検索する。
        scripts/contextual_retrieval.py で事前にテーブルを作成しておく必要がある。

        app_role: Postgres ロール切替（migration 0002 で導入の `teamagent_app`）。
          - 本番では必ず `teamagent_app` を渡し、SET ROLE で RLS を効かせる
          - ローカル開発で `teamagent_app` が未作成の環境では None を渡す（旧挙動）
          - 既存 proposals_chunks 系は RLS 未適用テーブルだが、teamagent_app role に
            migration 0002 で GRANT 済なので問題なくアクセス可能
        """
        self._bedrock = bedrock or BedrockClient.from_env()
        self._pgvector = pgvector or PgVectorClient.from_env()
        self._embedder = embedder
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
        """
        where: str | None = None
        if input.filter_industry and self._metadata_col is not None:
            # メタデータ JSONB のフィルタ。metadata 列を持つテーブルでのみ有効
            where = f"{self._metadata_col}->>'industry' = '{input.filter_industry}'"

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
            return self._pgvector.search_similar(
                conn=conn,
                embedding=embedding,
                table=self._target_table,
                limit=input.top_k,
                where=where,
                content_col=self._content_col,
                metadata_col=self._metadata_col,
                extra_cols=self._extra_cols,
            )

    def _summarize(
        self,
        query: str,
        hits: list[SearchHit],
        request_id: str,
    ) -> tuple[str, float]:
        """Bedrock に system prompt + chunks + query を渡して要約させる。"""
        if not hits:
            return ("該当する資料が見つかりませんでした。", 0.0)

        system = load_prompt("search", "v1", "system")
        context_block = "\n\n".join(
            f"[chunk_id: {h.chunk_id}, score: {h.score:.3f}]\n{h.content}" for h in hits
        )
        user_message = (
            f"以下の社内資料から質問に答えてください。\n\n"
            f"# 質問\n{query}\n\n"
            f"# 参考資料\n{context_block}"
        )

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,  # 同じ system prompt を頻繁に呼ぶのでキャッシュで input cost 1/10
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

        - metadata に "source" があればそれを使う（本番 RDS 想定）
        - 無ければ file_name + page_num から組み立てる（ローカル proposals_chunks）
        - どちらも無ければ None
        """
        meta = hit.metadata or {}
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
