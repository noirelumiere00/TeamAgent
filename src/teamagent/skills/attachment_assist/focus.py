"""長い資料から「依頼に出てくる語」を含むページを優先して詰める（純関数・I/O 無し）。

背景（2026-10-06 の本番依頼）: 事例集 PDF から「日本コカ・コーラ／紅茶花伝の該当事例」だけを
要約してほしい、という依頼。本文を先頭から ``MAX_INPUT_CHARS`` で切るだけだと、該当事例が
後半にあれば LLM に届かず、要約は「不明」になり、末尾で「該当箇所を指定して」と利用者へ
作業を戻していた（利用者は依頼文で既に指定している）。

やること:
  1. 依頼文から語を拾う（カタカナ 3 字以上・漢字 2 字以上・英数 3 字以上。依頼の定型語は除く）
  2. 資料のページごとに語の出現を数え、**多くのページに出る語（資料全体の話題語）は捨てる**
  3. 語を含むページとその前後を、上限の字数まで優先して詰める。余りは先頭から順に足す
     （語がたまたま脚注に出ただけでも、冒頭の文脈を失わない）。ページ順は保つ

語が 1 つも当たらなければ ``None`` を返す（呼び出し側は先頭から切り、正直にそう書く）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

_URL_RE = re.compile(r"https?://\S+|<https?://[^>]*>")
_KATAKANA_RE = re.compile(r"[ァ-ヺー・]{3,}")
_KANJI_RE = re.compile(r"[一-龥々〆ヶ]{2,}")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_SPACE_RE = re.compile(r"\s+")

# 依頼文に毎回のように出る語（資料の中身を指さない）。語として拾わない。
_STOPWORDS = frozenset(
    {
        "資料",
        "要約",
        "確認",
        "不明",
        "該当",
        "事例",
        "該当事例",
        "内容",
        "施策",
        "施策内容",
        "実績",
        "投稿",
        "添付",
        "指定",
        "以前",
        "記載",
        "部分",
        "場合",
        "以下",
        "英訳",
        "翻訳",
        "議事録",
        "集計",
        "修正",
        "修正案",
        "全体",
        "概要",
        "ポイント",
        "スレッド",
        "ファイル",
        "リンク",
        "チャンネル",
        "メッセージ",
        "slack",
        "pdf",
        "word",
        "excel",
        "powerpoint",
        "docx",
        "xlsx",
        "pptx",
        "csv",
        "aico",
        "http",
        "https",
    }
)
# この割合を超えるページに出る語は「資料全体の話題語」として捨てる（ページが少なければ捨てない）。
_COMMON_RATIO = 0.5
_MIN_PAGES_FOR_COMMON_FILTER = 4


@dataclass(frozen=True)
class FocusedBody:
    """語を含むページを優先して詰めた本文。"""

    body: str
    hit_pages: tuple[int, ...]  # 語が当たったページ番号（資料上の番号・昇順）


def _norm(text: str) -> str:
    """NFKC＋空白除去（PDF 抽出で日本語の字間に空白・改行が入っても当たるように）。"""
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", text)).lower()


def focus_terms(instruction: str, *, exclude: Sequence[str] = ()) -> list[str]:
    """依頼文から、資料の中を探す語を拾う（順序は出現順・重複なし）。

    ``exclude`` は依頼文から先に取り除く文字列（ファイル名など。資料の表紙にしか出ない語で
    ページ選びが引きずられないように）。
    """
    text = unicodedata.normalize("NFKC", instruction or "")
    text = _URL_RE.sub(" ", text)
    for ex in exclude:
        ex_n = unicodedata.normalize("NFKC", ex or "").strip()
        if ex_n:
            text = text.replace(ex_n, " ")
    found: list[str] = []
    for m in _KATAKANA_RE.finditer(text):
        run = m.group(0).strip("・")
        found.append(run)
        found.extend(part for part in run.split("・") if len(part) >= 3)
    found.extend(m.group(0) for m in _KANJI_RE.finditer(text))
    found.extend(m.group(0) for m in _LATIN_RE.finditer(text))
    terms: list[str] = []
    for raw in found:
        term = _norm(raw)
        if len(term) < 2 or term in _STOPWORDS or term in terms:
            continue
        terms.append(term)
    return terms


def focus_pages(
    pages: Sequence[tuple[int, str]], terms: Sequence[str], *, budget: int
) -> FocusedBody | None:
    """語を含むページ（とその前後）を ``budget`` 字まで優先して詰める。当たりが無ければ None。"""
    if not pages or not terms or budget <= 0:
        return None
    normed = [_norm(text) for _, text in pages]
    df = {t: sum(1 for p in normed if t in p) for t in terms}
    usable = [t for t in terms if df[t] > 0]
    if len(pages) >= _MIN_PAGES_FOR_COMMON_FILTER:
        usable = [t for t in usable if df[t] <= len(pages) * _COMMON_RATIO]
    if not usable:
        return None
    scores = [sum(1.0 / df[t] for t in usable if t in p) for p in normed]
    hits = [i for i, sc in enumerate(scores) if sc > 0]
    ranked = sorted(hits, key=lambda i: (-scores[i], i))

    chosen: set[int] = set()
    used = 0
    for center in ranked:
        for i in (center, center + 1, center - 1):
            if i < 0 or i >= len(pages) or i in chosen:
                continue
            size = len(pages[i][1]) + 2
            if chosen and used + size > budget:
                continue
            chosen.add(i)
            used += size
        if used >= budget:
            break
    # 余った枠は先頭から順に埋める（優先したページの文脈を補う）。
    for i in range(len(pages)):
        if used >= budget:
            break
        if i in chosen:
            continue
        size = len(pages[i][1]) + 2
        if used + size > budget:
            break
        chosen.add(i)
        used += size
    body = "\n\n".join(pages[i][1] for i in sorted(chosen) if pages[i][1].strip())
    hit_pages = tuple(sorted(pages[i][0] for i in hits if i in chosen))
    return FocusedBody(body=body, hit_pages=hit_pages)


def format_page_list(numbers: Sequence[int], *, limit: int = 6) -> str:
    """``[3, 4, 5, 9]`` → ``3〜5・9``（多すぎれば先頭 ``limit`` 区間＋「ほか」）。"""
    nums = sorted(set(numbers))
    if not nums:
        return ""
    spans: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append((start, prev))
        start = prev = n
    spans.append((start, prev))
    parts = [str(a) if a == b else f"{a}〜{b}" for a, b in spans[:limit]]
    return "・".join(parts) + ("ほか" if len(spans) > limit else "")


__all__ = ["FocusedBody", "focus_pages", "focus_terms", "format_page_list"]
