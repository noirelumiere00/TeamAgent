"""社外 MTG 判定・企業名抽出・注記（純関数のみ・LLM を一切通さない）。

予定タイトル・説明欄は第三者が自由に書ける外部入力だが、**命令として解釈する経路が
そもそも存在しない**（Bedrock/Gemini/Embedder への参照が 0 件であることを AST テストで
固定している）。ここにあるのは正規表現と表引きだけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from teamagent.skills.pre_meeting_brief.signals import (
    BriefSignals,
    normalize_text,
    split_clients,
    tighten_name,
)

Verdict = Literal["external", "uncertain", "internal"]

# ── 評価順（**この順序が仕様**。入れ替えると判定が変わる） ────────────────
#   S1 強シグナル（タイトルの角括弧語）
#   S2 強シグナル（説明欄のクライアント行）
#   S3 強シグナル（社外ドメインの参加者）
#   W1a 弱シグナル①（タイトルの「様」）
#   X   除外語（社内定例）
#   W1b 弱シグナル②（参加者リストが取れない）
# X を S1..S3 より先に評価すると「【社外】◯◯様 提出物確認」が消える。
# X を W1a より先に評価すると「◯◯様 提出物確認」が消える。
#
# ⚠️ PLAN の表は W1 を 1 段（「様」または 参加者リスト不可視）として X の **前** に
#   置いている。実装はそれを W1a / W1b に割り、W1b だけを X の後ろへ回す（明示的な
#   逸脱・PR 本文の「縮小した点」に記載）。理由: ゲストリスト非表示は社内定例でも
#   日常的に起きるため、PLAN どおりだと「週次ヨミ会」「部会」の毎回が uncertain に
#   なり、社内定例に対して毎朝 SQL を撃って事例を並べることになる。逆に社外ドメインの
#   参加者が見えている回は S3 で先に external になるので、この並びで落ちるのは
#   「除外語つき × 参加者不明」の回だけ。
_STRONG_TITLE_WORDS: tuple[str, ...] = (
    "【社外】",
    "【外出】",
    "【訪問】",
    "【来社】",
    "【商談】",
    "【打合せ】",
    "【テレカン】",
)
# 「【社外（外出）】」のように括弧内へ入れ子で書く運用も拾う。
_STRONG_BRACKET_RE = re.compile(r"【[^】]*(社外|外出|訪問|来社|商談|打合せ|テレカン)[^】]*】")

_INTERNAL_WORDS: tuple[str, ...] = (
    "ヨミ会",
    "部会",
    "1on1",
    "締め会",
    "全体会",
    "ブロック",
    "提出",
    "作成",
    "レポート作成",
)

_HONORIFIC_RE = re.compile(r"(様|さま|サマ)")


def classify_external(
    sig: BriefSignals, *, internal_domains: frozenset[str] = frozenset()
) -> Verdict:
    """社外 MTG かを決定論で判定する。

    評価順は S1 → S2 → S3 → W1a → X → W1b（モジュール冒頭の表と同一）。終日予定は対象外。
    """
    if sig.all_day:
        return "internal"
    title = normalize_text(sig.title)

    # --- S1: タイトルの強シグナル ---
    if any(w in title for w in _STRONG_TITLE_WORDS) or _STRONG_BRACKET_RE.search(title):
        return "external"

    # --- S2: 説明欄のクライアント行 ---
    if sig.has_client_line:
        return "external"

    # --- S3: 社外ドメインの参加者（リストが見えているときだけ意味を持つ） ---
    if sig.attendee_list_available and internal_domains:
        for dom in sig.attendee_domains:
            if dom and dom.lower() not in internal_domains:
                return "external"

    # --- W1a: 弱シグナル①「様」。除外語より **先**（「◯◯様 提出物確認」を捨てない） ---
    if _HONORIFIC_RE.search(title):
        return "uncertain"

    # --- X: 除外語（社内定例） ---
    if any(w in title for w in _INTERNAL_WORDS):
        return "internal"

    # --- W1b: 弱シグナル②「参加者リストが見えない」＝判らない（internal と断じない） ---
    #     ⚠️ PLAN の表では X より前。ここだけ後ろに置いている（冒頭の表を参照）。
    if not sig.attendee_list_available:
        return "uncertain"
    return "internal"


# ── 企業名の読み取り（P1 → P2 → P3 → P4・当たらなければ推測しない） ────────
# P2: 「【社外（外出）】○○様_クライアント名」の `_` 以降
_TITLE_UNDERSCORE_RE = re.compile(r"_(?P<name>[^_]+)$")
# P3: 「○○様」の直前
_BEFORE_HONORIFIC_RE = re.compile(r"(?P<name>[^\s　【】\[\]（）()／/・]+?)(?:様|さま)")
_BRACKET_STRIP_RE = re.compile(r"^【[^】]*】\s*")

# 会社形態語（正規化で落とす）。⚠️ ingest の derive_knowledge_client_name は変更しない
# （既存の取込へ影響を出さないため、ここに独自の軽い正規化だけ持つ）。
_CORP_FORMS: tuple[str, ...] = (
    "株式会社",
    "有限会社",
    "合同会社",
    "一般社団法人",
    "公益社団法人",
    "(株)",
    "（株）",
    "㈱",
)
# 部分一致に使ってはいけない短い一般語（誤爆で全社資料が返る）。
_STOPLIST: frozenset[str] = frozenset({"PR", "AI", "SNS", "IR", "DX", "EC", "CM", "TV"})
_MIN_PARTIAL_LEN = 3


@dataclass(frozen=True)
class ClientHint:
    """読み取れた取引先と代理店。読めなかったものは空文字のまま（推測しない）。"""

    clients: tuple[str, ...] = ()
    agency_display: str = ""


def normalize_company(raw: str) -> str:
    """会社形態語と装飾を落とした照合用の社名。空なら空文字。

    ⚠️ 「（代理店：桜通エージェンシー）」のような括弧注記は **呼び出し前に** 退避済みである前提
    （``signals`` 側が agency へ分離する）。ここでは括弧の中身を捨てる。
    """
    name = normalize_text(raw).strip()
    if not name:
        return ""
    name = _BRACKET_STRIP_RE.sub("", name)
    # 括弧注記（業種など）は照合対象にしない。
    name = re.sub(r"[（(][^）)]*[）)]", "", name)
    for form in _CORP_FORMS:
        name = name.replace(form, "")
    name = re.sub(r"(様|さま|御中)$", "", name.strip())
    return name.strip(" 　_-・/／")


def is_usable_partial(name: str) -> bool:
    """部分一致（段2）に使ってよい社名か。完全一致（段1）は長さを問わない。

    2 文字の社名は実在する。一律に最小長 3 で落とすと永久に引けないため、長さ制限は
    **部分一致だけ** に掛ける（完全一致は 2 文字でも撃つ）。
    """
    if not name:
        return False
    if name.upper() in _STOPLIST:
        return False
    return len(name) >= _MIN_PARTIAL_LEN


def extract_client(sig: BriefSignals) -> ClientHint:
    """P1 説明欄 → P2 タイトルの `_` 以降 → P3 「○○様」の直前 → P4 ドメイン。"""
    agency = tighten_name(normalize_text(sig.agency_hint))

    # P1: 説明欄のクライアント行（最優先。タイトルと食い違っても説明欄が勝つ）。
    if sig.client_hint:
        clients = tuple(split_clients(sig.client_hint))
        if clients:
            return ClientHint(clients=clients, agency_display=agency)

    title = normalize_text(sig.title)
    # ⚠️ P2..P4 も第三者が書ける自由文（予定タイトル）由来。捕った直後に
    #    tighten_name を通す＝社名 1 トークン以外を schema へ載せない。
    # P2: タイトル末尾の `_クライアント名`
    m = _TITLE_UNDERSCORE_RE.search(title)
    if m:
        name = tighten_name(m.group("name"))
        if name:
            return ClientHint(clients=(name,), agency_display=agency)

    # P3: 「○○様」の直前
    m2 = _BEFORE_HONORIFIC_RE.search(_BRACKET_STRIP_RE.sub("", title))
    if m2:
        name = tighten_name(m2.group("name"))
        if name:
            return ClientHint(clients=(name,), agency_display=agency)

    # P4: 社外ドメイン（企業名は確定できないのでドメインをそのまま置く＝「要確認」扱い）
    if sig.attendee_list_available:
        for dom in sig.attendee_domains:
            if dom:
                return ClientHint(clients=(tighten_name(dom),), agency_display=agency)
    return ClientHint(clients=(), agency_display=agency)


# ── 対外利用可否（3 値） ────────────────────────────────────────────────
ExternalUse = Literal["ok", "ng", "unknown"]

_NG_TOKENS = ("ng", "不可", "禁止", "confidential", "社外秘")
_OK_TOKENS = ("ok", "可", "公開可", "展開可")


def external_use(raw: str | None) -> ExternalUse:
    """マスター表の「対外利用可否」列 → 3 値。読めなければ ``unknown``（推測しない）。"""
    value = normalize_text(raw or "").strip().lower()
    if not value:
        return "unknown"
    if any(t in value for t in _NG_TOKENS):
        return "ng"
    if any(t in value for t in _OK_TOKENS):
        return "ok"
    return "unknown"


def ng_note(use: ExternalUse, note: str) -> str:
    """1 行の末尾に付ける注記。

    - ``ng``      → ``⚠`` ＋ 理由（列の文言をそのまま。空なら「対外利用NG」）
    - ``unknown`` → **⚠ を使わない** 平文。全件に ⚠ が付くと狼少年化して本命が効かない
    - ``ok``      → 何も付けない
    """
    text = normalize_text(note).strip()
    if use == "ng":
        return f"⚠{text}" if text else "⚠対外利用NG"
    if use == "unknown":
        return "（対外利用可否は資料で確認）"
    return ""


__all__ = [
    "ClientHint",
    "ExternalUse",
    "Verdict",
    "classify_external",
    "external_use",
    "extract_client",
    "is_usable_partial",
    "ng_note",
    "normalize_company",
]
