"""knowledge_deliver: 社内ナレッジを検索し、該当資料の実ファイルを依頼者 DM に届ける。

「〇〇業界の提案資料出して」「〇〇の成功事例ある？」「〇〇のレポート出して」のような、
**ファイル本体が欲しい**依頼に応える。リンクだけで良い時は search を使う。

設計:
- 検索＋要約は SearchSkill をそのまま再利用（Phase1 の自動分類フィルタも効く）。
- gdrive ヒットは source_uri、gsheets/slack ヒットは search が資料名解決した
  Drive 実ファイル URL（h.url）から file_id を解決し download_file_bytes で実体取得。
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
# 解決済み URL から file_id を取るのは「アップロード実体ファイル」の形だけに限定する。
# ナレッジシート行の自リンク（docs.google.com/spreadsheets/...）や
# Google ネイティブ文書（docs.google.com/document|presentation/...）を誤って候補化しない
# （後者は download_file_bytes が get_media 専用で export 未対応のため配信不能）。
_DRIVE_BINARY_FILE_URL_RE = re.compile(r"https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)")


def extract_drive_binary_file_id(url: str | None) -> str | None:
    """resolved URL（http(s)）からアップロード実体ファイルの file_id だけを取り出す。"""
    if not url:
        return None
    m = _DRIVE_BINARY_FILE_URL_RE.search(url.strip())
    return m.group(1) if m else None


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


def _format_applied_filters(input: KnowledgeDeliverInput) -> str:
    """適用した明示フィルタを「電通 × 提案書 × 食品」のラベルに整形する。

    取引先・施策・資料種別・業界の順で、指定されたものだけを ` × ` で連結する。
    何も指定されていなければ空文字（note 側で『何で絞ったか』を出さない）。
    """
    parts = [
        input.filter_client,
        input.filter_solution,
        input.filter_doc_type,
        input.filter_industry,
    ]
    return " × ".join(p.strip() for p in parts if p and p.strip())


@register
class KnowledgeDeliverSkill(BaseSkill[KnowledgeDeliverInput, KnowledgeDeliverOutput]):
    """検索 → 該当資料の実ファイルを依頼者 DM に届けるスキル。"""

    name: ClassVar[str] = "knowledge_deliver"
    description: ClassVar[str] = (
        "「〇〇への提案資料出して」「〇〇業界の提案事例ある？」「〇〇施策のレポート出して」"
        "のような依頼に対し、社内ナレッジを検索して要約し、該当資料の実ファイルを"
        "依頼者本人の DM（チャンネル/スレッド内ならその場）に添付して届ける。"
        "ファイル本体が欲しい時に使う（リンク・要約だけで良い時は search）。\n"
        "依頼文に含まれる条件は必ず該当フィールドに振り分けて埋めること（自然文の精度が上がる）:\n"
        "- 取引先/会社名（電通・サイバーエージェント・ニチレイ・アース製薬 等）→ filter_client\n"
        "- 資料種別（提案資料/提案書→提案書、レポート/施策レポート→報告書、議事録、"
        "価格表/料金表→価格表、契約書→契約）→ filter_doc_type\n"
        "- 施策/ソリューション（SNS運用・動画広告・インフルエンサー・SEO 等の『○○施策』の○○）"
        "→ filter_solution\n"
        "- 業界（食品・飲料・化粧品・小売・金融・IT 等の『○○業界』の○○）→ filter_industry\n"
        "例: 『電通への提案資料』→filter_client=電通, filter_doc_type=提案書 / "
        "『食品業界の提案事例』→filter_industry=食品, filter_doc_type=提案書 / "
        "『動画広告施策のレポート』→filter_solution=動画広告, filter_doc_type=報告書。\n"
        "query には依頼文全体（自然文）をそのまま入れてよい。"
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
                filter_client=input.filter_client,
                filter_doc_type=input.filter_doc_type,
                filter_solution=input.filter_solution,
            ),
            ctx,
        )

        # 2. gdrive は source_uri、gsheets/slack は search が資料名解決した
        #    h.url から Drive 実体を特定し、関連が高い資料だけを配信候補にする。
        #    確信配信ポリシー（無関係/本文なし/別業界を添付しない・"参考"ダンプ廃止）:
        #    - score >= 閾値（rerank relevance スケール。USE_COHERE_RERANK で真の関連度になる）
        #    - 低信頼(is_low_confidence)はスキップ
        #    - クエリが業界を指定し、ヒットの業界が設定済かつ不一致ならスキップ
        try:
            min_score = float(os.environ.get("KNOWLEDGE_DELIVER_MIN_SCORE", "0.5"))
        except ValueError:
            min_score = 0.5
        # 明示 filter_industry が来たらそれを優先（クエリ自動抽出より上位）。明示が無ければ
        # 従来どおりクエリ文字列から推定して別業界の誤添付を防ぐ（設計 E: 明示フィルタ優先）。
        query_industry = input.filter_industry or extract_query_industry(input.query)
        refs: list[KnowledgeRef] = []
        ref_by_fid: dict[str, list[KnowledgeRef]] = {}
        candidates: list[tuple[str, str]] = []  # (file_id, filename)
        seen_ids: set[str] = set()
        resolved_candidates = 0
        for h in s_out.hits:
            if h.source_type == "gdrive":
                file_id = extract_drive_file_id(h.source_uri)
            else:
                # gsheets 行 / slack ヒット: search が資料名→Drive 実ファイルに
                # 解決した URL を使う。drive_url は旧 SearchHitOut 呼び出しとの互換用。
                # 解決失敗時の行自リンク等は実体ファイル形に一致しないため None になる。
                file_id = extract_drive_binary_file_id(h.url)
                if file_id is None:
                    file_id = extract_drive_binary_file_id(h.drive_url)
            ref = KnowledgeRef(
                title=h.title or h.file_name,
                url=h.url or h.source_uri,
                doc_type=h.doc_type,
                industry=h.industry,
                score=h.score,
                delivered=False,
            )
            refs.append(ref)
            if file_id:
                ref_by_fid.setdefault(file_id, []).append(ref)
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
                candidates.append(
                    (file_id, _safe_filename(h.resolved_file_name or h.title or h.file_name))
                )
                if h.source_type != "gdrive":
                    resolved_candidates += 1

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
                # sanitize 後に同名となる別ファイルが上書きし合わないよう file_id で区切る
                # （file_id は [A-Za-z0-9_-] のみでパスとして安全）。
                path = str(Path(tmpdir) / file_id / filename)
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
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

        # 適用フィルタのラベル（例「電通 × 提案書」）。note に「何で絞ったか」を明示し、
        # 0 件時は絞りを述べて緩和提案する（設計 E）。
        applied = _format_applied_filters(input)
        filt_prefix = f"{applied} で" if applied else ""

        delivered_ids: set[str] = set()
        where = ""
        if not prepared:
            if applied:
                note = (
                    f"{applied} で該当する添付可能な資料が見つかりませんでした"
                    "（要約のみお返しします）。"
                    "取引先のみ／資料種別を外す等、条件を緩めて再検索しますか。"
                )
            else:
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
            n = len(delivered_ids)
            if where == "thread":
                note = f"{filt_prefix}該当資料 {n} 件をこのスレッドにお出ししました。"
            elif where == "dm":
                note = f"{filt_prefix}該当資料 {n} 件をあなたの DM にお送りしました。"
            else:
                note = "資料配信に失敗しました（要約のみお返しします）。"

        # 配信できたファイルに対応する ref を delivered=True に。
        for fid in delivered_ids:
            for ref in ref_by_fid.get(fid, []):
                ref.delivered = True

        log.info(
            "knowledge_deliver_done",
            hits=len(refs),
            candidates=len(candidates),
            resolved_candidates=resolved_candidates,
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
