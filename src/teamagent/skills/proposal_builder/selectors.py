"""Account and source-backed case selectors for proposal-builder."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from teamagent.cases.schema import CaseRecord
from teamagent.cases.store import MAX_STRUCTURE_SCORE, CaseSearchHit, search_case_records
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

_GENERAL_NEWS_TV = "general_news-tv"
_MAX_CASES = 3
_DEFAULT_SEARCH_POOL = 12
#: 既存 RAG 候補の先頭に差し込む事例レコード候補の上限（設計 §4.1「少なくとも 1 枠」）。
_MAX_CASE_RECORD_SLOTS = 2
_REGULATION_TRAIT = "薬機・景表規制"
_SAFE_CASE_LABELS = (
    "■施策",
    "■結果",
    "■成果",
    "■勝ち筋",
    "■目的",
    "施策:",
    "施策：",
    "結果:",
    "結果：",
    "成果:",
    "成果：",
    "勝ち筋:",
    "勝ち筋：",
)
_PRIORITY_METRICS = ("売上", "指名検索")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class AccountDatabaseMeta(_StrictModel):
    """Metadata embedded in the protected account JSON."""

    source: str
    count: int
    note: str


class AccountRecord(_StrictModel):
    """One protected, selectable owned-account record."""

    name: str
    category: list[str]
    desc: str
    tt: str
    ig: str
    yt: str


class AccountDatabase(_StrictModel):
    """Strict account DB wire contract."""

    metadata: AccountDatabaseMeta = Field(alias="_meta")
    accounts: list[AccountRecord]

    @model_validator(mode="after")
    def _count_matches_records(self) -> AccountDatabase:
        if self.metadata.count != len(self.accounts):
            raise ValueError("account database metadata count does not match records")
        return self


class AccountProspect(_StrictModel):
    """Gemini-derived selector vocabulary."""

    name: str | None = None
    target_categories: list[str]
    kaiwai_keywords: list[str]


class SelectedAccount(_StrictModel):
    """Ranked account result; callers must not log this raw object."""

    rank: int = Field(ge=1)
    score: int = Field(ge=0)
    name: str
    category: list[str]
    desc: str
    tt: str
    ig: str
    yt: str
    matched_categories: list[str]
    matched_keywords: list[str]


class CaseCandidate(_StrictModel):
    """A safe-to-render case reference sourced from existing RAG."""

    source: Literal["report_rag", "general_news-tv", "case_record"]
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)


class SearchRunner(Protocol):
    """DI seam implemented by the shared SearchSkill and lightweight fakes."""

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        """Run one retrieval-only search."""

        ...


def load_account_database(path: str | Path) -> AccountDatabase:
    """Load the protected account JSON from a strict local filesystem path."""

    local_path = Path(path).expanduser()
    if not local_path.is_file():
        raise FileNotFoundError(f"account database is not a local file: {local_path}")
    with local_path.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    return AccountDatabase.model_validate(payload, strict=True)


def _coerce_prospect(prospect: AccountProspect | Mapping[str, Any]) -> AccountProspect:
    if isinstance(prospect, AccountProspect):
        return prospect
    if isinstance(prospect, Mapping):
        return AccountProspect.model_validate(dict(prospect), strict=True)
    raise TypeError("account prospect must be AccountProspect or a mapping")


def select_top_accounts(
    database: AccountDatabase,
    prospect: AccountProspect | Mapping[str, Any],
) -> list[SelectedAccount]:
    """Apply flow_fill's exact scoring and return five stable-ranked accounts."""

    selected_for = _coerce_prospect(prospect)
    target_categories = set(selected_for.target_categories)

    scored: list[tuple[int, int, AccountRecord, list[str], list[str]]] = []
    for source_index, account in enumerate(database.accounts):
        matched_categories = sorted(set(account.category) & target_categories)
        matched_keywords = [
            keyword for keyword in selected_for.kaiwai_keywords if keyword in (account.desc or "")
        ]
        score = 2 * len(matched_categories) + len(matched_keywords)
        scored.append((score, source_index, account, matched_categories, matched_keywords))

    # source_index is the explicit secondary key, preserving flow_fill/Python's
    # stable input order for equal scores.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        SelectedAccount(
            rank=rank,
            score=score,
            name=account.name,
            category=list(account.category),
            desc=account.desc,
            tt=account.tt,
            ig=account.ig,
            yt=account.yt,
            matched_categories=matched_categories,
            matched_keywords=matched_keywords,
        )
        for rank, (score, _, account, matched_categories, matched_keywords) in enumerate(
            scored[:5], start=1
        )
    ]


