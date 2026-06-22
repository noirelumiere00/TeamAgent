"""knowledge_deliver: 社内ナレッジを検索し、該当資料の実ファイルを依頼者 DM に届ける。

「〇〇業界の提案資料出して」「〇〇の成功事例ある？」「〇〇のレポート出して」のような、
**ファイル本体が欲しい**依頼に応える。リンクだけで良い時は search を使う。

設計:
- 検索＋要約は SearchSkill をそのまま再利用（Phase1 の自動分類フィルタも効く）。
- ヒットの source_uri から Drive file_id を解決し download_file_bytes で実体取得。
- 依頼者本人（ctx.metadata["user_email"]）の DM を開いて upload_file で添付する。
- skill.run は同期だが dispatch が thread 実行するため、Slack 非同期呼び出しは asyncio.run で駆動。
- どこで失敗しても要約テキストは返す（fail-open）。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.knowledge_deliver.schema import (
    KnowledgeDeliverInput,
    KnowledgeDeliverOutput,
    KnowledgeRef,
)
from teamagent.skills.search.knowledge_query import extract_query_industry
from teamagent.skills.search.schema import SearchInput

logger = structlog.get_logger(__name__)

_DRIVE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
_DRIVE_QUERY_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")


def extract_drive_file_id(source_uri: str | None) -> str | None:
    """source_uri（`gdrive://FILE_ID` or Drive web リンク）から file_id を取り出す。"""
    if not source_uri:
        return None
    s = source_uri.strip()
    if s.startswith("gdrive://"):
        fid = s[len("gdrive://") :].strip().strip("/")
        return fid or None
    m = _DRIVE_ID_RE.search(s)
    if m:
        return m.group(1)
    m2 = _DRIVE_QUERY_ID_RE.search(s)
    if m2:
        return m2.group(1)
    return None


def _safe_filename(name: str | None, *, fallback: str = "document") -> str:
    base = (name or "").strip() or fallback
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", base)
    return base[:120] or fallback


@register
class KnowledgeDeliverSkill(BaseSkill[KnowledgeDeliverInput, KnowledgeDeliverOutput]):
    """検索 → 該当資料の実ファイルを依頼者 DM に届けるスキル。"""

    name: ClassVar[str] = "knowledge_deliver"
    description: ClassVar[str] = (
        "「〇〇業界の提案資料出して」「〇〇の成功事例ある？」「〇〇のレポート出して」のような依頼に対し、"
        "社内ナレッジを検索して要約し、該当資料の実ファイルを依頼者本人の DM に添付して届ける。"
        "ファイル本体が欲しい時に使う（リンク・要約だけで良い時は search）。"
        "呼び出し時は arguments に "
        "`_user_context: {slack_user_id: '<Slack相手のuser_id>'}` を必ず含める。"
    )
    input_schema: ClassVar[type[BaseModel]] = KnowledgeDeliverInput
    output_schema: ClassVar[type[BaseModel]] = KnowledgeDeliverOutput

    def __init__(self, *, search: Any = None, slack: Any = None, gdrive: Any = None) -> None:
        # search は factory が共有 SearchSkill を注入する（埋め込み二重ロード回避）。
        self._search = search
        self._slack = slack
        self._gdrive = gdrive

    def run(self, input: KnowledgeDeliverInput, ctx: SkillContext) -> KnowledgeDeliverOutput:
        log = ctx.bind_logger(self.name)

        # 1. 検索＋要約（Phase1 の分類フィルタ・再ランクをそのまま通す）。
        search = self._search or self._build_search()
        # 配信候補を広めに取るため top_k は最低 5 で検索し、添付は input.top_k 件に絞る。
        s_out = search.run(
            SearchInput(
                query=input.query,
                top_k=max(input.top_k, 5),
                filter_industry=input.filter_industry,
            ),
            ctx,
        )

        # 2. ヒットから「Drive 実体があり、かつ確信を持って関連が高い資料」だけを配信候補に。
        #    確信配信ポリシー（無関係/本文なし/別業界を添付しない・"参考"ダンプ廃止）:
        #    - score >= 閾値（rerank relevance スケール。USE_COHERE_RERANK で真の関連度になる）
        #    - 低信頼(is_low_confidence)はスキップ
        #    - クエリが業界を指定し、ヒットの業界が設定済かつ不一致ならスキップ
        try:
            min_score = float(os.environ.get("KNOWLEDGE_DELIVER_MIN_SCORE", "0.5"))
        except ValueError:
            min_score = 0.5
        query_industry = extract_query_industry(input.query)
        refs: list[KnowledgeRef] = []
        candidates: list[tuple[str, str]] = []  # (file_id, filename)
        seen_ids: set[str] = set()
        for h in s_out.hits:
            file_id = extract_drive_file_id(h.source_uri) if h.source_type == "gdrive" else None
            ref = KnowledgeRef(
                title=h.title or h.file_name,
                url=h.source_uri,
                doc_type=h.doc_type,
                industry=h.industry,
                score=h.score,
                delivered=False,
            )
            refs.append(ref)
            industry_mismatch = bool(query_industry and h.industry and h.industry != query_industry)
            if (
                file_id
                and h.score >= min_score
                and not h.is_low_confidence
                and not industry_mismatch
                and file_id not in seen_ids
                and len(candidates) < input.top_k
            ):
                seen_ids.add(file_id)
                candidates.append((file_id, _safe_filename(h.title or h.file_name)))

        # 3. 候補ファイルを Drive から取得 → 一時ファイル化。
        prepared: list[tuple[str, str, str]] = []  # (file_id, local_path, filename)
        if candidates:
            gdrive = self._gdrive or self._build_gdrive()
            tmpdir = tempfile.mkdtemp(prefix="aila_knowledge_")
            for file_id, filename in candidates:
                try:
                    data = gdrive.download_file_bytes(file_id=file_id, request_id=ctx.request_id)
                except Exception:
                    log.warning("knowledge_deliver_download_failed", file_id=file_id)
                    continue
                path = str(Path(tmpdir) / filename)
                try:
                    Path(path).write_bytes(data)
                except Exception:
                    log.warning("knowledge_deliver_tmpwrite_failed", file_id=file_id)
                    continue
                prepared.append((file_id, path, filename))

        # 4. 配信。聞かれたチャンネル/スレッドがあればそこに添付（メール以外は基本チャンネル完結）。
        #    無ければ依頼者本人の DM にフォールバック（個人的・気まずい依頼や DM 直依頼向け）。
        requester = ctx.metadata.get("user_email")
        requester_email = (
            requester.strip() if isinstance(requester, str) and requester.strip() else None
        )
        channel_id = ctx.metadata.get("channel_id")
        channel_id = channel_id if isinstance(channel_id, str) and channel_id else None
        thread_ts = ctx.metadata.get("thread_ts")
        thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None

        delivered_ids: set[str] = set()
        where = ""
        if not prepared:
            note = "該当する添付可能な資料が見つかりませんでした（要約のみお返しします）。"
        elif not channel_id and not requester_email:
            note = "資料は見つかりましたが、配信先が分からずお届けできませんでした（要約のみ）。"
        else:
            try:
                delivered_ids, where = asyncio.run(
                    self._deliver(
                        prepared=prepared,
                        answer=s_out.answer,
                        request_id=ctx.request_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        email=requester_email,
                    )
                )
            except Exception:
                log.warning("knowledge_deliver_failed")
                delivered_ids, where = set(), ""
            if where == "thread":
                note = f"該当資料 {len(delivered_ids)} 件をこのスレッドにお出ししました。"
            elif where == "dm":
                note = f"該当資料 {len(delivered_ids)} 件をあなたの DM にお送りしました。"
            else:
                note = "資料配信に失敗しました（要約のみお返しします）。"

        # 配信できたファイルに対応する ref を delivered=True に。
        if delivered_ids:
            delivered_urls = {
                fid: True for fid in delivered_ids
            }  # file_id ベース。ref は url から再解決して照合。
            for ref in refs:
                fid = extract_drive_file_id(ref.url)
                if fid and fid in delivered_urls:
                    ref.delivered = True

        log.info(
            "knowledge_deliver_done",
            hits=len(refs),
            candidates=len(candidates),
            delivered=len(delivered_ids),
            cost_usd=s_out.total_cost_usd,
        )
        return KnowledgeDeliverOutput(
            answer=s_out.answer,
            references=refs,
            delivered_count=len(delivered_ids),
            note=note,
            total_cost_usd=s_out.total_cost_usd,
        )

    async def _deliver(
        self,
        *,
        prepared: list[tuple[str, str, str]],
        answer: str,
        request_id: str,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        email: str | None = None,
    ) -> tuple[set[str], str]:
        """prepared を配信。返り値 (配信できた file_id 集合, 配信先種別 "thread"|"dm"|"")。

        1) channel_id があれば そのチャンネル/スレッドに添付（メール以外は基本チャンネル完結）。
        2) チャンネル配信が 0 件 or channel_id 無しなら、email から本人 DM にフォールバック。
        """
        slack = self._slack or self._build_slack()

        if channel_id:
            delivered = await self._upload_all(
                slack, channel_id, thread_ts, prepared, answer, request_id
            )
            if delivered:
                return delivered, "thread"

        if email:
            user_id = await slack.lookup_user_id_by_email(email, request_id)
            if user_id:
                dm = await slack.open_dm(user_id, request_id)
                if dm:
                    delivered = await self._upload_all(
                        slack, dm, None, prepared, answer, request_id
                    )
                    if delivered:
                        return delivered, "dm"
        return set(), ""

    @staticmethod
    async def _upload_all(
        slack: Any,
        channel: str,
        thread_ts: str | None,
        prepared: list[tuple[str, str, str]],
        answer: str,
        request_id: str,
    ) -> set[str]:
        """prepared を channel（任意で thread_ts）に添付。要約は最初の1件のコメントに同梱。"""
        delivered: set[str] = set()
        for i, (file_id, path, filename) in enumerate(prepared):
            ok = await slack.upload_file(
                channel,
                path,
                request_id,
                title=filename,
                initial_comment=answer if i == 0 else None,
                thread_ts=thread_ts,
            )
            if ok:
                delivered.add(file_id)
        return delivered

    # --- 遅延生成（factory が注入しない / 本番起動時のフォールバック） ---

    def _build_search(self) -> Any:
        from teamagent.orchestrator.factory import _build_search_skill

        return _build_search_skill()

    def _build_slack(self) -> Any:
        from teamagent.adapters.slack_client import SlackClient

        return SlackClient.from_env()

    def _build_gdrive(self) -> Any:
        from teamagent.adapters.gdrive_client import GDriveClient

        return GDriveClient.from_env(readonly=True)
