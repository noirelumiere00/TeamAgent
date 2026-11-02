"""基準案件の特徴で他社の提案書を探し、Drive 原本付きで最大3件返す。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import ClassVar

from pydantic import BaseModel

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.recommend.schema import RecommendInput, RecommendItem, RecommendOutput
from teamagent.skills.search import drive_first
from teamagent.skills.search.alias_expansion import resolve_topic_client
from teamagent.skills.search.answer_sections import _clean, entry_from_hit
from teamagent.skills.search.case_list import _details, _month, _source_text, case_link
from teamagent.skills.search.client_match import (
    aliases,
    hit_is_about_client,
    names_overlap,
    normalize_filter_client,
)
from teamagent.skills.search.knowledge_query import (
    document_type,
    extract_knowledge_filters,
    extract_query_industry,
)
from teamagent.skills.search.skill import SearchSkill

_FEATURES = {"cls_industry": "業種", "cls_solution": "施策", "cls_target": "対象層"}


@register
class RecommendSkill(BaseSkill[RecommendInput, RecommendOutput]):
    """類似する他社の提案書を、同一社の過去資料と分けて一覧提示する。"""

    name: ClassVar[str] = "recommend"
    description: ClassVar[str] = (
        "似た・類似の・近い・参考になる過去の提案書／資料を探して、という資料推薦の依頼に使う。"
        "業種・商材・施策の近い他社の提案書を優先し、原本リンク付きで最大3件を一覧提示する。"
        "同じ会社の過去資料は補足に分ける。answer をそのまま返す。"
        "例: 伊藤ハムの提案書に似た過去の提案書を探して。"
        "骨子生成はしない。提案（の骨子・たたき台）を作って、という明示依頼は proposal_draft。"
        "迷ったら recommend を優先して資料を見せ、骨子は求められてから。"
    )
    input_schema: ClassVar[type[BaseModel]] = RecommendInput
    output_schema: ClassVar[type[BaseModel]] = RecommendOutput

    def __init__(self, search: SearchSkill | None = None) -> None:
        self._search = search or SearchSkill()

    def run(self, input: RecommendInput, ctx: SkillContext) -> RecommendOutput:
        log = ctx.bind_logger(self.name)
        hits = self._search.retrieve_hits(
            input.brief[:1000], ctx, top_k=input.top_k, filter_industry=input.industry
        )
        vocabulary = [
            str(h.metadata[key])
            for h in hits
            for key in ("client_name", "cls_project")
            if h.metadata.get(key)
        ]
        target = resolve_topic_client(input.brief, vocabulary)
        source = [h for h in hits if target and self._same_client(h, target[1])]
        reference = next((h for h in source if self._is_proposal(h)), source[0] if source else None)
        features = {
            **(extract_knowledge_filters(input.brief) or {}),
            "cls_industry": input.industry or extract_query_industry(input.brief) or "",
        }
        for key in _FEATURES:
            if not features.get(key):
                features[key] = (
                    str(reference.metadata.get(key) or "")
                    if reference
                    else next(
                        (
                            str(h.metadata[key])
                            for h in hits
                            if h.metadata.get(key) and str(h.metadata[key]) in input.brief
                        ),
                        "",
                    )
                )
        features = {k: v for k, v in features.items() if v not in {"", "不明", "その他", "未分類"}}
        if target:
            # 通常検索の社名フィルタ・boostを引き継がず、題名・本文・分類で探し直す。
            names = {
                name
                for raw in (target[0], target[1], *aliases(target[1]))
                for name in (unicodedata.normalize("NFKC", raw), normalize_filter_client(raw))
                if name
            }
            pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            source_title = str(reference.metadata.get("title") or "") if reference else ""
            source_excerpt = reference.content[:400] if reference else ""
            topic = " ".join((source_title, source_excerpt, input.brief))
            query = " ".join([*(v for k, v in features.items() if k in _FEATURES), topic])
            query = re.sub(
                pattern,
                " ",
                unicodedata.normalize("NFKC", query),
                flags=re.IGNORECASE,
            )
            expanded = self._search.retrieve_hits(
                query[:1000],
                ctx,
                top_k=input.top_k,
                filter_industry=input.industry,
                prefer_same_client=False,
            )
            hits = [*expanded, *hits]

        candidates = [h for h in hits if self._is_proposal(h)]
        groups, _, _ = drive_first.annotate(self._search, [candidates], (), ctx)
        proposals: list[RecommendItem] = []
        same_client: list[RecommendItem] = []
        seen_files: set[str] = set()
        clients: list[str] = []
        for hit in groups[0]:
            ref = hit.metadata[drive_first.REFERENCE_KEY]
            if ref.kind != "drive" or not case_link(ref.url, "資料を見る"):
                continue
            if ref.drive_file_id in seen_files:
                continue
            seen_files.add(ref.drive_file_id)
            item = self._to_item(hit, features)
            if target and self._same_client(hit, target[1]):
                if not same_client:
                    same_client.append(item)
                continue
            client = item.client_name or item.cls_project or ""
            if client and any(self._same_client(hit, name) for name in clients):
                continue
            if len(proposals) < 3:
                proposals.append(item)
                if client:
                    clients.append(client)

        cards = [
            f"*{item.title}*（{item.date}）\n• {item.similarity}\n"
            f"• {case_link(item.source_uri, '資料を見る')}"
            for item in proposals
        ]
        answer = "\n\n".join(cards) or "Drive に原本のある他社の類似提案書は見つかりませんでした。"
        if same_client:
            item = same_client[0]
            answer += (
                f"\n\n同じ会社の過去資料：{item.title}（{item.date}） "
                f"{case_link(item.source_uri, '資料を見る')}"
            )
        log.info("recommend_done", proposals=len(proposals), same_client=len(same_client))
        return RecommendOutput(
            brief=input.brief,
            similar_proposals=proposals,
            same_client_materials=same_client,
            total_count=len(proposals),
            answer=answer,
        )

    @staticmethod
    def _same_client(hit: SearchHit, client: str) -> bool:
        return bool(hit_is_about_client(hit, client, aliases=aliases(client), use_entities=False))

    @staticmethod
    def _is_proposal(hit: SearchHit) -> bool:
        meta = hit.metadata or {}
        return (
            not meta.get("is_sales_fb")
            and document_type(
                str(meta.get("title") or meta.get("file_name") or ""), meta.get("cls_doc_type")
            )
            == "提案書"
        )

    @staticmethod
    def _to_item(hit: SearchHit, features: dict[str, str]) -> RecommendItem:
        meta = hit.metadata
        ref = meta[drive_first.REFERENCE_KEY]
        entry = entry_from_hit(replace(hit, metadata={**meta, "title": ref.title}), url=ref.url)
        common = [
            f"{label}「{_clean(value, 60)}」"
            for key, label in _FEATURES.items()
            if (value := features.get(key))
            and (
                names_overlap(value, str(meta.get(key) or ""))
                or value.casefold() in (entry.title + " " + hit.content).casefold()
            )
        ]
        details = _details(hit.content)
        similarity = (
            "似ている点：" + "・".join(common) + "が共通。"
            if common
            else "参考点：" + (details[0] if details else "類似資料として検索された提案書です。")
        )
        return RecommendItem(
            title=_clean(_source_text(entry.title), 100),
            source_uri=ref.url,
            score=hit.score,
            client_name=meta.get("client_name"),
            cls_project=meta.get("cls_project"),
            date=_month(entry.date),
            similarity=similarity,
        )
