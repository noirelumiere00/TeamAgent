"""訪問相談の会社名は完全一致・登録別名で照合し、近い事例と区別する。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from teamagent.adapters.pgvector_client import SearchHit, normalize_literal_term
from teamagent.ingest.industry_taxonomy import normalize_industry
from teamagent.skills.search import drive_first
from teamagent.skills.search.answer_sections import classify_hit
from teamagent.skills.search.case_list import render
from teamagent.skills.search.client_match import alias_pairs, normalize_client
from teamagent.skills.search.not_found import case_target_hits
from teamagent.skills.search.schema import DocumentReference


@dataclass(frozen=True)
class Visit:
    company: str
    category: str


def parse(query: str) -> Visit | None:
    if not re.search(r"事例|案件|実績", query):
        return None
    match = re.search(r"([^の\s、。？?「」]{2,80}?)(?:って|という)会社に(?:行く|伺う|訪問)", query)
    if match is None:
        return None
    category = query[: match.start()].rstrip("の ")
    category = re.sub(r"(?:系(?:の商品|商品)?|商品)$", "", category).strip()
    return Visit(match[1], category)


def same_company(hit: SearchHit, company: str) -> bool:
    wanted = normalize_client(company)
    if len(wanted) < 2:
        return False
    forms = {wanted}
    for a, b in alias_pairs():
        if wanted == normalize_client(a):
            forms.add(normalize_client(b))
        if wanted == normalize_client(b):
            forms.add(normalize_client(a))
    meta = hit.metadata or {}
    return any(
        normalize_client(str(name or "")) in forms
        for name in (
            meta.get(key) for key in ("client_name", "cls_project", "case_client", "advertiser")
        )
    )


def answer(
    visit: Visit,
    hits: Sequence[SearchHit],
    industry: str | None,
    refs: list[DocumentReference],
    *,
    complete: bool,
    category_hits: Sequence[SearchHit] | None,
) -> str:
    own = [hit for hit in [*hits, *(category_hits or [])] if same_company(hit, visit.company)]
    heading = ""
    if own:
        head = f"{visit.company}との過去案件です。"
        selected = own
    else:
        head = (
            f"{visit.company}との過去案件は見つかりませんでした。"
            if complete
            else f"{visit.company}との過去案件は検索が完了せず確認できませんでした。"
        )
        category = visit.category or "関連する商材"
        if category_hits is None:
            return head + f"\n\n{category} の資料は検索が完了せず確認できませんでした。"
        term = normalize_literal_term(visit.category)
        selected = [
            hit
            for hit in category_hits
            if term
            and classify_hit(hit.metadata or {}) not in {"karte", "research"}
            and any(
                term in normalize_literal_term(text)
                for text in (str((hit.metadata or {}).get("title") or ""), hit.content)
            )
        ]
        if selected:
            heading = f"{category} に触れた資料\n\n"
        else:
            canonical = normalize_industry(industry)
            selected = case_target_hits(hits, canonical) if canonical else []
            heading = f"同じ業種（{canonical}）の事例\n\n" if selected else ""
    cards = render(
        selected,
        None,
        lambda hit: drive_first.canonical_reference(hit.metadata, hit.content).url,
        displayed_refs=refs,
        cards=True,
        max_cases=3,
    )
    if not cards:
        if not complete:
            return head
        return f"{visit.category or '関連する商材'} の事例は見つかりませんでした。"
    return head + "\n\n" + heading + cards
