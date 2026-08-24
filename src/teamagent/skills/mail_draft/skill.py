"""mail_draft Skill 本体 — 「本人が選んだ 1 件」に返信下書きを作る（送信はしない）。

入口は 2 つ。どちらも **本人が明示的に選んだ**ものにだけ作る（勝手に量産しない）:

1. **ボタン押下**: 朝ダイジェストの「✏️ 下書きを作成」→ OpenClaw(socket) が system event として
   エージェントへ転送 → SOUL 指示で本ツールを呼ぶ（value=署名トークンを ``draft_token`` に渡す）。
2. **一覧からの選択**（2026-08-21 裁定 B/C）: mail_followup が受信箱全体から出した候補一覧に
   対して、本人が「1番で」「1と3、丁寧めで」と答えた **その文字列**を ``selection`` に渡す。
   本 Skill が判定層 :func:`~teamagent.skills._shared.inbox_triage.parse_selection` で
   同定し、**曖昧なら下書きを作らずに聞き返す**（推測で別の相手に下書きを作らない）。

裁定 B の「選んだ件だけ中身まで読む」は、既存の :class:`MailReplySkill` をそのまま
起草エンジンとして呼ぶことで満たす（本文・スレッド全文・社内 Slack 文脈は mail_reply が
既に持っている）。本 Skill が足すのは **選択の同定**と **同じ相手との別スレッド過去メール**
の有効化だけで、起草・下書き保存のロジックは 1 行も複製しない。

⚠️ 死守ライン:
  G1 本人受信箱限定（user_email→token, fail-closed）。未連携は error で案内。
  トークン検証: 署名・所有者照合・失効を decode_draft_token が担保（fail-closed）。
  G4' 書込は drafts.create のみ（**送信はアダプタ層 denylist で物理封鎖**）。
     連打/コスト対策に 1人10件/日（selection で複数件選んでも 1 件ずつ消費する）。
  G3 生 thread_id/件名/本文は value・ログに出さない（token と open_url のみ）。
     drafts[] の本文・返信先は **本人にだけ ephemeral 表示**される前提の戻り値。
"""

from __future__ import annotations

import datetime as _dt
import threading as _threading
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gmail_client import GmailClient
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills._shared.inbox_triage import (
    DEFAULT_LIMIT,
    InboxMailMeta,
    TriageCandidate,
    format_sender,
    format_subject,
    mentions_number,
    parse_selection,
    render_triage_message,
)
from teamagent.skills._shared.mail_compose import env_bool
from teamagent.skills._shared.mail_connection import (
    MESSAGE_BY_CONNECTION_ERROR,
    MailConnectionError,
    classify_gmail_failure,
    resolve_gmail_for_user,
)
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.mail_draft.schema import DraftedMail, MailDraftInput, MailDraftOutput
from teamagent.skills.mail_followup.skill import (
    TRIAGE_SCAN_DEFAULT,
    VANISHED_REF,
    InboxScan,
    MailFollowupSkill,
    evidence_ref,
    recall_scan,
    remember_scan,
)
from teamagent.skills.mail_reply.schema import MailReplyInput
from teamagent.skills.morning_digest.draft_token import decode_draft_token

logger = structlog.get_logger(__name__)

_MISCONFIG_MSG = "TokenStore が未設定です（mail_draft は本人連携前提）"

