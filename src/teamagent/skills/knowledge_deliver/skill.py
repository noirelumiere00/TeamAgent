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

        # 2. ヒットから「Drive 実体のある資料」を重複排除して配信候補に。
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
            if file_id and file_id not in seen_ids and len(candidates) < input.top_k:
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

        # 4. 依頼者本人の DM に添付配信（user_email 未解決なら配信せず note のみ）。
        requester = ctx.metadata.get("user_email")
        delivered_ids: set[str] = set()
        if not prepared:
            note = "該当する添付可能な資料が見つかりませんでした（要約のみお返しします）。"
        elif not isinstance(requester, str) or not requester.strip():
            note = (
                "資料は見つかりましたが、DM へお届けできませんでした"
                "（連携が未完了の可能性があります）。"
            )
        else:
            try:
                delivered_ids = asyncio.run(
                    self._deliver(
                        email=requester.strip(),
                        prepared=prepared,
                        answer=s_out.answer,
                        request_id=ctx.request_id,
                    )
                )
            except Exception:
                log.warning("knowledge_deliver_dm_failed")
                delivered_ids = set()
            if delivered_ids:
                note = f"該当資料 {len(delivered_ids)} 件をあなたの DM にお送りしました。"
            else:
                note = "DM 配信に失敗しました（要約のみお返しします）。"

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
        email: str,
        prepared: list[tuple[str, str, str]],
        answer: str,
        request_id: str,
    ) -> set[str]:
        """本人 DM を開いて prepared を添付。配信できた file_id 集合を返す。"""
        slack = self._slack or self._build_slack()
        user_id = await slack.lookup_user_id_by_email(email, request_id)
        if not user_id:
            return set()
        dm = await slack.open_dm(user_id, request_id)
        if not dm:
            return set()
        delivered: set[str] = set()
        for i, (file_id, path, filename) in enumerate(prepared):
            ok = await slack.upload_file(
                dm,
                path,
                request_id,
                title=filename,
                initial_comment=answer if i == 0 else None,
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
