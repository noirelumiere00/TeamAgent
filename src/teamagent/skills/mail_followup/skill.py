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
  G5 クエリ限定: 期間・受信トレイ・件数上限で必ず絞る。顧客名ありは client_name でも絞る。
     顧客名なしの一覧トリアージ（下記）は **本人の受信トレイ全体**を見るが、期間
     （既定 14 日・最大 90）・母数（messages.list を 300 件までページング）・
     確認件数（threads.get を既定 40 件まで）で必ず頭打ちにし、利用者入力は
     クエリへ一切載せない（＝Gmail 演算子インジェクションの面を作らない）。
     予算は **古い側から**割り当てる（放置検出で古い側を切ると、最も必要な 1 通を捨てる）。
  G6 N/A（本文を読まず LLM に渡さないため、インジェクション面が存在しない）。
  G7 監査ログ: who(masked)/when/件数のみ。件名・本文・PII は出さない。

⚠️ 正直ラベリング（重要）: gmail.readonly の users.threads.get(format='metadata') で
スレッド末尾を確認し、本人の返信が最後のスレッドは候補から除外する。本文取得・下書き作成・
ラベル変更は行わず、OAuth スコープも gmail.readonly から拡大しない。

⚠️ 顧客名が無いとき（2026-08-21 ユーザー裁定）: 「どちらのお客様ですか？」と**聞き返さない**。
受信箱全体を **メタデータだけ**で走査し、返信が止まっている候補を数件提示して選ばせる
（裁定 A: 一覧段階では件名・差出人・日時・放置日数だけ／本文は読まない・LLM に渡さない）。
判定と文面は :mod:`teamagent.skills._shared.inbox_triage`（純関数）に置き、本ファイルは
Gmail からメタデータを詰め替えるだけにしてある。

