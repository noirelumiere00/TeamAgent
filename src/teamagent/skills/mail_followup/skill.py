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
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gmail_client import GmailClient, extract_thread_participants
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.observability import scrub_value
from teamagent.skills._shared.mail_compose import env_bool, should_skip_mail
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


@register
class MailFollowupSkill(BaseSkill[MailFollowupInput, MailFollowupOutput]):
    """本人受信箱の放置気味メールをメタデータだけで列挙する Skill（読み取り専用・LLM不使用）。"""

    name: ClassVar[str] = "mail_followup"
    description: ClassVar[str] = (
        "本人の受信箱から、指定クライアントについて『相手から最後に来たまま動いていない』"
        "メールスレッドを放置日数つきで列挙する。本文は読まず件名・相手はマスク。"
        "スレッド末尾が本人の返信なら候補から除外する。"
        "本人が /teamagent connect で連携済みの時のみ使える（未連携は不可）。"
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
        # G7: 監査ログに本文・件名・生 email は出さない。
        log.info(
            "mail_followup_start",
            client_name=input.client_name,
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

        gmail = self._resolve_gmail(requester)

        # G5: クエリ限定（client + 期間 + 受信のみ。自分の送信は除外）。
        query = self._build_query(input)
        refs, _ = gmail.list_messages(query, ctx.request_id, max_results=input.max_messages)
        # Gmail quota が 10 units/threads.get のため、API 呼び出しより先にスレッド重複を除く。
        unique_refs = _dedupe_refs_by_thread(refs)
        log.info(
            "mail_followup_scan",
            scanned=len(refs),
            unique_threads=len(unique_refs),
        )  # 本文・件名なし

        now_ms = self._now_ms if self._now_ms is not None else int(time.time() * 1000)
        items: list[FollowupItem] = []
        excluded = 0
        kept = 0
        exclude_bulk = env_bool("MAIL_EXCLUDE_BULK", True)
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
                    occurred_at=_iso_or_none(anchor.internal_date_ms),
                    evidence_ref=_hash_id(anchor.id),
                )
            )

        log.info(
            "mail_bulk_excluded",
            skill=self.name,
            excluded=excluded,
            kept=kept,
            request_id=ctx.request_id,
        )

        # 放置日数が大きい順（最も後回しになっているもの＝失注リスクが高い）。
        items.sort(key=lambda it: it.idle_days, reverse=True)

        log.info("mail_followup_done", returned=len(items), scanned=len(refs))
        return MailFollowupOutput(
            client_name=input.client_name,
            items=items,
            scanned_count=len(refs),
            inbox_owner_masked=_mask_email(requester),
            note=_HONEST_NOTE,
            total_cost_usd=0.0,
        )

    # ── 依存解決 ───────────────────────────────────────────────────────────

    def _resolve_gmail(self, requester: str) -> GmailClient:
        """G1/G2: 本人 OAuth トークン（TokenStore）から readonly クライアントを構築する。

        テスト/明示注入があればそれを使う。無ければ TokenStore から本人トークンを引き、
        from_user_token で本人受信箱のみ参照可能な readonly クライアントを作る（G4）。
        未連携（トークン無し）は fail-closed（G2）。
        """
        if self._gmail is not None:
            return self._gmail
        if self._token_store is None:
            raise PermissionError("TokenStore が未設定です（mail_followup は本人連携前提）")
        token = self._token_store.get(requester)
        if token is None:
            raise PermissionError(
                "メール連携が未完了です（/teamagent connect で自分の Google を認可してください）"
            )
        try:
            return GmailClient.from_user_token(token, readonly=True)
        except ValueError as e:
            # 認証情報(GOOGLE_CLIENT_ID/SECRET 未設定・失効/空 refresh token)は連携案内に寄せる。
            raise PermissionError(
                "メール連携の認証情報を解決できませんでした。"
                "/teamagent connect で自分の Google を認可し直してください。"
            ) from e

    # ── クエリ構築（G5）─────────────────────────────────────────────────────

    @staticmethod
    def _build_query(input: MailFollowupInput) -> str:
        """Gmail 検索クエリ。client + 期間で必ず絞り、自分の送信は除外して受信のみ見る。

        `-in:sent` で送信済みを除外し `in:inbox` で受信トレイに限定する（放置 = 受信箱に残存）。
        idle_days 指定時は走査窓をその閾値より十分前まで広げる（広げないと post-filter で全部
        落ちて「見つかりませんでした」と誤答する＝信頼を損なう）。上限は schema 同様 90 日。
        """
        lookback = input.lookback_days
        if input.idle_days is not None:
            lookback = min(90, max(lookback, input.idle_days + 3))
        return f'"{input.client_name}" newer_than:{lookback}d -in:sent in:inbox'


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


def _dedupe_refs_by_thread(refs: list[object]) -> list[object]:
    """list_messages の newest-first 順を保ち、thread_id ごとに最初の ref だけを残す。"""
    seen: set[str] = set()
    unique: list[object] = []
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


def _iso_or_none(internal_date_ms: int | None) -> str | None:
    if not internal_date_ms:
        return None
    # epoch ms → ISO(UTC, 秒精度)。日付の手掛かりのみ（PII ではない）。
    import datetime

    return (
        datetime.datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
    )


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