def load_and_select_accounts(
    path: str | Path,
    prospect: AccountProspect | Mapping[str, Any],
) -> list[SelectedAccount]:
    """Convenience wrapper that keeps local loading and pure ranking separate."""

    return select_top_accounts(load_account_database(path), prospect)


def _normalise_channel(value: str | None) -> str:
    return (value or "").strip().lower().removeprefix("#")


def _is_general_news_tv_hit(hit: SearchHitOut, *, channel_id: str | None = None) -> bool:
    source_uri = (hit.source_uri or "").strip()
    if channel_id:
        return source_uri.startswith(f"slack://{channel_id}/")
    return _normalise_channel(hit.channel_name) == _GENERAL_NEWS_TV


def _candidate_url(hit: SearchHitOut) -> str | None:
    sensitive_terms = {
        value.strip().casefold()
        for value in (hit.client_name, hit.project)
        if value and value.strip()
    }
    for value in (hit.url, hit.drive_url, hit.source_uri):
        if not value or not value.strip():
            continue
        candidate = value.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        lowered = candidate.casefold()
        if any(term in lowered for term in sensitive_terms):
            continue
        return candidate
    return None


def _candidate_title(hit: SearchHitOut) -> str:
    industry = (hit.industry or "").strip() or "業種未分類"
    document_type = (hit.doc_type or "").strip() or "実績"
    return f"{industry}｜{document_type}事例"


def _candidate_excerpt(hit: SearchHitOut) -> str | None:
    """Return a metadata-only external projection, never the raw internal chunk.

    The ingest classifier is fail-open, and an internal report line can contain
    an unlabelled client/product name.  Even after heuristic masking that raw
    prose is not safe to put in an externally shareable deck.  We therefore use
    the chunk only to establish that known case labels exist, then emit a generic
    projection from structured industry metadata.
    """

    labels = [label for label in _SAFE_CASE_LABELS if label in hit.content]
    if not labels:
        return None
    replacement = (hit.industry or "").strip() or "同業種企業"
    kinds = "・".join(
        dict.fromkeys("成果" if "結果" in label or "成果" in label else "施策" for label in labels)
    )
    return (
        f"{replacement}領域の{kinds}ラベルを含む出典付き候補。"
        "固有名・数値は外部提出用投影から除外済み（詳細は原典確認）。"
    )


def format_case_candidates(
    report_hits: Sequence[SearchHitOut],
    slack_hits: Sequence[SearchHitOut],
    *,
    max_cases: int = _MAX_CASES,
    news_channel_id: str | None = None,
) -> list[CaseCandidate]:
    """Purely filter, deduplicate, rank, and render retrieved case hits."""

    if not 1 <= max_cases <= _MAX_CASES:
        raise ValueError(f"max_cases must be between 1 and {_MAX_CASES}")

    ranked: list[tuple[int, float, int, CaseCandidate]] = []
    ordinal = 0
    source_pairs: tuple[
        tuple[Literal["report_rag", "general_news-tv"], Sequence[SearchHitOut]], ...
    ] = (
        ("report_rag", report_hits),
        ("general_news-tv", slack_hits),
    )
    for source, hits in source_pairs:
        for hit in hits:
            ordinal += 1
            if hit.is_low_confidence:
                continue
            if source == "general_news-tv" and not _is_general_news_tv_hit(
                hit, channel_id=news_channel_id
            ):
                continue
            # External case slides are fail-closed: unclassified chunks cannot
            # establish either industry relevance or the client identity that
            # must be withheld from the projection.
            if not (hit.industry or "").strip() or not (
                (hit.client_name or "").strip() or (hit.project or "").strip()
            ):
                continue
            url = _candidate_url(hit)
            excerpt = _candidate_excerpt(hit)
            if not url or not excerpt:
                continue
            priority = sum(metric in hit.content for metric in _PRIORITY_METRICS)
            ranked.append(
                (
                    priority,
                    hit.score,
                    ordinal,
                    CaseCandidate(
                        source=source,
                        title=_candidate_title(hit),
                        url=url,
                        excerpt=excerpt,
                        score=hit.score,
                    ),
                )
            )

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    unique: list[CaseCandidate] = []
    seen: set[str] = set()
    for _, _, _, candidate in ranked:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
        if len(unique) == max_cases:
            break
    return unique


