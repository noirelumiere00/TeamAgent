"""attachment_assist Skill 本体 — 会話に添付されたファイルを読んで加工する（read-only）。

経路: Slack でファイルを @Aico に投げる → OpenClaw が SOUL 指示で本ツールを呼ぶ
（引数は mode / instruction / file_name のみ）→ mcp_gateway が **署名済み claim** 由来の
user_email / channel_id / thread_ts を注入（server.py:441-445）→ 本 Skill がその会話の
添付だけを発見・取得・本文化し、mode 別に整形して**テキストで**返す。

⚠️ 死守ライン:
  A1 **identity_verified 必須（fail-closed）**。server.py:442-443 が宣言するとおり
     channel_id は本来「配信先ルーティング hint（identity ではない）」であり、LEGACY 経路
     （resolver 未注入）では **LLM 申告の channel_id がそのまま metadata に入る**。
     読取の認可鍵に昇格させてよいのは署名 claim 由来（identity_verified=True）だけなので、
     真でなければ PermissionError で即座に閉じる。
  A2 **会話内の添付のみ**。file_id / URL / channel を入力に持たない（schema.py 参照）。
  A3 **外部共有ファイルは触らない**。url_private へは bot token を載せて GET するため、
     is_external / external_type 付きは download 対象外（discover.evaluate_file ①）。
  A4 **ホスト allowlist**（files.slack.com 系）を通ってからしか GET しない
     （adapters/slack_file_guard の 1 実装を skill 事前選別と adapter 直前の両方で共有）。
  A5 **サイズは落とす前に拒否**（metadata の size）＋ダウンロードは逐次サイズ検査で切断。
  A6 G6 インジェクション遮断（prompts.SYSTEM_PROMPT）。文書内 URL へはアクセスしない。
  A7 ログは counts / sizes のみ（本文・ファイル名の中身を出さない）。

P1 スコープ: **テキスト返答のみ**。docx/xlsx/pdf/pptx を作って返す配信は P2
（USE_ATTACHMENT_RENDER・別フラグ・別リリース）。

3 層分離: 本ファイルは Skill 層。slack_sdk / boto3 は触らず adapters 経由。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.slack_file_guard import SlackFileGuardError, slack_file_allowed_hosts
from teamagent.observability import redact_secrets
from teamagent.skills._shared.next_step import (
    ATTACHMENT_MODE_SUGGESTION,
    append_suggestion,
    suggestions_enabled,
)
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
# PDF の走査ページ数上限（高圧縮 PDF の decompression bomb 対策）。
MAX_PDF_PAGES = 300
# スレッドが無い（DM 直投げ等）ときに遡るチャンネル履歴の件数。
HISTORY_LOOKBACK = 20

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
    description: ClassVar[str] = (
        "いま話しているスレッド/チャンネルに**添付されたファイル**（PDF/Word/PowerPoint/"
        "Excel/テキスト）を読んで、要約・修正案・議事録フォーマット化・集計・英訳を返す"
        "読み取り専用ツール。ファイルが実際に添付されている時だけ使う。"
        "Drive 内の資料を探して取り出す依頼は knowledge_deliver を使うこと（別ツール）。"
        "ファイルの書き換え・生成・再配信はしない。" + USER_CONTEXT_RULE
    )
    input_schema: ClassVar[type[BaseModel]] = AttachmentAssistInput
    output_schema: ClassVar[type[BaseModel]] = AttachmentAssistOutput

    def __init__(
        self,
        *,
        slack: Any | None = None,
        ingest: Any | None = None,
        bedrock: Any | None = None,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
        max_input_chars: int = MAX_INPUT_CHARS,
        extract_timeout_s: float = EXTRACT_TIMEOUT_S,
    ) -> None:
        self._slack = slack
        self._ingest = ingest
        self._bedrock = bedrock
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
        if truncated:
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
            return _extract_pages(target, data, max_chars=self._max_input_chars, deadline=deadline)

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
) -> str:
    """決定的な見出し＋LLM 本文＋注記。LLM にこの整形をさせない。"""
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
    if truncated:
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
    if others:
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
