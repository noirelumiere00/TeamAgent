"""meeting_prep Skill — 今日の社外商談 1 件の準備レポート（v1＝DM でのオンデマンド）。

10-05 小俣さん依頼「社外商談の 30 分前に準備レポート（会社概要・相手の近況・当社との契約と
過去のやり取り・直近ニュース・当日の確認事項）。出典を添え、不明点は推測せず明記。社内会議・
キャンセル済みは除外」。v1 は DM で「次の商談の準備」と頼まれたときに作る。30 分前の自動
配信（v2）は、個人別の送信時刻（#496）の予約の仕組みに乗せる。

対象の商談の選び方（決定論・LLM なし）:
  - 今日の時刻つき予定のうち、開始が 30 分前より後のもの（終わった商談は選ばない）
  - タスク枠（ゲストも会議リンクも無い予定）は除く（calendar_window.is_personal_block）
  - 社外判定は事例ブリーフと同じ classify_external（internal は除く・uncertain は残す）
  - キャンセル済みは Google の events.list が返さない（showDeleted=false）
  - target があれば予定名か会社名にその語を含むものだけ。無ければ一番早いもの

守ること:
  - DM（本人だけの面）でだけ答える。メールと社内資料を読むため
  - 会社名は予定から読む（extract_client）。読めなければ聞き返す（推測しない）
  - 会社名は client_name_guard を通してから Gmail・Web 検索に使う
  - 材料のどれかが取れなくても、取れた分で作る（取れなかった材料は末尾に書く）
"""

from __future__ import annotations

import datetime as _dt
import os
import unicodedata
from collections.abc import Callable
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.identity import KEY_IDENTITY_VERIFIED, KEY_USER_EMAIL
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.meeting_prep.compose import (
    SYSTEM_PROMPT,
    build_user_message,
    postprocess,
    render_sources,
)
from teamagent.skills.meeting_prep.schema import MeetingPrepInput, MeetingPrepOutput, PrepSource
from teamagent.skills.meeting_prep.sources import (
    CrmLookup,
    Material,
    VaultCrmLookup,
    mail_materials,
    web_materials,
)
from teamagent.skills.morning_digest import calendar_window as _calwin

logger = structlog.get_logger(__name__)

DM_ONLY_MESSAGE = (
    "商談の準備レポートは、メールと社内資料を読むため Aico との DM でだけ作っています。"
)
NOT_CONNECTED_MESSAGE = (
    "カレンダーが Aico とつながっていないため、今日の商談を読めませんでした。"
    "「Google 連携」と送ると、つなぐためのリンクをお送りします。"
)
#: 開始からこの分数までは「まだ準備する商談」として選ぶ（遅れて開いた人にも出す）。
STARTED_GRACE_MINUTES = 30


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _internal_domains(requester: str) -> frozenset[str]:
    """社内ドメイン＝DIGEST_INTERNAL_DOMAIN ∪ 依頼者のメールのドメイン。

    env が空でも、依頼者自身のドメインは必ず社内に数える。数えないと extract_client の
    「社外参加者のドメイン」経路が自社ドメイン（例 vectorinc.co.jp）を相手の会社として返す（実測）。
    """
    from teamagent.skills.pre_meeting_brief.skill import internal_domains

    own = requester.rsplit("@", 1)[-1].strip().lower() if "@" in requester else ""
    return internal_domains() | ({own} if own else set())


