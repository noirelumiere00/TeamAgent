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

from typing import Any, ClassVar, cast

from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput


@register
class SearchSkill(BaseSkill[SearchInput, SearchOutput]):
    """過去資料を pgvector で検索 → Claude で要約する Skill。"""

    name: ClassVar[str] = "search"
    description: ClassVar[str] = (
        "営業16名が過去の提案書・議事録・メールを自然文クエリで検索する"
    )
    input_schema: ClassVar[type[BaseModel]] = SearchInput
    output_schema: ClassVar[type[BaseModel]] = SearchOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        pgvector: PgVectorClient | None = None,
        embedder: Any | None = None,
        target_table: str = "proposal_chunks",
    ) -> None:
        """Adapter は外から注入する（テストでモック差し替え可能にするため）。"""
        self._bedrock = bedrock or BedrockClient.from_env()
        self._pgvector = pgvector or PgVectorClient.from_env()
        # embedder は実装を後の Sprint で差し替えるためここでは型未定義のまま受ける
        self._embedder = embedder
        self._target_table = target_table

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

        # 2. pgvector で類似 chunk を取得
        hits = self._retrieve(embedding, input)

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
                    source=str(h.metadata.get("source")) if h.metadata.get("source") else None,
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

        Sprint 1 時点では embedder の実装を差し替え可能にしておく。
        後の Sprint で multilingual-e5-large（ローカル）か Titan Embed v2（Bedrock）に統一する。
        """
        if self._embedder is None:
            raise NotImplementedError(
                "embedder が注入されていません。Sprint 1 末で Titan Embed v2 を実装します"
            )
        return cast(list[float], self._embedder.embed(text))

    def _retrieve(self, embedding: list[float], input: SearchInput) -> list[SearchHit]:
        """pgvector で類似検索する。"""
        where = None
        if input.filter_industry:
            # メタデータ JSONB のフィルタ。実テーブル設計が固まったら厳密化する
            # 動的 SQL はホワイトリスト固定なので注入リスクなし
            where = f"metadata->>'industry' = '{input.filter_industry}'"

        with self._pgvector.connection() as conn:
            return self._pgvector.search_similar(
                conn=conn,
                embedding=embedding,
                table=self._target_table,
                limit=input.top_k,
                where=where,
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
            f"[chunk_id: {h.chunk_id}, score: {h.score:.3f}]\n{h.content}"
            for h in hits
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
        )
        return resp.text, resp.usage.cost_usd
