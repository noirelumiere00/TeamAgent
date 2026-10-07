"""資料（PowerPoint・お土産 HTML）の文字の共通処理。

- 表示用の絵文字・装飾記号の除去（お土産 FMT と共用。元データは変えない＝表示時だけ）。
- はみ出しの扱い: 縮小や「…」でなく、文の切れ目で切る。残りはノートの【全文】へ（原文は変えない）。
- 言い方の検査: 前の会を指す言い方・AI っぽい言い方・「PR 表記なし」をオーガニックと言い換えること。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 絵文字・装飾記号の除去（矢印・約物は残す）。元データは改変しない=表示時のみ。
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 絵文字ブロック全般
    "\U00002600-\U000027bf"  # Misc Symbols / Dingbats
    "\U0001f1e6-\U0001f1ff"  # Regional indicators
    "⬀-⯿"  # ⭐ 等
    "︎️‍⃣"  # VS15/16・ZWJ・囲み keycap
    "]+"
)

# 「オーガニック」は初出の定義注記があるときだけ使える（お土産 FMT の pr_labels ゲート）。
ORGANIC_WORD = "オーガニック"
ORGANIC_NOTE_FRAGMENT = "#PR等の表記が確認できない投稿"

# 前の会・別の資料を指す言い方（どの資料も単独で 1 回目として読めるようにする）。
_PAST_MEETING = re.compile(r"前回|先日|お見せし|ご覧いただい|前の資料")
# AI っぽい決まり文句（言い切り 1 行の題・本文に出さない）。
_AI_PHRASES = re.compile(
    r"いかがでしたか|と言えるでしょう|ではないでしょうか|徹底解説|シームレス|革新的|"
    r"ポテンシャル|鍵となる|カギとなる|重要なのは|まとめると"
)
_QUOTE = re.compile(r"「[^「」]*」|『[^『』]*』")
_SENTENCE = re.compile(r"[^。！？!?\n]*(?:[。！？!?]+|\n+|$)")
_SOFT_BREAKS = "、，,・）)」』 　/｜|"


def strip_display_symbols(text: str) -> str:
    return _EMOJI.sub("", text)


def clean(text: str) -> str:
    """表示用: 絵文字を落とし、空白の連続を 1 つにする。"""
    return re.sub(r"[ \t　]+", " ", strip_display_symbols(text or "")).strip()


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.findall(text) if s.strip()]


def fit_text(text: str, limit: int) -> tuple[str, bool]:
    """limit 字に収まる頭の部分を、文の切れ目（なければ読点などの区切り）で返す。

    戻り値は (載せる文字列, 切ったか)。「…」は付けない。切ったときは呼び出し側が原文を
    ノートの【全文】へ入れる。1 文目が区切りなしで limit を超えるときだけ limit 字で切る。
    """
    text = text.strip()
    if len(text) <= limit:
        return text, False
    head = ""
    for sentence in split_sentences(text):
        if len(head) + len(sentence.rstrip("\n")) > limit:
            break
        head += sentence
    head = head.strip()
    if head:
        return head, True
    window = text[: limit + 1]
    cut = max(window.rfind(ch) for ch in _SOFT_BREAKS)
    if cut >= limit // 2:
        return text[: cut + 1].rstrip(" 　、，,・/｜|"), True
    return text[:limit], True


def organic_without_definition(texts: Iterable[str]) -> bool:
    """「オーガニック」を使っているのに、初出の定義注記が無い。"""
    items = list(texts)
    return any(ORGANIC_WORD in t for t in items) and not any(
        ORGANIC_NOTE_FRAGMENT in t for t in items
    )


def wording_problems(text: str) -> list[str]:
    """資料に出せない言い方。投稿の引用（「…」の中）は作り手の言葉なので見ない。"""
    bare = _QUOTE.sub("", text)
    problems: list[str] = []
    for pattern, label in (
        (_PAST_MEETING, "前の会を指す言い方"),
        (_AI_PHRASES, "AI っぽい言い方"),
    ):
        match = pattern.search(bare)
        if match:
            problems.append(f"{label}「{match.group(0)}」")
    if ORGANIC_WORD in bare:
        problems.append("「PR 表記なし」の言い換え「オーガニック」")
    return problems


__all__ = [
    "ORGANIC_NOTE_FRAGMENT",
    "ORGANIC_WORD",
    "clean",
    "fit_text",
    "organic_without_definition",
    "split_sentences",
    "strip_display_symbols",
    "wording_problems",
]
