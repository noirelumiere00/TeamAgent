"""Recommend Skill 本体 (類似案件レコメンド、Phase1)。

新規案件の概要テキストから、過去の類似「提案書 / 議事録 / 営業 FB」をベクトル近傍で
取得し、3 バケットに振り分けて各上位3件を提示する。Bedrock 要約はしない (近傍提示のみ)。

検索は SearchSkill.retrieve_hits を再利用する (embed / RLS / dedup / 新スキーマ検索 /
Cohere Rerank などが全部入った gold set top-1 88% パイプライン)。embed も SQL も新規に
書かない。RLS は ctx.metadata['user_email'] を retrieve_hits に通すだけで既存配線が効く。

3 層分離: Skill 層。retrieval は SearchSkill (Adapter 越し) に委譲し、本 Skill は
振り分けと整形だけを担う。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.recommend.schema import (
    RecommendInput,
    RecommendItem,
    RecommendOutput,
)
from teamagent.skills.search.skill import SearchSkill

logger = structlog.get_logger(__name__)

# バケット上限 (各カテゴリの提示件数)。
_PER_BUCKET = 3
# cls_doc_type の予約語 (ingest/classify.py の _DOC_TYPES と揃える)。
_DOC_TYPE_PROPOSAL = "提案書"
_DOC_TYPE_MINUTES = "議事録"


@register
class RecommendSkill(BaseSkill[RecommendInput, RecommendOutput]):
    """新規案件概要から類似の過去提案書/議事録/営業 FB を近傍提示する Skill。"""

    name: ClassVar[str] = "recommend"
    description: ClassVar[str] = (
        "新規案件の概要から類似の過去提案書/議事録/営業FBをベクトル近傍で3カテゴリ提示する"
    )
    input_schema: ClassVar[type[BaseModel]] = RecommendInput
    output_schema: ClassVar[type[BaseModel]] = RecommendOutput

    def __init__(self, search: SearchSkill | None = None) -> None:
        # search は検索基盤の再利用。None なら自前生成 (本番は factory/bot が共有注入＝
        # embedder 二重ロード回避)。
        self._search = search or SearchSkill()

    def run(self, input: RecommendInput, ctx: SkillContext) -> RecommendOutput:
        log = ctx.bind_logger(self.name)
        log.info("recommend_start", brief_len=len(input.brief), top_k=input.top_k)

        # TODO(Phase2): 案件名寄せ(alias)マップを retrieve の前後に適用するフックをここに挟む
        # (query の別名展開 / hits の cls_project 正規化)。今回は実装しない。

        # 1. 類似の過去資料を取得 (検索基盤を再利用)。RLS は ctx.metadata['user_email'] を
        #    retrieve_hits に通すだけで _retrieve が GUC として接続に渡す (新規 SQL なし)。
        hits = self._search.retrieve_hits(
            input.brief,
            ctx,
            top_k=input.top_k,
            filter_industry=input.industry,
        )

        proposals, minutes, sales_fb = self._bucketize(hits)
        total = len(proposals) + len(minutes) + len(sales_fb)
        log.info(
            "recommend_done",
            proposals=len(proposals),
            minutes=len(minutes),
            sales_fb=len(sales_fb),
        )
        return RecommendOutput(
            brief=input.brief,
            similar_proposals=proposals,
            similar_minutes=minutes,
            similar_sales_fb=sales_fb,
            total_count=total,
        )

    @staticmethod
    def _to_item(hit: SearchHit) -> RecommendItem:
        meta = hit.metadata or {}
        return RecommendItem(
            title=meta.get("title"),
            source_uri=meta.get("source_uri"),
            score=hit.score,
            client_name=meta.get("client_name"),
            cls_project=meta.get("cls_project"),
        )

    @classmethod
    def _bucketize(
        cls, hits: list[SearchHit]
    ) -> tuple[list[RecommendItem], list[RecommendItem], list[RecommendItem]]:
        """hits を 類似提案書 / 類似議事録 / 類似営業FB の 3 バケットへ振り分ける。

        各バケットは元の (retrieve_hits の) スコア順を保ったまま上位 _PER_BUCKET 件で打ち切る。
        1 hit は最初に該当したバケットにのみ入れる (営業FB を最優先で判定)。
        """
        proposals: list[RecommendItem] = []
        minutes: list[RecommendItem] = []
        sales_fb: list[RecommendItem] = []

        for hit in hits:
            meta = hit.metadata or {}
            # 営業 FB マーカー (_fetch_related_drive_hits が立てる) を最優先で判定。
            if meta.get("is_sales_fb"):
                if len(sales_fb) < _PER_BUCKET:
                    sales_fb.append(cls._to_item(hit))
                continue
            doc_type = meta.get("cls_doc_type")
            if doc_type == _DOC_TYPE_PROPOSAL:
                if len(proposals) < _PER_BUCKET:
                    proposals.append(cls._to_item(hit))
            elif doc_type == _DOC_TYPE_MINUTES:
                if len(minutes) < _PER_BUCKET:
                    minutes.append(cls._to_item(hit))
            # 上記いずれにも該当しない種別 (報告書/価格表/契約/その他/未分類) は提示しない。

        return proposals, minutes, sales_fb