3 層分離: 本ファイルは Skill 層。googleapiclient / boto3 は触らず adapters/ 経由。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import threading as _threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from typing import Any, ClassVar, TypeVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gmail_client import GmailClient, extract_thread_participants
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.observability import scrub_value
from teamagent.skills._shared.client_name_guard import (
    ClientNameVerdict,
    classify_client_name,
    retry_disclosure,
    retry_zero_note,
    safe_client_name,
    to_gmail_phrase,
)
from teamagent.skills._shared.inbox_triage import (
    DEFAULT_LIMIT,
    InboxMailMeta,
    TriageCandidate,
    idle_days_of,
    rank_candidates,
    render_triage_message,
)
from teamagent.skills._shared.mail_compose import env_bool, should_skip_mail
from teamagent.skills._shared.mail_connection import (
    CONNECTION_LIVE,
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

# ── 一覧トリアージ（顧客名なし経路・2026-08-21 裁定）─────────────────────────

#: 一覧トリアージの決定論コード。「聞き返し」ではなく「候補を出した」ことを機械可読にする。
ERROR_INBOX_TRIAGE = "inbox_triage"

#: 一覧走査の既定件数＝**threads.get を叩く上限**（レイテンシとコストの実質的な支配項）。
#: 呼び出し側が ``max_messages`` を **明示した**ときはその値を尊重する（下の
#: :meth:`MailFollowupSkill._triage_scan_limit`）。既定 30 のまま流用すると
#: 「顧客名ありの 1 社ぶん」と同じ狭さで受信箱全体を見ることになり、候補が偏る。
TRIAGE_SCAN_DEFAULT = 40

#: ``messages.list`` の 1 ページ件数。ページ取得は 1 回 5 units・ヘッダを返さないので安い。
TRIAGE_LIST_PAGE = 100

#: ``messages.list`` をページングして**母数として数える**上限（＝最大 3 往復）。
#: 1 ページで打ち切ると「新着 N 件」しか見えず、**最も放置されている古い側**が
#: 構造的に視野の外へ落ちる（放置検出でそれをやると、最も必要な情報だけを捨てることになる）。
TRIAGE_LIST_MAX = 300

#: 一覧走査の結果を同一プロセス内で使い回す秒数。「一覧 → 番号で選ぶ」の 2 コールで
#: 同じ受信箱を 2 回フル走査すると threads.get が倍になる（実測 82 回）ため、
#: **利用者が実際に見た一覧そのもの**を短時間だけ保持して選択側から再利用する。
SCAN_CACHE_TTL_S = 180

#: 保持するプロセス内キャッシュの人数上限（メモリと PII 滞留の頭打ち）。
SCAN_CACHE_MAX_USERS = 32

#: 「一覧に出ていたが今は解決できない位置」を指す埋め草 ref。実 ref は 12 桁の hex なので
#: 決して衝突しない。番号の位置をずらさないためだけに存在する。
VANISHED_REF = "-"

_TRIAGE_NOTE = (
    "※ この一覧は件名・差出人・日時・放置日数だけで選んでいます（本文は読んでいません）。"
    "選んでいただいた 1 件だけ、中身まで見て下書きを作ります。"
)


def _triage_unfiltered_note(echo: str) -> str:
    """お客様名として使えなかった語を **フィルタに使っていない**ことを開示する一文。

    ここを黙ると、``client_name="今週の空き時間"`` で受信箱全体を出した結果を LLM が
    「今週の空き時間の件のメール」として提示しうる（帰属の誤り）。
    """
    return f"※『{echo}』はお客様名として扱えなかったため、受信箱全体から選んでいます。"


@dataclass(frozen=True)
class InboxSource:
    """一覧トリアージ 1 件分の中間表現（判定層へ渡すメタ＋出力に要る素材）。

    :class:`InboxMailMeta` には出力用の素材（messageId・相手アドレス）を持たせていない
    （＝判定層に余計な PII を渡さない）ので、その対応表をここで保持する。
    """

    meta: InboxMailMeta
    anchor_id: str
    counterpart: str | None


@dataclass(frozen=True)
class InboxScan:
    """:meth:`MailFollowupSkill.scan_inbox` の結果（一覧の**確定した並び**を含む）。

    「選ばれた 1 件を深掘りする」後段（mail_draft の selection 経路）が、利用者が見た
    一覧と **同じ順序・同じ判定**で候補を再現するために公開している。順序を再現できないと
    「1番で」の指し先がずれ、別の相手宛の下書きを作ってしまう。
    """

    candidates: tuple[TriageCandidate, ...]
    sources: dict[str, InboxSource]
    """thread_id → 素材（anchor messageId・相手アドレス）。"""
    scanned: int
    """実際にスレッド末尾まで**確認した**件数（＝threads.get を叩いた数）。"""
    truncated: bool
    """母数の一部を確認できていないか（打ち切りの正直な開示に使う）。"""
    window_days: int = 0
    """この走査が実際に遡った日数。後段が同じ窓を名乗れるよう scan 自身に持たせる。"""
    idle_days: int | None = None
    """この走査に効かせた放置日数の下限（None＝絞っていない）。"""
    listed: int = 0
    """``messages.list`` が返した母数（確認できた件数 ``scanned`` とは別物）。"""


# ── 一覧走査のプロセス内キャッシュ（同じ受信箱を 2 度フル走査しないため）───────────
#
# 「一覧を出す（mail_followup）」と「番号で選ぶ（mail_draft）」は **別々のツール呼び出し**
# なので、Skill インスタンスは毎回作り直される（ToolSpec.instantiate）。インスタンス変数に
# 持たせても本番では 1 度も再利用されず、下書き 1 通あたり threads.get が倍かかる。
# ここで持つのは **本人が直前に見た一覧そのもの**（マスク済み件名・相手アドレス・anchor id）で、
# 本人の email をキーに TTL 内だけ保持する（他人の受信箱に触れる経路はここには無い）。
_SCAN_CACHE: dict[str, tuple[float, InboxScan]] = {}
_SCAN_CACHE_LOCK = _threading.Lock()


def remember_scan(requester: str, scan: InboxScan) -> None:
    """本人が見た一覧を TTL 付きで覚える（次のツール呼び出しでの再走査を省く）。"""
    key = requester.strip().lower()
    if not key:
        return
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        # 期限切れを掃除してから入れる（人数上限を超えたら最も古いものを落とす）。
        for stale in [k for k, (at, _) in _SCAN_CACHE.items() if now - at > SCAN_CACHE_TTL_S]:
            _SCAN_CACHE.pop(stale, None)
        while len(_SCAN_CACHE) >= SCAN_CACHE_MAX_USERS:
            _SCAN_CACHE.pop(min(_SCAN_CACHE, key=lambda k: _SCAN_CACHE[k][0]), None)
        _SCAN_CACHE[key] = (now, scan)


def recall_scan(requester: str) -> InboxScan | None:
    """直前に本人へ提示した一覧を返す（TTL 切れ・別プロセスなら None＝素直に再走査）。"""
    key = requester.strip().lower()
    if not key:
        return None
    with _SCAN_CACHE_LOCK:
        entry = _SCAN_CACHE.get(key)
        if entry is None:
            return None
        at, scan = entry
        if time.monotonic() - at > SCAN_CACHE_TTL_S:
            _SCAN_CACHE.pop(key, None)
            return None
        return scan


def clear_scan_cache() -> None:
    """プロセス内キャッシュを空にする（テスト間の独立性・運用上の緊急退避用）。"""
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE.clear()


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
        "顧客名が特定できない依頼では client_name を**空のまま呼ぶ**。"
        "サーバが受信箱全体をメタデータだけで見て候補を数件返し（error='inbox_triage'）、"
        "**message をそのまま出して番号で選んでもらう**（聞き返しの文面を自作しない）。"
        "その返事（『1番で』『1と3、丁寧めで』等）が来たら **mail_draft を呼ぶ**"
        "（selection に返事をそのまま・candidate_refs に items[].evidence_ref を表示順のまま・"
        "lookback_days に本ツールが返した lookback_days をそのまま）。"
        "**candidate_refs を省くと番号は解釈されない**（別のお客様宛に作らないため）。"
        "未連携なら error='not_connected' と message を返す（message をそのまま伝え、"
        "oauth_connect＝@Aico に『連携』へ誘導する）。0 件なら error='no_hits'、"
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

        # P0-2: client_name が依頼文の断片（「今週の空き時間」等）や空なら、その語で
        # フレーズ検索してはいけない（'"今週の空き時間"' は完全一致で必ず 0 件になり、
        # 「連携が壊れている」と誤解させる）。
        # 2026-08-21 裁定: ここで「どちらのお客様ですか？」と**聞き返さず**、受信箱全体を
        # メタデータだけで見て候補を提示する（選ばれた 1 件だけ後段で深掘りする前提）。
        verdict = classify_client_name(input.client_name)
        if verdict.verdict != "ok":
            log.info(
                "mail_client_name_guard",
                skill=self.name,
                verdict=verdict.verdict,  # 値そのものは出さない（verdict/reason のみ）
                reason=verdict.reason,
            )
            return self._inbox_triage(
                gmail, input, ctx, requester=requester, verdict=verdict, log=log
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
            lookback_days=self._effective_lookback(input),
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

    # ── 一覧トリアージ（顧客名なし経路・G3/G4: メタデータのみ）──────────────

    def _inbox_triage(
        self,
        gmail: GmailClient,
        input: MailFollowupInput,
        ctx: SkillContext,
        *,
        requester: str,
        verdict: ClientNameVerdict,
        log: Any,
    ) -> MailFollowupOutput:
        """受信箱全体から「返信が止まっている候補」を数件返す（本文は読まない）。

        裁定 A の死守: ここで触れるのは ``threads.get(format='metadata')`` のヘッダだけ。
        ``messages.get``（本文経路）は 1 回も呼ばず、LLM も呼ばない（``total_cost_usd=0``）。

        失敗時は :meth:`_api_failure` へ落とす＝**「0 件」とは別物**として返す。
        """
        inbox_masked = _mask_email(requester)
        window = self._effective_lookback(input)

        try:
            scan = self.scan_inbox(
                gmail,
                requester,
                ctx,
                window_days=window,
                scan_limit=self._triage_scan_limit(input),
                idle_days=input.idle_days,
                # 提示は上位 DEFAULT_LIMIT 件だが、候補自体は広く持っておく。後段
                # （mail_draft の selection）が **この同じ scan** を使い回すため、
                # ここで 3 件に切ると「一覧に出ていた件が解決できない」が生まれる。
                limit=TRIAGE_SCAN_DEFAULT,
            )
        except Exception as e:
            return self._api_failure(e, log=log, client_display="", inbox=inbox_masked)
        # 直前に本人へ見せた一覧として覚える（選択側の再走査を省く＝threads.get を倍にしない）。
        remember_scan(requester, scan)
        cands = scan.candidates[:DEFAULT_LIMIT]
        truncated = scan.truncated

        lines = [
            render_triage_message(
                cands, scanned=scan.scanned, truncated=truncated, window_days=window
            )
        ]
        if verdict.normalized:
            # structural（依頼文の断片）のときだけ。「その語で絞ったわけではない」を明示する。
            lines.append(_triage_unfiltered_note(safe_client_name(verdict.normalized)))
        lines.append(_TRIAGE_NOTE)
        message = "\n".join(lines)

        log.info(
            "mail_inbox_triage",
            skill=self.name,
            scanned=scan.scanned,
            considered=len(scan.sources),
            candidates=len(cands),
            truncated=truncated,
        )  # 件名・本文・生アドレスは出さない（G7）
        return MailFollowupOutput(
            # ⚠️ 空にする。受信箱全体を見た結果を verdict.normalized の名前でエコーすると、
            # LLM が「『今週の空き時間』の件のメール」として提示しうる（帰属の誤り）。
            client_name="",
            items=[_followup_item(cand, scan.sources[cand.mail.thread_id]) for cand in cands],
            scanned_count=scan.scanned,
            lookback_days=scan.window_days,
            inbox_owner_masked=inbox_masked,
            note=message,
            total_cost_usd=0.0,
            error=ERROR_INBOX_TRIAGE,
            message=message,
            connection=CONNECTION_LIVE,
        )

    def scan_inbox(
        self,
        gmail: GmailClient,
        requester: str,
        ctx: SkillContext,
        *,
        window_days: int,
        scan_limit: int = TRIAGE_SCAN_DEFAULT,
        idle_days: int | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> InboxScan:
        """受信箱を**メタデータだけ**で走査し、候補を確定した並びで返す（公開・再利用可）。

        一覧（:meth:`_inbox_triage`）と、選ばれた 1 件を深掘りする後段（mail_draft の
        selection 経路）が **同じコードで同じ順序**を得るための入口。順序が再現できないと
        「1番で」の指し先がずれ、別の相手宛に下書きを作ってしまう。

        走査の形（2026-08-24 修正）:
          1. ``messages.list`` を :data:`TRIAGE_LIST_MAX` までページングする（新しい順）。
             1 ページで止めると「直近 N 件」しか母数に入らず、**最も放置されている古い側**が
             視野の外へ落ちる。放置検出でそれをやると、最も必要な 1 通だけを捨てる。
          2. スレッド重複を除いたうえで、``threads.get`` は ``scan_limit`` 件までに絞る
             （逐次 HTTP・バッチ不可なのでレイテンシの支配項）。予算を割り当てるのは
             **古い側から**＝この Skill が「放置」を定義している側から。
          3. 予算に収まらなかった側があれば ``truncated`` を立てる（黙って切らない）。

        Gmail 側の例外はそのまま送出する（呼び出し側が「0 件」と区別して扱う）。
        """
        refs, has_more = _list_inbox_refs(
            gmail, _build_inbox_query(window_days), ctx.request_id, cap=TRIAGE_LIST_MAX
        )
        unique_refs = _dedupe_refs_by_thread(refs)
        budget = max(1, scan_limit)
        # 新しい順の並びなので、末尾＝最も古い側。放置が長いものから予算を使う。
        inspected = unique_refs[-budget:] if len(unique_refs) > budget else unique_refs
        # 見ていないものを黙って「無い」に含めないため、正直に打ち切りを開示する。
        truncated = has_more or len(inspected) < len(unique_refs)

        sources = self._collect_inbox_sources(
            gmail,
            inspected,
            requester,
            ctx,
            exclude_bulk=env_bool("MAIL_EXCLUDE_BULK", True),
        )
        now_ms = self._now_ms if self._now_ms is not None else int(time.time() * 1000)
        # idle_days は顧客名あり経路と同じ意味で効かせる（「N 日以上放置のものだけ」）。
        # 窓を広げるだけで絞らないと、idle_days=10 の依頼に 2 日前の件を混ぜて返してしまう。
        metas = [
            src.meta
            for src in sources
            if idle_days is None or idle_days_of(src.meta.received_at_ms, now_ms) >= idle_days
        ]
        cands = rank_candidates(
            metas,
            now=_dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.UTC),
            limit=limit,
        )
        return InboxScan(
            candidates=cands,
            sources={src.meta.thread_id: src for src in sources},
            # 「確認した件数」は**実際に末尾まで見た**数（母数 listed とは別物）。
            scanned=len(inspected),
            truncated=truncated,
            window_days=window_days,
            idle_days=idle_days,
            listed=len(refs),
        )

    def _collect_inbox_sources(
        self,
        gmail: GmailClient,
        unique_refs: list[Any],
        requester: str,
        ctx: SkillContext,
        *,
        exclude_bulk: bool,
    ) -> list[InboxSource]:
        """スレッド末尾のヘッダだけを読み、判定層の入力へ詰め替える。

        ``GmailMessage.snippet`` は format='metadata' でも本文抜粋が返るが、
        :class:`InboxMailMeta` には置き場が無い＝**構造的に**本文が漏れない。
        """
        sources: list[InboxSource] = []
        for ref in unique_refs:
            thread_id = str(getattr(ref, "thread_id", "") or "")
            if not thread_id:
                continue
            anchor = _thread_anchor(gmail.get_thread(thread_id, ctx.request_id, format="metadata"))
            if anchor is None:
                continue
            headers = getattr(anchor, "headers", {}) or {}
            sources.append(
                InboxSource(
                    meta=InboxMailMeta(
                        thread_id=thread_id,
                        # G3: 件名は DLP マスクを通す（表示上限の切り詰めは判定層に任せる）。
                        subject=str(scrub_value(headers.get("Subject", ""))),
                        sender_name=_sender_display_name(headers),
                        # G3: 生アドレスは出さない。表示名が無い相手はマスク表示で出る。
                        sender_email=_mask_email(_sender_email(headers)),
                        received_at_ms=getattr(anchor, "internal_date_ms", None),
                        is_sole_recipient=_is_sole_recipient(headers, requester),
                        is_unreplied=not _is_from_requester(anchor, requester),
                        is_bulk=exclude_bulk and should_skip_mail(headers),
                    ),
                    anchor_id=str(getattr(anchor, "id", "") or ""),
                    counterpart=_first_counterpart(headers, requester),
                )
            )
        return sources

    @staticmethod
    def _triage_scan_limit(input: MailFollowupInput) -> int:
        """一覧走査の件数上限。既定 :data:`TRIAGE_SCAN_DEFAULT`、明示指定はそれを尊重する。

        ``max_messages`` の既定 30 は「顧客名で絞った 1 社ぶん」を想定した値なので、
        絞りの無い一覧走査にそのまま流用しない。一方でコスト上限を呼び出し側が下げたい
        （``max_messages=10``）意図は握り潰さないので、明示されたかどうかで分岐する。
        """
        if "max_messages" in input.model_fields_set:
            return input.max_messages
        return TRIAGE_SCAN_DEFAULT

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
            anchor = _thread_anchor(thread)
            if anchor is None:
                continue
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


def _build_inbox_query(lookback_days: int) -> str:
    """顧客名なし経路の走査クエリ（受信トレイ・期間・自分の送信除外）。

    ``morning_digest`` の受信箱クエリと同じ形（``-category:promotions -category:social``）に
    ``-in:sent`` を足したもの。**顧客名ありの :meth:`MailFollowupSkill._build_query` とは
    別関数**にしてあるのは、既存経路のクエリ文字列を 1 文字も動かさないため。
    """
    return f"in:inbox newer_than:{lookback_days}d -in:sent -category:promotions -category:social"


def _list_inbox_refs(
    gmail: GmailClient, query: str, request_id: str, *, cap: int
) -> tuple[list[Any], bool]:
    """``messages.list`` を ``cap`` 件までページングする。

    Returns:
        (refs（新しい順）, まだ先があるか)。``nextPageToken`` を捨てないのがこの関数の
        存在理由で、捨てると「直近 1 ページ」しか母数に入らない（＝古い側が消える）。
    """
    refs: list[Any] = []
    token: str | None = None
    while len(refs) < cap:
        page, token = gmail.list_messages(
            query,
            request_id,
            max_results=min(TRIAGE_LIST_PAGE, cap - len(refs)),
            page_token=token,
        )
        page = list(page)
        refs.extend(page)
        if not token or not page:
            break
    # トークンが残っている＝まだ先がある。「見ていないものがある」側に倒して開示する。
    return (refs[:cap], bool(token))


def _thread_anchor(thread: Sequence[Any]) -> Any | None:
    """スレッドの「最後の送受信」を返す（DRAFT は未送信なので数えない）。"""
    sent = [
        msg
        for msg in thread
        if "DRAFT"
        not in {str(label).strip().upper() for label in (getattr(msg, "label_ids", ()) or ())}
    ]
    if not sent:
        return None
    return sorted(sent, key=lambda msg: int(getattr(msg, "internal_date_ms", 0) or 0))[-1]


def _sender_display_name(headers: dict[str, str]) -> str:
    """From の表示名。**アドレスそのものが表示名の場合は空**にする（生アドレスを出さない）。"""
    name = parseaddr(str(headers.get("From", "") or ""))[0].strip()
    if "@" in name:
        return ""
    return str(scrub_value(name))


def _sender_email(headers: dict[str, str]) -> str:
    return parseaddr(str(headers.get("From", "") or ""))[1].strip()


def _addresses(value: str) -> set[str]:
    return {addr.strip().lower() for _, addr in getaddresses([value or ""]) if addr.strip()}


def _is_sole_recipient(headers: dict[str, str], requester: str) -> bool:
    """To が本人ひとりだけで Cc が無いか（＝相手はあなたの返事だけを待っている）。"""
    req = requester.strip().lower()
    if not req:
        return False
    return _addresses(str(headers.get("To", ""))) == {req} and not _addresses(
        str(headers.get("Cc", ""))
    )


def _followup_item(cand: TriageCandidate, source: InboxSource) -> FollowupItem:
    """候補 1 件を出力スキーマへ（G3: マスク済み・生 ID なし）。"""
    return FollowupItem(
        counterpart_masked=_mask_email(source.counterpart) if source.counterpart else "***",
        subject_scrubbed=cand.mail.subject[:80],
        idle_days=cand.idle_days,
        occurred_at=jst_iso_or_none(cand.mail.received_at_ms),
        occurred_at_display=jst_display_or_none(cand.mail.received_at_ms),
        evidence_ref=evidence_ref(source.anchor_id),
    )


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


def evidence_ref(msg_id: str) -> str:
    """:attr:`FollowupItem.evidence_ref` の値を再計算する（公開・一覧↔選択の突き合わせ用）。

    一覧を出した側と、選ばれた件を作る側（mail_draft）が **同じ鍵**で候補を照合するための
    入口。ハッシュなので生 messageId は外へ出ず、逆に外から任意のスレッドを指すこともできない
    （本人の受信箱を走査して一致したものだけが解決する）。

    anchor が取れなかった位置には :data:`VANISHED_REF` を返す。``hash("")`` のような
    「実在しそうな鍵」を出すと、後段が解決に失敗した理由を取り違える。**長さは必ず
    表示した一覧と一致させる**（詰めると次の「2番」が 3 番目の件を指す）。
    """
    return _hash_id(msg_id) if msg_id else VANISHED_REF


def _mask_email(email: str) -> str:
    """監査用の部分マスク（先頭1文字＋ドメイン）。例: s***@vectorinc.co.jp。"""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
