"""直近・関連度で他社の提案書を最大4件、自社の過去提案を1件返す。"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import replace
from typing import ClassVar

from pydantic import BaseModel

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.classify import _parse_rulebook_title
from teamagent.ingest.industry_taxonomy import normalize_industry
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.morning_digest.calendar_window import now_jst
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
from teamagent.skills.search.dates import extract_title_date
from teamagent.skills.search.knowledge_query import (
    document_type,
    extract_knowledge_filters,
    extract_query_industry,
)
from teamagent.skills.search.schema import DocumentReference
from teamagent.skills.search.skill import SearchSkill

_FEATURES = {
    "cls_industry": "業種",
    "cls_solution": "施策",
    "cls_target": "対象層",
    "case_product": "商材",
}
_BOILERPLATE = re.compile(
    r"社名|会社概要|株式会社|有限会社|御中|住所|所在地|〒|ビル|[都道府県].{0,15}[市区町村]"
    r"|\d+\s*(?:丁目|番地|号)|\d+[-－]\d+[-－]\d+"
    r"|(?:TEL|FAX|copyright|all rights reserved)|ご提案|提案書|企画書|提案資料|表紙|目次"
    r"|\b(?:proposal|presentation|agenda|contents|confidential)\b",
    re.IGNORECASE,
)
_TOPIC = re.compile(
    r"動画|広告|施策|訴求|認知|購買|購入|集客|配信|運用|販促|イベント|キャンペーン"
    r"|(?<![A-Za-z])(?:SNS|PR|SEO)(?![A-Za-z])",
    re.I,
)
_TITLE_DATE = re.compile(
    r"(?<!\d)(?:20\d{2}年\d{1,2}月(?:\d{1,2}日)?|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|(?:20)?\d{6})(?!\d)"
)


@register
class RecommendSkill(BaseSkill[RecommendInput, RecommendOutput]):
    """類似する他社の提案書を、同一社の過去資料と分けて一覧提示する。"""

    name: ClassVar[str] = "recommend"
    description: ClassVar[str] = (
        "似た・類似の・近い・参考になる過去の提案書／資料を探して、という資料推薦の依頼に使う。"
        "他社の提案書を直近最大2件、残りを関連度順で合計最大4件を一覧提示する。原本リンクありを優先する。"
        "リンク未登録なら担当への確認を案内する。同じ会社の最新の過去提案は最大1件を別に示す。"
        "answer をそのまま返す。"
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
        vocabulary = [name for h in hits if (name := self._client_name(h))]
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
        recent: list[RecommendItem] = []
        similar: list[RecommendItem] = []
        same_client: list[RecommendItem] = []
        seen_files: set[tuple[str, str]] = set()
        clients: list[str] = []
        skipped_company = False

        today = now_jst().date()
        year, month = divmod(today.year * 12 + today.month - 1 - 6, 12)
        month += 1
        cutoff = today.replace(
            year=year, month=month, day=min(today.day, monthrange(year, month)[1])
        ).isoformat()
        today_iso = today.isoformat()
        others = [h for h in groups[0] if not (target and self._same_client(h, target[1]))]
        own = [h for h in groups[0] if target and self._same_client(h, target[1])]
        dated = [
            h
            for h in others
            if (value := self._date(h))
            and cutoff <= (value + "-01" if len(value) == 7 else value) <= today_iso
        ]
        recent_hits = sorted(dated, key=self._date, reverse=True)
        recent_hits.sort(key=lambda h: self._material_url(h) is None)
        similar_hits = sorted(others, key=lambda h: (self._material_url(h) is None, -h.score))
        own_hits = sorted(own, key=self._date, reverse=True)

        for bucket, ranked, limit in (
            (recent, recent_hits, 2),
            (similar, similar_hits, 4),
            (same_client, own_hits, 1),
        ):
            is_same = bucket is same_client
            for hit in ranked:
                if len(bucket) >= limit or (not is_same and len(recent) + len(similar) >= 4):
                    break
                ref = hit.metadata[drive_first.REFERENCE_KEY]
                material_key = (
                    ("drive", ref.drive_file_id)
                    if self._material_url(hit)
                    else (
                        "record",
                        str(hit.metadata.get("document_id") or ref.source_uri or hit.chunk_id),
                    )
                )
                if material_key in seen_files:
                    continue
                client = self._client_name(hit)
                if not is_same and client and any(self._same_client(hit, name) for name in clients):
                    skipped_company = True
                    continue
                seen_files.add(material_key)
                item = self._to_item(hit, features)
                bucket.append(item)
                if not is_same and client:
                    clients.append(client)
                if not item.source_uri:
                    log.info(
                        "recommend_material_unregistered",
                        case_id=str(hit.metadata.get("document_id") or hit.chunk_id),
                        has_contact=bool(item.contact_name),
                    )

        proposals = [*recent, *similar]
        sections = []
        for heading, items in (
            ("直近の似た提案", recent),
            ("特に似ている提案", similar),
            ("自社の過去提案", same_client),
        ):
            if not items:
                continue
            cards = "\n\n".join(
                f"*{item.title}*（{item.date}）\n• {item.similarity}\n"
                f"• {self._material_notice(item)}"
                for item in items
            )
            sections.append(f"*{heading}*\n\n{cards}")
        if not proposals:
            sections.insert(0, "他社の類似提案書は見つかりませんでした。")
        answer = "\n\n".join(sections)
        if proposals and len(proposals) < 4 and skipped_company:
            answer += "\n\nこれ以上の他社の類似提案書は、検索した候補に見つかりませんでした。"
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
        return bool(
            hit_is_about_client(hit, client, aliases=aliases(client), use_entities=False)
            or any(
                names_overlap(RecommendSkill._client_name(hit), name)
                for name in (client, *aliases(client))
            )
        )

    @staticmethod
    def _client_name(hit: SearchHit) -> str:
        meta = hit.metadata
        for key in ("client_company", "case_company", "client_name", "advertiser"):
            name = normalize_filter_client(str(meta.get(key) or ""))
            if name and name not in {"不明", "その他", "未分類", "なし", "-"}:
                return name
        ref = meta.get(drive_first.REFERENCE_KEY)
        for title in (ref.title if ref else "", meta.get("title"), meta.get("file_name")):
            if match := _parse_rulebook_title(str(title or "")):
                return match.project
        return normalize_filter_client(str(meta.get("cls_project") or "")) or ""

    @staticmethod
    def _case_name(title: str) -> str:
        title = unicodedata.normalize("NFKC", _source_text(title)).strip(" _*")
        title = re.sub(r"\.(?:pdf|pptx?|docx?|xlsx?)$", "", title, flags=re.I)
        title = re.sub(r"【(?:ご?提案(?:書|資料)?|企画書|最終版|最新版|社外秘)】", "", title)
        title = _TITLE_DATE.sub(lambda m: "" if extract_title_date(m[0]) else m[0], title)
        title = title.replace("_", " ")
        title = re.sub(r"(?<!\S)(?:ご?提案(?:書|資料)?|企画書)(?!\S)", "", title)
        return _clean(title.strip(" -()（）"), 100)

    @staticmethod
    def _similarity(hit: SearchHit, features: dict[str, str]) -> str:
        meta = hit.metadata
        company = RecommendSkill._client_name(hit)
        companies = {company, *aliases(company)} - {""}

        def usable(text: str) -> bool:
            return (
                text not in {"", "不明", "その他", "未分類", "未定", "提案書"}
                and not _BOILERPLATE.search(text)
                and not any(name.casefold() in text.casefold() for name in companies)
            )

        industry = str(
            meta.get("cls_industry") or meta.get("case_industry") or meta.get("industry") or ""
        )
        classified = {
            **meta,
            "cls_industry": normalize_industry(industry) or industry,
            "cls_solution": meta.get("cls_solution") or meta.get("proposed_menu"),
        }
        values = {
            key: _clean(value, 60)
            for key in _FEATURES
            if usable(value := str(classified.get(key) or "").strip())
        }
        detail = next(
            (
                detail
                for line in re.split(r"[\r\n]+|(?<=[。！？])", hit.content)
                if len(line.strip()) >= 15 and usable(line.strip()) and _TOPIC.search(line)
                for detail in _details(line)
                if usable(detail)
            ),
            "",
        )
        common = [
            f"{label}「{_clean(shared_value, 60)}」"
            for key, label in _FEATURES.items()
            if key != "cls_industry"
            and (shared_value := features.get(key))
            and usable(shared_value)
            and (
                names_overlap(shared_value, values.get(key))
                or shared_value.casefold() in detail.casefold()
            )
        ]
        industry = values.pop("cls_industry", "")
        if common:
            summary = "・".join(common) + "が共通。" + detail
        elif detail:
            summary = detail
        elif values:
            summary = (
                "・".join(f"{_FEATURES[k]}「{v}」" for k, v in values.items()) + "を扱う提案。"
            )
            if industry:
                summary += f"業種「{industry}」。"
        elif industry:
            summary = (
                f"同じ業種（{industry}）の提案。"
                if names_overlap(features.get("cls_industry"), industry)
                else f"業種「{industry}」の提案。"
            ) + "中身の共通点は資料で確認してください。"
        else:
            summary = "業種・施策の共通点は確認できませんでした。"
        # _clean が付ける省略記号も含めて90字以内にする。
        return _clean("似ている点：" + summary, 89)

    @staticmethod
    def _is_proposal(hit: SearchHit) -> bool:
        meta = hit.metadata or {}
        return (
            document_type(
                str(meta.get("title") or meta.get("file_name") or ""), meta.get("cls_doc_type")
            )
            == "提案書"
        )

    @staticmethod
    def _material_url(hit: SearchHit) -> str | None:
        ref: DocumentReference = hit.metadata[drive_first.REFERENCE_KEY]
        return ref.url if ref.kind == "drive" and case_link(ref.url, "資料を見る") else None

    @staticmethod
    def _material_notice(item: RecommendItem) -> str:
        link = case_link(item.source_uri, "資料を見る")
        if link:
            return link
        contact = (
            f"担当の {item.contact_name} さんに確認してください"
            if item.contact_name
            else "担当者を確認できませんでした"
        )
        return f"資料はナレッジに登録されていません。{contact}"

    @staticmethod
    def _date(hit: SearchHit) -> str:
        meta = hit.metadata
        ref = meta[drive_first.REFERENCE_KEY]
        entry = entry_from_hit(replace(hit, metadata={**meta, "title": ref.title}), url=ref.url)
        return entry.date or ""

    @staticmethod
    def _to_item(hit: SearchHit, features: dict[str, str]) -> RecommendItem:
        meta = hit.metadata
        ref = meta[drive_first.REFERENCE_KEY]
        # owner_email は取込アカウントなので担当者の代用にしない。
        contact_key = (
            "submitter"
            if (meta.get("source_type") == "slack" or meta.get("is_knowledge_share"))
            and not meta.get("is_sales_fb")
            and meta.get("topic") != "案件決定"
            else "case_owner"
        )
        contact = _clean(_source_text(str(meta.get(contact_key) or "")), 60).strip()
        if "@" in contact or contact in {"不明", "未定", "なし", "-"}:
            contact = ""
        contact = contact.split()[0].removesuffix("さん") if contact else ""
        url = RecommendSkill._material_url(hit)
        title = RecommendSkill._case_name(str(meta.get("client_case") or ref.title))
        return RecommendItem(
            title=title or _clean(meta.get("cls_project") or "案件名不明", 100),
            source_uri=url,
            score=hit.score,
            client_name=meta.get("client_name"),
            cls_project=meta.get("cls_project"),
            date=_month(RecommendSkill._date(hit)),
            similarity=RecommendSkill._similarity(hit, features),
            contact_name=contact or None,
        )