def search_case_candidates(
    search: SearchRunner,
    query: str,
    ctx: SkillContext,
    *,
    max_cases: int = _MAX_CASES,
    search_pool: int = _DEFAULT_SEARCH_POOL,
    news_channel_id: str | None = None,
) -> list[CaseCandidate]:
    """Search report RAG and recent general_news-tv posts through shared SearchSkill."""

    if not query.strip():
        raise ValueError("case search query must not be empty")
    if not 1 <= max_cases <= _MAX_CASES:
        raise ValueError(f"max_cases must be between 1 and {_MAX_CASES}")
    if not max_cases <= search_pool <= 50:
        raise ValueError("search_pool must be between max_cases and 50")

    report_output = search.run(
        SearchInput(
            query=query.strip()[:1000],
            top_k=search_pool,
            filter_doc_type="報告書",
            include_answer=False,
        ),
        ctx,
    )

    # SearchInput currently has no exact channel/source filter. A channel phrase
    # can improve retrieval but cannot guarantee source identity, so retrieval is
    # deliberately broad and format_case_candidates post-filters exact structured
    # channel_name/source_uri metadata. Hits lacking that metadata are dropped,
    # which is a known recall limitation until SearchSkill adds an exact source filter.
    slack_suffix = f"#{_GENERAL_NEWS_TV} 実績速報 売上 指名検索"
    slack_query = f"{query.strip()[: 999 - len(slack_suffix)]} {slack_suffix}"
    slack_output = search.run(
        SearchInput(
            query=slack_query[:1000],
            top_k=search_pool,
            include_answer=False,
        ),
        ctx,
    )
    return format_case_candidates(
        report_output.hits,
        slack_output.hits,
        max_cases=max_cases,
        news_channel_id=news_channel_id,
    )


class CaseRecordRepository(Protocol):
    """``case_records`` 検索の DI seam（store.search_case_records と同じ引数）。"""

    def search_case_records(
        self,
        *,
        sector: str | None,
        purpose: Sequence[str] | None,
        product_state: str | None,
        traits: Sequence[str] | None,
        query_embedding: Sequence[float] | None,
        exclude_client: str | None,
        limit: int,
    ) -> Sequence[CaseSearchHit]:
        """Return structure-scored case records."""

        ...


def _is_http_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _product_meta_value(meta: Any, key: str) -> Any:
    if isinstance(meta, Mapping):
        return meta.get(key)
    return getattr(meta, key, None)


def product_meta_traits(product_meta: Any) -> list[str]:
    """Gemini product_meta から traits を組む（現状 regulation → 薬機・景表規制 のみ）。"""
    return [_REGULATION_TRAIT] if bool(_product_meta_value(product_meta, "regulation")) else []


def case_record_excerpt(record: CaseRecord) -> str:
    """result_masked＋winpattern＋出典つき metrics（社名は含めない・masked のみ）。"""
    parts: list[str] = []
    if record.result_masked.strip():
        parts.append(f"結果: {record.result_masked.strip()}")
    if record.winpattern.strip():
        parts.append(f"勝ち筋: {record.winpattern.strip()}")
    cited = [
        f"{metric.name} {metric.value}{metric.unit}（出典: {metric.source_url}）"
        for metric in record.metrics
        if _is_http_url(metric.source_url)
    ]
    if cited:
        parts.append("実績値: " + "／".join(cited[:6]))
    if record.external_use == "unknown":
        parts.append("対外利用可否: 未確認（原典で確認）")
    return "\n".join(parts)


