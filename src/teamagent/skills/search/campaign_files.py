"""施策実績のヒットに、同じ施策の Drive 資料（レポート・提案書）と広告主の業種を添える（純関数）。

背景（2026-10-06 14:12・小俣さんの DM の実例）:
  「飲料メーカーの施策事例」に、Aico は施策実績（ショート動画DBの案件集計・
  campaign_aggregate のシート行）だけを返し、「資料は？」に「実ファイルが Drive に紐づいて
  いない」と答えた。実際には金庫に同じ施策の Drive レポートがあった（例: 広告主名と施策名を
  題名に持つ「〜拡散施策レポート.pptx」）。また飲料ではない広告主の施策が混ざった。

やること:
  1. 施策実績のヒットの広告主（名寄せで法人格・敬称を剥ぐ）で Drive 文書の候補を **1 回の
     SQL** で引き（adapter・RLS は本人の接続のまま＝見えない資料は添えない）、施策名の文字の
     重なりで同じ施策の資料を選んで、上位 2 件を ``related_files`` として添える。
     題名に「レポート」「報告」「提案」を含むもの（または資料種別が報告書・提案書）を優先。
  2. 広告主の業種を Drive 文書の cls_industry / case_industry の最頻値で決める
     （同じ施策の資料があればその中の最頻値・無ければ広告主の資料全体の最頻値）。
  3. 問いに業種語（飲料・食品・化粧品 等）があれば、要約に渡す施策実績をその業種で絞る
     （別の業種は外す・業種が分からないものは「業種不明」として末尾へ）。
  4. 施策の数字を言うときに資料のリンクを併記させ、無い施策は「シートのみ」と書かせる。
     要約が落としたときはコードで末尾に足す（プロンプトだけに任せない）。

本モジュールは env を読まない・DB を引かない（SQL は adapter・env と配線は skill 側）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import normalize_industry
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.search.client_match import normalize_filter_client
from teamagent.util.grapheme_cut import truncate_graphemes

#: 1 つの施策に添える資料の上限。
MAX_FILES_PER_CAMPAIGN = 2
#: 施策名の文字の重なり（施策名の 2 文字組のうち題名にも出る割合）の下限。
#: 「アサイースムージー」と「アサイーバナナスムージー」は 7/8＝0.875、無関係な題名はほぼ 0。
SIMILARITY_MIN = 0.6
#: 業種不明のときの表記（ヘッダ・回答で使う固定語）。
UNKNOWN_INDUSTRY = "業種不明"
#: 関連ファイルが無い施策の書き方（固定語・要約が落としたらコードで足す）。
SHEET_ONLY_NOTE = "施策の数字はシート（ショート動画データベース）のみ"

_KIND_REPORT = "レポート"
_KIND_PROPOSAL = "提案書"
_KIND_OTHER = "資料"
_TITLE_PREFIX = "施策実績 "
_SEP_RE = re.compile(r"[\s　_\-‐‑–—－−・/／|｜()（）\[\]【】「」『』.,，、。:：#＃]+")
_FOOTER_MAX = 3
#: 添える資料名の表示上限（ツール結果の字数予算・OC のツール結果上限 2 万字の内側に収める）。
TITLE_CHARS = 60


def is_campaign_hit(hit: SearchHit) -> bool:
    return str((getattr(hit, "metadata", None) or {}).get("campaign_aggregate") or "") == "true"


def campaign_key(hit: SearchHit) -> tuple[str, str] | None:
    """施策実績のヒットの (広告主, 施策名)。取れなければ None。

    検索 SQL が射影する ``advertiser`` / ``campaign`` を優先し、無ければ題名
    「施策実績 <広告主> <施策名>」（ingest.campaign_aggregate.campaign_title）から復元する。
    """
    meta = getattr(hit, "metadata", None) or {}
    advertiser = str(meta.get("advertiser") or meta.get("cls_project") or "").strip()
    campaign = str(meta.get("campaign") or "").strip()
    if advertiser and not campaign:
        title = str(meta.get("title") or "")
        head = f"{_TITLE_PREFIX}{advertiser} "
        if title.startswith(head):
            campaign = title[len(head) :].strip()
    if not advertiser or not campaign:
        return None
    return advertiser, campaign


def advertiser_pattern(advertiser: str) -> str:
    """広告主名から Drive 資料を探す語（法人格・括弧・敬称を剥いだ形・既存の名寄せ）。"""
    return (normalize_filter_client(advertiser) or advertiser).strip()


def _fold(text: str) -> str:
    return _SEP_RE.sub("", unicodedata.normalize("NFKC", text or "")).casefold()


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def campaign_similarity(campaign: str, title: str) -> float:
    """施策名の 2 文字組のうち題名にも出る割合（0〜1）。1 文字の施策名は包含で判定。"""
    c, t = _fold(campaign), _fold(title)
    if not c or not t:
        return 0.0
    if c in t:
        return 1.0
    grams = _bigrams(c)
    if not grams:
        return 0.0
    return len(grams & _bigrams(t)) / len(grams)


def file_kind(title: str, doc_type: str | None) -> str:
    """資料の種類（レポート / 提案書 / 資料）。題名の語 → 資料種別の順で決める。"""
    text = unicodedata.normalize("NFKC", title or "")
    if "レポート" in text or "報告" in text or doc_type == "報告書":
        return _KIND_REPORT
    if "提案" in text or doc_type == "提案書":
        return _KIND_PROPOSAL
    return _KIND_OTHER


def _industry_of(row: Mapping[str, Any]) -> str | None:
    for key in ("case_industry", "cls_industry"):
        value = normalize_industry(str(row.get(key) or "") or None)
        if value:
            return value
    return None


def _mode_industry(rows: Iterable[Mapping[str, Any]]) -> str | None:
    counts = Counter(i for i in (_industry_of(r) for r in rows) if i)
    if not counts:
        return None
    # 同数は先に出た方（候補は新しい順）。
    return counts.most_common(1)[0][0]


def pick_related_files(
    campaign: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    max_files: int = MAX_FILES_PER_CAMPAIGN,
) -> list[dict[str, str]]:
    """同じ施策の資料を最大 ``max_files`` 件。

    並べ順はレポート・提案書を優先 → 施策名の重なり → 新しさ。
    """
    scored: list[tuple[int, float, str, dict[str, str]]] = []
    seen: set[str] = set()
    for row in candidates:
        title = str(row.get("title") or "").strip()
        uri = str(row.get("source_uri") or "").strip()
        if not title or not uri or uri in seen:
            continue
        similarity = campaign_similarity(campaign, title)
        if similarity < SIMILARITY_MIN:
            continue
        url = hit_doc_url({"source_uri": uri})
        if not url:
            continue
        seen.add(uri)
        kind = file_kind(title, str(row.get("cls_doc_type") or "") or None)
        shown = truncate_graphemes(title, TITLE_CHARS)
        if len(shown) < len(title):
            shown += "…"
        priority = 0 if kind in (_KIND_REPORT, _KIND_PROPOSAL) else 1
        scored.append(
            (
                priority,
                -similarity,
                str(row.get("updated_at") or ""),
                {"title": shown, "url": url, "source_uri": uri, "kind": kind},
            )
        )
    # 新しさは降順にしたいので、優先度・重なりで並べた後に安定ソートで日付を先に効かせる。
    scored.sort(key=lambda x: x[2], reverse=True)
    scored.sort(key=lambda x: (x[0], x[1]))
    return [item[3] for item in scored[:max_files]]


def attach_related_files(
    hits: Sequence[SearchHit], rows: Sequence[Mapping[str, Any]]
) -> tuple[int, int]:
    """施策実績のヒットへ ``related_files`` と ``campaign_industry`` を in-place で添える。

    ``rows`` は adapter の find_drive_files_for_advertisers の結果（``advertiser`` は
    ``advertiser_pattern`` で渡した値）。戻り値は (施策実績の件数, 資料を添えた件数)。
    """
    by_advertiser: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_advertiser.setdefault(str(row.get("advertiser") or ""), []).append(row)
    campaigns = attached = 0
    for hit in hits:
        if not is_campaign_hit(hit):
            continue
        key = campaign_key(hit)
        if key is None:
            continue
        campaigns += 1
        advertiser, campaign = key
        candidates = by_advertiser.get(advertiser_pattern(advertiser), [])
        files = pick_related_files(campaign, candidates)
        matched_rows = [
            r
            for r in candidates
            if campaign_similarity(campaign, str(r.get("title") or "")) >= SIMILARITY_MIN
        ]
        industry = _mode_industry(matched_rows) or _mode_industry(candidates)
        hit.metadata["related_files"] = files
        if industry:
            hit.metadata["campaign_industry"] = industry
        if files:
            attached += 1
    return campaigns, attached


def hit_industry(hit: SearchHit) -> str | None:
    """ヒットの業種（施策実績は添えた広告主の業種・それ以外は cls_industry）。正準値か None。"""
    meta = getattr(hit, "metadata", None) or {}
    return normalize_industry(
        str(meta.get("campaign_industry") or meta.get("cls_industry") or "") or None
    )


def scope_by_industry(hits: Sequence[SearchHit], asked_industry: str | None) -> list[SearchHit]:
    """要約に渡すヒットを問いの業種で絞る（施策実績だけ・他の資料は触らない）。

    - 業種が問いと一致する施策実績はそのまま
    - 業種が分からない施策実績は末尾へ（ヘッダで「業種不明」と明示する）
    - 別の業種の施策実績は外す
    問いに業種語が無ければ何もしない。
    """
    wanted = normalize_industry(asked_industry) if asked_industry else None
    if not wanted:
        return list(hits)
    kept: list[SearchHit] = []
    unknown: list[SearchHit] = []
    for hit in hits:
        if not is_campaign_hit(hit):
            kept.append(hit)
            continue
        industry = hit_industry(hit)
        if industry is None:
            unknown.append(hit)
        elif industry == wanted:
            kept.append(hit)
    return kept + unknown


def campaign_header(hit: SearchHit) -> str:
    """要約器へ渡すチャンクヘッダの施策実績の部分（業種・関連ファイル）。施策実績以外は空。"""
    meta = hit.metadata or {}
    if not is_campaign_hit(hit) or "related_files" not in meta:
        # 添付を試していない（機能 OFF・照会失敗）なら何も主張しない（「無い」と書かない）。
        return ""
    industry = hit_industry(hit) or UNKNOWN_INDUSTRY
    files = meta.get("related_files") or []
    if files:
        listed = " ／ ".join(f"『{f['title']}』 {f['url']}（{f['kind']}）" for f in files)
        return f", 業種: {industry}, 関連ファイル: {listed}"
    return f", 業種: {industry}, 関連ファイル: なし（{SHEET_ONLY_NOTE}）"


def links_footer(hits: Sequence[SearchHit], answer: str) -> str:
    """要約が施策の資料リンク・「シートのみ」を落としたときに末尾へ足す行（無ければ空）。"""
    lines: list[str] = []
    sheet_only: list[str] = []
    for hit in hits:
        if not is_campaign_hit(hit) or "related_files" not in (hit.metadata or {}):
            continue
        key = campaign_key(hit)
        label = f"{key[0]} {key[1]}" if key else str((hit.metadata or {}).get("title") or "施策")
        files = (hit.metadata or {}).get("related_files") or []
        if files:
            if not any(f["url"] in answer for f in files):
                links = " ／ ".join(f"『{f['title']}』 {f['url']}" for f in files)
                lines.append(f"- {label}: {links}")
        elif SHEET_ONLY_NOTE not in answer:
            sheet_only.append(label)
        if len(lines) + len(sheet_only) >= _FOOTER_MAX:
            break
    parts: list[str] = []
    if lines:
        parts.append("📎 施策のレポート・提案書\n" + "\n".join(lines))
    if sheet_only:
        parts.append(f"{SHEET_ONLY_NOTE}: " + "、".join(sheet_only))
    return "\n\n".join(parts)


__all__ = [
    "MAX_FILES_PER_CAMPAIGN",
    "SHEET_ONLY_NOTE",
    "SIMILARITY_MIN",
    "UNKNOWN_INDUSTRY",
    "advertiser_pattern",
    "attach_related_files",
    "campaign_header",
    "campaign_key",
    "campaign_similarity",
    "file_kind",
    "hit_industry",
    "is_campaign_hit",
    "links_footer",
    "pick_related_files",
    "scope_by_industry",
]
