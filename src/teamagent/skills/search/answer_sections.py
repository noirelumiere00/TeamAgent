"""fan-out 検索の答えのうち、コードが書く部分（探し先ごとの節と「探した範囲」の 1 行・純関数）。

背景（2026-10-14・設計 fanout_design.json の answer_format）:
  資料検索 1 回で金庫・本人の Slack・カルテ・事例・案件・言い換えを並行に引く（fan-out・DM だけ）。
  各探し先の結果は要約器に書かせず、コードが 1 件 1 行・日付と元のリンク付きで並べる。
  「無い」と「探せなかった」と「使わなかった」を、答えの最後の 1 行（探した範囲）で書き分ける。
  これまで別々に出していた ℹ️・🔒 の Slack の状態行（composite.status_note）と、
  再検索のときだけの範囲表示（alias_expansion.scope_footer）の役割は、この 1 行にまとめる
  （置き換えは組み込み側が行う。ここは部品だけ）。

答えの並び（上から）:
  head（警告ヘッダ・要約・📎 施策のレポート・📊 動画ごとの数字。または該当なしの定型文）
  → 探し先ごとの節（📁 🏆 🤝 🗂 💬 🗃 🔎 の順・0 件の節は出さない）
  → tail（📎 実ファイルの提案 1 行）
  → 探した範囲（毎回・必ず最後）

節の分類（ヒットの metadata だけで決める・LLM に推測させない）:
  - campaign_aggregate='true' / case_corpus='true' → 🏆 事例・施策実績
  - topic='案件決定' → 🤝 案件（決定・進行中の案件。実績ではありません）
  - is_sales_fb → 🗂 カルテ（取引先が決まっていれば「◯◯のカルテ」、無ければ「営業FBの記録」）
  - x_research_tool あり（research_persist の保存物）→ 🗃 過去の調べもの
  - それ以外 → 📁 金庫の資料
  業種はメタデータの正準値（normalize_industry を通ったもの）だけを書き、無ければ「業種不明」。
  事例は case_external_use が ng なら「社外展開NG（社内のみ）」、ok 以外（unknown・空・未知の値）
  は「社外利用は要確認」を必ず付ける（判定できないものは要確認へ倒す）。🔎 に出た事例にも付ける。
  案件が 🤝 以外（🔎）に出たときは「案件（実績ではありません）」を付ける。

字数（answer は ANSWER_MAX_CHARS＝2,500 字以下・payload_offload が要約系を切る長さ 500×5 の内側）:
  超えたら次の順で削る（探した範囲と tail は削らない）。
    段 1: 各節を 2→1 件にする。🔎→🗃→💬→🗂→🤝→🏆→📁 の順に、節の後ろの行から。
    段 2: 🔎 の節を外す。
    段 3: 残りの節を 🗃→💬→🗂→🤝→🏆→📁 の順に丸ごと外す。
    段 4: それでも超えるとき、📊 を先に確保して要約を行の境目で切り「…」を付ける。
          📊 だけで超える場合も行の境目で切る。リンクの途中・URL の途中では切らない。
  1 段ごとに描き直して測り、収まった時点で止める（決定論）。

書き方（Slack で崩れないこと）:
  - リンクは既存の _source_links_block（skill.py）と同じ標準 Markdown の ``[表示名](URL)``。
    配信側（OpenClaw の markdownToSlackMrkdwn）が Slack の ``<URL|表示名>`` に変える
    （SOUL.md の「リンクは [表示名](URL)」とも同じ）。書式を変えるときは _link の 1 か所だけ。
  - 行は ``- `` の箇条書き（配信側で行の分かれ目が保たれる）。見出しは行頭の絵文字から書く
    （direct_video_metrics.extract_section の見出しの許可リストと合わせる）。
  - 節との間は空行 1 つ。extract_section は次の節の見出しで 📊 を閉じる。
  - 第三者・利用者の文字列（資料名・抜粋・効果・取引先名・問いの語）は、制御文字を落として 1 行に
    潰し、search_surface_check の slack_safe で ``& < > * ~ ` | [ ]`` を効かない字にしてから
    差し込む。``<!channel>``・``<@U…>``・``<https://x|偽名>``・``[偽名](https://x)`` を
    組み立てさせない。
  - リンクにする URL は http(s) の ASCII の URL 文字だけ・ホストあり・500 字以下のものだけ。
    それ以外は表示名だけにする（壊れたリンクを出さない）。
  - 「—」は字の通り（em ダッシュ）。組み上げた answer 全体に strip_ai_decoration を掛けないこと
    （行中の「—」が「、」に変わる）。

本モジュールは env を読まない・DB を引かない・ログを出さない
（純関数。並べ方と字数を単体テストで固定する）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import normalize_industry
from teamagent.observability import scrub_value
from teamagent.skills._shared.slack_blocks import plain
from teamagent.skills._shared.source_url import hit_doc_url
from teamagent.skills.search import drive_first
from teamagent.skills.search.campaign_files import (
    UNKNOWN_INDUSTRY,
    VIDEO_METRICS_HEADING,
    is_campaign_hit,
)
from teamagent.skills.search.composite import SlackLookup, slack_query_terms
from teamagent.skills.search.dates import extract_title_date
from teamagent.skills.search.schema import DocumentReference, SlackHitOut
from teamagent.skills.search_surface_check.summary import slack_safe
from teamagent.util.grapheme_cut import truncate_graphemes

# ── 節 ────────────────────────────────────────────────────────────────────

#: 節の鍵。vault=📁 金庫の資料・case=🏆 事例・施策実績・deal=🤝 案件・karte=🗂 カルテ・
#: slack=💬 Slack・research=🗃 過去の調べもの・expand=🔎 近い業種・言い換え。
SectionKey = Literal["vault", "case", "deal", "karte", "slack", "research", "expand"]

#: 答えに並べる順（上から）。
SECTION_ORDER: tuple[SectionKey, ...] = (
    "vault",
    "case",
    "deal",
    "karte",
    "slack",
    "research",
    "expand",
)
#: 字数を超えたときに削る順（先頭から削る）。段 1 は各節を 1 件へ、段 2・3 は節ごと外す。
TRIM_ORDER: tuple[SectionKey, ...] = (
    "expand",
    "research",
    "slack",
    "karte",
    "deal",
    "case",
    "vault",
)

#: answer の上限（payload_offload の要約系の切り詰め 500×5 の内側。テストで固定）。
ANSWER_MAX_CHARS = 2500
#: 1 節に載せる件数の上限（設計: 関連度がしきい値以上の上位 2 件）。
PER_SECTION_MAX = 2

#: 案件決定の topic（search の案件の床 _apply_deal_floor と同じ値）。
DEAL_TOPIC = "案件決定"
#: 事例の社外利用の印（固定語・テストと SOUL が見る）。
CASE_NG_MARK = "社外展開NG（社内のみ）"
CASE_UNKNOWN_MARK = "社外利用は要確認"
#: 案件が 🤝 以外の節に出たときの印。
DEAL_MARK = "案件（実績ではありません）"
#: 探した範囲の書き出し。SOUL の条件はこの文字列で fan-out の答えだけを見分ける。
#: 今の検索（alias_expansion.scope_footer）も「探した範囲: 」で始まるので、同じ書き出しにしない
#: （同じだと SOUL の「2 本目のツールを呼ばない」が fan-out でない全員に効く・10-14 Claude 審査）。
SCOPE_PREFIX = "探した範囲（まとめて検索）: "
MORE = "…"

_TITLE_CHARS = 50
_DETAIL_CHARS = 90
_EFFECT_CHARS = 60
_EXCERPT_CHARS = 80
_NEXT_ACTION_CHARS = 40
_NAME_CHARS = 30
_QUERY_CHARS = 30
_QUERIES_MAX = 3
_URL_MAX = 500
#: 業種を書く節（資料・事例・言い換え）。カルテ・Slack・案件には書かない。
_SHOW_INDUSTRY: frozenset[SectionKey] = frozenset({"vault", "case", "expand"})


@dataclass(frozen=True)
class Entry:
    """節の 1 行の材料（表示前の生の値。無害化と切り詰めは描画で 1 回だけ行う）。

    ``kind`` はその行の本来の種類（分類の結果）。出す節（🔎 など）と違うことがある。
    ``industry`` は描画でも normalize_industry を通す（正準値でなければ「業種不明」）。
    """

    title: str
    url: str | None = None
    date: str | None = None
    industry: str | None = None
    detail: str = ""
    kind: SectionKey = "vault"
    case_external_use: str | None = None
    document_id: str | None = None
    source_title: str = ""
    reference: DocumentReference | None = None


@dataclass(frozen=True)
class Sections:
    """節ごとの行（並べる順のまま）。件数の上限・重複の除去は描画で行う。

    ``karte_client`` は 🗂 の見出し「◯◯のカルテ」、``expand_subject`` は 🔎 の見出し
    「『◯◯』そのものの資料ではありません」に入る（どちらも無害化して出す）。
    """

    entries: Mapping[SectionKey, Sequence[Entry]] = field(default_factory=dict)
    karte_client: str = ""
    expand_subject: str = ""
    required_counts: Mapping[SectionKey, int] = field(default_factory=dict)

    timeline_lines: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def of(self, key: SectionKey) -> Sequence[Entry]:
        return self.entries.get(key, ())


# ── 探し先の状態（探した範囲の 1 行）──────────────────────────────────────────

#: 探した範囲に並べる探し先。sns は X・TikTok（自動では探さない・案内だけ）。
LaneName = Literal["vault", "expand", "slack", "karte", "case", "sns"]
LANE_ORDER: tuple[LaneName, ...] = ("vault", "expand", "slack", "karte", "case", "sns")
LaneKind = Literal["found", "zero", "failed", "skipped"]

_LANE_LABELS: dict[str, str] = {
    "vault": "金庫",
    "expand": "言い換え",
    "slack": "Slack",
    "karte": "カルテ",
    "case": "事例",
    "sns": "X・TikTok",
}
#: 探せなかったときに添える「◯◯に無いという意味ではありません」（場所でない探し先には付けない）。
#: Slack の文言は composite.STATUS_NOTES と同じ（英字の後ろは半角空白）。
_NOT_ABSENT_NOTES: dict[str, str] = {
    "vault": "金庫に無いという意味ではありません",
    "slack": "Slack に無いという意味ではありません",
    "karte": "カルテに無いという意味ではありません",
    "case": "事例集に無いという意味ではありません",
}
#: failed の理由コード → 表示（固定語）。Slack の理由コードは composite.SlackLookup と同じ語彙。
_FAILED_TEXTS: dict[str, str] = {
    "timeout": "時間切れ",
    "search_failed": "検索に失敗しました",
    "internal_error": "検索に失敗しました",
    "no_identity": "本人確認ができませんでした",
    "not_connected": "未連携",
    "reconnect_required": "連携切れ",
}
_FAILED_DEFAULT = "探せませんでした"
#: 利用者が直せる失敗は、直し方を書く（「無いという意味ではありません」の代わり）。
_FAILED_HINTS: dict[str, str] = {
    "not_connected": "@Aico に「連携」と話しかけると一緒に探せます",
    "reconnect_required": "@Aico に「連携」と話しかけて連携し直してください",
}
#: skipped の理由コード → 表示（固定語）。
_SKIPPED_TEXTS: dict[str, str] = {
    "no_client": "取引先の名指しがないため",
    "no_industry": "業種が特定できないため",
    "not_shown": "主な結果で足りたため載せていません",
    "not_auto": "自動では探していません。「X の声も見て」と言えば調べます",
}
_SKIPPED_DEFAULT = "条件に合わず使っていません"


@dataclass(frozen=True)
class LaneStatus:
    """1 つの探し先の結果（探した範囲の 1 項目）。生成は found / zero / failed / skipped で。

    - found(n): ✓n件（見つかった）
    - zero(): 0件（探して無かった）
    - failed(reason): ✗理由（探せなかった。「無い」とは書かない）
    - skipped(reason): —（条件に合わず使わなかった）
    ``reason`` は固定の理由コード（_FAILED_TEXTS・_SKIPPED_TEXTS）。未知のコードは汎用の文になる
    （利用者の文やエラー本文を理由にしない）。``queries`` は実際に探した語（無害化して出す）、
    ``partial`` は再検索の一部が時間切れ・失敗、``hidden`` はここに出せない非公開の一致の件数。
    """

    kind: LaneKind
    count: int = 0
    reason: str = ""
    queries: tuple[str, ...] = ()
    partial: bool = False
    hidden: int = 0

    @classmethod
    def found(
        cls,
        count: int,
        *,
        queries: Iterable[str] = (),
        partial: bool = False,
        hidden: int = 0,
    ) -> LaneStatus:
        """✓n件。n が 0 以下なら zero と同じ（「✓0件」は出さない）。"""
        if count <= 0:
            return cls.zero(queries=queries, partial=partial, hidden=hidden)
        return cls(
            "found", count=count, queries=tuple(queries), partial=partial, hidden=max(0, hidden)
        )

    @classmethod
    def zero(
        cls, *, queries: Iterable[str] = (), partial: bool = False, hidden: int = 0
    ) -> LaneStatus:
        return cls("zero", queries=tuple(queries), partial=partial, hidden=max(0, hidden))

    @classmethod
    def failed(cls, reason: str) -> LaneStatus:
        return cls("failed", reason=reason)

    @classmethod
    def skipped(cls, reason: str) -> LaneStatus:
        return cls("skipped", reason=reason)


def lane_from_slack(lookup: SlackLookup | None, *, query: str = "") -> LaneStatus | None:
    """複合検索の Slack の結果（composite.SlackLookup）を探した範囲の 1 項目にする。

    status_note の役割（未連携・連携切れ・失敗・非公開の一致）をここで引き受ける。
    0 件のときだけ、Slack で実際に探した語（slack_query_terms）を添える。Slack を探していない
    （複合検索の対象外）なら None＝探した範囲に Slack を書かない。
    """
    if lookup is None:
        return None
    if lookup.status == "ok":
        if lookup.items:
            return LaneStatus.found(len(lookup.items), hidden=lookup.hidden_count)
        terms = slack_query_terms(query) if query.strip() else ""
        return LaneStatus.zero(queries=(terms,) if terms else (), hidden=lookup.hidden_count)
    if lookup.status == "not_connected":
        reason = "reconnect_required" if lookup.reason == "reconnect_required" else "not_connected"
        return LaneStatus.failed(reason)
    if lookup.reason in ("timeout", "no_identity"):
        return LaneStatus.failed(lookup.reason)
    return LaneStatus.failed("search_failed")


# ── 無害化 ────────────────────────────────────────────────────────────────


def _clean(text: object, limit: int) -> str:
    """第三者・利用者の文字列を 1 行・記法の効かない形にし、``limit`` 字で切る（書記素は割らない）。

    制御文字・向きを変える文字は落とし、改行と連続空白は 1 つの空白にする。
    """
    flat = slack_safe(plain(text))
    cut = truncate_graphemes(flat, limit)
    return f"{cut}{MORE}" if len(cut) < len(flat) else cut


def _quoted(text: object, limit: int) -> str:
    """『』で囲んで出す語（問いの語・取引先名）。秘密・個人情報の形はマスクし、『』は落とす。"""
    raw = str(scrub_value(str(text or ""))).replace("『", "").replace("』", "")
    return _clean(raw, limit)


# URL に使える ASCII の文字だけ（Markdown のリンクを壊す ( ) [ ] と < > | ` * 空白は含めない）。
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'+,;=%]+")
_DATE_RE = re.compile(r"20\d{2}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?")


def _safe_url(url: object) -> str | None:
    """リンクにしてよい URL か。だめなら None（表示名だけにする）。"""
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > _URL_MAX or _URL_RE.fullmatch(u) is None:
        return None
    try:
        if not urlsplit(u).hostname:
            return None
    except ValueError:
        return None
    return u


def _date_text(value: object) -> str:
    """YYYY-MM-DD / YYYY-MM だけを通す（時刻つきは日付だけ・形の違うものは出さない）。"""
    s = str(value or "").strip()
    for candidate in (s[:10], s[:7]):
        if _DATE_RE.fullmatch(candidate):
            return candidate
    return ""


def _link(label: str, url: str | None) -> str:
    """表示名つきリンク（標準 Markdown。配信側が Slack の <URL|表示名> に変える）。"""
    return f"[{label}]({url})" if url else label


def case_external_use_mark(value: object) -> str:
    """事例の社外利用の印。ng→社外展開NG・ok→なし・それ以外（unknown・空・未知）→要確認。"""
    normalized = str(value or "").strip().lower()
    if normalized == "ng":
        return CASE_NG_MARK
    if normalized == "ok":
        return ""
    return CASE_UNKNOWN_MARK


# ── ヒット → 行 ───────────────────────────────────────────────────────────


def _truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() == "true"


def classify_hit(meta: Mapping[str, Any]) -> SectionKey:
    """ヒットの metadata から節を決める（事例・施策実績 → 案件 → 営業FB → 調べもの → 資料）。"""
    if (
        _truthy(meta.get("campaign_aggregate"))
        or _truthy(meta.get("case_corpus"))
        or meta.get("case_source") in ("deck", "sheet", "campaign_db")
        or meta.get("cls_doc_type") == "事例"
    ):
        return "case"
    if str(meta.get("topic") or "").strip() == DEAL_TOPIC or DEAL_TOPIC in str(
        meta.get("channel_name") or ""
    ):
        return "deal"
    if _truthy(meta.get("is_sales_fb")):
        return "karte"
    if meta.get("x_research_tool"):
        return "research"
    return "vault"


def _hit_title(meta: Mapping[str, Any]) -> str:
    for key in ("title", "file_name"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    if meta.get("channel_name"):
        return f"#{meta['channel_name']}"
    for key in ("cls_project", "client_name"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return "資料"


def _hit_date(meta: Mapping[str, Any], title: str) -> str:
    """資料名の日付 → 取込元の更新日 → 発生日（search の date_basis と同じ優先順）。"""
    for candidate in (
        extract_title_date(title),
        extract_title_date(str(meta.get("file_name") or "")),
        meta.get("updated_at"),
        meta.get("occurred_at"),
    ):
        date = _date_text(candidate)
        if date:
            return date
    return ""


def _hit_industry(meta: Mapping[str, Any]) -> str | None:
    """業種はメタデータの正準値だけ（施策実績の広告主の業種 → 事例の業種 → 分類の業種）。"""
    for key in ("campaign_industry", "case_industry", "cls_industry"):
        value = normalize_industry(str(meta.get(key) or "") or None)
        if value:
            return value
    return None


def _case_title(company: object, brand: object) -> str:
    """事例の見出し「◯◯様：ブランド」。会社名が無ければ空（呼び側が資料名に戻す）。"""
    name = str(company or "").strip().removesuffix("様").strip()
    item = str(brand or "").strip()
    if not name:
        return ""
    return f"{name}様：{item}" if item else f"{name}様"


def _karte_detail(meta: Mapping[str, Any]) -> str:
    action = str(meta.get("next_action") or "").strip()
    return f"次: {_clean(action, _NEXT_ACTION_CHARS)}" if action else ""


def entry_from_hit(hit: SearchHit, *, url: str | None) -> Entry:
    """金庫のヒット 1 件を行の材料にする（``url`` は呼び側が解いた正準 URL）。"""
    meta = hit.metadata or {}
    document_id = str(meta.get("document_id") or "") or None
    kind = classify_hit(meta)
    title = _hit_title(meta)
    date = _hit_date(meta, title)
    if kind == "case":
        case_title = _case_title(
            meta.get("case_company") or meta.get("client_name"),
            meta.get("case_brand") or meta.get("case_product"),
        )
        return Entry(
            title=title if is_campaign_hit(hit) else case_title or title,
            reference=meta.get(drive_first.REFERENCE_KEY),
            url=url,
            date=date,
            industry=_hit_industry(meta),
            detail=_clean(meta.get("case_effect"), _EFFECT_CHARS),
            kind="case",
            document_id=document_id,
            source_title=title,
            case_external_use=str(meta.get("case_external_use") or "") or None,
        )
    if kind == "karte":
        phase = str(meta.get("deal_phase") or "").strip() or "営業FB"
        client = str(meta.get("client_name") or "").strip()
        return Entry(
            title=f"{client} {phase}" if client else phase,
            reference=meta.get(drive_first.REFERENCE_KEY),
            url=url,
            date=date,
            detail=_karte_detail(meta),
            kind="karte",
        )
    return Entry(
        title=title,
        reference=meta.get(drive_first.REFERENCE_KEY),
        url=url,
        date=date,
        industry=_hit_industry(meta),
        kind=kind,
        document_id=document_id,
    )


def entry_from_case_row(row: Mapping[str, Any]) -> Entry:
    """事例集の 1 行（pgvector_client.list_case_studies の結果）を行の材料にする。"""
    title = str(row.get("title") or "").strip()
    case_title = _case_title(
        row.get("case_client"), row.get("case_brand") or row.get("case_product")
    )
    return Entry(
        title=title if row.get("case_source") == "campaign_db" else case_title or title or "事例",
        reference=row.get(drive_first.REFERENCE_KEY),
        url=row[drive_first.REFERENCE_KEY].url
        if row.get(drive_first.REFERENCE_KEY)
        else hit_doc_url({"source_uri": row.get("source_uri")}),
        date=_date_text(row.get("updated_at")) or _date_text(extract_title_date(title)),
        industry=normalize_industry(str(row.get("case_industry") or "") or None),
        detail=_clean(row.get("case_effect"), _EFFECT_CHARS),
        kind="case",
        document_id=str(row.get("document_id") or row.get("id") or "") or None,
        source_title=title,
        case_external_use=str(row.get("case_external_use") or "") or None,
    )


def entries_from_karte(
    hits: Sequence[SearchHit], *, url_of: Callable[[SearchHit], str | None] | None = None
) -> list[Entry]:
    """カルテの時系列（list_client_timeline_recent・古い順）を新しい順の行にする。"""
    resolve = url_of or _default_url
    entries = [
        Entry(
            title=str((h.metadata or {}).get("deal_phase") or "").strip() or "営業FB",
            reference=(h.metadata or {}).get(drive_first.REFERENCE_KEY),
            url=resolve(h),
            date=_hit_date(h.metadata or {}, ""),
            detail=_karte_detail(h.metadata or {}),
            kind="karte",
        )
        for h in reversed(list(hits))
    ]
    # 同じ日の中は後ろの行（新しい方）を先に。sort は安定なので反転済みの並びを保つ。
    return sorted(entries, key=lambda e: e.date or "", reverse=True)


def entries_from_slack(hits: Sequence[SlackHitOut]) -> list[Entry]:
    """複合検索の Slack の一致（slack_hits）を行にする（場所・日付・抜粋・permalink）。"""
    out: list[Entry] = []
    for hit in hits:
        excerpt = _clean(hit.excerpt, _EXCERPT_CHARS)
        out.append(
            Entry(
                title=hit.channel or "Slack",
                reference=hit.reference,
                url=hit.reference.url if hit.reference else hit.permalink or None,
                date=hit.posted_on or None,
                detail=f"「{excerpt}」" if excerpt else "",
                kind="slack",
            )
        )
    return out


def _default_url(hit: SearchHit) -> str | None:
    return hit_doc_url(hit.metadata or {})


def build_sections(
    *,
    vault_hits: Sequence[SearchHit] = (),
    expand_hits: Sequence[SearchHit] = (),
    karte_hits: Sequence[SearchHit] = (),
    case_rows: Sequence[Mapping[str, Any]] = (),
    near_case_rows: Sequence[Mapping[str, Any]] = (),
    slack_hits: Sequence[SlackHitOut] = (),
    url_of: Callable[[SearchHit], str | None] | None = None,
    min_score: float = 0.0,
    karte_client: str = "",
    expand_subject: str = "",
) -> Sections:
    """各探し先の結果を節に振り分ける。

    - ``vault_hits``: 金庫の主レーンの rerank 後のヒット（並びは関連度順のまま）。
      ``min_score`` 未満は載せない（設計: SEARCH_WEAK_RESULT_THRESHOLD）。metadata で 📁🏆🤝🗂🗃 へ。
    - ``expand_hits``: 言い換えレーンのヒット。すべて 🔎 へ（found に格上げしない）。
      ``min_score`` で絞る。
    - ``karte_hits``: カルテの時系列（取引先の名指しがあるときだけ）。🗂 の先頭へ新しい順で。
    - ``case_rows``: 名指しの取引先の事例（list_case_studies の段 1→2）。🏆 の先頭へ。
    - ``near_case_rows``: 言い換えの業種の事例（段 4）。🔎 の先頭に 1 件、残りは言い換えの後ろへ。
    - ``slack_hits``: 複合検索の Slack の一致。💬 へ。
    - ``url_of``: ヒットの正準 URL（既定は source_url.hit_doc_url。search は _hit_url を渡す）。
    """
    resolve = url_of or _default_url
    grouped: dict[SectionKey, list[Entry]] = {key: [] for key in SECTION_ORDER}
    grouped["case"].extend(entry_from_case_row(row) for row in case_rows)
    grouped["karte"].extend(entries_from_karte(karte_hits, url_of=resolve))
    for hit in vault_hits:
        if hit.score < min_score:
            continue
        entry = entry_from_hit(hit, url=resolve(hit))
        grouped[entry.kind].append(entry)
    grouped["slack"].extend(entries_from_slack(slack_hits))
    near = [entry_from_case_row(row) for row in near_case_rows]
    expanded = [entry_from_hit(h, url=resolve(h)) for h in expand_hits if h.score >= min_score]
    grouped["expand"] = near[:1] + expanded + near[1:]
    return Sections(
        entries={key: tuple(values) for key, values in grouped.items() if values},
        karte_client=karte_client,
        expand_subject=expand_subject,
    )


# ── 描画 ──────────────────────────────────────────────────────────────────


def _header(key: SectionKey, count: int, sections: Sections) -> str:
    if key == "vault":
        return f"📁 金庫の資料（{count}件）"
    if key == "case":
        return f"🏆 事例・施策実績（{count}件）"
    if key == "deal" and sections.timeline_lines.get(key):
        return "🤝 案件（記録に基づく状態）"
    if key == "deal":
        return "🤝 案件（決定・進行中の案件。実績ではありません）"
    if key == "karte":
        client = _quoted(sections.karte_client, _NAME_CHARS)
        return f"🗂 {client}のカルテ（最新{count}件）" if client else f"🗂 営業FBの記録（{count}件）"
    if key == "slack":
        return f"💬 Slack（あなたの Slack・{count}件）"
    if key == "research":
        return "🗃 過去の調べもの（X・TikTok の調査結果）"
    subject = _quoted(sections.expand_subject, _NAME_CHARS)
    if subject:
        return f"🔎 近い業種・言い換え（『{subject}』そのものの資料ではありません）"
    return "🔎 近い業種・言い換え（問いそのものの資料ではありません）"


def _line(entry: Entry, key: SectionKey) -> str:
    label = _clean(entry.title, _TITLE_CHARS) or "資料"
    text = f"- {_link(label, _safe_url(entry.url))}"
    if entry.reference is not None:
        text = f"- {drive_first.label(entry.reference)}: {_link(label, _safe_url(entry.url))}"
    meta: list[str] = []
    date = _date_text(entry.date)
    if date:
        meta.append(date)
    if key in _SHOW_INDUSTRY:
        meta.append(f"業種: {normalize_industry(entry.industry) or UNKNOWN_INDUSTRY}")
    if meta:
        text += f"（{'・'.join(meta)}）"
    detail = _clean(entry.detail, _DETAIL_CHARS)
    if detail:
        text += f" {detail}"
    if entry.kind == "case" or key == "case":
        mark = case_external_use_mark(entry.case_external_use)
        if mark:
            text += f" {mark}"
    if entry.kind == "deal" and key != "deal":
        text += f" {DEAL_MARK}"
    return text


def _identity(entry: Entry) -> tuple[str, ...]:
    url = _safe_url(entry.url)
    if entry.kind == "case":
        # 同じシートのタブ・事例集ファイルにも複数の施策がある。
        return ("case", url or entry.document_id or "", entry.source_title or entry.title)
    if url:
        return ("url", url)
    return ("text", _clean(entry.title, _TITLE_CHARS), _date_text(entry.date), entry.detail)


def _blocks(sections: Sections, caps: Mapping[SectionKey, int]) -> list[str]:
    """節の塊（見出し＋行）を並べる順に。同じ資料（URL）は先に出た節だけに載せる。"""
    shown: set[tuple[str, ...]] = set()
    blocks: list[str] = []
    for key in SECTION_ORDER:
        cap = min(
            caps.get(key, PER_SECTION_MAX),
            max(PER_SECTION_MAX, sections.required_counts.get(key, 0)),
        )
        if cap <= 0:
            continue
        lines: list[str] = []
        for entry in sections.of(key):
            ident = _identity(entry)
            if ident in shown:
                continue
            shown.add(ident)
            lines.append(_line(entry, key))
            if len(lines) >= cap:
                break
        count = len(lines)
        if lines:
            status_lines = [
                "- 状態: " + _clean(line, 350) for line in sections.timeline_lines.get(key, ())
            ]
            lines = status_lines + lines
            blocks.append(_header(key, count, sections) + "\n" + "\n".join(lines))
    return blocks


def graph_link(sections: Sections, caps: Mapping[SectionKey, int], base: str) -> str:
    """実際に描画した資料だけを種にする。本文・検索語はURLへ入れない。"""
    if not base or _safe_url(base.rstrip("/")) is None:
        return ""
    parsed = urlsplit(base)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        return ""
    shown: set[tuple[str, ...]] = set()
    ids: list[str] = []
    for key in SECTION_ORDER:
        cap = min(
            caps.get(key, PER_SECTION_MAX),
            max(PER_SECTION_MAX, sections.required_counts.get(key, 0)),
        )
        count = 0
        for entry in sections.of(key) if cap > 0 else ():
            ident = _identity(entry)
            if ident in shown:
                continue
            shown.add(ident)
            count += 1
            if key in ("vault", "case", "deal") and entry.document_id:
                try:
                    nid = str(UUID(entry.document_id))
                except ValueError:
                    pass
                else:
                    if nid not in ids and len(ids) < 12:
                        ids.append(nid)
            if count >= cap:
                break
    if not ids:
        return ""
    return (
        "🕸 関係図で見る: <" + base.rstrip("/") + "/search/graph/focus?seed=" + ",".join(ids) + ">"
    )


def render_sections(sections: Sections) -> str:
    """探し先ごとの節（0 件の節は出さない・各節は上位 PER_SECTION_MAX 件）。無ければ空。"""
    return "\n\n".join(_blocks(sections, {}))


def _queries_text(queries: Sequence[str]) -> str:
    labels: list[str] = []
    for query in queries:
        label = _quoted(query, _QUERY_CHARS)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= _QUERIES_MAX:
            break
    return "".join(f"『{label}』" for label in labels)


def _lane_text(name: str, status: LaneStatus, *, deal_count: int) -> str:
    label = _LANE_LABELS[name]
    if status.kind in ("found", "zero"):
        if status.kind == "found":
            head = f"{label} ✓{'最新' if name == 'karte' else ''}{status.count}件"
        else:
            head = f"{label} 0件"
        parts: list[str] = []
        queries = _queries_text(status.queries)
        if queries:
            parts.append(queries)
        if status.partial:
            parts.append("再検索の一部は時間切れ・失敗")
        if status.kind == "found" and deal_count > 0:
            parts.append(f"うち案件 {deal_count}件")
        if status.hidden > 0:
            parts.append(f"非公開の場所の一致 {status.hidden}件は DM でだけ出せます")
        return head + (f"（{'・'.join(parts)}）" if parts else "")
    if status.kind == "failed":
        text = _FAILED_TEXTS.get(status.reason, _FAILED_DEFAULT)
        hint = _FAILED_HINTS.get(status.reason)
        if hint:
            return f"{label} ✗ {text}（{hint}）"
        note = _NOT_ABSENT_NOTES.get(name)
        return f"{label} ✗ {text}" + (f"（{note}）" if note else "")
    return f"{label} —（{_SKIPPED_TEXTS.get(status.reason, _SKIPPED_DEFAULT)}）"


def scope_line(lanes: Mapping[str, LaneStatus], *, deal_count: int = 0) -> str:
    """探した範囲の 1 行（LANE_ORDER の順・渡された探し先だけ）。

    記号: ✓＝見つかった・0件＝探して無かった・✗＝探せなかった（理由つき・「無い」とは書かない）・
    —＝条件に合わず使わなかった。``deal_count`` は金庫の ✓ に「うち案件 N件」として添える。
    """
    parts = [
        _lane_text(name, lanes[name], deal_count=deal_count if name == "vault" else 0)
        for name in LANE_ORDER
        if name in lanes
    ]
    if "slack" in lanes and lanes["slack"].kind in ("found", "zero"):
        parts.append("Slack: 公開チャンネル全体＋参加している非公開・DM")
    return SCOPE_PREFIX + ("・".join(parts) if parts else "記録なし")


def _join(head: str, blocks: Sequence[str], tail: str, scope: str) -> str:
    out = head.rstrip()
    body = "\n\n".join(blocks)
    if body:
        out = f"{out}\n\n{body}" if out else body
    for part in (tail.strip(), scope):
        if part:
            out = f"{out}\n\n{part}" if out else part
    return out


_TRAILING_URL_RE = re.compile(r"https?://\S*$")


def _clip_block(text: str, budget: int) -> str:
    """``budget`` 字以内に、行の境目で切って「…」を付ける。リンク・URL の途中では切らない。"""
    if len(text) <= budget:
        return text
    if budget <= len(MORE):
        return ""
    cut = truncate_graphemes(text, budget - len(MORE))
    newline = cut.rfind("\n")
    if newline >= len(cut) // 2:
        # 行の境目で切れた＝最後の行は丸ごと残る（リンクも URL も途中で切れていない）。
        return cut[:newline].rstrip() + MORE
    # 行の途中で切るときだけ、末尾の切れた URL・<…> のリンク・[表示名](URL) を落とす。
    url = _TRAILING_URL_RE.search(cut)
    if url is not None:
        cut = cut[: url.start()]
    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    if cut.rfind("[") > cut.rfind(")"):
        cut = cut[: cut.rfind("[")]
    return cut.rstrip() + MORE


def _clip_head(text: str, budget: int) -> str:
    """コードの動画実績を先に確保し、要約の長さで 📊 全体が消えるのを防ぐ。"""
    before, heading, rows = text.partition(VIDEO_METRICS_HEADING)
    if not heading or len(text) <= budget:
        return _clip_block(text, budget)
    metrics = heading + rows
    if len(metrics) >= budget:
        return _clip_block(metrics, budget)
    before = _clip_block(before.rstrip(), budget - len(metrics) - 2)
    return before + "\n\n" + metrics if before else metrics


def compose_answer(
    *,
    head: str,
    sections: Sections,
    lanes: Mapping[str, LaneStatus],
    tail: str = "",
    graph_base_url: str = "",
    limit: int | None = ANSWER_MAX_CHARS,
) -> str:
    """答え全体（head → 節 → tail → 探した範囲）を ``limit`` 字以内で組む。

    探した範囲は毎回・必ず最後に付く。超えたときの削り方はモジュールの docstring の段 1〜4。
    ``head`` は警告ヘッダ・要約・📎 施策・📊（または該当なしの定型文）を組み上げた文字列、
    ``tail`` は 📎 実ファイルの提案 1 行（無ければ空）。
    直接投稿の ``limit=None`` は head を先頭に置き、モデル用 2,500 字予算で本文を削らない。
    """
    # 「うち案件 N件」は同じ資料（URL）の重複を 1 件と数える（描画の重複除去と同じ鍵）。
    scope = scope_line(lanes, deal_count=len({_identity(e) for e in sections.of("deal")}))
    caps: dict[SectionKey, int] = {
        key: max(PER_SECTION_MAX, sections.required_counts.get(key, 0)) for key in SECTION_ORDER
    }
    # 名指し資料を優先する。要約が長いときは先に要約だけを切る。
    if sections.required_counts and limit is not None:
        blocks = _blocks(
            sections, {key: sections.required_counts.get(key, 0) for key in SECTION_ORDER}
        )
        budget = limit - len(_join("", blocks, tail, scope)) - 2
        head = _clip_head(head, max(0, budget))

    def render(text_head: str, text_tail: str = tail) -> str:
        blocks = _blocks(sections, caps)
        if sections.timeline_lines and limit is not None:
            leading = [b for b in blocks if b.startswith(("🤝", "🗂"))]
            rest = [b for b in blocks if b not in leading]
            text_head = "\n\n".join([*leading, text_head] if text_head else leading)
            blocks = rest
        link = graph_link(sections, caps, graph_base_url)
        footer = link + "\n\n" + scope if link else scope
        return _join(text_head, blocks, text_tail, footer)

    text = render(head)
    if limit is None or len(text) <= limit:
        return text
    # 段 1: 各節を 2→1 件（削る順に）。
    for key in TRIM_ORDER:
        if caps[key] > max(1, sections.required_counts.get(key, 0)):
            caps[key] = max(1, sections.required_counts.get(key, 0))
            text = render(head)
            if len(text) <= limit:
                return text
    # 段 2・3: 🔎 を外し、残りの節も削る順に丸ごと外す。
    for key in TRIM_ORDER:
        if caps[key] > 0 and not sections.required_counts.get(key):
            caps[key] = 0
            text = render(head)
            if len(text) <= limit:
                return text
    if graph_base_url:
        graph_base_url = ""
        text = render(head)
        if len(text) <= limit:
            return text
    # 段 4: head を切る（探した範囲と tail は残す）。tail だけで超える異常時は tail も切る。
    tail_budget = limit - len(_join("", (), "", scope)) - 2
    clipped_tail = _clip_block(tail.strip(), max(0, tail_budget))
    head_budget = limit - len(_join("", (), clipped_tail, scope)) - 2
    text = render(_clip_head(head.rstrip(), max(0, head_budget)), clipped_tail)
    if len(text) <= limit:
        return text
    return truncate_graphemes(scope, limit)


__all__ = [
    "ANSWER_MAX_CHARS",
    "CASE_NG_MARK",
    "CASE_UNKNOWN_MARK",
    "DEAL_MARK",
    "DEAL_TOPIC",
    "LANE_ORDER",
    "PER_SECTION_MAX",
    "SCOPE_PREFIX",
    "SECTION_ORDER",
    "TRIM_ORDER",
    "Entry",
    "LaneKind",
    "LaneName",
    "LaneStatus",
    "SectionKey",
    "Sections",
    "build_sections",
    "case_external_use_mark",
    "classify_hit",
    "compose_answer",
    "entries_from_karte",
    "entries_from_slack",
    "entry_from_case_row",
    "entry_from_hit",
    "lane_from_slack",
    "render_sections",
    "scope_line",
]