def case_candidate_from_hit(hit: CaseSearchHit) -> CaseCandidate | None:
    """検索ヒット → 描画可能な候補。出典 URL が http(s) でなければ None（fail-closed）。"""
    record = hit.record
    if record.external_use == "ng" or not record.sources:
        return None
    url = record.sources[0].url.strip()
    if not _is_http_url(url):
        return None
    excerpt = case_record_excerpt(record)
    if not excerpt:
        return None
    title = record.client_masked.strip()
    if record.product.strip():
        title = f"{title}｜{record.product.strip()}"
    score = min(1.0, max(0.0, hit.structure_score / MAX_STRUCTURE_SCORE))
    return CaseCandidate(
        source="case_record",
        title=title,
        url=url,
        excerpt=excerpt,
        score=score,
    )


def search_case_record_candidates(
    conn_or_repo: Any,
    product_meta: Any,
    *,
    max_cases: int = _MAX_CASE_RECORD_SLOTS,
    exclude_client: str | None = None,
    query_embedding: Sequence[float] | None = None,
) -> list[CaseCandidate]:
    """``case_records`` から product_meta（sector / purpose / product_state / regulation）で引く。

    ``conn_or_repo`` は psycopg 接続（store.search_case_records を直接呼ぶ）か、
    ``search_case_records`` メソッドを持つリポジトリ（テスト用フェイク等）。
    """
    if not 1 <= max_cases <= _MAX_CASE_RECORD_SLOTS:
        raise ValueError(f"max_cases must be between 1 and {_MAX_CASE_RECORD_SLOTS}")
    sector = str(_product_meta_value(product_meta, "sector") or "").strip() or None
    purpose_raw = _product_meta_value(product_meta, "purpose") or []
    purpose = [str(p).strip() for p in purpose_raw if str(p).strip()]
    product_state = str(_product_meta_value(product_meta, "product_state") or "").strip() or None
    traits = product_meta_traits(product_meta)
    kwargs: dict[str, Any] = {
        "sector": sector,
        "purpose": purpose,
        "product_state": product_state,
        "traits": traits,
        "query_embedding": query_embedding,
        "exclude_client": exclude_client,
        # 出典欠落・ng で落ちる分を見込んで多めに引く
        "limit": max_cases * 3,
    }
    repo_search = getattr(conn_or_repo, "search_case_records", None)
    hits: Sequence[CaseSearchHit]
    if callable(repo_search):
        hits = repo_search(**kwargs)
    else:
        hits = search_case_records(conn_or_repo, **kwargs)
    candidates: list[CaseCandidate] = []
    seen: set[str] = set()
    for hit in hits:
        candidate = case_candidate_from_hit(hit)
        if candidate is None or candidate.url in seen:
            continue
        seen.add(candidate.url)
        candidates.append(candidate)
        if len(candidates) == max_cases:
            break
    return candidates


def merge_case_candidates(
    record_candidates: Sequence[CaseCandidate],
    rag_candidates: Sequence[CaseCandidate],
    *,
    max_records: int = _MAX_CASE_RECORD_SLOTS,
) -> list[CaseCandidate]:
    """事例レコード候補を **先頭** に最大 ``max_records`` 件差し込み、URL 重複は後ろ側を落とす。"""
    merged: list[CaseCandidate] = []
    seen: set[str] = set()
    for candidate in list(record_candidates)[: max(0, max_records)]:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        merged.append(candidate)
    for candidate in rag_candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        merged.append(candidate)
    return merged


__all__ = [
    "AccountDatabase",
    "AccountDatabaseMeta",
    "AccountProspect",
    "AccountRecord",
    "CaseCandidate",
    "CaseRecordRepository",
    "SearchRunner",
    "SelectedAccount",
    "case_candidate_from_hit",
    "case_record_excerpt",
    "format_case_candidates",
    "load_account_database",
    "load_and_select_accounts",
    "merge_case_candidates",
    "product_meta_traits",
    "search_case_candidates",
    "search_case_record_candidates",
    "select_top_accounts",
]