# generate_draft_for_thread の error → 本人向け案内文（ephemeral）。
_ERR_MSG: dict[str, str] = {
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "quota": "本日の下書き作成上限（10件/日）に達しました。明日また利用できます。",
    "not_connected": "下書き作成には Google の連携が必要です"
    "（@NewsTV AI に『連携』と話しかけて許可してください）。",
    "reauth_needed": "下書き作成には Google の再連携が必要です"
    "（下書き作成権限を許可してください）。",
    "not_addressed": "このスレッドはご本人宛（To）ではないため下書きは作成しません。",
    "thread_gone": "対象のスレッドが見つかりませんでした。",
    "thread_error": "スレッドの取得に失敗しました。時間をおいて再度お試しください。",
    "invalid_thread": "ボタンの情報が不正です。最新のダイジェストから操作してください。",
    "not_draftable": "下書きを作成できませんでした（返信先不明/一斉送信 等）。",
    # ── selection 経路 ────────────────────────────────────────────────────
    "no_selection": "どの件の下書きを作るか分かりませんでした。"
    "『返信が止まっているメール』と聞いていただくと候補をお出しします。",
    "no_candidates": "返信が止まっているメールが見当たらないため、下書きは作りませんでした。",
    "gmail_api_failed": "受信箱の確認に失敗しました。時間をおいて再度お試しください"
    "（メールが 0 件という意味ではありません）。",
}
_OK_MSG = (
    "✅ 返信下書きを作成しました（未送信・Slackでは送信しません）。"
    "Gmail で確認して送信してください。"
)
_ALREADY_MSG = "✏️ この案件は既に下書きがあります。Gmail で確認してください。"

# ── selection 経路の固定文言（自由文生成はしない）──────────────────────────

#: 1 回の呼び出しで作る下書きの上限（コストと「押し間違いの被害」を頭打ちにする）。
MAX_SELECTED = 3

_AMBIGUOUS_HEAD = (
    "どの件のことか確定できなかったので、**下書きはまだ作っていません**"
    "（取り違えると別のお客様に返信案を作ってしまうため、推測しません）。"
)
#: 番号は「一覧の何行目か」でしかない。その一覧（candidate_refs）を渡されないまま
#: 番号を解釈すると、再走査で並びが変わっていた場合に **別のお客様へ下書きを作る**。
#: ピン留めが唯一の防波堤なので、無いときは推測せず出し直す（裁定の「推測で決めない」）。
_NO_REFS_HEAD = (
    "その番号がどの件を指しているか確認できなかったので、**下書きはまだ作っていません**"
    "（別のお客様に返信案を作ってしまうため、番号だけでは決めません）。"
    "お手数ですが、下の一覧の番号でもう一度お知らせください。"
)
_TRUNCATED_NOTE = (
    f"※ 一度に作るのは {MAX_SELECTED} 件までにしました。残りは改めてお知らせください。"
)
_MISSING_NOTE = (
    "※ 選んでいただいた番号のうち、受信箱で見つからなくなっていたものがありました"
    "（ご自身で返信済み・移動された等）。その件の下書きは作っていません。"
)

#: 一覧に出したが今は候補でなくなった位置の埋め草。**番号の位置をずらさない**ための存在で、
#: 差出人も件名も空なので :func:`parse_selection` の名前照合には決して当たらない。
#: 番号でここを指されたら「その件は見つからなかった」と正直に返す（別の件へ繰り上げない）。
_VANISHED = TriageCandidate(mail=InboxMailMeta(thread_id=""), idle_days=0, score=0)

# ── 1人1日あたりの上限（プロセス内で共有する）──────────────────────────────
#
# ⚠️ ここを Skill のインスタンス変数に持たせてはいけない。本番は呼び出しごとに
# ``ToolSpec.instantiate()`` で作り直される（orchestrator/tools.py・mcp_gateway/server.py）ため、
# インスタンス変数のカウンタは毎回 0 に戻り **上限が 1 度も効かない**（連続実行で全部作れる）。
# プロセス（＝ECS タスク）単位の頭打ちなので、タスクが複数あれば全体では上限×タスク数まで
# 通りうる。完全な保証ではなく「暴走の頭打ち」であることを正直に書いておく。
_DAILY_COUNTS: dict[str, tuple[str, int]] = {}
_QUOTA_LOCK = _threading.Lock()


def reset_daily_quota() -> None:
    """プロセス内の日次カウンタを空にする（テスト間の独立性・運用上の緊急退避用）。"""
    with _QUOTA_LOCK:
        _DAILY_COUNTS.clear()


_DRAFT_HEAD = "✅ Gmail の下書きに保存しました（**送信はしていません**）。"
_DRAFT_FOOT = "内容をご確認のうえ、ご自身で送信してください。直したい点があれば言ってください。"


