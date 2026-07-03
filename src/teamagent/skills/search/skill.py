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

import os
import re
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.embeddings_client import Embedder
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.search.aggregation import extract_aggregation_filter
from teamagent.skills.search.dedup import cap_per_document, collapse_near_duplicates
from teamagent.skills.search.fusion import reciprocal_rank_fusion
from teamagent.skills.search.knowledge_query import (
    extract_knowledge_filters,
    extract_query_industry,
)
from teamagent.skills.search.query_planner import QueryPlanner
from teamagent.skills.search.rerank import sort_by_budget_proximity, sort_by_client_match
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

logger = structlog.get_logger(__name__)

# ユーザー向け回答から内部マーカー（chunk_id 引用・低信頼タグ）を除去する。
# v2d プロンプトでも chunk_id を出さない指示にしたが、Bedrock が入力チャンクの
# `[chunk_id: N]` を echo することがあるため後段でも保険で落とす（営業に技術IDを見せない）。
_CHUNK_ID_RE = re.compile(r"\s*[\[(][^\[\]()]*chunk_id[^\[\]()]*[\])]")


def _strip_internal_markers(text: str) -> str:
    if not text:
        return text
    out = _CHUNK_ID_RE.sub("", text)
    out = out.replace("（関連度低・参考）", "")
    return out.strip()


