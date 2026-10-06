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
- 取引先ガード（KNOWLEDGE_DELIVER_CLIENT_GUARD・既定 ON）: 質問が取引先を名指ししていれば
  その取引先の資料と言えるヒットだけを添付・根拠リンクにし、検索側が「該当なし」
  （found=False）なら何も添付しない。判定は search の警告と同じ部品（client_match /
  result_guard）で行う。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills._shared.drive_slack_delivery import (
    PreparedFile,
    deliver_files,
    extract_drive_binary_file_id,
    extract_drive_file_id,
    prepare_drive_files,
    safe_filename,
)
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.knowledge_deliver.schema import (
    KnowledgeDeliverInput,
    KnowledgeDeliverOutput,
    KnowledgeRef,
)
from teamagent.skills.search.client_match import (
    _MIN_CLIENT_LEN,
    hit_is_about_client,
    normalize_client,
    normalize_filter_client,
)
from teamagent.skills.search.knowledge_query import extract_query_industry
from teamagent.skills.search.result_guard import (
    aliases,
    detect_query_client,
    hit_client_vocabulary,
    is_self_org_name,
)
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput

logger = structlog.get_logger(__name__)

# file_id 抽出 / ファイル名 sanitize / Drive DL / Slack 添付は
# _shared/drive_slack_delivery.py に集約（clientkarte と同じ部品を使う）。
# 既存の import 経路（connect_web / tests）を壊さないため、ここからも明示的に再公開する。
__all__ = [
    "KnowledgeDeliverSkill",
    "extract_drive_binary_file_id",
    "extract_drive_file_id",
]


# 取引先ガード（2026-10-06 本番事故の再発防止）。既定 ON・"0" で従来どおり。
# 事故: 「日本コカ・コーラの紅茶花伝…の事例を根拠の PDF つきで」に対し、本文は紅茶花伝を
# 要約したのに、添付は東洋水産・アイホン・日立など別取引先の提案書 3 件だった。top1（本当の
# 記録）に Drive 実体が無く、配信基準（スコア・低信頼・業界）だけを見ていたため下位の別取引先が
# 回った。名指しの取引先がある依頼では「その取引先の資料」と言えるものだけを添付し、
# 検索側が「該当なし」（found=False）と判定したときは何も添付しない。
_CLIENT_GUARD_ENV = "KNOWLEDGE_DELIVER_CLIENT_GUARD"

# search の回答末尾の「📎 資料リンク」ブロック（SearchSkill._source_links_block）の 1 行。
_LINKS_HEAD = "📎 *資料リンク*"
_LINK_LINE_RE = re.compile(r"^- \[[^\]]*\]\((?P<url>[^)\s]+)\)")