@register
class MailDraftSkill(BaseSkill[MailDraftInput, MailDraftOutput]):
    """本人が選んだ 1 件へ返信下書きを作る Skill（ボタン押下 / 一覧からの選択・送信は人間）。"""

    name: ClassVar[str] = "mail_draft"
    description: ClassVar[str] = (
        "本人が選んだメールに返信下書きを作る（Gmail の下書き保存のみ・送信はしない）。"
        "入口は 2 つ。"
        "(1) 朝ダイジェストの『✏️ 下書きを作成』ボタン押下（action='mail_draft'）を受けたら、"
        "その value（署名トークン）を draft_token に渡す。"
        "(2) **mail_followup が返した候補一覧に対して本人が『1番で』『1と3、丁寧めで』"
        "『〇〇の件』と答えたら、その返事をそのまま selection に渡す**"
        "（言い換え・番号への変換をしない）。あわせて直前の items[].evidence_ref を"
        "**表示順のまま candidate_refs に必ず入れる**（番号は一覧の位置でしかないので、"
        "これが無いと番号は解釈されず候補を出し直す）。mail_followup が返した "
        "lookback_days があれば同じ値を渡し、トーン等の希望は instructions に入れる。"
        "曖昧な指定なら error='ambiguous_selection' で**下書きを作らずに聞き返す**ので、"
        "message をそのまま出し、次の呼び出しには戻り値の candidate_refs をそのまま渡す。"
        "成功時は drafts[] の本文と open_url を原文のまま併記する。"
        "候補の一覧を出すのは mail_followup、条件を指定した返信起草は mail_reply。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = MailDraftInput
    output_schema: ClassVar[type[BaseModel]] = MailDraftOutput

    _QUOTA_LIMIT: ClassVar[int] = 10

    def __init__(
        self,
        token_store: TokenStore | None = None,
        *,
        deal_provider: Any | None = None,
        gmail: GmailClient | None = None,
        reply: Any | None = None,
        now_ms: int | None = None,
    ) -> None:
        self._token_store = token_store
        # 本人 Slack 文脈プロバイダ（mail_reply へそのまま渡す。未注入なら Slack は見ない）。
        self._deal_provider = deal_provider
        # 一覧の再走査に使う readonly クライアント（テスト注入用。本番は TokenStore から）。
        self._gmail = gmail
        # 起草エンジン（テスト注入用。本番は MailReplySkill を都度構築）。
        self._reply = reply
        self._now_ms = now_ms

    def run(self, input: MailDraftInput, ctx: SkillContext) -> MailDraftOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人受信箱限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("mail_draft は本人 user_email が必須です（本人受信箱限定）")

        if input.selection.strip():
            return self._run_selection(input, ctx, requester=requester, log=log)
        if not input.draft_token.strip():
            # どちらの入口でもない＝何を作るか決まっていない。推測で作らない。
            log.info("mail_draft_no_selection")
            return MailDraftOutput(error="no_selection", message=_ERR_MSG["no_selection"])
        return self._run_token(input, ctx, requester=requester, log=log)

    # ── 入口 1: ボタン押下（既存経路・挙動は変えない）────────────────────────

    def _run_token(
        self, input: MailDraftInput, ctx: SkillContext, *, requester: str, log: Any
    ) -> MailDraftOutput:
        # トークン検証（署名・所有者・失効）。生 thread_id はここで初めて復元（ログには出さない）。
        thread_id = decode_draft_token(input.draft_token, requester)
        if not thread_id:
            log.info("mail_draft_invalid_token")  # token 値・thread_id は出さない
            return MailDraftOutput(created=False, error="expired", message=_ERR_MSG["expired"])

        if not self._quota_ok(requester):
            log.info("mail_draft_quota_exceeded")
            return MailDraftOutput(created=False, error="quota", message=_ERR_MSG["quota"])

        # 生成本体は morning_digest skill を再利用（全文取得→anchor→Reply-All→drafts.create）。
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        skill = MorningDigestSkill(token_store=self._token_store)
        res = skill.generate_draft_for_thread(thread_id, requester, ctx)
        open_url = str(res.get("thread_url", "") or "")
        err = res.get("error")

        if res.get("created"):
            self._quota_consume(requester)
            log.info("mail_draft_created", cost_usd=float(res.get("cost_usd", 0.0) or 0.0))
            return MailDraftOutput(created=True, open_url=open_url, message=_OK_MSG)
        if res.get("already"):
            log.info("mail_draft_already")
            return MailDraftOutput(
                created=False, already=True, open_url=open_url, message=_ALREADY_MSG
            )

        key = str(err or "not_draftable")
        log.info("mail_draft_failed", err=key)  # 種別のみ（本文・宛先は出さない）
        return MailDraftOutput(
            created=False,
            error=key,
            open_url=open_url,
            message=_ERR_MSG.get(key, _ERR_MSG["not_draftable"]),
        )

    # ── 入口 2: 一覧からの選択（2026-08-21 裁定 B/C）─────────────────────────

    def _run_selection(
        self, input: MailDraftInput, ctx: SkillContext, *, requester: str, log: Any
    ) -> MailDraftOutput:
        """本人の返事から候補を同定し、その件だけ中身まで読んで下書きを作る。

        **曖昧なら 1 件も作らない**（``error='ambiguous_selection'`` で聞き返す）。
        送信 API は経路上どこからも呼ばない（起草は mail_reply＝drafts.create のみ）。
        """
        try:
            gmail = self._gmail or resolve_gmail_for_user(
                self._token_store, requester, misconfig_message=_MISCONFIG_MSG
            )
        except MailConnectionError as e:
            log.info("mail_draft_not_connected", err=e.code)
            return MailDraftOutput(error=e.code, message=MESSAGE_BY_CONNECTION_ERROR[e.code])

        # 直前に本人へ出した一覧がプロセス内に残っていれば **それを使う**。
        # 一覧（mail_followup）と選択（mail_draft）は別のツール呼び出しなので、素直に
        # 作り直すと同じ受信箱を 2 度フル走査する（threads.get が 41→82 回・逐次 HTTP で
        # 8〜16 秒）。しかも窓が一覧と違えば「見つからなくなっていました」と嘘をつく。
        scan = _reusable_scan(requester, input)
        if scan is None:
            scanner = MailFollowupSkill(token_store=self._token_store, now_ms=self._now_ms)
            try:
                # 一覧と同じ判定・同じ並び。limit を広く取るのは candidate_refs で「一覧に出て
                # いたが今は上位 3 件から押し出された件」も指せるようにするため（並びは同じ）。
                scan = scanner.scan_inbox(
                    gmail,
                    requester,
                    ctx,
                    window_days=_effective_lookback(input),
                    scan_limit=TRIAGE_SCAN_DEFAULT,
                    idle_days=input.idle_days,
                    limit=TRIAGE_SCAN_DEFAULT,
                )
            except Exception as e:
                code = classify_gmail_failure(e)
                log.warning("mail_draft_scan_failed", err=code, exc=type(e).__name__)
                return MailDraftOutput(
                    error=code,
                    message=MESSAGE_BY_CONNECTION_ERROR.get(code, _ERR_MSG["not_draftable"]),
                )
            remember_scan(requester, scan)

        if not scan.candidates:
            # 受信箱に「返信が止まっている件」自体が無い。作らずに正直に返す。
            # ⚠️ ここを「ピン留め後に全部 _VANISHED」で判定してはいけない。渡された ref が
            # 全部古いだけで受信箱には候補がある場合に「見当たりません」と嘘をつく。
            log.info("mail_draft_no_candidates")
            return MailDraftOutput(error="no_candidates", message=_ERR_MSG["no_candidates"])

        fresh = list(scan.candidates[:DEFAULT_LIMIT])
        if not input.candidate_refs and mentions_number(input.selection):
            # 番号は「一覧の何行目か」でしかない。その一覧を渡されていない以上、指し先は
            # 確認できない（実測: 一覧の 1番=佐藤 に対し 田中 へ下書きを作った）。
            # 作らずに出し直す＝「推測で決めない」裁定に一致する。
            log.info("mail_draft_number_without_refs", candidates=len(fresh))
            return MailDraftOutput(
                error="ambiguous_selection",
                message=self._ambiguous_message(fresh, scan, head=_NO_REFS_HEAD),
                candidate_refs=_refs_of(fresh, scan),
            )
        shown = _pin_to_shown_list(scan, input.candidate_refs)

        picked = parse_selection(input.selection, shown)
        if not picked:
            # 推測で決めない（判定層が None を返した＝手掛かり無し/範囲外/2 件以上に該当）。
            log.info("mail_draft_ambiguous", candidates=len(shown))
            return MailDraftOutput(
                error="ambiguous_selection",
                message=self._ambiguous_message(fresh, scan),
                # 次に選び直すときの照合鍵。**いま出し直した一覧**の並びに対応する。
                candidate_refs=_refs_of(fresh, scan),
            )

        drafts: list[DraftedMail] = []
        cost = 0.0
        vanished = 0
        truncated = len(picked) > MAX_SELECTED
        for cand in picked[:MAX_SELECTED]:
            source = scan.sources.get(cand.mail.thread_id) if cand is not _VANISHED else None
            anchor_id = str(getattr(source, "anchor_id", "") or "")
            if not anchor_id:
                # 番号で「消えた位置」を指された。繰り上げずに、その件だけ作らない。
                vanished += 1
                continue
            if not self._quota_ok(requester):
                log.info("mail_draft_quota_exceeded")
                if not drafts:
                    return MailDraftOutput(error="quota", message=_ERR_MSG["quota"])
                break
            try:
                made, made_cost = self._draft_one(cand, anchor_id, input, ctx)
            except PermissionError:
                # gmail.modify 未認可（403）等。ここまで来ている＝連携そのものは在る。
                log.warning("mail_draft_reply_denied")
                if not drafts:
                    return MailDraftOutput(error="reauth_needed", message=_ERR_MSG["reauth_needed"])
                break
            cost += made_cost
            if made is None:
                continue
            self._quota_consume(requester)
            drafts.append(made)

        if not drafts:
            key = "vanished_selection" if vanished else "not_draftable"
            log.info("mail_draft_selection_not_draftable", err=key)
            return MailDraftOutput(
                error=key,
                total_cost_usd=cost,
                message=_MISSING_NOTE if vanished else _ERR_MSG["not_draftable"],
            )

        log.info(
            "mail_draft_selection_created", created=len(drafts), vanished=vanished, cost_usd=cost
        )  # 件名・本文・宛先は出さない（G7）
        return MailDraftOutput(
            created=True,
            open_url=drafts[0].open_url,
            drafts=drafts,
            total_cost_usd=cost,
            message=_render_drafts_message(drafts, truncated=truncated, vanished=vanished),
        )

    def _draft_one(
        self, cand: TriageCandidate, anchor_id: str, input: MailDraftInput, ctx: SkillContext
    ) -> tuple[DraftedMail | None, float]:
        """候補 1 件を mail_reply に起草させる（本文・スレッド全文・過去メール・Slack）。"""
        reply = self._reply_skill()
        out = reply.run(
            MailReplyInput(
                # 受信箱一覧から選ばれた件なので顧客名は無い。件名が Slack 検索の手掛かりになる。
                client_name="",
                instructions=input.instructions,
                target_message_id=anchor_id,
            ),
            ctx,
        )
        cost = float(getattr(out, "total_cost_usd", 0.0) or 0.0)
        if not getattr(out, "created", False):
            return (None, cost)
        return (
            DraftedMail(
                label=_candidate_label(cand),
                to_display=str(getattr(out, "to_display", "") or ""),
                subject=str(getattr(out, "draft_subject", "") or ""),
                body=str(getattr(out, "draft_body", "") or ""),
                gmail_draft_id=str(getattr(out, "gmail_draft_id", "") or ""),
                open_url=str(getattr(out, "open_url", "") or ""),
            ),
            cost,
        )

    def _reply_skill(self) -> Any:
        """起草エンジン。既存 mail_reply をそのまま使う（起草ロジックを複製しない）。"""
        if self._reply is not None:
            return self._reply
        from teamagent.skills.mail_reply.skill import MailReplySkill

        return MailReplySkill(
            token_store=self._token_store,
            deal_provider=self._deal_provider,
            # 裁定 B の「同じ相手との過去メールまで読む」はこの経路だけ既定 ON。
            # ただし **env で止められる**ようにしておく（180日 ×4通 ×800字が毎回
            # プロンプトに乗るので、コスト事故のときに再ビルド無しで殺せる口が要る）。
            # 他経路は同じ env の既定 False のままで、この経路だけ既定 True。
            counterpart_history=env_bool("MAIL_REPLY_COUNTERPART_HISTORY", True),
        )

    @staticmethod
    def _ambiguous_message(
        fresh: list[TriageCandidate], scan: InboxScan, *, head: str = _AMBIGUOUS_HEAD
    ) -> str:
        """聞き返し文面。一覧の描画は判定層の関数をそのまま再利用する（文言を増やさない）。

        出し直すのは **いまの受信箱の上位**（``fresh``）。ピン留め済みの一覧には「消えた位置」の
        埋め草が混ざりうるので、そのまま描くと空行の候補を見せてしまう。番号の指し先が
        ずれないよう、この一覧に対応する ``candidate_refs`` を呼び出し側が併せて返す。

        遡り日数は **走査そのものが持っている値**（``scan.window_days``）を出す。入力の
        ``lookback_days`` を書くと、キャッシュ再利用や idle_days による窓の拡張で
        実際に見た範囲とズレた数字を本人へ提示してしまう。
        """
        return "\n".join(
            [
                head,
                render_triage_message(
                    fresh[:DEFAULT_LIMIT],
                    scanned=scan.scanned,
                    truncated=scan.truncated,
                    window_days=scan.window_days,
                ),
            ]
        )

    # ── 1人1日あたりの上限（連打/コスト対策・in-memory）─────────────────────
    def _quota_ok(self, email: str) -> bool:
        today = _dt.date.today().isoformat()
        with _QUOTA_LOCK:
            day, n = _DAILY_COUNTS.get(email, (today, 0))
        return today != day or n < self._QUOTA_LIMIT

    def _quota_consume(self, email: str) -> None:
        today = _dt.date.today().isoformat()
        with _QUOTA_LOCK:
            day, n = _DAILY_COUNTS.get(email, (today, 0))
            _DAILY_COUNTS[email] = (today, (n + 1) if today == day else 1)


# ── モジュール関数（純粋・テスト容易）──────────────────────────────────────


def _pin_to_shown_list(scan: InboxScan, refs: list[str]) -> list[TriageCandidate]:
    """利用者が **実際に見た一覧**へ、番号の位置ごと固定する。

    ``refs``（提示時の evidence_ref を表示順で並べたもの）を指定すると、その順番どおりに
    候補を並べ直す。指定が無ければ再走査した並びの上位を使う。

    なぜ固定するか: 一覧を出してから返事が来るまでの間に新しいメールが届くと、点数順が
    入れ替わって「1番」が別のスレッドを指しうる。**別のお客様宛に下書きを作る**事故なので、
    順序は利用者が見たものに合わせる。

    受信箱から消えた（本人が自分で返信した 等）ref は :data:`_VANISHED` で**位置を残す**。
    詰めてしまうと「2番」が 3 番目の件を指し、やはり別のお客様に下書きを作ってしまう。
    """
    if not refs:
        return list(scan.candidates[:DEFAULT_LIMIT])
    thread_by_ref = {
        evidence_ref(src.anchor_id): src.meta.thread_id
        for src in scan.sources.values()
        if src.anchor_id
    }
    cand_by_thread = {cand.mail.thread_id: cand for cand in scan.candidates}
    pinned: list[TriageCandidate] = []
    seen: set[str] = set()
    for ref in refs:
        thread_id = thread_by_ref.get(ref, "")
        cand = cand_by_thread.get(thread_id) if thread_id else None
        pinned.append(_VANISHED if cand is None or thread_id in seen else cand)
        if thread_id:
            seen.add(thread_id)
    return pinned


def _candidate_label(cand: TriageCandidate) -> str:
    """どの候補への下書きかを示す 1 行（描画は判定層の整形関数を再利用）。"""
    return (
        f"{format_sender(cand.mail)}「{format_subject(cand.mail.subject)}」"
        f" ・{cand.idle_days}日経過"
    )


def _refs_of(cands: list[TriageCandidate], scan: InboxScan) -> list[str]:
    """いま提示した一覧に対応する evidence_ref（次の選び直しで番号を合わせるための鍵）。

    ⚠️ **描画した行数と必ず同じ長さ**にする。anchor が取れない候補を黙って落とすと
    「表示は 3 件・refs は 2 件」になり、次の『2番』が 3 番目の件を指す（＝別のお客様へ
    下書きを作る失敗クラス）。解決できない位置には :data:`VANISHED_REF` を置いて
    位置だけを保つ（指されたら「見つからなかった」と正直に返る）。
    """
    refs: list[str] = []
    for cand in cands[:DEFAULT_LIMIT]:
        source = scan.sources.get(cand.mail.thread_id)
        anchor_id = str(getattr(source, "anchor_id", "") or "")
        refs.append(evidence_ref(anchor_id) if anchor_id else VANISHED_REF)
    return refs


def _effective_lookback(input: MailDraftInput) -> int:
    """一覧を作ったときと同じ実効窓（mail_followup の ``_effective_lookback`` と同じ式）。

    idle_days で絞ると mail_followup 側は窓を ``idle_days + 3`` まで広げる。こちらだけ
    既定 14 日のままだと、一覧に出ていた「20 日放置」の件が選択時の窓に入らず
    『見つからなくなっていました（ご自身で返信済み 等）』と**事実と異なる**説明をする。
    """
    lookback = input.lookback_days
    if input.idle_days is not None:
        lookback = min(90, max(lookback, input.idle_days + 3))
    return lookback


def _reusable_scan(requester: str, input: MailDraftInput) -> InboxScan | None:
    """直前に本人へ見せた一覧を、この呼び出しに使ってよければ返す。

    使ってよい条件:
      - 走査条件の指定が無い → そもそも「さっき見た一覧」の話なので、そのまま使うのが正しい
        （既定 14 日で走査し直すと、idle_days で広げた一覧の件が窓の外へ落ちる）。
      - 条件が指定されていて、キャッシュがそれを**満たしている**（窓が同じか広い・
        同じ絞り込み）→ 走査し直しても同じものが出るので、往復を増やす理由が無い。

    条件を満たさない指定（もっと広い窓・違う idle_days）が来たときだけ ``None`` を返して
    素直に走査し直す。**キャッシュの都合で利用者の指定を握り潰さない**ための分岐。
    """
    scan = recall_scan(requester)
    if scan is None:
        return None
    if not {"lookback_days", "idle_days"} & input.model_fields_set:
        return scan
    if scan.window_days >= _effective_lookback(input) and scan.idle_days == input.idle_days:
        return scan
    return None


def _render_drafts_message(drafts: list[DraftedMail], *, truncated: bool, vanished: int) -> str:
    """本文とリンクをそのまま本人へ返す（裁定 C）。要約しない・リンクを削らない。"""
    lines: list[str] = [_DRAFT_HEAD]
    for draft in drafts:
        lines.append("")
        lines.append(f"▼ {draft.label}")
        if draft.to_display:
            lines.append(f"宛先: {draft.to_display}")
        if draft.subject:
            lines.append(f"件名: {draft.subject}")
        lines.append(draft.body)
        if draft.open_url:
            lines.append(f"Gmail で開く: {draft.open_url}")
    lines.append("")
    if truncated:
        lines.append(_TRUNCATED_NOTE)
    if vanished:
        lines.append(_MISSING_NOTE)
    lines.append(_DRAFT_FOOT)
    return "\n".join(lines)
