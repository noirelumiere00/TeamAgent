"""Slack の Block Kit 部品（mcp が Slack API へ直接投稿する経路専用）。

背景（2026-09-28 本番）: 検索上位チェック・2 段目の追記・動画分析の完了は、mcp が
``chat.postMessage`` で依頼元の DM へ直接出している。文面（``slack_summary``）は Aico が中継して
Markdown→mrkdwn に変換される前提で書かれているため、直接投稿では ``[@id](<url>)`` が生のまま出る・
「- 」がただのハイフンになる・40 行近い壁になる、と小俣さんから「見ずらい」と言われた。
そこで直接投稿だけ Block Kit（見出し・要点・数字の欄・1 本 1 行・文字リンク）で組み直す。
``slack_summary`` は Aico が中継する経路（直接投稿の対象外の人）向けにそのまま残す。

守ること:
- 第三者の文字列（キャプション・表示名・KW・LLM/Gemini の出力）は ``esc`` を**1 回だけ**通して
  差し込む（``& < >`` を実体参照に、``* ~ ` |`` を効かない字に）。``<!here>``・``<@U…>``・
  ``<https://x|偽名>`` を作らせない。組み立てた後の文面全体には掛けない（自前のリンクが壊れる）。
- リンクにするのは自分で組み立てた URL だけ（``post_url``＝safe_href を通した SNS の投稿 URL、
  ``link_url``＝自前のレポートの署名 URL）。ボタンは使わない（interactivity の受け口が無い）。
- mrkdwn の text object はすべて ``verbatim: true``（URL・#チャンネル名・@メンションの自動変換を
  止める）。
- Block Kit の上限（message 50 blocks・section text 3000 字・field 2000 字×10・header 150 字・
  context 要素 10）を超えない。超えそうなら行の境目で切り、``…`` を付ける。
- 色や絵文字だけで意味を伝えない（DADS）。絵文字は見出しの目印で、意味は必ず文字で書く。
- 太字 ``*語*`` の閉じの直後は改行か半角空白にする（和文が続くと太字にならない例がある）。
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog

from teamagent.skills._shared.text_safety import safe_href

logger = structlog.get_logger(__name__)

Block = dict[str, Any]

# Block Kit の上限（Slack の仕様）。テストで固定する。
MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000
MAX_FIELD_TEXT = 2000
MAX_FIELDS = 10
MAX_HEADER_TEXT = 150
MAX_CONTEXT_ELEMENTS = 10
MAX_CONTEXT_TEXT = 3000
# 通知・フォールバックの文（blocks があるときは通知のプレビューと、会話の履歴に出る）。
MAX_FALLBACK_TEXT = 3000
# 文字リンクにできる URL の長さ（1 つの section 3000 字に、見出しと表示名と一緒に収める）。
# 署名つき URL（STS の presigned は 1,500〜2,000 字）も入るように。超えたら文字だけの投稿に戻す。
MAX_URL = 2800

MORE = "…"
MORE_IN_REPORT = "…ほかはレポートをご覧ください"

JST = _dt.timezone(_dt.timedelta(hours=9))


@dataclass(frozen=True)
class RichMessage:
    """直接投稿 1 通分。``text`` は通知・フォールバック（エスケープ済みの mrkdwn）。"""

    text: str
    blocks: list[Block] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {"text": self.text, "blocks": self.blocks}


# ── 第三者の文字列 ─────────────────────────────────────────────────────


# 書式の記号は効かない字へ（``_`` はハンドル @spice_koki のコピペを壊すので触らない）。
_NEUTRAL = str.maketrans({"*": "＊", "~": "〜", "`": "'", "|": "｜"})
# 制御文字と、表示の向きを変える文字（リンクの表示名を並べ替えて見せる細工を防ぐ）。
_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def plain(text: object) -> str:
    """改行・連続空白を 1 つの空白にし、制御文字を落とす（header の plain_text 用）。"""
    return " ".join(_CONTROL.sub("", str(text or "")).split())


def esc(text: object) -> str:
    """第三者/LLM の文字列を mrkdwn に差し込める形にする（1 つの文字列に 1 回だけ）。

    ``& < >`` は実体参照（表示は元の字のまま）、``* ~ ` |`` は書式として効かない字にする。
    改行は潰す（1 項目 1 行を崩させない）。
    """
    s = plain(text).translate(_NEUTRAL)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── リンク（自分で組み立てた URL だけ）──────────────────────────────────

# URL に使える ASCII の文字だけ（``<`` ``>`` ``|`` ``"`` 空白・全角は含めない）。
_URL_CHARS = re.compile(r"https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def link_url(url: object) -> str | None:
    """自前の URL（レポートの署名 URL など）をリンクの宛先に使える形にする。使えなければ None。

    https だけ・ASCII の URL 文字だけ・ホストあり・長すぎない。``&`` は実体参照にする
    （Slack はリンクの宛先の実体参照を元に戻す。直接投稿の既存経路と同じ扱い）。
    """
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > MAX_URL or _URL_CHARS.fullmatch(u) is None:
        return None
    try:
        if not urlsplit(u).hostname:
            return None
    except ValueError:
        return None
    return u.replace("&", "&amp;")


def post_url(url: object) -> str | None:
    """SNS の投稿 URL（safe_href の許可ホストだけ）をリンクの宛先に使える形にする。"""
    if not isinstance(url, str):
        return None
    href = safe_href(url)
    return link_url(href) if href else None


def link(url: str | None, label: object) -> str:
    """``<url|表示名>``。宛先が作れなければ表示名だけ（文字）を返す。

    ``url`` は ``link_url`` / ``post_url`` を通した値を渡す（生の URL を渡さない）。
    """
    shown = esc(label) or "リンク"
    return f"<{url}|{shown}>" if url else shown


# ── 長さ ───────────────────────────────────────────────────────────────


_PARTIAL_ENTITY = re.compile(r"&[a-z]{0,4}$")


def clip(text: str, limit: int, *, more: str = MORE) -> str:
    """``limit`` 字に収める。行の境目で切り、``<…>`` や実体参照の途中では切らない。"""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(more))]
    newline = cut.rfind("\n")
    if newline >= len(cut) // 2:
        cut = cut[:newline]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:
        cut = cut[:lt]
    cut = _PARTIAL_ENTITY.sub("", cut).rstrip()
    return cut + more


# ── ブロック ────────────────────────────────────────────────────────────


def mrkdwn(text: str, *, limit: int = MAX_SECTION_TEXT) -> Block:
    return {"type": "mrkdwn", "text": clip(text, limit), "verbatim": True}


def header(text: object) -> Block:
    """見出し（plain_text・150 字で切る）。

    plain_text は記法もメンションも効かないが、念のため ``< >`` も全角にする
    （KW など第三者の文字列が入る。表示はほぼ同じ）。
    """
    shown = plain(text).replace("<", "＜").replace(">", "＞")
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": clip(shown, MAX_HEADER_TEXT), "emoji": False},
    }


def section(text: str, *, fields: Sequence[str] = ()) -> Block:
    block: Block = {"type": "section", "text": mrkdwn(text)}
    if fields:
        block["fields"] = [mrkdwn(f, limit=MAX_FIELD_TEXT) for f in list(fields)[:MAX_FIELDS]]
    return block


def context(*texts: str) -> Block:
    elements = [mrkdwn(t, limit=MAX_CONTEXT_TEXT) for t in texts if t]
    return {"type": "context", "elements": elements[:MAX_CONTEXT_ELEMENTS]}


def divider() -> Block:
    return {"type": "divider"}


def assemble(head: list[Block], tail: list[Block]) -> list[Block]:
    """``head``（本文）＋``tail``（レポートのリンク・注記）を 50 blocks に収める。

    収まらなければ本文の後ろを削り「ほかはレポート」を置く（レポートのリンクは必ず残す）。
    """
    head = [b for b in head if b]
    tail = [b for b in tail if b]
    room = MAX_BLOCKS - len(tail)
    if len(head) > room:
        head = [*head[: max(0, room - 1)], context(MORE_IN_REPORT)]
    blocks = head + tail
    validate(blocks)
    return blocks


def validate(blocks: list[Block]) -> None:
    """Block Kit の上限を検査する（違反は ValueError＝呼び出し側が文字だけの投稿に戻す）。"""
    if not blocks or len(blocks) > MAX_BLOCKS:
        raise ValueError(f"blocks: {len(blocks)}")
    for block in blocks:
        kind = block.get("type")
        if kind == "header":
            if len(block["text"]["text"]) > MAX_HEADER_TEXT or not block["text"]["text"]:
                raise ValueError("header text")
        elif kind == "section":
            text = block.get("text", {}).get("text", "")
            fields = block.get("fields", [])
            if not text and not fields:
                raise ValueError("empty section")
            if len(text) > MAX_SECTION_TEXT:
                raise ValueError("section text")
            if len(fields) > MAX_FIELDS or any(len(f["text"]) > MAX_FIELD_TEXT for f in fields):
                raise ValueError("section fields")
        elif kind == "context":
            elements = block.get("elements", [])
            if not elements or len(elements) > MAX_CONTEXT_ELEMENTS:
                raise ValueError("context elements")
            if any(len(e["text"]) > MAX_CONTEXT_TEXT or not e["text"] for e in elements):
                raise ValueError("context text")
        elif kind != "divider":
            raise ValueError(f"block type: {kind}")


def fallback_text(lines: Sequence[str]) -> str:
    """通知・フォールバックの文（行ごとにエスケープ済みの文を渡す）。"""
    return clip("\n".join(line for line in lines if line), MAX_FALLBACK_TEXT)


# ── 数字と段階 ───────────────────────────────────────────────────────────


def stage(count: int, total: int) -> str:
    """分母から段階の語を決める（コードが決める。LLM には選ばせない）。

    2 本以上のときだけ: 全員に共通（n/n）・多数派（過半数）・半数・少数派・0 本。1 本以下は空。
    """
    if total < 2 or count < 0 or count > total:
        return ""
    if count == total:
        return "全員に共通"
    if count == 0:
        return "0本"
    if count * 2 > total:
        return "多数派"
    if count * 2 == total:
        return "半数"
    return "少数派"


def count_ja(n: int) -> str:
    """35.1万 / 520万 / 8,200 の形（search_surface_check.display.fmt_count と同じ）。"""
    if n >= 100_000_000:
        return f"{_one_decimal(n / 100_000_000)}億"
    if n >= 1_000_000:
        return f"{round(n / 10_000)}万"
    if n >= 10_000:
        return f"{_one_decimal(n / 10_000)}万"
    return f"{n:,}"


def _one_decimal(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def measured_at(epoch: int) -> str:
    """実測の日時（JST・分まで）。同じ日の再検索（顔ぶれが変わる）を見分けられるようにする。"""
    if epoch <= 0:
        return ""
    return _dt.datetime.fromtimestamp(epoch, JST).strftime("%Y-%m-%d %H:%M")


# ── 描画の失敗は文字だけの投稿に戻す ─────────────────────────────────────


def render_or_none(
    render: Callable[[], RichMessage | None], *, request_id: str, kind: str
) -> RichMessage | None:
    """描画して返す。例外は None（呼び出し側が今の文字だけの投稿に戻す＝結果を消さない）。"""
    try:
        return render()
    except Exception as exc:
        logger.warning(
            "slack_blocks_render_failed",
            request_id=request_id,
            kind=kind,
            error=type(exc).__name__,
        )
        return None


__all__ = [
    "MAX_BLOCKS",
    "MAX_CONTEXT_ELEMENTS",
    "MAX_CONTEXT_TEXT",
    "MAX_FALLBACK_TEXT",
    "MAX_FIELDS",
    "MAX_FIELD_TEXT",
    "MAX_HEADER_TEXT",
    "MAX_SECTION_TEXT",
    "MAX_URL",
    "MORE_IN_REPORT",
    "Block",
    "RichMessage",
    "assemble",
    "clip",
    "context",
    "count_ja",
    "divider",
    "esc",
    "fallback_text",
    "header",
    "link",
    "link_url",
    "measured_at",
    "mrkdwn",
    "plain",
    "post_url",
    "render_or_none",
    "section",
    "stage",
    "validate",
]
