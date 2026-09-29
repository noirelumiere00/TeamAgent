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
- メッセージ全体の大きさも抑える（text object の合計 ``MAX_TOTAL_TEXT`` 字）。Slack は block
  ごとの上限とは別に ``msg_blocks_too_long`` で弾く（しきい値は非公開。約 13,200 字で弾かれた
  報告がある）。
- 最上位の ``text`` には blocks と同じ中身をすべて入れる（``message_text``）。Slack の仕様で、
  blocks があるとき**スクリーンリーダーは最上位の text だけを読み、blocks の中は読まない**。
  通知のプレビューと会話の履歴（Aico が後で読む）にもこの text が使われる。
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
# 最上位の text（スクリーンリーダーが読む全文・通知のプレビュー・会話の履歴）。
# Slack の推奨は 4,000 字以内（40,000 字を超えると切られる）。
MAX_FALLBACK_TEXT = 4000
# blocks の text object の合計の上限（msg_blocks_too_long の手前。報告のあった約 13,200 字
# より小さく）。
MAX_TOTAL_TEXT = 12_000
# 文字リンクにできる URL の長さ（1 つの section 3000 字に、見出しと表示名と一緒に収める）。
# 署名つき URL（STS の presigned は 1,500〜2,000 字）も入るように。超えたら文字だけの投稿に戻す。
MAX_URL = 2800

MORE = "…"
MORE_IN_REPORT = "…ほかはレポートをご覧ください"

JST = _dt.timezone(_dt.timedelta(hours=9))


@dataclass(frozen=True)
class RichMessage:
    """直接投稿 1 通分。``text`` は最上位の text（エスケープ済みの mrkdwn）。

    blocks があるとき、スクリーンリーダーはこの text だけを読む（blocks の中は読まない）。通知の
    プレビューと会話の履歴にも使われる。``message_text`` で blocks の全文から組む。
    """

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


def text_size(blocks: Sequence[Block]) -> int:
    """blocks の text object（本文・欄・注記・見出し）の字数の合計。"""
    total = 0
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            total += len(str(text.get("text", "")))
        total += sum(len(str(f.get("text", ""))) for f in block.get("fields", []))
        total += sum(len(str(e.get("text", ""))) for e in block.get("elements", []))
    return total


def assemble(head: list[Block], tail: list[Block]) -> list[Block]:
    """``head``（本文）＋``tail``（レポートのリンク・注記）を 50 blocks・合計
    ``MAX_TOTAL_TEXT`` 字に収める。

    収まらなければ本文の後ろの block を削り「ほかはレポート」を置く（レポートのリンクは必ず残す）。
    """
    head = [b for b in head if b]
    tail = [b for b in tail if b]
    room = MAX_BLOCKS - len(tail)
    budget = MAX_TOTAL_TEXT - text_size(tail)
    if len(head) > room or text_size(head) > budget:
        more = context(MORE_IN_REPORT)
        budget -= text_size([more])
        kept: list[Block] = []
        used = 0
        for block in head:
            size = text_size([block])
            if len(kept) >= room - 1 or used + size > budget:
                break
            kept.append(block)
            used += size
        while kept and kept[-1].get("type") == "divider":
            kept.pop()
        head = [*kept, more]
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
    if text_size(blocks) > MAX_TOTAL_TEXT:
        raise ValueError(f"blocks total text: {text_size(blocks)}")


# ── 最上位の text（スクリーンリーダー・通知・会話の履歴）─────────────────────


def block_text(block: Block) -> str:
    """1 つの block の中身を平文の mrkdwn にする（blocks と同じエスケープ済みの文字列を使う）。

    見出し（plain_text）は mrkdwn に入れるので ``esc`` を通す。欄は「題 値」の 1 行にする。
    区切り線は読み上げの雑音になるので入れない。
    """
    kind = block.get("type")
    if kind == "header":
        return esc(block["text"]["text"])
    if kind == "section":
        parts = [str(block.get("text", {}).get("text", ""))]
        parts += [str(f.get("text", "")).replace("\n", " ") for f in block.get("fields", [])]
        return "\n".join(p for p in parts if p)
    if kind == "context":
        return "\n".join(str(e.get("text", "")) for e in block.get("elements", []) if e.get("text"))
    return ""


def _fair_clip(parts: list[str], room: int) -> list[str]:
    """合計が ``room`` を超えたら、長い部分から同じ長さまで削る（どの部分も頭は残す）。"""
    sizes = [len(p) + 1 for p in parts]
    if sum(sizes) <= room:
        return parts
    low, high = 0, max(sizes)
    while low < high:  # 合計が room 以内になる最大の上限を探す
        mid = (low + high + 1) // 2
        if sum(min(s, mid) for s in sizes) <= room:
            low = mid
        else:
            high = mid - 1
    cap = max(low - 1, len(MORE) + 1)
    return [p if len(p) <= cap else clip(p, cap) for p in parts]


def message_text(
    lead: Sequence[str],
    body: Sequence[Block],
    tail: Sequence[Block],
    *,
    urls: Sequence[tuple[str, str]] = (),
) -> str:
    """最上位の text: 通知の要点（``lead``）＋blocks の全文（``body``）＋末尾（``tail``）。

    Slack はスクリーンリーダーに最上位の text だけを読ませるので、blocks と同じ中身をすべて入れる。
    ``MAX_FALLBACK_TEXT`` を超えるときは ``body`` の長い block から削り（どの節も頭の行は残す）、
    ``lead`` と ``tail``（レポートのリンク）は残す。``urls``（(表示, リンク済みの URL)）は、削った
    結果 text に無くなった投稿の URL だけを最後に足す（会話の履歴から「2位の動画」を辿れるように）。
    """
    lead_s = "\n".join(line for line in lead if line)
    tail_s = "\n".join(t for t in (block_text(b) for b in tail) if t)
    url_room = sum(len(label) + len(url) + 6 for label, url in urls) + 12 if urls else 0
    room = MAX_FALLBACK_TEXT - len(lead_s) - len(tail_s) - url_room - 2
    parts = [t for t in (block_text(b) for b in body) if t]
    if room < len(MORE_IN_REPORT) + 2:
        # 末尾だけで溢れる（長い署名 URL が並ぶ）。本文は付けず、全体を切る
        # （リンクの途中では切らない）。
        return clip("\n".join(x for x in (lead_s, tail_s) if x), MAX_FALLBACK_TEXT)
    body_s = "\n".join(_fair_clip(parts, room)) if parts else ""
    missing = [f"{label} <{url}>" for label, url in urls if url not in lead_s and url not in body_s]
    url_s = ("上位の投稿: " + " ／ ".join(missing)) if missing else ""
    text = "\n".join(x for x in (lead_s, body_s, url_s, tail_s) if x)
    return clip(text, MAX_FALLBACK_TEXT)


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
    "MAX_TOTAL_TEXT",
    "MAX_URL",
    "MORE_IN_REPORT",
    "Block",
    "RichMessage",
    "assemble",
    "block_text",
    "clip",
    "context",
    "count_ja",
    "divider",
    "esc",
    "header",
    "link",
    "link_url",
    "measured_at",
    "message_text",
    "mrkdwn",
    "plain",
    "post_url",
    "render_or_none",
    "section",
    "stage",
    "text_size",
    "validate",
]