@register
class MeetingPrepSkill(BaseSkill[MeetingPrepInput, MeetingPrepOutput]):
    """今日の社外商談 1 件について、出典つきの準備レポートを作る。"""

    name: ClassVar[str] = "meeting_prep"
    description: ClassVar[str] = (
        "Prep report for one of today's external client meetings (商談の準備・アポ前の下調べ): "
        "company overview, recent news, our past deals/contracts and mail with them, and points "
        "to confirm, each with sources. Use for 次の商談の準備して・今日の商談の下調べ・"
        "14時の〇〇社の準備・〇〇との打ち合わせの予習. Call it first; do not ask back for the "
        "company. target = part of the meeting title or the company (empty = next external "
        "meeting). "
        "DM only. Return `message` verbatim."
    )
    input_schema: ClassVar[type[BaseModel]] = MeetingPrepInput
    output_schema: ClassVar[type[BaseModel]] = MeetingPrepOutput
    mcp_relay_fields: ClassVar[tuple[str, ...] | None] = ("message", "error", "total_cost_usd")
    audit_tag: ClassVar[str] = "meeting-prep"

    def __init__(
        self,
        *,
        calendar_factory: Callable[[str], Any] | None = None,
        crm: CrmLookup | None = None,
        search: Any | None = None,
        web: Any | None = None,
        gmail_factory: Callable[[str], Any] | None = None,
        bedrock: Any | None = None,
        now: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self._calendar_factory = calendar_factory
        self._crm = crm
        self._search = search
        self._web = web
        self._gmail_factory = gmail_factory
        self._bedrock = bedrock
        self._now = now or _calwin.now_jst

    # ---- 依存の遅延生成（本番） ----

    def _calendar(self, email: str) -> Any | None:
        if self._calendar_factory is not None:
            return self._calendar_factory(email)
        from teamagent.adapters.gcalendar_client import GCalendarClient
        from teamagent.adapters.gcalendar_readonly import ReadOnlyCalendar
        from teamagent.orchestrator.factory import _build_token_store

        token = _build_token_store().get(email)
        return ReadOnlyCalendar(GCalendarClient.from_user_token(token)) if token else None

    def _crm_lookup(self) -> CrmLookup:
        if self._crm is None:
            if self._search is None:
                from teamagent.orchestrator.factory import build_search_skill_from_env

                self._search = build_search_skill_from_env()
            self._crm = VaultCrmLookup(self._search)
        return self._crm

    def _web_skill(self) -> Any:
        if self._web is None:
            from teamagent.skills.web_research.skill import WebResearchSkill

            self._web = WebResearchSkill()
        return self._web

    def _gmail(self, email: str) -> Any:
        if self._gmail_factory is not None:
            return self._gmail_factory(email)
        from teamagent.orchestrator.factory import _build_token_store
        from teamagent.skills._shared.mail_connection import resolve_gmail_for_user

        return resolve_gmail_for_user(
            _build_token_store(), email, misconfig_message="メールの設定が見つかりません"
        )

    def _llm(self) -> Any:
        if self._bedrock is None:
            from teamagent.adapters.bedrock_client import BedrockClient

            self._bedrock = BedrockClient.from_env()
        return self._bedrock

    # ---- 本体 ----

    def run(self, input: MeetingPrepInput, ctx: SkillContext) -> MeetingPrepOutput:
        meta = ctx.metadata or {}
        email = str(meta.get(KEY_USER_EMAIL, "") or "")
        if not email:
            raise PermissionError("meeting_prep requires user_email")
        if not rollout_allowed("MEETING_PREP_ALLOWED_EMAILS", email):
            return MeetingPrepOutput(message=ROLLOUT_DENIED_MESSAGE, error="rollout_denied")
        verified = meta.get(KEY_IDENTITY_VERIFIED) is True
        if not is_private_surface(meta.get("channel_id"), verified):
            return MeetingPrepOutput(message=DM_ONLY_MESSAGE, error="dm_only")

        try:
            calendar = self._calendar(email)
        except Exception as exc:
            logger.warning(
                "meeting_prep_calendar_failed", request_id=ctx.request_id, error=type(exc).__name__
            )
            calendar = None
        if calendar is None:
            return MeetingPrepOutput(message=NOT_CONNECTED_MESSAGE, error="calendar_not_connected")

        now = self._now()
        picked = self._pick_meeting(calendar, now, input.target, ctx, requester=email)
        if picked is None:
            what = f"「{input.target}」に当てはまる" if input.target else "これからの"
            return MeetingPrepOutput(
                message=f"今日の予定に{what}社外の商談が見つかりませんでした（社内会議・タスク枠は除いています）。",
                error="no_meeting",
            )
        event, _sig, company = picked
        title = str(getattr(event, "summary", "") or "")
        when = self._when(event)
        if not company:
            return MeetingPrepOutput(
                message=(
                    f"{when}「{title}」の相手の会社名を予定から読み取れませんでした。"
                    "「〇〇社の商談の準備」のように会社名を添えてもう一度お願いします。"
                ),
                meeting_title=title,
                error="no_company",
            )
        return self._build(email, title, when, company, ctx)

    def _pick_meeting(
        self, calendar: Any, now: _dt.datetime, target: str, ctx: SkillContext, *, requester: str
    ) -> tuple[Any, Any, str] | None:
        from teamagent.skills.pre_meeting_brief.classify import classify_external, extract_client
        from teamagent.skills.pre_meeting_brief.signals import build_signal_input

        day = now.astimezone(_calwin.JST).date()
        start = _dt.datetime.combine(day, _dt.time.min, tzinfo=_calwin.JST)
        events = calendar.list_events(
            ctx.request_id,
            time_min=start.isoformat(),
            time_max=(start + _dt.timedelta(days=1)).isoformat(),
            max_results=100,
            want_description=True,
        )
        domains = _internal_domains(requester)
        want = _norm(target)
        candidates: list[tuple[_dt.datetime, Any, Any, str]] = []
        for ev in events:
            if bool(getattr(ev, "all_day", False)):
                continue
            begins = _calwin.parse_jst_datetime(str(getattr(ev, "start", "") or ""))
            if begins is None or begins < now - _dt.timedelta(minutes=STARTED_GRACE_MINUTES):
                continue
            if _calwin.is_personal_block(
                getattr(ev, "attendees", None), getattr(ev, "meeting_url", "")
            ):
                continue
            sig = build_signal_input(ev)
            if classify_external(sig, internal_domains=domains) == "internal":
                continue
            hint = extract_client(sig)
            names = [c for c in hint.clients if c.strip().lower() not in domains]
            company = names[0] if names else ""
            if (
                want
                and want not in _norm(str(getattr(ev, "summary", "")))
                and want not in _norm(company)
            ):
                continue
            if not company and want:
                company = target.strip()
            candidates.append((begins, ev, sig, company))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        _, ev, sig, company = candidates[0]
        return ev, sig, company

    @staticmethod
    def _when(event: Any) -> str:
        s = _calwin.parse_jst_datetime(str(getattr(event, "start", "") or ""))
        e = _calwin.parse_jst_datetime(str(getattr(event, "end", "") or ""))
        if s is None:
            return ""
        return s.strftime("%H:%M") + (f"–{e.strftime('%H:%M')}" if e else "")

    def _build(
        self, email: str, title: str, when: str, company: str, ctx: SkillContext
    ) -> MeetingPrepOutput:
        from teamagent.skills._shared.client_name_guard import classify_client_name, to_gmail_phrase

        verdict = classify_client_name(company)
        if verdict.verdict != "ok" or not verdict.search_terms:
            return MeetingPrepOutput(
                message=(
                    f"{when}「{title}」の相手の会社名を確かめられませんでした。"
                    "「〇〇社の商談の準備」のように会社名を添えてもう一度お願いします。"
                ),
                meeting_title=title,
                error="no_company",
            )
        name = verdict.search_terms[0]
        missing: list[str] = []
        cost = 0.0

        web: list[Material] = []
        web_summary = ""
        try:
            web, web_summary = web_materials(self._web_skill(), name, ctx)
        except Exception as exc:
            logger.warning(
                "meeting_prep_web_failed", request_id=ctx.request_id, error=type(exc).__name__
            )
        if not web:
            missing.append("公開情報（会社概要・ニュース）")

        vault: list[Material] = []
        try:
            vault = self._crm_lookup().lookup(name, ctx)
        except Exception as exc:
            logger.warning(
                "meeting_prep_crm_failed", request_id=ctx.request_id, error=type(exc).__name__
            )
        if not vault:
            missing.append("社内資料・案件の記録")

        mail: list[Material] = []
        try:
            mail = mail_materials(self._gmail(email), to_gmail_phrase(name), ctx)
        except Exception as exc:
            logger.warning(
                "meeting_prep_mail_failed", request_id=ctx.request_id, error=type(exc).__name__
            )
        if not mail:
            missing.append("相手とのメール")

        # 番号は web → 金庫 → メールの順（web_research の要約の [n] がそのまま通し番号になる）
        materials = web + vault + mail
        user_message = build_user_message(
            title=title, when=when, company=name, web_summary=web_summary, materials=materials
        )
        try:
            resp = self._llm().converse(
                messages=[{"role": "user", "content": [{"text": user_message}]}],
                request_id=ctx.request_id,
                system=SYSTEM_PROMPT,
                max_tokens=1200,
            )
        except Exception as exc:
            logger.warning(
                "meeting_prep_compose_failed", request_id=ctx.request_id, error=type(exc).__name__
            )
            return MeetingPrepOutput(
                message="準備レポートの作成に失敗しました。少し待ってからもう一度お願いします。",
                meeting_title=title,
                company=name,
                error="compose_failed",
            )
        cost += float(getattr(getattr(resp, "usage", None), "cost_usd", 0.0) or 0.0)
        body = postprocess(
            str(getattr(resp, "text", "") or ""),
            materials=materials,
            grounding_texts=[user_message],
        )
        parts = [f"📋 *商談の準備* {when}「{title}」", f"相手: {name}", "", body]
        if missing:
            parts += ["", "取れなかった材料: " + "・".join(missing)]
        if materials:
            parts += ["", render_sources(materials)]
        message = "\n".join(parts)[:3990]
        logger.info(
            "meeting_prep_done",
            request_id=ctx.request_id,
            materials=len(materials),
            web=len(web),
            vault=len(vault),
            mail=len(mail),
            missing=len(missing),
            cost_usd=round(cost, 4),
        )
        return MeetingPrepOutput(
            message=message,
            meeting_title=title[:200],
            company=name[:120],
            sources=[
                PrepSource(index=i, kind=m.kind, label=m.label[:160], url=m.url[:1000])
                for i, m in enumerate(materials, start=1)
            ],
            total_cost_usd=round(cost, 6),
        )


def enabled() -> bool:
    return os.environ.get("USE_MEETING_PREP_TOOL", "").strip().lower() in {"1", "true", "yes"}
