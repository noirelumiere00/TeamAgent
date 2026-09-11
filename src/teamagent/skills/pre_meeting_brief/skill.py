"""pre_meeting_brief Skill 本体 — 当日の社外 MTG に効く実績を引いて並べる。

死守ライン（いずれも構造で担保・テストで固定）:
  G1 本人限定: ``user_email`` 必須。無ければ ``PermissionError``（fail-closed）。
  書込ゼロ: ``GCalendarClient`` を受け取らない。受けるのは ``ReadOnlyCalendar``
    facade（``list_events`` 以外は AttributeError）。denylist は ``events.insert`` を
    意図的に通しているので根拠にならない。
  RLS: ``build_rls_metadata()`` を唯一の変換点にし、``user_groups`` を **必ず** 渡す。
    落とすと ``_apply_session`` が GUC を立てず、domain 共有の事例が誰にも 0 件になる。
  宛先: ``is_private_surface`` が真のときだけ本文を描く（deny-by-default）。空文字・
    ``C``/``G``・未知 prefix は件数も社名も出さない。
  LLM 非経由: Bedrock / Gemini / Embedder への参照が 0 件（AST テストで固定）。
  情報最小化: 生 description は schema にも出力にもログにも載せない。参加者はドメインのみ。
  外部送信ゼロ: Slack も Gmail も呼ばない。触るのは金庫の read だけ。
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, ClassVar

import structlog

from teamagent.identity import KEY_IDENTITY_VERIFIED, KEY_USER_EMAIL, build_rls_metadata
from teamagent.observability.sentry import scrub_value
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.morning_digest import calendar_window as _calwin
from teamagent.skills.pre_meeting_brief.classify import (
    ClientHint,
    classify_external,
    external_use,
    extract_client,
    is_usable_partial,
    ng_note,
    normalize_company,
)
from teamagent.skills.pre_meeting_brief.render import MASTER_SHEET_SOURCE, harden
from teamagent.skills.pre_meeting_brief.schema import (
    CaseRef,
    PreMeetingBriefInput,
    PreMeetingBriefItem,
    PreMeetingBriefOutput,
)
from teamagent.skills.pre_meeting_brief.signals import (
    BriefSignals,
    build_signal_input,
    signals_from_item,
)

logger = structlog.get_logger(__name__)

#: 面ガードで伏せたときの定型文。件数も社名も含まない（ここが漏れ口になる）。
SURFACE_BLOCKED_MESSAGE = (
    "事例ブリーフはご本人の DM でだけお出ししています。Aico との DM で聞いてください。"
)
#: 事例集が未取込のとき。**利用者には配信せず** 運用ログにだけ落とす文言。
CORPUS_MISSING_REASON = "case_corpus_not_ingested"


def internal_domains() -> frozenset[str]:
    """社内ドメイン集合（``DIGEST_INTERNAL_DOMAIN``・カンマ区切り）。"""
    raw = os.environ.get("DIGEST_INTERNAL_DOMAIN", "").strip()
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _target_date(relative_day: str, today: _dt.date) -> _dt.date:
    value = (relative_day or "today").strip().lower()
    if value == "tomorrow":
        return today + _dt.timedelta(days=1)
    if value and value != "today":
        parsed = _calwin.parse_jst_date(value)
        if parsed is not None:
            return parsed
    return today


@register
class PreMeetingBriefSkill(BaseSkill[PreMeetingBriefInput, PreMeetingBriefOutput]):
    """当日の社外 MTG ごとに、効きそうな自社実績を金庫から引いて並べる Skill。"""

    name: ClassVar[str] = "pre_meeting_brief"
    description: ClassVar[str] = (
        "今日/明日の社外 MTG を予定から拾い、相手企業に効きそうな自社の実施事例を"
        "社内の事例集から引いて並べるツール。カレンダーは読むだけで、予定の作成・変更・"
        "出欠応答は一切しない（引数が存在しない）。メール送信・下書き作成もしない。"
        "企業名を直接指定して引くこともできる。"
    )
    input_schema: ClassVar[type[PreMeetingBriefInput]] = PreMeetingBriefInput
    output_schema: ClassVar[type[PreMeetingBriefOutput]] = PreMeetingBriefOutput

    def __init__(
        self,
        *,
        calendar: Any | None = None,
        pg: Any | None = None,
        events: list[Any] | None = None,
    ) -> None:
        """``calendar`` は **read-only facade** のみ（``GCalendarClient`` を渡さない）。

        ``events`` は定期便経路の注入口（morning_digest が既に取得済みの
        ``CalendarEventItem`` をそのまま渡す＝Google API 呼び出しを 1 回も増やさない）。
        """
        self._calendar = calendar
        self._pg = pg
        self._injected_events = events

    # ── 入口 ──────────────────────────────────────────────────────────
    def run(self, input: PreMeetingBriefInput, ctx: SkillContext) -> PreMeetingBriefOutput:
        meta = dict(ctx.metadata or {})
        rls = build_rls_metadata(meta.get(KEY_USER_EMAIL))
        if rls is None:
            # G1: 本人が確定できないなら何も出さない（fail-closed）。
            raise PermissionError("pre_meeting_brief: user_email が必要です")

        # 宛先ガード（deny-by-default）。channel_id が無い／DM でないなら定型文だけ。
        channel_id = str(meta.get("channel_id") or "")
        verified = bool(meta.get(KEY_IDENTITY_VERIFIED, True))
        # 定期便（runner）は channel を metadata に持たず、配信側で本人 DM を解決し直す。
        # そのため「注入経路（events あり）」は面ガードの対象外＝本人 DM 固定。
        if self._injected_events is None and not is_private_surface(channel_id, verified):
            logger.info(
                "pre_meeting_brief_surface_blocked",
                request_id=ctx.request_id,
                has_channel=bool(channel_id),
            )
            return PreMeetingBriefOutput(message=SURFACE_BLOCKED_MESSAGE, scanned=False)

        today = _calwin.now_jst().date()
        day = _target_date(input.relative_day, today)

        signals = self._gather_signals(input, ctx, day)
        domains = internal_domains()
        pairs = [(sig, classify_external(sig, internal_domains=domains)) for sig in signals]
        targets: list[tuple[BriefSignals, str]] = [
            (s, str(v)) for s, v in pairs if v in ("external", "uncertain")
        ]

        out = PreMeetingBriefOutput(
            date=day.isoformat(),
            scanned=True,
            external_count=sum(1 for _, v in targets if v == "external"),
            uncertain_count=sum(1 for _, v in targets if v == "uncertain"),
        )
        try:
            self._fill_cases(out, targets[: input.max_meetings], input, ctx)
        except Exception as exc:  # 節は独立 try/except（digest 本体を落とさない）
            out.errors.append(f"cases:{type(exc).__name__}")
            logger.warning("pre_meeting_brief_cases_failed", request_id=ctx.request_id)

        # ⚠️ ログは件数・判定内訳・request_id のみ（社名・MTG名・URL・ドメインは出さない）。
        # ``cases`` は監視の要: RLS の穴を踏むと症状が「毎朝 0 件で正常終了」になり、
        # error alarm には合流しない。cases=0 かつ external>0 を専用計で拾う。
        logger.info(
            "pre_meeting_brief_done",
            request_id=ctx.request_id,
            external=out.external_count,
            uncertain=out.uncertain_count,
            items=len(out.items),
            cases=sum(len(i.cases) for i in out.items),
            corpus=out.corpus_available,
        )
        return out

    # ── 予定 → BriefSignals ───────────────────────────────────────────
    def _gather_signals(
        self, input: PreMeetingBriefInput, ctx: SkillContext, day: _dt.date
    ) -> list[BriefSignals]:
        if input.client:
            # 企業名の直接指定はカレンダーを介さない（取りこぼしの自力回復口）。
            return [
                BriefSignals(
                    title=input.client,
                    has_client_line=True,
                    client_hint=input.client,
                    attendee_list_available=True,
                )
            ]
        if self._injected_events is not None:
            # 定期便経路: morning_digest が build_signal_input で作った派生値を復元するだけ。
            return [signals_from_item(item) for item in self._injected_events]
        if self._calendar is None:
            return []
        window_start = _dt.datetime.combine(day, _dt.time.min, tzinfo=_calwin.JST)
        window_end = window_start + _dt.timedelta(days=1)
        events = self._calendar.list_events(
            ctx.request_id,
            time_min=window_start.isoformat(),
            time_max=window_end.isoformat(),
            max_results=100,
            want_description=True,
        )
        return [build_signal_input(ev) for ev in events]

    # ── 引き当て ──────────────────────────────────────────────────────
    def _fill_cases(
        self,
        out: PreMeetingBriefOutput,
        targets: list[tuple[BriefSignals, str]],
        input: PreMeetingBriefInput,
        ctx: SkillContext,
    ) -> None:
        if not targets:
            # 0 件でも「事例集が入っているか」は判定する（節を出すかの分岐に要る）。
            out.corpus_available = self._corpus_available(ctx)
            return
        pg = self._ensure_pg()
        if pg is None:
            out.errors.append("cases:no_pg")
            return
        rls = build_rls_metadata((ctx.metadata or {}).get(KEY_USER_EMAIL))
        if rls is None:
            raise PermissionError("pre_meeting_brief: user_email が必要です")
        sources: list[str] = []
        with pg.connection(
            app_role="teamagent_app",
            user_email=rls["user_email"],
            # ⚠️ user_groups を落とすと GUC が立たず domain 共有の事例が 0 件になる。
            user_groups=rls["user_groups"],
            user_role=rls["user_role"],
        ) as conn:
            out.corpus_available = pg.case_corpus_available(conn, ctx.request_id)
            if not out.corpus_available:
                return
            for sig, verdict in targets:
                out.items.append(self._build_item(conn, pg, sig, verdict, input, ctx, sources))
        if out.items:
            sources.insert(0, MASTER_SHEET_SOURCE)
        seen: list[str] = []
        for src in sources:
            if src and src not in seen:
                seen.append(src)
        out.source_lines = seen[:6]

    def _build_item(
        self,
        conn: Any,
        pg: Any,
        sig: BriefSignals,
        verdict: str,
        input: PreMeetingBriefInput,
        ctx: SkillContext,
        sources: list[str],
    ) -> PreMeetingBriefItem:
        hint: ClientHint = extract_client(sig)
        clients = [normalize_company(c) or c for c in hint.clients]
        industries: list[str] = []
        cases: list[CaseRef] = []
        no_exact = ""
        # ⚠️ 社ごとの枠を切る。全社を max_cases で引いて連結してから先頭で切ると、
        #   1 社目が枠を食い尽くして **2 社目が丸ごと消える**（利用者は落ちたことにも
        #   気づけない）。PLAN「複数社連記は…事例は各1件」・DELTA §3 の実物例
        #   （[A系]1行＋[B系]1行）は、この枠が無いと構造的に再現できない。
        per_client = max(1, input.max_cases // max(1, len(clients)))
        for client in clients:
            industry = pg.get_industry_for_client(conn, client, ctx.request_id)
            industries.append(industry or "")
            found, exact, stage = self._lookup(conn, pg, client, industry, per_client, ctx)
            if not exact and found and industry:
                no_exact = (
                    f"「{client}」自体の実施事例はDrive上で確認できず（{industry}で近い実績）。"
                )
            for row in found:
                cases.append(self._to_case(row, client=client, group=client, stage=stage))
        # ⚠️ 出典は **実際に描く事例** だけを載せる（切り詰めた先の資料名を出典に並べると、
        #    利用者は本文に無い資料名を見て「どこに出ているのか」を探すことになる）。
        shown = cases[: input.max_cases]
        for case in shown:
            if case.source_title:
                sources.append(case.source_title)
        return PreMeetingBriefItem(
            start_at=sig.start_at,
            end_at=sig.end_at,
            title_display=harden(sig.title, 120),
            title_scrubbed=str(scrub_value(sig.title))[:120],
            verdict=verdict,
            clients_display=[harden(c, 40) for c in clients],
            clients_scrubbed=[str(scrub_value(c))[:40] for c in clients],
            client_industries=industries,
            agency_display=harden(hint.agency_display, 60),
            agency_scrubbed=str(scrub_value(hint.agency_display))[:60],
            attendee_domains=list(sig.attendee_domains)[:10],
            cases=shown,
            no_exact_note=no_exact,
        )

    def _lookup(
        self,
        conn: Any,
        pg: Any,
        client: str,
        industry: str | None,
        limit: int,
        ctx: SkillContext,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """段1 → 段2 → 段3 → 段4。当たった段で **打ち切る**（下の段の SQL を発行しない）。

        戻り値: (行, 完全一致/部分一致で当たったか, 当たった段)。
        ⚠️ 段は ``CaseRef.match_stage`` へそのまま載せる。段を捨てると「段3/4 に落ちて
        精度が悪化している」が出力にも監視にも残らず、後追いできない。
        """
        if not client:
            return ([], False, 0)
        rows = pg.list_case_studies(
            conn, client_name=client, limit=limit, stage=1, request_id=ctx.request_id
        )
        if rows:
            return (rows, True, 1)
        if is_usable_partial(client):
            rows = pg.list_case_studies(
                conn, client_name=client, limit=limit, stage=2, request_id=ctx.request_id
            )
            if rows:
                return (rows, True, 2)
        # ⚠️ 業種が金庫に無ければ段3/4 を **実行しない**（業種を推測しない）。
        if not industry:
            return ([], False, 0)
        rows = pg.list_case_studies(
            conn,
            industry=industry,
            product=client,
            limit=limit,
            stage=3,
            request_id=ctx.request_id,
        )
        if rows:
            return (rows, False, 3)
        rows = pg.list_case_studies(
            conn, industry=industry, limit=limit, stage=4, request_id=ctx.request_id
        )
        return (rows, False, 4 if rows else 0)

    def _to_case(self, row: dict[str, Any], *, client: str, group: str, stage: int) -> CaseRef:
        """マスター表の構造化列だけから 1 件を組む（pptx の chunk 本文を使わない）。"""
        company = str(row.get("case_client") or row.get("title") or "")
        owner = str(row.get("case_owner") or "").strip()
        if not owner:
            email = str(row.get("owner_email") or "")
            owner = email.split("@", 1)[0] if "@" in email else ""
        use = external_use(row.get("case_external_use"))
        same = normalize_company(company) == normalize_company(client) and bool(client)
        return CaseRef(
            company_display=harden(company, 60),
            company_scrubbed=str(scrub_value(company))[:60],
            product_display=harden(row.get("case_product"), 60),
            product_scrubbed=str(scrub_value(row.get("case_product") or ""))[:60],
            industry_display=harden(row.get("case_industry"), 40),
            effect_display=harden(row.get("case_effect"), 160),
            effect_scrubbed=str(scrub_value(row.get("case_effect") or ""))[:160],
            owner_display=harden(owner, 40),
            external_use=use,
            external_use_note=harden(
                ng_note(use, str(row.get("case_external_use_note") or "")), 60
            ),
            source_title=harden(row.get("title"), 120),
            # URL は source_uri の実値のみ（文字列連結で作らない）。
            source_uri=str(row.get("source_uri") or "")[:600],
            match_stage=stage,
            client_group=harden(group, 40),
            same_client=same,
        )

    # ── 金庫 ─────────────────────────────────────────────────────────
    def _ensure_pg(self) -> Any:
        if self._pg is None:
            try:
                from teamagent.adapters.pgvector_client import PgVectorClient

                self._pg = PgVectorClient.from_env()
            except Exception:  # 金庫が引けない日も digest 本体は配る
                return None
        return self._pg

    def _corpus_available(self, ctx: SkillContext) -> bool:
        pg = self._ensure_pg()
        if pg is None:
            return False
        rls = build_rls_metadata((ctx.metadata or {}).get(KEY_USER_EMAIL))
        if rls is None:
            return False
        try:
            with pg.connection(
                app_role="teamagent_app",
                user_email=rls["user_email"],
                user_groups=rls["user_groups"],
                user_role=rls["user_role"],
            ) as conn:
                return bool(pg.case_corpus_available(conn, ctx.request_id))
        except Exception:
            return False


__all__ = ["CORPUS_MISSING_REASON", "SURFACE_BLOCKED_MESSAGE", "PreMeetingBriefSkill"]
