"""準備レポートの材料集め（金庫・メール・公開情報）。

材料 1 件＝出典 1 件。番号は集めた順に機械的に振り、LLM には番号つきで渡す。LLM の出力に
URL があっても採用しない（出典の URL は常にここで持っているもの）。

CRM（Salesforce）は当面使えない（10-05 小俣さん）。``CrmLookup`` を 1 つの口にしておき、
いまは金庫（#proj-01 案件決定・案件決定 V2 シート・Drive の提案書や契約書）を引く
``VaultCrmLookup`` で代える。Salesforce が開放されたら同じ口の実装を 1 つ足すだけでよい。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from teamagent.skills.base import SkillContext
from teamagent.skills.meeting_prep.schema import MaterialKind

logger = structlog.get_logger(__name__)

#: 1 材料あたり LLM に渡す本文の上限（文字）。長い資料 1 本で他の材料が押し出されないように。
MATERIAL_TEXT_MAX = 600
#: メールを遡る日数。
MAIL_LOOKBACK_DAYS = 180


@dataclass(frozen=True)
class Material:
    kind: MaterialKind
    label: str  # 出典欄に出す名前（資料名・件名・ページ名）
    url: str  # 開ける URL（無ければ空）
    text: str  # LLM に渡す本文（上限で切る）


class CrmLookup(Protocol):
    """会社名 → 当社との契約・案件・やり取りの材料。Salesforce の実装はここに足す。"""

    def lookup(self, company: str, ctx: SkillContext) -> list[Material]: ...


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MATERIAL_TEXT_MAX else text[:MATERIAL_TEXT_MAX] + "…"


class VaultCrmLookup:
    """金庫の検索（search skill・RLS つき）で CRM の代わりをする。

    会社名で絞った検索（filter_client）と、会社名を含む語での検索の 2 回を引いて重ねる。
    #proj-01 の案件決定投稿は client_name が付いていないことがあるため、絞り込みだけだと漏れる。
    """

    def __init__(self, search: Any, *, top_k: int = 6) -> None:
        self._search = search
        self._top_k = top_k

    def lookup(self, company: str, ctx: SkillContext) -> list[Material]:
        from teamagent.skills.search.schema import SearchInput

        queries = [
            SearchInput(
                query=f"{company} 案件 受注 契約 提案",
                filter_client=company,
                include_answer=False,
                top_k=self._top_k,
            ),
            SearchInput(
                query=f"{company} 案件決定 発注 見積 契約", include_answer=False, top_k=self._top_k
            ),
        ]
        out: list[Material] = []
        seen: set[str] = set()
        for q in queries:
            try:
                result = self._search.run(q, ctx)
            except Exception as exc:  # 金庫が落ちていても他の材料で続ける
                logger.warning(
                    "meeting_prep_vault_failed",
                    request_id=ctx.request_id,
                    error=type(exc).__name__,
                )
                continue
            for hit in getattr(result, "hits", []) or []:
                key = str(getattr(hit, "url", "") or "") or f"chunk:{getattr(hit, 'chunk_id', '')}"
                if key in seen:
                    continue
                seen.add(key)
                label = (
                    getattr(hit, "title", None)
                    or getattr(hit, "file_name", None)
                    or getattr(hit, "channel_name", None)
                    or "社内資料"
                )
                updated = str(getattr(hit, "updated_at", "") or "")[:10]
                out.append(
                    Material(
                        kind="vault",
                        label=f"{label}（{updated}）" if updated else str(label),
                        url=str(getattr(hit, "url", "") or ""),
                        text=_clip(str(getattr(hit, "content", "") or "")),
                    )
                )
        return out[: self._top_k]


def mail_materials(
    gmail: Any, phrase: str, ctx: SkillContext, *, max_results: int = 6
) -> list[Material]:
    """相手とのメール（件名・日付・差出人・冒頭の一部）。本文は読まない（metadata のみ）。"""
    refs, _ = gmail.list_messages(
        f"{phrase} newer_than:{MAIL_LOOKBACK_DAYS}d", ctx.request_id, max_results=max_results
    )
    out: list[Material] = []
    for ref in refs[:max_results]:
        msg = gmail.get_message(ref.id, ctx.request_id, format="metadata")
        headers = getattr(msg, "headers", {}) or {}
        subject = str(headers.get("Subject", "") or "（件名なし）")
        sent = ""
        if getattr(msg, "internal_date_ms", None):
            sent = _dt.datetime.fromtimestamp(
                msg.internal_date_ms / 1000, tz=_dt.timezone(_dt.timedelta(hours=9))
            ).strftime("%Y-%m-%d")
        sender = str(headers.get("From", "") or "")
        out.append(
            Material(
                kind="mail",
                label=f"メール「{subject[:60]}」（{sent}）"
                if sent
                else f"メール「{subject[:60]}」",
                url=f"https://mail.google.com/mail/u/0/#all/{ref.thread_id}",
                text=_clip(f"{sent} {sender} 件名:{subject} {getattr(msg, 'snippet', '')}"),
            )
        )
    return out


def web_materials(web: Any, company: str, ctx: SkillContext) -> tuple[list[Material], str]:
    """会社概要と直近のニュース（web_research・Google 検索グラウンディング）。

    戻り値の 2 つ目は web_research の要約本文（出典番号つき）。出典 1 件ずつを材料にして、
    要約本文は最初の材料にまとめて載せる（番号は 1 本にそろえる）。
    """
    from teamagent.skills.web_research.schema import WebResearchInput

    result = web.run(
        WebResearchInput(query=f"{company} 会社概要 最新ニュース", max_results=6, recency_days=180),
        ctx,
    )
    if getattr(result, "error", ""):
        return [], ""
    out = [
        Material(
            kind="web",
            label=str(getattr(s, "title", "") or getattr(s, "domain", "") or "公開情報"),
            url=str(getattr(s, "url", "") or ""),
            text="",
        )
        for s in getattr(result, "sources", []) or []
    ]
    return out, str(getattr(result, "message", "") or "")
