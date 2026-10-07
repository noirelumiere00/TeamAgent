"""attachment_assist Skill 本体 — 会話に添付されたファイルを読んで加工する（read-only）。

経路: Slack でファイルを @Aico に投げる → OpenClaw が SOUL 指示で本ツールを呼ぶ
（引数は mode / instruction / file_name のみ）→ mcp_gateway が **署名済み claim** 由来の
user_email / channel_id / thread_ts を注入（mcp_gateway/server.py の注入部）→ 本 Skill がその会話の
添付だけを発見・取得・本文化し、mode 別に整形して**テキストで**返す。

⚠️ 死守ライン:
  A1 **identity_verified 必須（fail-closed）**。mcp_gateway/server.py の注入部が宣言するとおり
     channel_id は本来「配信先ルーティング hint（identity ではない）」であり、LEGACY 経路
     （resolver 未注入）では **LLM 申告の channel_id がそのまま metadata に入る**。
     読取の認可鍵に昇格させてよいのは署名 claim 由来（identity_verified=True）だけなので、
     真でなければ PermissionError で即座に閉じる。
  A2 **会話内の添付のみ**。file_id / URL / channel を入力に持たない（schema.py 参照）。
     例外は投稿リンク（permalink・既定 OFF）で、本人 xoxp で読めた投稿の添付だけ（L1〜L4）。
  A3 **外部共有ファイルは触らない**。url_private へは bot token を載せて GET するため、
     is_external / external_type 付きは download 対象外（discover.evaluate_file ①）。
  A4 **ホスト allowlist**（files.slack.com 系）を通ってからしか GET しない
     （adapters/slack_file_guard の 1 実装を skill 事前選別と adapter 直前の両方で共有）。
  A5 **サイズは落とす前に拒否**（metadata の size）＋ダウンロードは逐次サイズ検査で切断。
  A6 G6 インジェクション遮断（prompts.SYSTEM_PROMPT）。文書内 URL へはアクセスしない。
  A7 ログは counts / sizes のみ（本文・ファイル名の中身を出さない）。

P1 スコープ: **テキスト返答のみ**。docx/xlsx/pdf/pptx を作って返す配信は P2
（USE_ATTACHMENT_RENDER・別フラグ・別リリース）。

投稿リンク経路（``permalink``・``ATTACHMENT_PERMALINK_ENABLED``・既定 OFF）:
  L1 リンク先の投稿は **依頼者本人の xoxp** で読む（見えなければ中身も存在も言わない一様文）。
     他ワークスペースのリンクは拒否（permalink.parse_permalink）。
  L2 ファイル本体は bot で取得（既存 download_file_guarded・A3〜A5 はそのまま効く）。
     bot が取れなければ「取り込めなかった」と正直に言う（添付し直しを頼まない）。
  L3 情報漏れの規則（permalink.may_repost）: 本人 DM での依頼か、同じチャンネルの投稿か、
     公開ファイルのときだけ中身を出し、元ファイルを依頼元へ添付し直す。それ以外は一様文。
  L4 書き込みは元ファイルの添付 1 回だけ（files.upload v2）。ログは件数・error code のみ。

3 層分離: 本ファイルは Skill 層。slack_sdk / boto3 は触らず adapters 経由。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import httpx
import structlog
from pydantic import BaseModel

from teamagent.adapters.slack_file_guard import SlackFileGuardError, slack_file_allowed_hosts
from teamagent.observability import redact_secrets
from teamagent.skills._shared.drive_slack_delivery import safe_filename
from teamagent.skills._shared.next_step import (
    ATTACHMENT_MODE_SUGGESTION,
    append_suggestion,
    suggestions_enabled,
    tool_enabled,
)
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.attachment_assist.aggregate import compute_xlsx_stats, format_stats_ja
from teamagent.skills.attachment_assist.discover import (
    REASON_BAD_URL,
    REASON_EXTERNAL,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED,
    AttachmentCandidate,
    collect_candidates,
    select_candidate,
)
from teamagent.skills.attachment_assist.focus import (
    focus_pages,
    focus_terms,
    format_page_list,
)
from teamagent.skills.attachment_assist.permalink import (
    ERR_BAD_PERMALINK,
    ERR_OTHER_WORKSPACE,
    may_repost,
    parse_permalink,
)
from teamagent.skills.attachment_assist.prompts import (
    MODE_SPECS,
    SYSTEM_PROMPT,
    build_user_message,
)
from teamagent.skills.attachment_assist.schema import (
    AttachmentAssistInput,
    AttachmentAssistOutput,
)
from teamagent.skills.base import BaseSkill, SkillContext, register

logger = structlog.get_logger(__name__)

# ダウンロード上限（Slack metadata の事前拒否と逐次検査の両方で使う）。
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024  # 30MB
# LLM へ渡す本文の hard cap。超過分は切って「冒頭のみ処理した」と決定的に伝える
# （translate/minutes を 1 回の converse で「全文」やろうとすると後半が無言で消える）。
MAX_INPUT_CHARS = 20_000
# 抽出（pypdf / OOXML パース）の壁時計上限。
EXTRACT_TIMEOUT_S = 45.0
# 抽出（本文化）で保持する字数の上限 = LLM へ渡す上限の何倍か。長い資料でも、依頼の語を
# 含むページを後半から拾えるように広めに抜く（LLM へ渡すのは MAX_INPUT_CHARS まで）。
EXTRACT_SCAN_FACTOR = 10
# PDF の走査ページ数上限（高圧縮 PDF の decompression bomb 対策）。
MAX_PDF_PAGES = 300
# スレッドが無い（DM 直投げ等）ときに遡るチャンネル履歴の件数。
HISTORY_LOOKBACK = 20
# 投稿リンク経路のスイッチ（読む範囲が会話の外へ広がるので既定 OFF）。
PERMALINK_FLAG = "ATTACHMENT_PERMALINK_ENABLED"
# 本人の権限で見えない・存在しない・出してはいけない、を区別しない error code。
_UNIFORM_DENY_CODES = frozenset(
    {
        "not_in_channel",
        "channel_not_found",
        "thread_not_found",
        "message_not_found",
        "is_archived",
        "access_denied",
        "bad_target",
    }
)
# bot がファイルを取れない＝権限の問題とみなす HTTP 状態（429・5xx は一時的な失敗）。
_BOT_DENIED_STATUSES = frozenset({403, 404})
# 本人の連携（xoxp）を作り直せば直る error code。
_RECONNECT_CODES = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
    }
)

_ERR_MSG: dict[str, str] = {
    "no_conversation": "どの会話のファイルか特定できませんでした（Slack 上でファイルを"
    "投稿したスレッドから話しかけてください）。",
    "no_attachment": "この会話に読み取れる添付ファイルが見つかりませんでした。",
    REASON_EXTERNAL: "外部サービス共有のファイル（Google Drive 等のリンク）は"
    "このツールでは開けません。Slack に直接アップロードしていただければ読み取れます。",
    REASON_TOO_LARGE: f"ファイルが大きすぎます（上限 {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB）。"
    "分割いただくか、必要な部分だけを共有してください。",
    REASON_UNSUPPORTED: "この形式には未対応です（PDF / Word / PowerPoint / Excel / "
    "テキスト系に対応しています）。",
    REASON_BAD_URL: "ファイルの取得先を確認できませんでした（Slack にアップロードされた"
    "ファイルのみ取り扱えます）。",
    "download_failed": "ファイルの取得に失敗しました。時間をおいて再度お試しください。",
    "extract_failed": "ファイルの中身を読み取れませんでした"
    "（パスワード保護・破損・画像のみの可能性があります）。",
    "empty_text": "ファイルからテキストを取り出せませんでした"
    "（スキャン画像だけの PDF などの可能性があります）。",
    "llm_failed": "内容の処理に失敗しました。時間をおいて再度お試しください。",
}

# 投稿リンク経路の文面。**利用者に添付し直し・保存先を頼む文を出さない**。
_PERMALINK_MSG: dict[str, str] = {
    ERR_BAD_PERMALINK: "Slack の投稿リンクの形を読み取れませんでした"
    "（https://…slack.com/archives/…/p… の形のリンクを扱えます）。",
    ERR_OTHER_WORKSPACE: "このワークスペース以外の Slack のリンクは開けません。",
    # 一様文（見えない・無い・この場に出せない、を区別しない）。DM とチャンネルで 2 種。
    "not_found_dm": "そのリンク先の投稿は、見つからないか、あなたの権限では見られませんでした。",
    "not_found_channel": "この場所では、そのリンク先の添付を開けません"
    "（投稿が無いか、ここにいる全員は見られない投稿の可能性があります）。"
    "本人の DM で同じ依頼をいただければ、あなたが見られる範囲で読み取ります。",
    "read_failed": "Slack から投稿を取得できませんでした（時間をおいて再度お試しください）。",
    "no_attachment": "そのリンク先の投稿には、読み取れる添付ファイルがありませんでした。",
    REASON_EXTERNAL: "リンク先の添付は外部サービス（Google Drive 等）のファイルのため、"
    "Aico では開けませんでした。",
    REASON_TOO_LARGE: "リンク先のファイルが大きすぎるため取り込めませんでした"
    f"（上限 {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB）。",
    REASON_BAD_URL: "リンク先のファイルの取得先を確認できなかったため、取り込めませんでした。",
    "bot_cannot_fetch": "投稿は確認できましたが、Aico がそのファイルを取り込めませんでした"
    "（Aico が参加していない場所のファイルの可能性があります）。",
    "permalink_disabled": "Slack の投稿リンク先の添付を読む機能は、まだ有効になっていません"
    "（リンク先の内容は読んでいません）。",
}

_KIND_LABEL: dict[str, str] = {
    "pdf": "PDF",
    "docx": "Word",
    "pptx": "PowerPoint",
    "xlsx": "Excel",
    "text": "テキスト",
}
_UNIT_LABEL: dict[str, str] = {
    "pdf": "ページ",
    "pptx": "スライド",
    "xlsx": "シート",
    "docx": "ページ",
    "text": "ページ",
}


@register
class AttachmentAssistSkill(BaseSkill[AttachmentAssistInput, AttachmentAssistOutput]):
    """会話に添付されたファイルを読んで要約・修正案・議事録化・集計・英訳する Skill。"""

    name: ClassVar[str] = "attachment_assist"
    # スイッチ OFF（既定）の説明＝この経路を足す前と同じ文。投稿リンクへ誘導しない。
    description: ClassVar[str] = (
        "いま話しているスレッド/チャンネルに**添付されたファイル**（PDF/Word/PowerPoint/"
        "Excel/テキスト）を読んで、要約・修正案・議事録フォーマット化・集計・英訳を返す"
        "読み取り専用ツール。ファイルが実際に添付されている時だけ使う。"
        "Drive 内の資料を探して取り出す依頼は knowledge_deliver を使うこと（別ツール）。"
        "ファイルの書き換え・生成・再配信はしない。" + USER_CONTEXT_RULE
    )
    # スイッチ ON の説明。「添付がある時だけ」「再配信しない」に投稿リンクの例外を明記する
    # （例外を書かないと、LLM が「添付が無いので使えない」「添付し直しはできない」と読む）。
    # スレッド・会話の要約は slack_summary（添付ファイルを読む依頼だけをここへ回す）。
    description_with_permalink: ClassVar[str] = (
        "いま話しているスレッド/チャンネルに**添付されたファイル**（PDF/Word/PowerPoint/"
        "Excel/テキスト）を読んで、要約・修正案・議事録フォーマット化・集計・英訳を返す"
        "ツール。ファイルが実際に添付されている時だけ使う（Slack 投稿リンクの先の**添付"
        "ファイル**を読む・添付してと頼まれた時は例外で、添付が無くても permalink に渡す。"
        "スレッドや会話の要約は slack_summary）。"
        "Drive 内の資料を探して取り出す依頼は knowledge_deliver を使うこと（別ツール）。"
        "ファイルの書き換え・生成はしない。再配信もしない（投稿リンクの場合だけ、元ファイルを"
        "この会話に 1 回添付し直す）。" + USER_CONTEXT_RULE
    )
    input_schema: ClassVar[type[BaseModel]] = AttachmentAssistInput
    output_schema: ClassVar[type[BaseModel]] = AttachmentAssistOutput

    @classmethod
    def tool_description(cls) -> str:
        """MCP に出す説明。投稿リンク経路のスイッチが ON のときだけ例外の文を含める。"""
        if tool_enabled(PERMALINK_FLAG):
            return cls.description_with_permalink
        return cls.description

    def __init__(
        self,
        *,
        slack: Any | None = None,
        ingest: Any | None = None,
        bedrock: Any | None = None,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
        max_input_chars: int = MAX_INPUT_CHARS,
        extract_timeout_s: float = EXTRACT_TIMEOUT_S,
        slack_store: Any | None = None,
        slack_store_factory: Callable[[], Any] | None = None,
        reader_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._slack = slack
        self._ingest = ingest
        self._bedrock = bedrock
        # 投稿リンク経路だけが使う本人 xoxp の保管庫（初回利用時に遅延生成）。
        self._slack_store = slack_store
        self._slack_store_factory = slack_store_factory
        self._reader_factory = reader_factory
        self._max_bytes = max_bytes
        self._max_input_chars = max_input_chars
        self._extract_timeout_s = extract_timeout_s

    # ── 本体 ────────────────────────────────────────────────────────────────

    def run(self, input: AttachmentAssistInput, ctx: SkillContext) -> AttachmentAssistOutput:
        log = ctx.bind_logger(self.name)

        # ── A1: 署名済み本人でなければ即閉じる（LEGACY の channel_id を認可鍵にしない）──
        if ctx.metadata.get("identity_verified") is not True:
            raise PermissionError(
                "attachment_assist は署名済み本人（identity_verified）でのみ使えます"
            )
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("attachment_assist は本人 user_email が必須です")

        # ── A2: 読む会話は claim 由来のものだけ（入力に channel を持たせていない）──
        channel_id = ctx.metadata.get("channel_id")
        channel_id = channel_id.strip() if isinstance(channel_id, str) else ""
        if not channel_id:
            return self._fail("no_conversation", input.mode)
        thread_ts = ctx.metadata.get("thread_ts")
        thread_ts = thread_ts.strip() if isinstance(thread_ts, str) else ""

        # ── 投稿リンク経路（既定 OFF）。OFF のときは何も読まずに正直に断る。
        #    リンクを無視して会話内を読むと、会話にある**別の添付**を「リンク先の資料」として
        #    要約してしまう（10-06 の本番スレッドには無関係な PDF が 3 件あった）。
        #    permalink が空の依頼は OFF/ON とも従来どおり。
        if input.permalink.strip():
            if not tool_enabled(PERMALINK_FLAG):
                log.info("attachment_assist_permalink_disabled")
                return self._permalink_fail("permalink_disabled", input.mode)
            return self._run_permalink(
                input, ctx, log, requester=requester, origin=channel_id, origin_thread=thread_ts
            )

        allowed_hosts = slack_file_allowed_hosts()

        # ── 会話内のファイル発見 ────────────────────────────────────────────
        try:
            messages = self._conversation_messages(channel_id, thread_ts, ctx.request_id)
        except Exception as e:
            log.warning("attachment_assist_history_failed", err=type(e).__name__)
            return self._fail("no_attachment", input.mode)

        candidates, rejected = collect_candidates(
            messages,
            max_bytes=self._max_bytes,
            allowed_hosts=allowed_hosts,
            request_id=ctx.request_id,
        )
        log.info(
            "attachment_assist_scan",
            source="thread" if thread_ts else "history",
            messages=len(messages),
            candidates=len(candidates),
            rejected=len(rejected),
        )  # ファイル名・本文はログに出さない

        if not candidates:
            # 拒否理由があるならそれを返す（「見つからない」と混同しない）。
            if rejected:
                reason = _worst_reason([r.reason for r in rejected])
                return self._fail(reason, input.mode)
            return self._fail("no_attachment", input.mode)

        target = select_candidate(candidates, input.file_name)
        if target is None:
            names = [c.name for c in candidates]
            return AttachmentAssistOutput(
                mode=input.mode,
                other_files=names,
                error="no_attachment",
                message=(
                    f"「{input.file_name}」に一致する添付が見つかりませんでした。"
                    f"この会話にあるのは {'、'.join(names)} です。"
                ),
            )
        others = [c.name for c in candidates if c.file_id != target.file_id]

        # ── 取得（A4 ホスト検証 + A5 逐次サイズ検査）───────────────────────
        try:
            data = self._download(target, ctx.request_id, allowed_hosts)
        except SlackFileGuardError as e:
            log.warning("attachment_assist_download_blocked", err=str(e).split(":", 1)[0])
            reason = REASON_TOO_LARGE if "TOO_LARGE" in str(e) else REASON_BAD_URL
            return self._fail(reason, input.mode, file_name=target.name, others=others)
        except Exception as e:
            log.warning("attachment_assist_download_failed", err=type(e).__name__)
            return self._fail("download_failed", input.mode, file_name=target.name, others=others)
        return self._answer(input, ctx, log, target=target, others=others, data=data)

    def _answer(
        self,
        input: AttachmentAssistInput,
        ctx: SkillContext,
        log: Any,
        *,
        target: AttachmentCandidate,
        others: list[str],
        data: bytes,
        others_in_link: bool = False,
    ) -> AttachmentAssistOutput:
        """取得済みのファイルを本文化 → mode 別に処理 → 決定的な文面に整える（両経路で共有）。

        ``others_in_link`` は投稿リンク経路（others はリンク先の投稿にある別ファイル）。
        """
        # ── 本文化（既存抽出器の zip-bomb / 文字数 cap を活かす）───────────
        try:
            pages = self._extract(target, data)
        except TimeoutError:
            log.warning("attachment_assist_extract_timeout", kind=target.kind)
            return self._fail("extract_failed", input.mode, file_name=target.name, others=others)
        except Exception as e:
            log.warning("attachment_assist_extract_failed", kind=target.kind, err=type(e).__name__)
            return self._fail("extract_failed", input.mode, file_name=target.name, others=others)

        body_raw = "\n\n".join(text for _, text in pages if text.strip())
        if not body_raw.strip():
            return self._fail("empty_text", input.mode, file_name=target.name, others=others)

        # シークレットだけ落とす（scrub_value は 2000 字 hard cap を持つので使わない）。
        body = redact_secrets(body_raw)
        truncated = len(body) > self._max_input_chars
        # 長いときは、依頼文に出てくる語を含むページ（と前後）を優先して詰める。
        # 先頭から切るだけだと、事例集の後半にある指定事例が LLM に届かない（10-06 本番）。
        terms: list[str] = []
        hit_pages: tuple[int, ...] = ()
        if truncated:
            terms = focus_terms(input.instruction, exclude=[input.file_name, target.name])
            focused = focus_pages(pages, terms, budget=self._max_input_chars)
            if focused is not None:
                body = redact_secrets(focused.body)
                hit_pages = focused.hit_pages
            body = body[: self._max_input_chars]

        # ── aggregate は数値を Python で決定的に出し、LLM には整形だけさせる ──
        precomputed = ""
        if input.mode == "aggregate" and target.kind == "xlsx":
            try:
                precomputed = format_stats_ja(compute_xlsx_stats(data))
            except Exception as e:
                log.warning("attachment_assist_aggregate_failed", err=type(e).__name__)
                precomputed = ""

        answer, cost = self._process(
            mode=input.mode,
            instruction=input.instruction,
            file_name=target.name,
            body=body,
            truncated=truncated,
            precomputed=precomputed,
            ctx=ctx,
            focused=bool(hit_pages),
        )
        if not answer:
            return self._fail("llm_failed", input.mode, file_name=target.name, others=others)

        message = _compose_message(
            target=target,
            mode=input.mode,
            pages=len(pages),
            chars=len(body),
            truncated=truncated,
            answer=answer,
            others=others,
            aggregated=bool(precomputed),
            searched_terms=bool(terms),
            hit_pages=hit_pages,
            others_in_link=others_in_link,
        )
        log.info(
            "attachment_assist_done",
            mode=input.mode,
            kind=target.kind,
            pages=len(pages),
            chars=len(body),
            truncated=truncated,
            bytes=len(data),
            cost_usd=cost,
        )
        return AttachmentAssistOutput(
            file_name=target.name,
            kind=target.kind,
            pages=len(pages),
            chars=len(body),
            truncated=truncated,
            mode=input.mode,
            other_files=others,
            message=message,
            total_cost_usd=cost,
        )

    # ── 投稿リンク経路 ──────────────────────────────────────────────────────

    def _run_permalink(
        self,
        input: AttachmentAssistInput,
        ctx: SkillContext,
        log: Any,
        *,
        requester: str,
        origin: str,
        origin_thread: str,
    ) -> AttachmentAssistOutput:
        """投稿リンクの先の添付を本人の権限で確かめて読み、依頼元へ原本を添付し直す。"""
        mode = input.mode
        deny_key = "not_found_dm" if is_private_surface(origin, True) else "not_found_channel"

        # ── L1: 形とワークスペース（他 WS・設定なしは拒否）────────────────
        link, err = parse_permalink(input.permalink)
        if link is None:
            log.info("attachment_assist_permalink_rejected", reason=err)
            return self._permalink_fail(err, mode)

        # ── L1: 本人 xoxp で「見えるか」を確かめて 1 件取る ────────────────
        reader = self._resolve_reader(requester, log)
        if reader is None:
            return self._not_connected(ctx, mode)
        read = reader.read_message_checked(
            link.channel_id, link.ts, ctx.request_id, thread_ts=link.thread_ts
        )
        if read.error:
            log.info("attachment_assist_permalink_read_denied", slack_error=read.error)
            if read.error in _RECONNECT_CODES:
                return self._not_connected(ctx, mode)
            key = deny_key if read.error in _UNIFORM_DENY_CODES else "read_failed"
            return self._permalink_fail(key, mode, error="not_found" if key == deny_key else "")

        # ── L3: 情報漏れの規則。出してよいファイルだけを候補に残す ─────────
        allowed_hosts = slack_file_allowed_hosts()
        candidates, rejected = collect_candidates(
            read.messages,
            max_bytes=self._max_bytes,
            allowed_hosts=allowed_hosts,
            request_id=ctx.request_id,
        )
        restricted = not may_repost(
            origin_channel=origin,
            identity_verified=True,
            source_channel=link.channel_id,
            file_is_public=False,
        )
        if restricted:
            # 別チャンネルからの依頼: 公開ファイルだけ。理由や件数も出さない（存在を言わない）。
            candidates = [
                c
                for c in candidates
                if may_repost(
                    origin_channel=origin,
                    identity_verified=True,
                    source_channel=link.channel_id,
                    file_is_public=c.is_public,
                    file_public_channels=c.public_channels,
                )
            ]
            rejected = []
        log.info(
            "attachment_assist_permalink_scan",
            restricted=restricted,
            candidates=len(candidates),
            rejected=len(rejected),
        )  # ファイル名・チャンネル・本文はログに出さない
        if not candidates:
            if restricted:
                return self._permalink_fail(deny_key, mode, error="not_found")
            if rejected:
                return self._permalink_fail(_worst_reason([r.reason for r in rejected]), mode)
            return self._permalink_fail("no_attachment", mode)

        target = select_candidate(candidates, input.file_name)
        if target is None:
            names = [c.name for c in candidates]
            return AttachmentAssistOutput(
                mode=mode,
                other_files=names,
                error="no_attachment",
                message=(
                    f"「{input.file_name}」に一致する添付がリンク先の投稿にありませんでした。"
                    f"その投稿にあるのは {'、'.join(names)} です。"
                ),
            )
        others = [c.name for c in candidates if c.file_id != target.file_id]

        # ── L2: 本体は bot で取得（ホスト検証・逐次サイズ検査は既存のまま）───
        # 「投稿は確認できました」は、別チャンネルからの依頼では投稿の実在を漏らすので
        # 一様文に置き換える（restricted のときは中身も存在も言わない）。
        def cannot_fetch() -> AttachmentAssistOutput:
            if restricted:
                return self._permalink_fail(deny_key, mode, error="not_found")
            return self._permalink_fail(
                "bot_cannot_fetch", mode, file_name=target.name, others=others
            )

        try:
            data = self._download(target, ctx.request_id, allowed_hosts)
        except SlackFileGuardError as e:
            code = str(e).split(":", 1)[0]
            log.warning("attachment_assist_permalink_download_blocked", err=code)
            if "TOO_LARGE" in code:
                return self._permalink_fail(
                    REASON_TOO_LARGE, mode, file_name=target.name, others=others
                )
            return cannot_fetch()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            log.warning("attachment_assist_permalink_download_denied", status=status)
            if status in _BOT_DENIED_STATUSES:
                return cannot_fetch()
            # 429・5xx は一時的な失敗＝権限の問題と言わない（時間をおいて再度）。
            return self._fail("download_failed", mode, file_name=target.name, others=others)
        except Exception as e:
            log.warning("attachment_assist_permalink_download_failed", err=type(e).__name__)
            return self._fail("download_failed", mode, file_name=target.name, others=others)
        if _looks_like_login_page(target, data):
            # 権限の無い bot に Slack はログイン画面の HTML を 200 で返すことがある。
            log.warning("attachment_assist_permalink_download_denied", status="html")
            return cannot_fetch()

        # ── L4: 依頼元へ原本を添付し直す（唯一の書き込み）→ 読んで答える ───
        #    同じ会話に既にある（リンク先がこのスレッドの投稿・直前に Aico が添付済み）なら
        #    添付し直さない＝聞き直しのたびに同じ原本を重複して投下しない。
        repost_key = (origin, origin_thread, target.file_id)
        if _same_conversation(link, origin, origin_thread):
            attached, skipped = False, "here"
        elif _recently_reposted(repost_key):
            attached, skipped = False, "already"
        else:
            attached = self._repost(target, data, origin, origin_thread, ctx.request_id, log)
            skipped = ""
            if attached:
                _remember_repost(repost_key)
        out = self._answer(
            input, ctx, log, target=target, others=others, data=data, others_in_link=True
        )
        if skipped == "here":
            note = f"📎 元ファイル（{target.name}）は、この会話のリンク先の投稿に添付されています。"
        elif skipped == "already":
            note = f"📎 元ファイル（{target.name}）は、この会話に添付済みです。"
        elif attached:
            note = f"📎 元ファイル（{target.name}）をこの会話に添付しました。"
        else:
            note = "※ 元ファイルをこの会話へ添付できませんでした（Slack への添付に失敗しました）。"
        log.info(
            "attachment_assist_permalink_done",
            attached=attached,
            skipped=skipped,
            failed=bool(out.error),
        )
        return out.model_copy(update={"attached": attached, "message": f"{out.message}\n\n{note}"})

    def _permalink_fail(
        self,
        key: str,
        mode: str,
        *,
        error: str = "",
        file_name: str = "",
        others: list[str] | None = None,
    ) -> AttachmentAssistOutput:
        return AttachmentAssistOutput(
            file_name=file_name,
            mode=mode,
            other_files=others or [],
            error=error or key,
            message=_PERMALINK_MSG.get(key) or _ERR_MSG.get(key) or _PERMALINK_MSG["read_failed"],
        )

    def _not_connected(self, ctx: SkillContext, mode: str) -> AttachmentAssistOutput:
        from teamagent.skills.slack_summary.skill import _connection_message

        return AttachmentAssistOutput(
            mode=mode,
            error="not_connected",
            message=_connection_message(ctx, purpose="投稿リンクの読み取り"),
        )

    def _resolve_reader(self, requester: str, log: Any) -> Any | None:
        """本人 xoxp から SlackUserReader を作る。未連携・失敗は None（bot token は使わない）。"""
        store = self._slack_store
        if store is None and self._slack_store_factory is not None:
            try:
                store = self._slack_store = self._slack_store_factory()
            except Exception as e:
                log.warning("attachment_assist_store_failed", err=type(e).__name__)
                return None
        if store is None:
            log.info("attachment_assist_not_connected", reason="no_store")
            return None
        try:
            tok = store.get(requester)
        except Exception as e:
            log.warning("attachment_assist_store_failed", err=type(e).__name__)
            return None
        if tok is None or not getattr(tok, "access_token", ""):
            log.info("attachment_assist_not_connected", reason="no_token")
            return None
        factory = self._reader_factory
        if factory is None:
            from teamagent.adapters.slack_user_reader import SlackUserReader

            factory = SlackUserReader.from_user_token
        try:
            return factory(tok.access_token)
        except Exception as e:
            log.warning("attachment_assist_reader_failed", err=type(e).__name__)
            return None

    def _repost(
        self,
        target: AttachmentCandidate,
        data: bytes,
        channel: str,
        thread_ts: str,
        request_id: str,
        log: Any,
    ) -> bool:
        """取得した原本を依頼元へ添付する（既存 upload_file・失敗は False）。

        一時ファイル名は中身を表さない名前にする（upload_file のログに path が出るため）。
        表示名は ``filename`` で渡す。
        """
        slack = self._slack or self._build_slack()
        fd, path = tempfile.mkstemp(prefix="aila_permalink_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            name = safe_filename(target.name)
            return bool(
                asyncio.run(
                    slack.upload_file(
                        channel,
                        path,
                        request_id,
                        title=name,
                        filename=name,
                        thread_ts=thread_ts or None,
                    )
                )
            )
        except Exception as e:
            log.warning("attachment_assist_permalink_upload_failed", err=type(e).__name__)
            return False
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ── 依存解決・下請け ────────────────────────────────────────────────────

    def _fail(
        self,
        error: str,
        mode: str,
        *,
        file_name: str = "",
        others: list[str] | None = None,
    ) -> AttachmentAssistOutput:
        return AttachmentAssistOutput(
            file_name=file_name,
            mode=mode,
            other_files=others or [],
            error=error,
            message=_ERR_MSG.get(error, _ERR_MSG["no_attachment"]),
        )

    def _conversation_messages(self, channel_id: str, thread_ts: str, request_id: str) -> list[Any]:
        """claim 由来の会話のメッセージを読む（スレッドが無ければ直近 N 件の履歴）。"""
        ingest = self._ingest or self._build_ingest()
        if thread_ts:
            batch = ingest.list_thread_replies(channel_id, thread_ts, request_id)
        else:
            batch = ingest.list_channel_history(channel_id, request_id, limit=HISTORY_LOOKBACK)
        return list(batch.messages)

    def _download(
        self,
        target: AttachmentCandidate,
        request_id: str,
        allowed_hosts: frozenset[str],
    ) -> bytes:
        slack = self._slack or self._build_slack()
        return bytes(
            asyncio.run(
                slack.download_file_guarded(
                    target.url,
                    request_id=request_id,
                    max_bytes=self._max_bytes,
                    allowed_hosts=allowed_hosts,
                )
            )
        )

    def _extract(self, target: AttachmentCandidate, data: bytes) -> list[tuple[int, str]]:
        """抽出を別スレッドで壁時計上限つきに実行する（超えたら **待たずに** 見切る）。

        ⚠️ ``asyncio.run(asyncio.wait_for(asyncio.to_thread(...)))`` にしてはいけない。
        ``asyncio.run`` は終了時に既定 executor の join を待つため、**timeout しても
        抽出スレッドが終わるまで戻ってこない**（実測: 2 秒かかる抽出 × timeout 0.05 秒で
        2.008 秒ブロック。同条件の ThreadPoolExecutor は 0.055 秒）。それでは
        「重いファイルで mcp タスクを占有させない」という目的を果たさない。

        二段構え:
          1. office 抽出は ``progress_callback`` の deadline で**協調的に**打ち切る
             （スレッド自体が止まる）。
          2. それでも返らないケースは ``future.result(timeout=...)`` で見切り、
             executor を ``wait=False`` で捨てる。
        """
        deadline = time.monotonic() + self._extract_timeout_s

        def _work() -> list[tuple[int, str]]:
            return _extract_pages(
                target,
                data,
                max_chars=self._max_input_chars * EXTRACT_SCAN_FACTOR // 2,
                deadline=deadline,
            )

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attach-extract")
        try:
            # concurrent.futures.TimeoutError は Python 3.11+ で組込 TimeoutError と同一。
            return pool.submit(_work).result(timeout=self._extract_timeout_s)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _process(
        self,
        *,
        mode: str,
        instruction: str,
        file_name: str,
        body: str,
        truncated: bool,
        precomputed: str,
        ctx: SkillContext,
        focused: bool = False,
    ) -> tuple[str, float]:
        if self._bedrock is None:
            from teamagent.adapters.bedrock_client import BedrockClient

            self._bedrock = BedrockClient.from_env()
        spec = MODE_SPECS[mode]
        user_message = build_user_message(
            mode=mode,
            instruction=instruction,
            file_name=file_name,
            body=body,
            truncated=truncated,
            precomputed=precomputed,
            focused=focused,
        )
        try:
            resp = self._bedrock.converse(
                messages=[{"role": "user", "content": [{"text": user_message}]}],
                request_id=ctx.request_id,
                system=SYSTEM_PROMPT,
                cache_system=True,
                max_tokens=spec.max_tokens,
            )
        except Exception:
            logger.warning("attachment_assist_llm_failed", request_id=ctx.request_id)
            return ("", 0.0)
        return (
            str(resp.text).strip(),
            float(getattr(getattr(resp, "usage", None), "cost_usd", 0.0)),
        )

    def _build_slack(self) -> Any:
        from teamagent.adapters.slack_client import SlackClient

        return SlackClient.from_env()

    def _build_ingest(self) -> Any:
        from teamagent.adapters.slack_channel_ingest_client import SlackChannelIngestClient

        return SlackChannelIngestClient.from_env()


# ── モジュール関数（純粋・テスト容易）──────────────────────────────────────

# 投稿リンク経路で「この会話にこの原本を添付済み」を覚える時間（同じ mcp プロセス内だけ）。
# 聞き直し（「同じPDFの他の事例も」）で同じ原本が何度も投下されるのを防ぐ。プロセスが
# 替われば忘れる（その場合は 1 回だけ重複し得る）。
REPOST_MEMORY_S = 6 * 60 * 60
_REPOSTED: dict[tuple[str, str, str], float] = {}
_REPOSTED_LOCK = threading.Lock()


def _recently_reposted(key: tuple[str, str, str]) -> bool:
    now = time.monotonic()
    with _REPOSTED_LOCK:
        for k, at in list(_REPOSTED.items()):
            if now - at > REPOST_MEMORY_S:
                del _REPOSTED[k]
        return key in _REPOSTED


def _remember_repost(key: tuple[str, str, str]) -> None:
    with _REPOSTED_LOCK:
        _REPOSTED[key] = time.monotonic()


def _same_conversation(link: Any, origin: str, origin_thread: str) -> bool:
    """リンク先の投稿が、いま話しているスレッドそのものにあるか（添付し直す必要が無い）。"""
    if origin != link.channel_id or not origin_thread:
        return False
    return origin_thread in (link.ts, link.thread_ts)


def _deadline_callback(deadline: float | None) -> Callable[[], None] | None:
    """office 抽出の heartbeat で期限超過を**協調的に**打ち切る hook を作る。

    ``office_extract._report_progress`` は callback の例外を
    ``_OfficeProgressCallbackError`` で包み、``extract_office_pages`` が cause を
    そのまま再送出する＝ここで投げた ``TimeoutError`` が呼び出し側へ届く
    （payload 破損とは混同されない）。
    """
    if deadline is None:
        return None

    def _cb() -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("office extraction exceeded deadline")

    return _cb


def _extract_pages(
    target: AttachmentCandidate, data: bytes, *, max_chars: int, deadline: float | None = None
) -> list[tuple[int, str]]:
    """kind に応じて既存抽出器へ dispatch する（上限は必ず明示で渡す）。"""
    if target.kind == "pdf":
        from teamagent.ingest.pdf_extract import extract_pdf_pages

        return extract_pdf_pages(data, max_pages=MAX_PDF_PAGES, max_total_chars=max_chars * 2)
    if target.kind in ("docx", "pptx", "xlsx"):
        from teamagent.ingest.office_extract import (
            DOCX_MIME,
            PPTX_MIME,
            XLSX_MIME,
            extract_office_pages,
        )

        mime = {"docx": DOCX_MIME, "pptx": PPTX_MIME, "xlsx": XLSX_MIME}[target.kind]
        return extract_office_pages(
            data,
            mime,
            include_notes=True,
            include_tables=True,
            max_extracted_chars=max_chars * 2,
            progress_callback=_deadline_callback(deadline),
        )
    text = data.decode("utf-8", errors="replace").strip()
    return [(1, text)] if text else []


_HTML_PREFIXES = (b"<!doctype html", b"<html")
_HTML_EXTENSIONS = (".html", ".htm", ".xhtml")


def _looks_like_login_page(target: AttachmentCandidate, data: bytes) -> bool:
    """HTML の原本でないのに HTML が返ってきた＝権限が無くログイン画面を返された、とみなす。

    テキスト系（csv / txt / md 等）も対象にする（ログイン画面の HTML を原本として要約・
    添付し直さない）。元から HTML のファイル（名前が .html 等・mimetype が text/html）だけは
    中身が HTML で当然なので判定しない。
    """
    if not data[:512].lstrip().lower().startswith(_HTML_PREFIXES):
        return False
    if target.kind == "text":
        name = target.name.strip().lower()
        mime = target.mime.split(";", 1)[0].strip().lower()
        if mime in ("text/html", "application/xhtml+xml") or name.endswith(_HTML_EXTENSIONS):
            return False
    return True


def _worst_reason(reasons: list[str]) -> str:
    """複数の拒否理由から、利用者に伝えるべき 1 つを決定的に選ぶ。

    「大きすぎ」「外部ファイル」は利用者が対処できる情報なので優先度を高くする。
    """
    for r in (REASON_TOO_LARGE, REASON_EXTERNAL, REASON_UNSUPPORTED, REASON_BAD_URL):
        if r in reasons:
            return r
    return "no_attachment"


def _compose_message(
    *,
    target: AttachmentCandidate,
    mode: str,
    pages: int,
    chars: int,
    truncated: bool,
    answer: str,
    others: list[str],
    aggregated: bool,
    searched_terms: bool = False,
    hit_pages: tuple[int, ...] = (),
    others_in_link: bool = False,
) -> str:
    """決定的な見出し＋LLM 本文＋注記。LLM にこの整形をさせない。

    長い資料の注記は 3 通り。依頼に語があれば「該当箇所を指定して」と作業を戻さない
    （利用者は依頼文で既に指定している）:
      - 語を含むページを優先して詰めた → どのページを読んだかを書く
      - 語はあったが資料に見当たらない → 冒頭だけ読んだ・見当たらなかった、と正直に書く
      - 語が無い（「要約して」だけ）→ 従来どおり冒頭だけ・続きは指定を、と書く
    """
    spec = MODE_SPECS[mode]
    unit = _UNIT_LABEL.get(target.kind, "ページ")
    kind_label = _KIND_LABEL.get(target.kind, target.kind)
    head = f"📄 {target.name}（{kind_label}・{pages}{unit}）の{spec.label}"
    parts = [head, "", answer.strip()]
    notes: list[str] = []
    # 出典 URL 方針: 読んだ「原本」（Slack 上のそのファイル）へのリンクを必ず添える。
    # Slack が返した permalink をそのまま使う（自作・推測はしない。無ければ省略）。
    if target.permalink:
        parts.extend(["", f"🔗 出典: {target.permalink}"])
    if truncated and hit_pages:
        notes.append(
            f"※ 資料が長いため、ご依頼の語を含む {format_page_list(hit_pages)} {unit}目と"
            f"その前後を優先して、{chars:,} 文字ぶんを処理しました。"
        )
    elif truncated and searched_terms:
        notes.append(
            f"※ 資料が長いため冒頭 {chars:,} 文字ぶんのみを処理しました"
            "（ご依頼の語は、読み取れた範囲には見当たりませんでした）。"
        )
    elif truncated:
        notes.append(
            f"※ 資料が長いため冒頭 {chars:,} 文字ぶんのみを処理しました"
            "（続きが必要なら該当箇所を指定してください）。"
        )
    if aggregated:
        notes.append(
            "※ 集計値はセル値から機械的に算出しています（AI が数えたものではありません）。"
        )
    if spec.footer:
        notes.append(spec.footer)
    if others and others_in_link:
        notes.append(
            f"※ リンク先の投稿には他に {'、'.join(others)} もあります"
            "（ファイル名を指定していただければ、そちらを読みます）。"
        )
    elif others:
        notes.append(
            f"※ この会話には他に {'、'.join(others)} もあります"
            "（file_name で指定すると切り替えられます）。"
        )
    if notes:
        parts.extend(["", "\n".join(notes)])
    # 次の一手: summary の結果にだけ「他モードもできる」を 1 個だけ添える
    # （revise/minutes/translate は同じツールの別 mode ＝必ず実在する）。
    return _with_mode_suggestion("\n".join(parts).strip(), mode)


def _with_mode_suggestion(message: str, mode: str) -> str:
    """``mode=summary`` の結果末尾に他モードの案内を 1 個だけ添える（決定論）。

    要約以外（revise/minutes/aggregate/translate）は利用者が既に目的を指定して
    呼んでいる＝依頼が完結しているので提案しない。
    """
    if mode != "summary" or not suggestions_enabled():
        return message
    return append_suggestion(message, ATTACHMENT_MODE_SUGGESTION)
