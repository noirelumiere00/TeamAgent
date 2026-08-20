"""mail_followup Skill 本体（要返信トリアージ・メタデータのみ・読み取り専用）。

本人の受信箱から、指定クライアントについて「相手から最後に来たまま動いていない」スレッドを
新しい順／放置日数つきで列挙する。thread_id で重複排除し、スレッド末尾が本人の返信なら除外。
**本文を一切読まない**（format='metadata'）純メタデータ処理なので、LLM 不使用＝コスト0・
プロンプトインジェクション面ゼロ・PII 最小。

⚠️ 死守ライン（mail_constraints と同じ G1-G7。本 Skill は本文を読まないので G6 は N/A）:
  G1 本人受信箱限定: ctx.metadata.user_email→TokenStore。LLM/呼出側に受信箱を選ばせない。
  G2 連携必須（オプトイン）: TokenStore に本人トークンが無ければ fail-closed。
  G3 生データを返さない: 件名は scrub_value でマスク＋短縮、From はマスク、messageId はハッシュ。
  G4 readonly 最小スコープ（gmail.readonly）。書込メソッドは呼ばない（drafts/labels なし）。
  G5 クエリ限定: client_name + 期間で必ず絞る（無差別走査禁止）。max_messages で上限。
  G6 N/A（本文を読まず LLM に渡さないため、インジェクション面が存在しない）。
  G7 監査ログ: who(masked)/when/件数のみ。件名・本文・PII は出さない。

⚠️ 正直ラベリング（重要）: gmail.readonly の users.threads.get(format='metadata') で
スレッド末尾を確認し、本人の返信が最後のスレッドは候補から除外する。本文取得・下書き作成・
ラベル変更は行わず、OAuth スコープも gmail.readonly から拡大しない。

3 層分離: 本ファイルは Skill 層。googleapiclient / boto3 は触らず adapters/ 経由。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from typing import Any, ClassVar, TypeVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gmail_client import GmailClient, extract_thread_participants
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.observability import scrub_value
from teamagent.skills._shared.client_name_guard import (
    ERROR_BY_VERDICT,
    classify_client_name,
    guard_message,
    retry_disclosure,
    retry_zero_note,
    safe_client_name,
    to_gmail_phrase,
)
from teamagent.skills._shared.mail_compose import env_bool, should_skip_mail
from teamagent.skills._shared.mail_connection import (
    CONNECTION_LIVE,
    CONNECTION_OK,
    MESSAGE_BY_CONNECTION_ERROR,
    MailConnectionError,
    classify_gmail_failure,
    resolve_gmail_for_user,
    searched_inbox_prefix,
)
from teamagent.skills._shared.timefmt import jst_display_or_none, jst_iso_or_none
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.mail_followup.schema import (
    FollowupItem,
    MailFollowupInput,
    MailFollowupOutput,
)

logger = structlog.get_logger(__name__)

_MS_PER_DAY = 86_400_000

_HONEST_NOTE = (
    "※ スレッドの最新メッセージをメタデータで確認し、あなたの返信が最後のものは除外して"
    "います（gmail.readonly のみ・本文は読みません）。"
)

_MISCONFIG_MSG = "TokenStore が未設定です（mail_followup は本人連携前提）"


def _no_hits_message(name: str, lookback_days: int, inbox_masked: str) -> str:
    """該当 0 件の決定論文言（P0-3）。

    「連携は正常」「実際に検索した」をサーバが断言することで、LLM が空白を埋めて
    「Google 連携が未完了かもしれません」と創作するのを止める。本 Skill の 0 件は
    「メールが 1 通も無い」ではなく「**相手から来たまま止まっているものが**無い」なので、
    文言もそのとおりに書く（返信済みで除外された分を「無かった」ことにしない）。
    """
    return (
        searched_inbox_prefix(inbox_masked) + f"「{name}」で直近 {lookback_days} 日に"
        "『相手から来たまま止まっている』受信メールは 0 件でした。"
    )


@register
class MailFollowupSkill(BaseSkill[MailFollowupInput, MailFollowupOutput]):
    """本人受信箱の放置気味メールをメタデータだけで列挙する Skill（読み取り専用・LLM不使用）。"""

    name: ClassVar[str] = "mail_followup"
    description: ClassVar[str] = (
        "**『要返信』『返信が必要』『返信漏れ』『返信待ち』『放置しているメール』"
        "と言われたらこれ。**"
        "本人の受信箱から、指定クライアントについて『相手から最後に来たまま動いていない』"
        "メールスレッドを放置日数つきで列挙する。本文は読まず件名・相手はマスク。"
        "スレッド末尾が本人の返信なら候補から除外する。"
        "メール内容の横断要約は mail_summary、空き時間は calendar_freebusy、"
        "予定の一覧は calendar_freebusy(mode='agenda')。"
        "顧客名が特定できない依頼では client_name を空にして呼ぶ（サーバが案内を返す）。"
        "未連携なら error='not_connected' と message を返す（message をそのまま伝え、"
        "oauth_connect＝@NewsTV AI に『連携』へ誘導する）。0 件なら error='no_hits'、"
        "connection='live'（＝連携は正常）。**0 件の原因を推測して補わないこと**。"
        "呼び出し時は arguments に "
        "`_user_context: {slack_user_id: '<Slack相手のuser_id>'}` を"
        "必ず含める（mcp 境界の本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = MailFollowupInput
    output_schema: ClassVar[type[BaseModel]] = MailFollowupOutput

    def __init__(
        self,
        token_store: TokenStore | None = None,
        gmail: GmailClient | None = None,
        *,
        now_ms: int | None = None,
    ) -> None:
        # gmail はテストで fake 注入。本番は token_store から本人トークンで構築（per-user）。
        self._token_store = token_store
        self._gmail = gmail
        # 経過日数の基準時刻。テストの決定性のため注入可能（未指定は実行時の now）。
        self._now_ms = now_ms

    def run(self, input: MailFollowupInput, ctx: SkillContext) -> MailFollowupOutput:
        log = ctx.bind_logger(self.name)
        # G7: 監査ログに本文・件名・生 email・client_name の値そのものは出さない。
        log.info(
            "mail_followup_start",
            client_name_chars=len(input.client_name),
            lookback_days=input.lookback_days,
            idle_days=input.idle_days,
            max_messages=input.max_messages,
        )

        # G1: 本人受信箱限定（fail-closed）。
        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str):
            raise PermissionError(
                "mail_followup は本人 user_email が必須です（本人受信箱限定・fail-closed）"
            )
        requester = requester.strip()
        if not requester:
            raise PermissionError("本人 user_email が必須です（空不可・fail-closed）")

        # 連携の解決を先に済ませる（TokenStore 参照＋Credentials 構築のみで **Gmail API は
        # 叩かない**）。ガードの文言は「連携は正常です」と断言するため、未連携のまま
        # ガードへ落ちると嘘を返してしまう。
        # P0-4: 未連携/再連携要は **例外ではなく構造化 return**（calendar_freebusy と同型）。
        # PermissionError のままだと MCP 境界で和文 1 本に潰れ、SOUL の
        # 「error=not_connected なら oauth_connect へ誘導」契約に載らない。
        try:
            gmail = self._resolve_gmail(requester)
        except MailConnectionError as e:
            log.info("mail_not_connected", skill=self.name, error=e.code)
            return MailFollowupOutput(
                client_name=safe_client_name(input.client_name),
                items=[],
                scanned_count=0,
                inbox_owner_masked=_mask_email(requester),
                note=e.message,
                total_cost_usd=0.0,
                error=e.code,
                message=e.message,
                connection="",
            )

        # P0-2: client_name が依頼文の断片（「今週の空き時間」等）や空なら、**Gmail を
        # 1 回も叩かずに**案内文を返す。無検査で通すと '"今週の空き時間"' という完全一致
        # フレーズ検索になり必ず 0 件＝「連携が壊れている」と誤解させるため。
        verdict = classify_client_name(input.client_name)
        if verdict.verdict != "ok":
            log.info(
                "mail_client_name_guard",
                skill=self.name,
                verdict=verdict.verdict,  # 値そのものは出さない（verdict/reason のみ）
                reason=verdict.reason,
            )
            message = guard_message(verdict)
            return MailFollowupOutput(
                # エコーも scrub 済みにする（message だけマスクして client_name は生、
                # という二重基準を同じ応答の中に作らない）。
                client_name=safe_client_name(verdict.normalized),
                items=[],
                scanned_count=0,
                inbox_owner_masked=_mask_email(requester),
                # SOUL は「message をそのまま返す」規約を持たないので、既に表示対象に
                # なっている note にも同じ文言を載せる（二重掲載は意図的）。
                note=message,
                total_cost_usd=0.0,
                error=ERROR_BY_VERDICT[verdict.verdict],
                message=message,
                connection=CONNECTION_OK,
            )

        inbox_masked = _mask_email(requester)
        # エコーは scrub 済み（client_name にメールアドレス等が入っていても素で返さない）。
        display_name = safe_client_name(verdict.normalized)

        # G5: クエリ限定（client + 期間 + 受信のみ。自分の送信は除外）。
        # ⚠️ ここから先は実際に Gmail を叩く。失効トークンはここで初めて露見するので、
        # 例外のまま抜けさせない（要修正3: reauth_needed に落として再連携へ誘導する）。
        try:
            refs, retried_with = self._search(gmail, verdict, input, ctx, log)
        except Exception as e:
            return self._api_failure(e, log=log, client_display=display_name, inbox=inbox_masked)
        # Gmail quota が 10 units/threads.get のため、API 呼び出しより先にスレッド重複を除く。
        unique_refs = _dedupe_refs_by_thread(refs)
        log.info(
            "mail_followup_scan",
            scanned=len(refs),
            unique_threads=len(unique_refs),
        )  # 本文・件名なし

        now_ms = self._now_ms if self._now_ms is not None else int(time.time() * 1000)
        exclude_bulk = env_bool("MAIL_EXCLUDE_BULK", True)
        try:
            items, excluded, kept = self._triage(
                gmail, unique_refs, requester, input, ctx, now_ms=now_ms, exclude_bulk=exclude_bulk
            )
        except Exception as e:
            return self._api_failure(e, log=log, client_display=display_name, inbox=inbox_masked)

        log.info(
            "mail_bulk_excluded",
            skill=self.name,
            excluded=excluded,
            kept=kept,
            request_id=ctx.request_id,
        )

        # 放置日数が大きい順（最も後回しになっているもの＝失注リスクが高い）。
        items.sort(key=lambda it: it.idle_days, reverse=True)

        # 2 本目は「東京メール大学」→「東京大学」のように別法人へ化けうる。どの語で
        # 当てたかを黙ると、別クライアントのメールを自分の案件として読ませてしまう。
        retry_note = retry_disclosure(verdict.normalized, retried_with) if retried_with else ""
        note = f"{retry_note}\n{_HONEST_NOTE}" if retry_note else _HONEST_NOTE
        message = ""
        error = ""
        if not items:
            # P0-3: 0 件の意味づけをサーバ側で確定させる（LLM に理由を創作させない）。
            zero = _no_hits_message(
                display_name,
                self._effective_lookback(input),
                inbox_masked,
            )
            if retried_with:
                zero += retry_zero_note(retried_with)
            note = zero + _HONEST_NOTE
            message = note
            error = "no_hits"

        log.info("mail_followup_done", returned=len(items), scanned=len(refs))
        return MailFollowupOutput(
            client_name=display_name,
            items=items,
            scanned_count=len(refs),
            inbox_owner_masked=inbox_masked,
            note=note,
            total_cost_usd=0.0,
            error=error,
            message=message,
            connection=CONNECTION_LIVE,
        )

    # ── 依存解決 ───────────────────────────────────────────────────────────

    def _resolve_gmail(self, requester: str) -> GmailClient:
        """G1/G2: 本人 OAuth トークン（TokenStore）から readonly クライアントを構築する。

        テスト/明示注入があればそれを使う。無ければ TokenStore から本人トークンを引き、
        from_user_token で本人受信箱のみ参照可能な readonly クライアントを作る（G4）。
        未連携（トークン無し）は fail-closed（G2）＝受信箱には触れず MailConnectionError。
        """
        if self._gmail is not None:
            return self._gmail
        return resolve_gmail_for_user(
            self._token_store, requester, misconfig_message=_MISCONFIG_MSG
        )

    def _search(
        self,
        gmail: GmailClient,
        verdict: Any,
        input: MailFollowupInput,
        ctx: SkillContext,
        log: Any,
    ) -> tuple[list[Any], str]:
        """1 本目（原文フレーズ）→ 0 件なら 2 本目（固有名詞残差）。Gmail 往復は最大 2 回。"""
        query = self._build_query(input, verdict.search_terms[0])
        refs, _ = gmail.list_messages(query, ctx.request_id, max_results=input.max_messages)
        if refs or len(verdict.search_terms) <= 1:
            return (list(refs), "")
        # 「花王のメール」のように依頼文が混じった名前は 1 本目が 0 件になる。
        log.info("mail_followup_retry_residual", skill=self.name)
        retried_with = str(verdict.search_terms[1])
        refs, _ = gmail.list_messages(
            self._build_query(input, retried_with), ctx.request_id, max_results=input.max_messages
        )
        return (list(refs), retried_with)

    def _api_failure(
        self, exc: BaseException, *, log: Any, client_display: str, inbox: str
    ) -> MailFollowupOutput:
        """受信箱アクセスの失敗を「0 件」と区別できる構造化 return にする（要修正3）。"""
        code = classify_gmail_failure(exc)
        log.warning("mail_search_failed", skill=self.name, error=code, err=type(exc).__name__)
        message = MESSAGE_BY_CONNECTION_ERROR[code]
        return MailFollowupOutput(
            client_name=client_display,
            items=[],
            scanned_count=0,
            inbox_owner_masked=inbox,
            note=message,
            total_cost_usd=0.0,
            error=code,
            message=message,
            connection="",
        )

    # ── トリアージ（G3/G4: メタデータのみ）──────────────────────────────────

    def _triage(
        self,
        gmail: GmailClient,
        unique_refs: list[Any],
        requester: str,
        input: MailFollowupInput,
        ctx: SkillContext,
        *,
        now_ms: int,
        exclude_bulk: bool,
    ) -> tuple[list[FollowupItem], int, int]:
        """スレッド末尾をメタデータで確認し、放置中のものだけを列挙する。

        Returns:
            (items, bulk 除外数, 判定対象数)。Gmail 側の例外はそのまま送出し、
            呼び出し側が :meth:`_api_failure` で「0 件」と区別できる形へ落とす。
        """
        items: list[FollowupItem] = []
        excluded = 0
        kept = 0
        for ref in unique_refs:
            thread_id = str(getattr(ref, "thread_id", "") or "")
            if not thread_id:
                # list_messages は thread_id を返す契約。欠損時は末尾を判定できないため除外する。
                continue
            # users.threads.get は gmail.readonly 域内。format='metadata' のみを使い、
            # 本文取得・書き込み・OAuth スコープ拡大は行わない（G4/G6）。
            thread = gmail.get_thread(
                thread_id,
                ctx.request_id,
                format="metadata",
            )
            # threads.get に下書きが混ざっても「返信済み」とは扱わず、最後の送受信だけを見る。
            thread = [
                msg
                for msg in thread
                if "DRAFT"
                not in {
                    str(label).strip().upper() for label in (getattr(msg, "label_ids", ()) or ())
                }
            ]
            if not thread:
                continue
            thread = sorted(
                thread,
                key=lambda msg: int(getattr(msg, "internal_date_ms", 0) or 0),
            )
            anchor = thread[-1]
            if exclude_bulk and should_skip_mail(anchor.headers):
                excluded += 1
                continue
            kept += 1
            if _is_from_requester(anchor, requester):
                continue
            counterpart = _first_counterpart(anchor.headers, requester)
            idle_days = _idle_days(anchor.internal_date_ms, now_ms)
            if input.idle_days is not None and idle_days < input.idle_days:
                continue
            subject = str(scrub_value(anchor.headers.get("Subject", "")))[:80]
            items.append(
                FollowupItem(
                    counterpart_masked=_mask_email(counterpart) if counterpart else "***",
                    subject_scrubbed=subject,
                    idle_days=idle_days,
                    occurred_at=jst_iso_or_none(anchor.internal_date_ms),
                    occurred_at_display=jst_display_or_none(anchor.internal_date_ms),
                    evidence_ref=_hash_id(anchor.id),
                )
            )
        return (items, excluded, kept)

    # ── クエリ構築（G5）─────────────────────────────────────────────────────

    @staticmethod
    def _effective_lookback(input: MailFollowupInput) -> int:
        """実際に検索した遡り日数。idle_days 指定時は窓を広げる（0 件文言と必ず一致させる）。"""
        lookback = input.lookback_days
        if input.idle_days is not None:
            lookback = min(90, max(lookback, input.idle_days + 3))
        return lookback

    @staticmethod
    def _build_query(input: MailFollowupInput, term: str) -> str:
        """Gmail 検索クエリ。client + 期間で必ず絞り、自分の送信は除外して受信のみ見る。

        `-in:sent` で送信済みを除外し `in:inbox` で受信トレイに限定する（放置 = 受信箱に残存）。
        idle_days 指定時は走査窓をその閾値より十分前まで広げる（広げないと post-filter で全部
        落ちて「見つかりませんでした」と誤答する＝信頼を損なう）。上限は schema 同様 90 日。

        term は client_name_guard が検査済みのキーワード（原文 or 固有名詞残差）。生の
        client_name をここに流さないこと（`"` を含む値でフレーズを閉じられるため）。
        """
        lookback = MailFollowupSkill._effective_lookback(input)
        return f"{to_gmail_phrase(term)} newer_than:{lookback}d -in:sent in:inbox"


# ── モジュール関数（純粋・テスト容易）──────────────────────────────────────


def _first_counterpart(headers: dict[str, str], requester: str) -> str | None:
    """From → 無ければ To/Cc から、requester 本人以外の最初のアドレスを 1 件返す。

    生 From 文字列はそのまま使わない（マスク前提）。adapter の抽出器でアドレスのみ取る。
    """
    req = requester.strip().lower()
    # From 優先（受信メールの相手）。無ければ参加者全体から本人以外。
    for field in ("From", "To", "Cc"):
        v = headers.get(field, "")
        if not v:
            continue
        for email in extract_thread_participants({field: v}):
            if email.strip().lower() != req:
                return email
    return None


def _is_from_requester(msg: object, requester: str) -> bool:
    """スレッド末尾が本人の送信済み返信なら True。

    Gmail の SENT ラベルを第一根拠にすることで send-as alias にも対応する。テスト fake や
    ラベル欠損時は From を本人 email と照合する。DRAFT は未送信なので返信済みに数えない。
    """
    labels = {
        str(label).strip().upper()
        for label in (getattr(msg, "label_ids", ()) or ())
        if str(label).strip()
    }
    if "DRAFT" in labels:
        return False
    if "SENT" in labels:
        return True

    req = requester.strip().lower()
    headers = getattr(msg, "headers", {}) or {}
    if not req or not isinstance(headers, dict):
        return False
    return any(
        email.strip().lower() == req
        for email in extract_thread_participants({"From": str(headers.get("From", ""))})
    )


_RefT = TypeVar("_RefT")


def _dedupe_refs_by_thread(refs: Sequence[_RefT]) -> list[_RefT]:
    """list_messages の newest-first 順を保ち、thread_id ごとに最初の ref だけを残す。"""
    seen: set[str] = set()
    unique: list[_RefT] = []
    for ref in refs:
        thread_id = str(getattr(ref, "thread_id", "") or "")
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        unique.append(ref)
    return unique


def _idle_days(internal_date_ms: int | None, now_ms: int) -> int:
    if not internal_date_ms:
        return 0
    delta = now_ms - int(internal_date_ms)
    if delta < 0:
        return 0
    return delta // _MS_PER_DAY


def _hash_id(msg_id: str) -> str:
    """messageId をハッシュ化して evidence_ref に使う（生 id を出さない）。"""
    return hashlib.sha256(msg_id.encode("utf-8")).hexdigest()[:12]


def _mask_email(email: str) -> str:
    """監査用の部分マスク（先頭1文字＋ドメイン）。例: s***@vectorinc.co.jp。"""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