def _client_guard_enabled() -> bool:
    """取引先ガードが有効か（既定 ON。0 / false / no / off で従来どおり）。"""
    raw = os.environ.get(_CLIENT_GUARD_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _as_search_hit(h: SearchHitOut) -> SearchHit:
    """``hit_is_about_client`` / ``hit_client_vocabulary`` が読むメタだけを持つ SearchHit。

    search の警告（result_guard）と同じ部品で判定するための詰め替え。本文は渡さない
    （取引先判定は本文を見ない契約・競合比較ページで沈黙させないため）。
    """
    meta: dict[str, Any] = {
        "client_name": h.client_name,
        "cls_project": h.project,
        "title": h.title,
    }
    if h.entities:
        meta["cls_entities"] = list(h.entities)
    return SearchHit(chunk_id=h.chunk_id, content="", score=h.score, metadata=meta)


def _resolve_asked_client(
    input: KnowledgeDeliverInput, s_out: SearchOutput
) -> tuple[str | None, str]:
    """利用者が名指しした取引先と、その出どころ（ログ用の固定語）を返す。

    search と同じ規則・同じ優先順:
      1. search が retrieval で確定させた値（明示 filter_client → 既知語彙への語境界つき一致）
      2. 明示 filter_client（search 側の判定が無効な構成のとき）
      3. ヒットに付いた取引先名を語彙にした語境界つき一致（result_guard の fallback と同じ）
    自社・自社プロダクト名は取引先として扱わない。短すぎる名前は判定不能＝従来どおり。
    """
    candidates: list[tuple[str | None, str]] = [
        (s_out.query_client, "search"),
        (input.filter_client, "filter"),
    ]
    if not any(name and name.strip() for name, _ in candidates):
        vocabulary = hit_client_vocabulary([_as_search_hit(h) for h in s_out.hits])
        candidates.append((detect_query_client(input.query, vocabulary), "hits"))
    for name, source in candidates:
        if not name or not name.strip():
            continue
        asked = normalize_filter_client(name) or name.strip()
        if is_self_org_name(asked) or len(normalize_client(asked)) < _MIN_CLIENT_LEN:
            return None, "self_or_short"
        return asked, source
    return None, "none"


def _drop_link_lines(answer: str, urls: set[str]) -> str:
    """回答末尾の「📎 資料リンク」から ``urls`` の行を落とす（見出しだけ残ったら見出しも）。"""
    if not answer or not urls:
        return answer
    lines = answer.split("\n")
    kept = [
        line for line in lines if not ((m := _LINK_LINE_RE.match(line)) and m.group("url") in urls)
    ]
    if len(kept) == len(lines):
        return answer
    out: list[str] = []
    for i, line in enumerate(kept):
        nxt = kept[i + 1] if i + 1 < len(kept) else ""
        if line.strip() == _LINKS_HEAD and not _LINK_LINE_RE.match(nxt):
            continue
        out.append(line)
    return "\n".join(out).rstrip()


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
        "依頼者本人の DM に添付して届ける（チャンネルで頼まれても DM・その場は note の 1 行だけ）。"
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
        "query には依頼文全体（自然文）をそのまま入れてよい。" + USER_CONTEXT_RULE
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
        # 取引先ガード: 名指しの取引先があれば、その取引先の資料と言えるヒットだけを添付・
        # 根拠リンクにする。検索側が「該当なし」と判定したら何も添付しない。
        client_guard = _client_guard_enabled()
        asked_client, asked_source = (
            _resolve_asked_client(input, s_out) if client_guard else (None, "off")
        )
        asked_aliases = sorted(aliases(asked_client)) if asked_client else []
        not_found_block = client_guard and not s_out.found
        client_excluded = 0  # 配信基準は満たしたが別取引先（または取引先不明）で外した件数
        other_client_urls: set[str] = set()
        kept_urls: set[str] = set()
        evidence_refs: list[KnowledgeRef] = []
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
            about_client = (
                asked_client is None
                or hit_is_about_client(_as_search_hit(h), asked_client, aliases=asked_aliases)
                is not None
            )
            hit_urls = {u for u in (h.url, h.drive_url) if u}
            if about_client:
                evidence_refs.append(ref)
                kept_urls |= hit_urls
            else:
                other_client_urls |= hit_urls
            industry_mismatch = bool(query_industry and h.industry and h.industry != query_industry)
            if (
                file_id
                and h.score >= min_score
                and not h.is_low_confidence
                and not industry_mismatch
                and file_id not in seen_ids
                and len(candidates) < input.top_k
            ):
                if not_found_block:
                    continue
                if not about_client:
                    client_excluded += 1
                    continue
                seen_ids.add(file_id)
                candidates.append(
                    (file_id, safe_filename(h.resolved_file_name or h.title or h.file_name))
                )
                if h.source_type != "gdrive":
                    resolved_candidates += 1

        # 根拠に使わない（別取引先の）リンクを回答末尾の「📎 資料リンク」から外す。
        # 同じ URL を名指しの取引先のヒットも持っていれば残す。
        answer = s_out.answer
        if asked_client:
            answer = _drop_link_lines(answer, other_client_urls - kept_urls)
        # ガードが無ければ添付していた（＝ガードが 0 件の原因）なら、従来の理由文ではなく
        # 「名指しの取引先の資料は無い」とそのまま言う。
        guard_emptied = not candidates and (not_found_block or client_excluded > 0)

        # 3. 候補ファイルを Drive から取得 → 一時ファイル化（_shared の共通部品）。
        #    tmpdir の後始末は呼び出し側の責務（_shared/drive_slack_delivery の契約）。
        #    常駐 ECS タスクの /tmp に最大 256MB × top_k が残り続けるのを防ぐため、
        #    添付が終わったら（失敗しても）必ず消す。
        # 出力面ガード（10-01 監査候補①・clientkarte と同じ型・deny-by-default）。
        # 本人 DM 以外（チャンネル・グループ・外部共有・判定不能）で頼まれたら、実ファイルと
        # 要約は依頼者本人の DM へ送り、その場には資料名も要約も出さない。チャンネルに
        # 第三者が置いた指示文でモデルが呼ばされても、資料が共有面へ出ない。
        on_private_surface = is_private_surface(
            ctx.metadata.get("channel_id"), ctx.metadata.get("identity_verified") is True
        )
        prepared: list[PreparedFile] = []
        tmpdir: str | None = None
        delivered_ids: set[str] = set()
        where = ""
        try:
            if candidates:
                gdrive = self._gdrive or self._build_gdrive()
                tmpdir, prepared = prepare_drive_files(
                    gdrive,
                    candidates,
                    request_id=ctx.request_id,
                    log=log,
                    log_prefix="knowledge_deliver",
                    tmp_prefix="aila_knowledge_",
                )

            # 4. 配信。聞かれたチャンネル/スレッドがあればそこに添付
            #    （メール以外は基本チャンネル完結）。無ければ依頼者本人の DM に
            #    フォールバック（個人的・気まずい依頼や DM 直依頼向け）。
            requester = ctx.metadata.get("user_email")
            requester_email = (
                requester.strip() if isinstance(requester, str) and requester.strip() else None
            )
            channel_id = ctx.metadata.get("channel_id")
            channel_id = channel_id if isinstance(channel_id, str) and channel_id else None
            thread_ts = ctx.metadata.get("thread_ts")
            thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None
            # 本人 DM で頼まれた → その DM へそのまま添付。それ以外 → channel は使わず
            # email から本人 DM を開いて送る（deliver_files の DM 経路）。
            dm_channel: str | None = None
            if on_private_surface:
                dm_channel, channel_id, thread_ts = channel_id, None, None
            else:
                channel_id, thread_ts = None, None

            # 適用フィルタのラベル（例「電通 × 提案書」）。note に「何で絞ったか」を明示し、
            # 0 件時は絞りを述べて緩和提案する（設計 E）。
            applied = _format_applied_filters(input)
            filt_prefix = f"{applied} で" if applied else ""

            if not prepared:
                # 0 件の理由を分けて出す。2026-08-27 の本番調査で、
                # 「FB 行だけがヒットして Drive 実ファイルが 1 件も紐づかなかった」場合にも
                # 「該当する添付可能な資料が見つかりませんでした」と返しており、
                # ユーザーには**資料そのものが存在しない**と読めていた（実際には資料は在る）。
                # hits / file_id / 配信基準 のどこで落ちたかを文言に出し、誤読を止める。
                if not refs:
                    reason = "関連する記録・資料が見つかりませんでした"
                elif guard_emptied or not_found_block:
                    # 取引先ガード: 該当なし／名指しの取引先の資料が無い。別取引先の資料を
                    # 根拠として出さず、無いことをそのまま言う（条件緩和の提案もしない＝
                    # 取引先を外すと別取引先の資料が根拠の顔で戻るため）。
                    if asked_client:
                        reason = f"{asked_client}の資料のファイル本体は見つかりませんでした"
                    else:
                        reason = "問いに該当する資料は見つかりませんでした"
                elif not ref_by_fid:
                    reason = (
                        "社内のやり取り（Slack / 管理シートの行）は見つかりましたが、"
                        "添付できる Drive の実ファイルに紐づきませんでした"
                    )
                elif not candidates:
                    reason = "関連資料は見つかりましたが、配信の関連度基準に届きませんでした"
                else:
                    reason = "該当資料の取得に失敗しました"
                if refs and (guard_emptied or not_found_block):
                    note = f"{reason}（要約のみお返しします）。"
                    if client_excluded:
                        note += "別の取引先の資料は根拠にならないため、お送りしていません。"
                elif applied:
                    note = (
                        f"{applied} で{reason}"
                        "（要約のみお返しします）。"
                        "取引先のみ／資料種別を外す等、条件を緩めて再検索しますか。"
                    )
                else:
                    note = f"{reason}（要約のみお返しします）。"
            elif not dm_channel and not requester_email:
                note = (
                    "資料は見つかりましたが、配信先が分からずお届けできませんでした（要約のみ）。"
                )
            else:
                try:
                    delivered_ids, where = asyncio.run(
                        self._deliver(
                            prepared=prepared,
                            answer=answer,
                            request_id=ctx.request_id,
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            email=requester_email,
                            dm_channel=dm_channel,
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
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        # 配信できたファイルに対応する ref を delivered=True に。
        for fid in delivered_ids:
            for ref in ref_by_fid.get(fid, []):
                ref.delivered = True

        # 利用者へ返すリンク一覧（根拠）。取引先ガード有効時は、該当なしなら空・
        # 名指しの取引先があればその取引先の資料だけ（別取引先を根拠として並べない）。
        if not_found_block:
            out_refs: list[KnowledgeRef] = []
        elif asked_client:
            out_refs = evidence_refs
        else:
            out_refs = refs

        log.info(
            "knowledge_deliver_done",
            hits=len(refs),
            candidates=len(candidates),
            resolved_candidates=resolved_candidates,
            delivered=len(delivered_ids),
            cost_usd=s_out.total_cost_usd,
            # 取引先ガードの観測値（G8: 取引先名・資料名は載せない。固定語と件数だけ）。
            client_guard=client_guard,
            asked_source=asked_source,
            found=s_out.found,
            client_excluded=client_excluded,
        )
        if not on_private_surface:
            # その場（チャンネル等）へ返す本文には資料名・要約を載せない（DM 側にだけ出る）。
            log.info("knowledge_deliver_surface_guard", delivered=len(delivered_ids))
            return KnowledgeDeliverOutput(
                answer="",
                references=[],
                delivered_count=len(delivered_ids),
                note=(
                    f"該当資料 {len(delivered_ids)} 件と要約をあなたの DM にお送りしました"
                    "（このチャンネルには資料名・要約を出していません）。"
                    if delivered_ids
                    else "資料と要約は DM でお出しします。Aico との DM でもう一度お声がけください。"
                ),
                total_cost_usd=s_out.total_cost_usd,
            )
        return KnowledgeDeliverOutput(
            answer=answer,
            references=out_refs,
            delivered_count=len(delivered_ids),
            note=note,
            total_cost_usd=s_out.total_cost_usd,
        )

    async def _deliver(
        self,
        *,
        prepared: list[PreparedFile],
        answer: str,
        request_id: str,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        email: str | None = None,
        dm_channel: str | None = None,
    ) -> tuple[set[str], str]:
        """prepared を配信（配信先の決定ルールは _shared/drive_slack_delivery に集約）。"""
        slack = self._slack or self._build_slack()
        return await deliver_files(
            slack,
            prepared=prepared,
            comment=answer,
            request_id=request_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            email=email,
            dm_channel=dm_channel,
        )

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
