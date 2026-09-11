"""判定の入力（BriefSignals）を作る **唯一の変換点**。

死守ライン: 生 ``description`` を読むのはこのモジュールだけ。schema にも出力にも
Scheduler 入力にもログにも生 description は出さない。ここから先へ渡るのは
「クライアント行があったか」「そこから読めた企業名/代理店」の派生値だけ。

なぜ 1 本に絞るか: 定期便（runner）と on-demand（tool）が別々の抽出器を持つと、
同じ予定が経路によって違う判定になる。「朝の DM には出たのに聞き直すと出ない」は
利用者から見て機能が壊れているのと同じなので、構造で止める。

2 経路の接続:
  - tool 経路   : ``build_signal_input(CalendarEventDetail)`` ← 生 items から直接
  - 定期便経路  : morning_digest が同じ ``build_signal_input`` を通して
                 ``CalendarEventItem`` へ **派生値だけ** 写し、``signals_from_item``
                 が単純な field コピーで復元する（再計算しない）

``signals_from_item`` にロジックを持たせないこと。持たせた瞬間に 2 経路が分岐する。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# 説明欄の「クライアント行」。全角/半角コロン・「得意先」表記ゆれを吸収する。
_CLIENT_LINE_RE = re.compile(r"^[\s　]*(?:クライアント|得意先|顧客|CL)[\s　]*[:：](?P<body>.*)$")
# 「代理店：電通（吉田様）」「代理店: 博報堂」。
_AGENCY_LINE_RE = re.compile(r"^[\s　]*(?:代理店|代理店名|AG)[\s　]*[:：](?P<body>.*)$")

# クライアント行の中に「／代理店：…」が同居する書き方（9/11 実物のテスト送信で確認済み）。
_INLINE_AGENCY_RE = re.compile(r"[／/]\s*(?:代理店|AG)\s*[:：]\s*(?P<agency>.+)$")

# 複数社の連記に使われる区切り。読点は社名内に出うるので使わない。
_CLIENT_SPLIT_RE = re.compile(r"[／/・･,、]+")

_MAX_CLIENTS = 2  # 名寄せ・引き当ての対象にする最大社数（表示は全社）
_MAX_TEXT = 200  # 派生値の字数上限（description 全文を持ち回らせない）


def normalize_text(raw: str | None) -> str:
    """NFKC 正規化＋制御文字/改行除去。判定・抽出の共通前処理。

    NFKC で【社外】の全角・半角、コロンの全半角、㈱などの表記ゆれを吸収する。
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    # 改行はタブ/スペースへ（行構造は _lines 側で扱う）。制御文字は落とす。
    return "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))


@dataclass(frozen=True)
class BriefSignals:
    """社外判定・企業名抽出に必要な材料（生 description を **含まない**）。

    ``attendee_list_available=False`` は「社外参加者ゼロ」ではなく「参加者リストが
    見えていない」。この 2 つを混同すると、ゲストリスト非表示の商談が毎回 internal に
    落ちて消える（Google は非表示時に空配列ではなく本人＋主催者を返す）。
    """

    title: str = ""
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool = False
    has_client_line: bool = False
    client_hint: str = ""  # クライアント行 / タイトルから読めた企業名（連記のまま）
    agency_hint: str = ""  # 「電通（吉田様）」等の表示用（担当者名まで含む）
    attendee_domains: tuple[str, ...] = field(default_factory=tuple)
    attendee_list_available: bool = False


def _lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln.strip()]


def _from_description(description: str) -> tuple[bool, str, str]:
    """説明欄から (クライアント行の有無, 企業名連記, 代理店表示) を読む。"""
    text = normalize_text(description)
    if not text:
        return (False, "", "")
    has_line = False
    client = ""
    agency = ""
    for line in _lines(text):
        m = _CLIENT_LINE_RE.match(line)
        if m and not client:
            has_line = True
            body = m.group("body").strip()
            inline = _INLINE_AGENCY_RE.search(body)
            if inline:
                agency = agency or inline.group("agency").strip()
                body = body[: inline.start()].strip()
            client = body
            continue
        a = _AGENCY_LINE_RE.match(line)
        if a and not agency:
            agency = a.group("body").strip()
    return (has_line, client[:_MAX_TEXT], agency[:_MAX_TEXT])


def build_signal_input(event: Any) -> BriefSignals:
    """``CalendarEventDetail``（または同形の object）→ BriefSignals。

    ⚠️ ここが生 description に触れる唯一の場所。戻り値に description は載らない。
    """
    description = str(getattr(event, "description", "") or "")
    has_client_line, client_hint, agency_hint = _from_description(description)
    domains = tuple(str(d) for d in (getattr(event, "attendee_domains", ()) or ()))
    return BriefSignals(
        title=normalize_text(str(getattr(event, "summary", "") or ""))[:_MAX_TEXT],
        start_at=str(getattr(event, "start", "") or "") or None,
        end_at=str(getattr(event, "end", "") or "") or None,
        all_day=bool(getattr(event, "all_day", False)),
        has_client_line=has_client_line,
        client_hint=client_hint,
        agency_hint=agency_hint,
        attendee_domains=domains[:10],
        attendee_list_available=bool(getattr(event, "attendee_list_available", False)),
    )


def signals_from_item(item: Any) -> BriefSignals:
    """``CalendarEventItem``（定期便の写し）→ BriefSignals。**field コピーのみ**。

    ⚠️ ここで再計算・再抽出をしないこと。ここに 1 行でもロジックが入ると
    「定期便と on-demand で結果が違う」が再発する（round-trip テストが赤になる）。
    """
    return BriefSignals(
        title=normalize_text(
            str(getattr(item, "summary_display", "") or getattr(item, "summary_scrubbed", "") or "")
        )[:_MAX_TEXT],
        start_at=getattr(item, "start_at", None),
        end_at=getattr(item, "end_at", None),
        all_day=bool(getattr(item, "all_day", False)),
        has_client_line=bool(getattr(item, "has_client_line", False)),
        client_hint=str(getattr(item, "client_hint_display", "") or ""),
        agency_hint=str(getattr(item, "agency_display", "") or ""),
        attendee_domains=tuple(str(d) for d in (getattr(item, "attendee_domains", ()) or ())),
        attendee_list_available=bool(getattr(item, "attendee_list_available", False)),
    )


def split_clients(raw: str) -> list[str]:
    """連記された企業名を最大 2 社まで切り出す（表示は呼び出し側で全社を使う）。"""
    if not raw:
        return []
    out: list[str] = []
    for part in _CLIENT_SPLIT_RE.split(normalize_text(raw)):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out[:_MAX_CLIENTS]


__all__ = [
    "BriefSignals",
    "build_signal_input",
    "normalize_text",
    "signals_from_item",
    "split_clients",
]