@register
class SearchSkill(BaseSkill[SearchInput, SearchOutput]):
    """過去資料を pgvector で検索 → Claude で要約する Skill。"""

    name: ClassVar[str] = "search"
    description: ClassVar[str] = (
        "営業16名が過去の提案書・議事録・メールを自然文で検索する一次窓口。"
        "『探して/あったっけ/どれ?/見つけて』など“あるか/どれが該当するか”の探索・列挙はここ"
    )
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
        rerank_return_size: int = 100,
        min_relevance: float = 0.0,
        min_relevance_fallback: float = 0.0,
        use_client_boost: bool = False,
        client_boost_limit: int = 10,
        use_aggregation_mode: bool = False,
        use_knowledge_filters: bool = False,
        prompt_version: str = "v1",
        summary_max_tokens: int = 4096,
        app_role: str | None = "teamagent_app",
        query_planner: QueryPlanner | None = None,
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
        # QW-4: rerank が返す件数を top_k から切り離す。Cohere rerank を top_n=top_k(=5) で
        # 呼ぶと min_relevance/fallback の母数も 5 件に痩せ、6 位以降の閾値超 chunk を救済できない。
        # top_n=min(len(hits), rerank_return_size) で広く返し、min_relevance 適用後に最終段で
        # [:top_k] する。SEARCH_MIN_RELEVANCE=0.0（既定）では rerank の上位 top_k が変わらず
        # 完全等価（no-regression）。Cohere 課金は結果数非依存のためコスト不変。
        self._rerank_return_size = rerank_return_size
        # Sprint 5: 反ハルシネーション閾値。Rerank relevance がこの値未満の hit は
        # 「根拠として弱い」とみなし落とす。全 hit が落ちれば 0 件 = Bot は
        # 「資料に記載がありません」と返し、無い情報を捏造しない。
        # SEARCH_MIN_RELEVANCE env で制御 (既定 0.0 = OFF)。Rerank score (0-1) 前提。
        # gold set 実測: 実ヒット最低 0.50 / expect_zero 最高 0.23 → 0.4 で綺麗に分離。
        self._min_relevance = min_relevance
        # Sprint 7: 2段階しきい値の fallback。strict(min_relevance)で全 hit が落ちた
        # クエリのみ、この緩いしきい値で救出し is_low_confidence を付与する（borderline
        # 実ヒットの 0 件化を防ぎつつ、弱い根拠での断定を抑える）。既定 0.0 = fallback 無効
        # = 従来の単一しきい値挙動と完全一致（後方互換）。SEARCH_MIN_RELEVANCE_FALLBACK で制御。
        self._min_relevance_fallback = min_relevance_fallback
        # Sprint 7: クライアント名ブースト。固有名詞クエリ（例「ユニーの2回目提案」）で
        # 既知クライアント名に substring 一致したら、client_name で絞った検索を追加で実行し
        # rerank プールに合流させる（dense が固有名詞を取りこぼすリコール弱点を補強）。
        # 既定 OFF（USE_CLIENT_BOOST）。語彙は初回に1度だけ取得しキャッシュする。
        self._use_client_boost = use_client_boost
        self._client_boost_limit = client_boost_limit
        self._client_vocab: list[str] | None = None
        # Sprint 5: 集約・一覧クエリモード。「BANT A の案件一覧」等を検出したら
        # 意味検索ではなくメタデータフィルタ列挙 (list_by_metadata) で答える。
        # USE_AGGREGATION_MODE=true で有効化 (既定 OFF)。new_schema 前提。
        self._use_aggregation_mode = use_aggregation_mode
        # ナレッジ Q&A: 「○○業界の提案事例」等の資料種別語を cls_doc_type フィルタに変換し、
        # まず分類メタで絞った意味検索→0件なら外して通常検索にフォールバック。
        # USE_KNOWLEDGE_FILTERS で有効化（既定 OFF）。new_schema + 自動分類済 docs 前提。
        self._use_knowledge_filters = use_knowledge_filters
        # Day 8 (2026-05-28) Sprint 4-B: prompt v2 (insight + actionable thinking)。
        # 「過去のチャンク要約」から「パターン抽出 + 推奨アクション」に役割を進化させる。
        # ユーザー指摘 (Day 8): "あたりまえの過去事例リサーチは不要、リサーチ＆改善の思考が欲しい"
        # PROMPT_VERSION 環境変数 (v1 / v2 / v2c) で切替可能。
        # v2c は v2 の compact 版 (104→41行)、output token 削減でレイテンシ 46s→目標 20s 以下。
        self._prompt_version = prompt_version
        # Day 8 Sprint 4-D: Bedrock Converse の max_tokens 制限。
        # SEARCH_MAX_TOKENS env で runtime 制御、v2c と組合せて latency を半減狙い。
        self._summary_max_tokens = summary_max_tokens
        # P3 エージェント検索: 注入時のみ multi-query/HyDE→RRF＋LLMルーティングを使う。
        # None（既定）なら従来の単一クエリ + substring ルーティングのまま（後方互換）。
        self._query_planner = query_planner
        # L1 検索結果の「資料の被り」対策。テンプレページ（表紙・会社紹介・料金FMT等）が
        # 結果を埋め尽くす / 複数資料から同一テンプレチャンクが重複ヒットするのを retrieval
        # 側で潰す。env 読み取りはここ（skill 側）で行い、純関数にはパラメータで渡す
        # （純関数は os.environ を読まない＝テスト容易）。**既定 OFF・後方互換**：
        # SEARCH_DEDUP_RESULTS が無効なら _retrieve は 1 バイトも挙動が変わらない。
        self._dedup_results = self._envflag("SEARCH_DEDUP_RESULTS")
        self._per_doc_cap = self._envint("SEARCH_PER_DOC_CAP", 2)
        self._neardup_jaccard = self._envfloat("SEARCH_NEARDUP_JACCARD", 0.9)
        # テンプレ（表紙・会社紹介・料金 FMT 等の定型ページ）を new_schema 検索から除外する。
        # env 読み取りはここ（skill 側・__init__ で1回）で行い、search_similar_new_schema へ
        # exclude_boilerplate として渡す。テンプレ判定（指紋＋出現 document 数）は pgvector 側
        # （SQL）で行う＝この skill は flag を運ぶだけ。**既定 OFF・後方互換**：
        # BOILERPLATE_EXCLUDE_SEARCH が無効なら exclude_boilerplate=False で従来と完全一致。
        self._exclude_boilerplate = self._envflag("BOILERPLATE_EXCLUDE_SEARCH")
        # 重複資料（PDF/PPTX の二重取込など「基本同一」文書）を new_schema 検索から除外する。
        # ingest 側（DOC_DEDUP_DETECT）が非正本に metadata.suppressed=true を打つので、検索側は
        # その doc を WHERE 除外するだけ。env 読み取りはここ（skill 側・__init__ で1回）で行い、
        # search_similar_new_schema へ exclude_duplicates として渡す（boilerplate と同じ流儀）。
        # 判定・除外は pgvector 側（SQL）の責務＝この skill は flag を運ぶだけ。**既定 OFF・
        # 後方互換**：DOC_DEDUP_EXCLUDE_SEARCH 無効なら exclude_duplicates=False で従来と完全一致。
        self._exclude_duplicates = self._envflag("DOC_DEDUP_EXCLUDE_SEARCH")
        # テンプレ/雛形（cls_is_template）・定期報告（cls_is_recurring）の文書単位除外。
        # ingest.classify の 2 フラグ（決定論タイトルルール OR LLM）を検索側 WHERE で使う。
        # TEMPLATE_EXCLUDE_SEARCH（既定 OFF・後方互換）で:
        # - exclude_templates は**常時** True（テンプレは何を探していても事例ではない）
        # - exclude_recurring は「提案書 intent」（明示 filter_doc_type=提案書 or 自動
        #   knowledge_filters / plan.doc_type=提案書）のときだけ True＝「上期報告を見たい」
        #   クエリを殺さない。判定は _retrieve（_is_proposal_intent）で行い _pool_search /
        #   _apply_client_boost へパラメータで運ぶ。無効なら両フラグ False で従来と完全一致。
        self._exclude_templates = self._envflag("TEMPLATE_EXCLUDE_SEARCH")
        # L3: boilerplate/suppressed の SQL 除外句が fail-open（業界フィルタ解除）の再検索すら
        # 0 件にしてしまい、近傍があるのに「該当資料なし」と返す事故への最後の砦。
        # _pool_search の通常経路で hits が空 かつ exclude 系（boilerplate/duplicates）の
        # どちらかが真のとき、両 exclude を False にして 1 回だけ再検索し、救済できた hit に
        # is_low_confidence=True を付ける（弱い根拠＝テンプレ/重複かもしれないため断定を抑える）。
        # **既定 ON**だが、exclude 系が両方 OFF のときは経路自体に入らないので無影響。
        # SEARCH_EXCLUSION_RESCUE=0/false/no で明示的に無効化できる（安全側 gating）。
        self._exclusion_rescue = self._envflag("SEARCH_EXCLUSION_RESCUE", default="true")
        # 予算近接ソート（sort_budget_near 指定時に取得後 Python で1段並べ替え）。
        # env 読み取りは __init__ で1回（factory 無改修・_build_search_skill はモジュール関数で
        # self を持たないため）。**既定 OFF・後方互換**：無効なら sort 段を一切呼ばない（恒等）。
        self._budget_sort = self._envflag("SEARCH_BUDGET_SORT")
        # B6: クライアント名クエリで実案件（cls_project/client_name 一致）を rerank 後の
        # 最終 top_k 内で前出しする 1 段並べ替え（絞らない＝取りこぼしても最悪ランク後退のみ）。
        # 既知クライアント名（client_name ∪ cls_project の UNION）に substring 一致したときだけ
        # 発火。env-gate SEARCH_CLIENT_MATCH_SORT（既定 OFF・恒等）。USE_CLIENT_BOOST は不変。
        self._client_match_sort = self._envflag("SEARCH_CLIENT_MATCH_SORT")
        # 検索に使う chunks の embedding 列（既定 'embedding'＝e5・従来挙動と完全一致）。
        # EMBEDDING_COLUMN env を __init__ で 1 回だけ解決し（boilerplate flag と同じ流儀）、
        # search_similar_new_schema へ embedding_col として運ぶ（純 SQL は pgvector 側の責務）。
        # Bedrock Cohere 移行時は EMBEDDING_COLUMN=embedding_cohere。EMBEDDER_BACKEND との
        # ペア整合（cohere⇄embedding_cohere / local⇄embedding）を起動時 fail-loud で検証する。
        # 検証は embedder が build_embedder_from_env() 経由（factory）で既に行われるが、
        # skill 単体構築（テスト/旧経路）でも空間不整合を防ぐためここでも検証する。
        from teamagent.adapters.embeddings_client import (
            resolve_embedder_backend,
            resolve_embedding_column,
            validate_embedder_column_pair,
        )

        self._embedding_column = resolve_embedding_column()
        validate_embedder_column_pair(resolve_embedder_backend(), self._embedding_column)

    @staticmethod
    def _envflag(name: str, default: str = "false") -> bool:
        return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")

    @staticmethod
    def _envint(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _envfloat(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

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
                    title=(str(h.metadata["title"]) if h.metadata.get("title") else None),
                    project=(
                        str(h.metadata["cls_project"]) if h.metadata.get("cls_project") else None
                    ),
                    industry=(
                        str(h.metadata["cls_industry"]) if h.metadata.get("cls_industry") else None
                    ),
                    doc_type=(
                        str(h.metadata["cls_doc_type"]) if h.metadata.get("cls_doc_type") else None
                    ),
                    budget=(
                        str(h.metadata["cls_budget"]) if h.metadata.get("cls_budget") else None
                    ),
                    is_low_confidence=bool(h.metadata.get("is_low_confidence", False)),
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

    def _pool_search(
        self,
        *,
        conn: Any,
        embedding: list[float],
        limit: int,
        filter_industry: str | None,
        strict_industry: bool,
        metadata_filters: dict[str, str] | None,
        request_id: str,
        sticky_filters: dict[str, str] | None = None,
        metadata_contains: dict[str, str] | None = None,
        exclude_recurring: bool = False,
    ) -> list[SearchHit]:
        """新スキーマの単一ベクトル検索。フィルタ指定で 0 件なら外して再検索（fail-open）。

        L3: フィルタ解除後も 0 件 かつ exclude 系（boilerplate/duplicates/templates/
        recurring）が真のときは、最後の砦として exclude を全て外して 1 回だけ再検索し、
        救済 hit に is_low_confidence=True を付ける（テンプレ/重複除外が近傍まで巻き込んで
        0 件化する事故の保険）。SEARCH_EXCLUSION_RESCUE で gating
        （既定 ON、exclude 系 OFF なら無影響）。

        sticky_filters / metadata_contains（ユーザー明示の budget / client）は、自動付与の
        metadata_filters や filter_industry を fail-open で外すときでも**必ず再注入**する
        （「500万〜で絞ったのに 0 件→黙って全予算帯が返る」無音 drop を防ぐ）。

        exclude_recurring は _retrieve が「提案書 intent」のときだけ True にして渡す
        （env TEMPLATE_EXCLUDE_SEARCH が前提）。exclude_templates は常時 env 値
        （self._exclude_templates）。fail-open 再検索でも両者は維持し、rescue 経路でのみ
        他 exclude と一緒に False へ倒す。
        """
        hits = self._pgvector.search_similar_new_schema(
            conn=conn,
            embedding=embedding,
            limit=limit,
            filter_industry=filter_industry,
            request_id=request_id,
            strict_industry=strict_industry,
            metadata_filters=metadata_filters,
            sticky_filters=sticky_filters,
            metadata_contains=metadata_contains,
            exclude_boilerplate=self._exclude_boilerplate,
            exclude_duplicates=self._exclude_duplicates,
            exclude_templates=self._exclude_templates,
            exclude_recurring=exclude_recurring,
            embedding_col=self._embedding_column,
        )
        if not hits and (metadata_filters or filter_industry):
            hits = self._pgvector.search_similar_new_schema(
                conn=conn,
                embedding=embedding,
                limit=limit,
                filter_industry=None,
                request_id=request_id,
                strict_industry=strict_industry,
                sticky_filters=sticky_filters,  # 明示 budget は保持
                metadata_contains=metadata_contains,  # 明示 client は保持
                exclude_boilerplate=self._exclude_boilerplate,
                exclude_duplicates=self._exclude_duplicates,
                exclude_templates=self._exclude_templates,
                exclude_recurring=exclude_recurring,
                embedding_col=self._embedding_column,
            )
        # L3: ここまで 0 件 かつ exclude 系が効いている → exclude を全外しで最後の再検索。
        if (
            not hits
            and self._exclusion_rescue
            and (
                self._exclude_boilerplate
                or self._exclude_duplicates
                or self._exclude_templates
                or exclude_recurring
            )
        ):
            rescued = self._pgvector.search_similar_new_schema(
                conn=conn,
                embedding=embedding,
                limit=limit,
                filter_industry=None,
                request_id=request_id,
                strict_industry=strict_industry,
                sticky_filters=sticky_filters,  # 明示 budget は最後まで保持
                metadata_contains=metadata_contains,  # 明示 client は最後まで保持
                exclude_boilerplate=False,
                exclude_duplicates=False,
                exclude_templates=False,
                exclude_recurring=False,
                embedding_col=self._embedding_column,
            )
            if rescued:
                # frozen dataclass の可変 dict なので in-place 付与（再代入はしない）。
                for h in rescued:
                    h.metadata["is_low_confidence"] = True
                logger.info(
                    "search_exclusion_rescued",
                    request_id=request_id,
                    rescued=len(rescued),
                    exclude_boilerplate=self._exclude_boilerplate,
                    exclude_duplicates=self._exclude_duplicates,
                    exclude_templates=self._exclude_templates,
                    exclude_recurring=exclude_recurring,
                )
                hits = rescued
        return hits

    @staticmethod
    def _is_proposal_intent(filter_doc_type: str | None, auto_doc_type: str | None) -> bool:
        """「提案書 intent」判定（exclude_recurring を立てるかどうか）。

        明示 filter_doc_type があればそれだけで判定する（明示優先＝ユーザーが「議事録」と
        指定したのに自動抽出の「提案事例」で定期報告を落とす、を防ぐ）。明示が無ければ
        自動抽出（extract_knowledge_filters の cls_doc_type / plan.doc_type）の 提案書 を
        採用する。どちらも無ければ False＝「上期報告を見たい」等の定期報告クエリを殺さない。
        """
        if filter_doc_type:
            return filter_doc_type == "提案書"
        return auto_doc_type == "提案書"

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
                # ユーザー明示フィルタ（client/budget）。USE_KNOWLEDGE_FILTERS の ON/OFF や
                # query_planner の有無と無関係に、両経路へ同一配線する（blocker 3）。
                # client は __client__ キーで cls_project/client_name/title の OR-ILIKE。
                # budget は sticky（fail-open でも外さない）。include_unknown_budget=True なら
                # 専用キー __budget_or_unknown__ で (cls_budget=値 OR '不明') の soft 化、
                # 既定（False）は strict（指定バンドのみ）。
                mc: dict[str, str] | None = (
                    {"__client__": input.filter_client} if input.filter_client else None
                )
                # NL client 配線: query_planner が抽出した client 名を、ユーザーが明示的に
                # filter_client を指定していないときだけ __client__ に昇格する（plan 取得後）。
                # 昇格したら filter_client 明示時と同じ扱いにするため boost をスキップする。
                plan_client_used = False
                # ユーザー明示の doc_type / solution は等価 sticky（budget と同じ・fail-open でも
                # 落とさない）。自動抽出（extract_knowledge_filters / plan.doc_type）と衝突したら
                # この明示フィルタを優先するため、後段で自動抽出側から該当キーを外す。
                sticky_pairs: dict[str, str] = {}
                if input.filter_budget:
                    if input.include_unknown_budget:
                        sticky_pairs["__budget_or_unknown__"] = input.filter_budget
                    else:
                        sticky_pairs["cls_budget"] = input.filter_budget
                if input.filter_doc_type:
                    sticky_pairs["cls_doc_type"] = input.filter_doc_type
                if input.filter_solution:
                    sticky_pairs["cls_solution"] = input.filter_solution
                sticky: dict[str, str] | None = sticky_pairs or None
                # 定期報告（cls_is_recurring）の除外は「提案書 intent」のときだけ。
                # env OFF（既定）なら _exclude_templates=False で常に False（後方互換）。
                excl_recurring = False
                if self._query_planner is not None:
                    # P3: LLM ルーティング + multi-query/HyDE → RRF 融合。
                    plan = self._query_planner.plan(input.query, ctx.request_id)
                    # plan 由来のメタフィルタ（業界 / 資料種別）は USE_KNOWLEDGE_FILTERS が
                    # 有効なときのみ適用する（単一クエリ経路の substring ルーティングと同じ
                    # flag に揃える＝一貫性）。OFF なら multi-query/HyDE/RRF だけを使う。
                    eff_industry: str | None
                    kf: dict[str, str] | None
                    if self._use_knowledge_filters:
                        eff_industry = input.filter_industry or plan.industry
                        # 明示 filter_doc_type は sticky 側で優先するため、自動 plan.doc_type は
                        # 明示が無いときだけ採用する（衝突時に二重 AND で 0 件化させない）。
                        kf = (
                            {"cls_doc_type": plan.doc_type}
                            if plan.doc_type and not input.filter_doc_type
                            else None
                        )
                        # NL client 抽出を __client__ へ昇格（自然言語処理の肝）。明示
                        # filter_client があれば上書きしない（mc is None ガード）。先頭のみ。
                        # client は metadata_contains（OR-ILIKE 部分一致）側で扱い、昇格時は
                        # plan_client_used=True にして boost をスキップ（明示時と挙動を揃え
                        # 別 client 混入を防ぐ）。fail-open（空 plan は no-op）。
                        if mc is None and plan.client_names:
                            mc = {"__client__": plan.client_names[0]}
                            plan_client_used = True
                    else:
                        eff_industry = input.filter_industry
                        kf = None
                    # 提案書 intent（明示 filter_doc_type 優先・無ければ plan.doc_type）。
                    # plan.doc_type はフィルタ適用と同じく USE_KNOWLEDGE_FILTERS 有効時のみ。
                    excl_recurring = self._exclude_templates and self._is_proposal_intent(
                        input.filter_doc_type,
                        plan.doc_type if self._use_knowledge_filters else None,
                    )
                    # multi-query: 元クエリ + 言い換え + HyDE を埋め込む。重複文は除いて
                    # 余計な embed / RRF リストを増やさない（Haiku が近似文を返しがちなため）。
                    sub_embeddings = [embedding]
                    seen_texts = {input.query.strip()}
                    for para in plan.paraphrases:
                        norm = para.strip() if para else ""
                        if norm and norm not in seen_texts:
                            seen_texts.add(norm)
                            sub_embeddings.append(self._embed(para))
                    if plan.hyde_answer:
                        hyde_norm = plan.hyde_answer.strip()
                        if hyde_norm and hyde_norm not in seen_texts:
                            seen_texts.add(hyde_norm)
                            sub_embeddings.append(self._embed(plan.hyde_answer))
                    ranked_lists = [
                        self._pool_search(
                            conn=conn,
                            embedding=emb,
                            limit=retrieve_limit,
                            filter_industry=eff_industry,
                            strict_industry=input.strict_industry,
                            metadata_filters=kf,
                            sticky_filters=sticky,
                            metadata_contains=mc,
                            request_id=ctx.request_id,
                            exclude_recurring=excl_recurring,
                        )
                        for emb in sub_embeddings
                    ]
                    hits = reciprocal_rank_fusion(ranked_lists)[:retrieve_limit]
                else:
                    # 従来: substring ルーティング（資料種別・業界）＋単一クエリ検索。
                    knowledge_filters = (
                        extract_knowledge_filters(input.query)
                        if self._use_knowledge_filters
                        else None
                    )
                    # 提案書 intent（明示 filter_doc_type 優先・無ければ自動抽出の
                    # cls_doc_type）。pop 前に読む（明示 doc_type 指定時は明示側で判定）。
                    excl_recurring = self._exclude_templates and self._is_proposal_intent(
                        input.filter_doc_type,
                        (knowledge_filters or {}).get("cls_doc_type"),
                    )
                    # 明示 doc_type / solution があれば、同名の自動抽出キーは外す（sticky 側で
                    # 等価に効くため二重 AND を避け、明示フィルタを優先する）。
                    if knowledge_filters:
                        if input.filter_doc_type:
                            knowledge_filters.pop("cls_doc_type", None)
                        if input.filter_solution:
                            knowledge_filters.pop("cls_solution", None)
                        knowledge_filters = knowledge_filters or None
                    eff_industry = input.filter_industry or (
                        extract_query_industry(input.query) if self._use_knowledge_filters else None
                    )
                    hits = self._pool_search(
                        conn=conn,
                        embedding=embedding,
                        limit=retrieve_limit,
                        filter_industry=eff_industry,
                        strict_industry=input.strict_industry,
                        metadata_filters=knowledge_filters,
                        sticky_filters=sticky,
                        metadata_contains=mc,
                        request_id=ctx.request_id,
                        exclude_recurring=excl_recurring,
                    )
                # Sprint 7: クライアント名ブースト。固有名詞クエリで dense が正解 chunk を
                # 取りこぼすのを補強（client_name 絞り検索を rerank プールへ合流）。
                # ただし input.filter_client 明示時はユーザーの絞り込みを優先し boost をスキップ
                # （自動 boost が別 client を混ぜ「A 社で絞ったのに B 社が混ざる」のを防ぐ）。
                # plan 由来 client を __client__ に昇格したときも明示時と同じ扱いでスキップする。
                if self._use_client_boost and not input.filter_client and not plan_client_used:
                    hits = self._apply_client_boost(
                        conn=conn,
                        query=input.query,
                        hits=hits,
                        embedding=embedding,
                        input=input,
                        request_id=ctx.request_id,
                        sticky_filters=sticky,  # 明示 doc_type/solution/budget を boost でも保持
                        metadata_contains=mc,  # 通常 None（filter_client 未指定時のみ boost）
                        exclude_recurring=excl_recurring,  # 提案書 intent 時のみ（boost も同じ）
                    )
                # M1 資料の被り対策。**プール段階（rerank の前）**に噛ませる。
                # 旧実装は rerank→top_k 後段に置いていたため、最良 doc が 2 chunk に圧縮され
                # 最終件数が top_k 未満に痩せていた。rerank 前に畳む/cap することで、rerank は
                # 重複を除いた広いプールから top_k を選び直せ、最終件数が top_k を維持する。
                # 順序は「near-dup 畳み込み → per-doc cap」。env 無効なら no-op（恒等）＝
                # 既存挙動と完全一致（後方互換）。純関数なので副作用なし。
                # ※drive-match の関連資料は rerank 後に別途付与され、ここでは cap 対象外。
                if self._dedup_results and hits:
                    before = len(hits)
                    hits = collapse_near_duplicates(hits, jaccard_threshold=self._neardup_jaccard)
                    after_collapse = len(hits)
                    hits = cap_per_document(hits, max_per_doc=self._per_doc_cap)
                    if len(hits) != before:
                        logger.info(
                            "search_dedup_results",
                            request_id=ctx.request_id,
                            before=before,
                            after_collapse=after_collapse,
                            after_cap=len(hits),
                            jaccard=self._neardup_jaccard,
                            per_doc_cap=self._per_doc_cap,
                        )
                # Rerank: relevance_score で再ソートし、広いプール（min(len, return_size)）を返す。
                # QW-4: top_k には絞らず、min_relevance/fallback の母数を広く保つ。最終 [:top_k] は
                # 閾値フィルタの後段で行う。
                if self._use_cohere_rerank and hits:
                    rerank_n = min(len(hits), self._rerank_return_size)
                    hits = self._apply_cohere_rerank(
                        query=input.query,
                        hits=hits,
                        top_n=rerank_n,
                        request_id=ctx.request_id,
                    )
                # Sprint 5: 反ハルシネーション閾値。relevance < 閾値の hit を落とす。
                # Rerank 後の relevance score に対して適用 (drive-match の固定 score=1.0
                # より前に評価し、弱い根拠しか無いクエリでは drive-match も発火させない)。
                if self._min_relevance > 0.0 and hits:
                    kept = [h for h in hits if h.score >= self._min_relevance]
                    if not kept and self._min_relevance_fallback > 0.0:
                        # strict で全滅 → fallback しきい値で救出（低信頼マーク付き）。
                        # borderline 実ヒットの 0 件化を防ぐ。metadata は frozen dataclass の
                        # 可変 dict なので in-place 付与（再代入はしない）。
                        rescued = [h for h in hits if h.score >= self._min_relevance_fallback]
                        for h in rescued:
                            h.metadata["is_low_confidence"] = True
                        if rescued:
                            logger.info(
                                "min_relevance_fallback",
                                request_id=ctx.request_id,
                                strict=self._min_relevance,
                                fallback=self._min_relevance_fallback,
                                before=len(hits),
                                rescued=len(rescued),
                                top_score=hits[0].score,
                            )
                        kept = rescued
                    elif len(kept) != len(hits):
                        logger.info(
                            "min_relevance_filter",
                            request_id=ctx.request_id,
                            min_relevance=self._min_relevance,
                            before=len(hits),
                            after=len(kept),
                            top_score=hits[0].score,
                        )
                    hits = kept
                # QW-4: rerank は広いプールを返すので、min_relevance 確定後に top_k へ絞る。
                # rerank を使ったときだけ適用（rerank 無効時は _pool_search の limit=top_k で
                # 既に top_k 以内＝この truncation は no-op）。budget_sort / fb_drive_match は
                # 従来どおり top_k 件のリストに対して動く（後方互換）。
                if self._use_cohere_rerank and len(hits) > input.top_k:
                    hits = hits[: input.top_k]
                # 予算近接ソート。rerank・min_relevance 確定後に1段だけ並べ替える（絞らない）。
                # sort_budget_near に近い順 → 同帯内は低信頼末尾 → 関連度降順。env-gate
                # SEARCH_BUDGET_SORT（既定 OFF）。FB drive-match（固定 score=1.0）の前に置く。
                if self._budget_sort and input.sort_budget_near and hits:
                    hits = sort_by_budget_proximity(hits, input.sort_budget_near)
                # B6: クライアント名クエリで実案件（cls_project/client_name 一致）を前出し。
                # rerank・min_relevance・top_k 絞り確定後、fb_drive_match の前に 1 段だけ
                # 並べ替える（絞らない）。明示 filter_client があればそれを、無ければ既知
                # クライアント語彙への substring 一致（_match_client・初回のみ語彙取得しキャッシュ）
                # を基準にする。env-gate SEARCH_CLIENT_MATCH_SORT（既定 OFF・恒等）。
                if self._client_match_sort and hits:
                    client_for_sort = input.filter_client or self._match_client(
                        input.query, conn, ctx.request_id
                    )
                    if client_for_sort:
                        before_top = hits[0].chunk_id if hits else None
                        hits = sort_by_client_match(hits, client_for_sort)
                        if hits and hits[0].chunk_id != before_top:
                            logger.info(
                                "search_client_match_sort",
                                request_id=ctx.request_id,
                                client=client_for_sort,
                                pool=len(hits),
                            )
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
        top_n: int,
        request_id: str,
    ) -> list[SearchHit]:
        """Cohere Rerank v3.5 で hits を relevance score 順に並べ替え、top_n に絞る。

        Day 8 (2026-05-28) Sprint 4-A の中核処理。
        - 入力: pgvector top_pool_size hits (dense retrieval 結果)
        - 処理: Bedrock Rerank API で query との関連性を再評価
        - 出力: relevance_score 降順 top_n 件 (元 hits.score は Rerank score で上書き)

        QW-4: top_n は最終 top_k ではなく救済プール幅（min(len(hits), rerank_return_size)）。
        最終 top_k への絞り込みは呼び出し側で min_relevance 適用後に行う。

        失敗時はオリジナル hits を top_n で truncate して返す (fail-safe, 副作用最小化)。
        """
        try:
            documents = [h.content for h in hits]
            response = self._bedrock.rerank(
                query=query,
                documents=documents,
                request_id=request_id,
                top_n=top_n,
            )
        except Exception:
            # Rerank 失敗は致命傷ではない: dense retrieval 結果をそのまま使う
            logger.exception("cohere_rerank_failed_falling_back_to_dense", request_id=request_id)
            return hits[:top_n]

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

    def _match_client(self, query: str, conn: Any, request_id: str) -> str | None:
        """クエリ文字列に既知クライアント名が substring で含まれれば最長一致を返す。"""
        if self._client_vocab is None:
            try:
                self._client_vocab = self._pgvector.list_client_names(
                    conn=conn, request_id=request_id
                )
            except Exception:  # 語彙取得失敗時はブースト無効（検索本体は継続）
                self._client_vocab = []
        # 長い名前を優先（短い部分名の誤爆を避ける）
        matched = [n for n in self._client_vocab if n and n in query]
        return max(matched, key=len) if matched else None

    def _apply_client_boost(
        self,
        *,
        conn: Any,
        query: str,
        hits: list[SearchHit],
        embedding: list[float],
        input: SearchInput,
        request_id: str,
        sticky_filters: dict[str, str] | None = None,
        metadata_contains: dict[str, str] | None = None,
        exclude_recurring: bool = False,
    ) -> list[SearchHit]:
        """固有名詞クエリで client_name 絞り検索を追加し rerank プールへ合流する。

        sticky_filters / metadata_contains は _retrieve で構築したユーザー明示フィルタ
        （cls_doc_type / cls_solution / cls_budget の等価 sticky・client の __client__ 部分一致）。
        boost のサブ検索にも必ず渡す。さもないと client 名だけにマッチした別種別の資料
        （議事録・価格表等）が doc_type/solution を無視してプールへ合流し、明示フィルタ違反の
        ヒットが rerank 後に表面化する（設計 §A/§E「明示フィルタは全再検索で保持」の穴）。
        client_boost は input.filter_client 未指定時のみ走るため通常 metadata_contains の
        __client__ は None だが、後方互換のため受け取って素通しする。

        exclude_templates / exclude_recurring も本検索（_pool_search）と同値で渡す。
        さもないと client 名一致のテンプレ/定期報告がここからプールへ合流し、除外設計が
        boost 経路だけ素通しになる（boilerplate/duplicates で過去 2 回検出済みの取りこぼしと
        同型の穴）。exclude_recurring は _retrieve が判定した提案書 intent の値。
        """
        matched = self._match_client(query, conn, request_id)
        if not matched:
            return hits
        boost = self._pgvector.search_similar_new_schema(
            conn=conn,
            embedding=embedding,
            limit=self._client_boost_limit,
            filter_industry=input.filter_industry,
            request_id=request_id,
            strict_industry=input.strict_industry,
            metadata_filters={"client_name": matched},
            sticky_filters=sticky_filters,
            metadata_contains=metadata_contains,
            exclude_boilerplate=self._exclude_boilerplate,
            exclude_duplicates=self._exclude_duplicates,
            exclude_templates=self._exclude_templates,
            exclude_recurring=exclude_recurring,
            embedding_col=self._embedding_column,
        )
        if not boost:
            return hits
        seen = {h.chunk_id for h in hits}
        added = [h for h in boost if h.chunk_id not in seen]
        if added:
            logger.info(
                "client_boost_applied",
                request_id=request_id,
                client_name=matched,
                added=len(added),
                pool_before=len(hits),
            )
        return list(hits) + added

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
            f"[chunk_id: {h.chunk_id}, score: {h.score:.3f}"
            + ("（関連度低・参考）" if (h.metadata or {}).get("is_low_confidence") else "")
            + f"]\n{h.content}"
            for h in primary_hits
        )
        sections = [f"# 質問\n{query}\n\n# 参考資料\n{primary_block}"]
        # 2段階しきい値の fallback で救出した低信頼 hit がある場合、断定を抑える注意を付す。
        if any((h.metadata or {}).get("is_low_confidence") for h in primary_hits):
            sections.append(
                "# 注意（グラウンディング）\n"
                "上記で『関連度低・参考』と付記した資料は関連度が低い参考情報です。確証が持てない"
                "場合は断定せず、『資料に明確な記載はないが関連しうる』等と不確実性を明示してください。"
            )
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
        return _strip_internal_markers(resp.text), resp.usage.cost_usd

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
