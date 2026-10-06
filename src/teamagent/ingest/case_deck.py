"""事例集 PPTX（「ショート動画事例集」型）を **事例単位** に切る（2026-10-06）。

アポ前 事例ブリーフィング（pre_meeting_brief）の母集団（``case_corpus='true'``）を、提案者が
示した正本の PPTX から作る。LLM は使わない（正規表現と並び順だけ・決定論）。

実 deck の型（Drive コネクタで全文を読み、11 事例で確認・2026-10-06）::

    [区切り]   ブランド名 1 語だけのスライド（＋全スライド共通のフッタ住所）
    [表題]     「<会社>様：<ブランド>様」などの表題 shape
               「① <見出し>」「<数字の成果>」… ②③（④）の成果ハイライト
               「本施策の全体結果ハイライト」＋ 小見出し ＋ 概要文
    [続き]     施策結果：再生数（表）／構造設計／クリエイティブ分析 …
               ※事例によっては続きのスライドにも表題（「◯◯ 様」）を繰り返す

区切り方（この順序が仕様）:
  1. 事例の **始まり** は「全体結果ハイライト」を含むスライド。表題（「◯◯様」）だけで
     切らない（続きスライドに表題を 4 枚繰り返す事例が 2 件あり、表題で切ると 1 事例が
     4 件に割れる）。
  2. 始まりの直前が区切りスライド（共通フッタとページ番号を除いて 30 字以下・「施策」
     「結果」「：」を含まない）なら、それも事例に含める。
  3. 事例の終わりは、次の事例の始まり（区切りがあれば区切り）の直前。最初の事例より前の
     スライド（表紙・目次）はどの事例にも入れない。
  4. 表題は始まりのスライドの shape を **1 つずつ完全一致** で照合する（本文中の
     「依頼先の◯◯様からは…」を拾わない）。表題が読めない事例は **推測で埋めず落とす**
     （件数は ``unmatched_titles`` で返し、取り込み側が warning にする）。

照合キー（client_name / case_brand）は pre_meeting_brief の ``normalize_company`` を
そのまま使う（段1 の完全一致は「MTG 側の正規化 == 金庫側の正規化」でしか当たらないため、
同等品を別に書かず同じ関数を共有する）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from teamagent.skills.pre_meeting_brief.classify import normalize_company
from teamagent.util.grapheme_cut import truncate_graphemes

#: 事例の始まりの印（実 deck は「本施策の全体結果ハイライト」「…（4つの成功）」の 2 形）。
CASE_START_MARKER_RE = re.compile(r"全体結果ハイライト")

#: 表題 shape の型。shape の文字列（段落は空白なしで連結・NFKC 済み）に **完全一致** で当てる。
#: 会社名は空白区切り 4 語まで（「North Field Japan株式会社」「SEA LINE振興会」）。
#: 様の後ろは 4 形のどれか、または何も無い（例はすべて架空の社名・実 deck の型は同じ）:
#:   ：<ブランド>（様）   「青葉レコード様：北斗シスターズ様」「みなと食品様：フルーツ酢」
#:   「<ブランド>」『…』 「銀河フィルム株式会社 様「羊の箱」」
#:   （<注記>）          「North Field Japan株式会社様（整備済み端末の再販事業）」
#:   空白 <ブランド>     「白樺様 白樺スリープ「深呼吸まくら」」
#:   （無し）            「みなと銀行 様」
_COMPANY_TOKEN = r"[^\s。、:「『（(]{1,40}"
CASE_TITLE_RE = re.compile(
    rf"^(?P<company>{_COMPANY_TOKEN}(?:\s{_COMPANY_TOKEN}){{0,3}}?)\s*様"
    r"(?:"
    r"\s*:\s*(?P<brand_colon>[^。]{1,60}?)(?:\s*様)?"
    r"|\s*(?P<brand_quoted>[「『][^」』]{1,60}[」』])"
    r"|\s*[（(](?P<note>[^）)]{1,60})[）)]"
    r"|\s+(?P<brand_space>[^。\s][^。]{0,60}?)"
    r")?$"
)

#: 成果ハイライトの見出し（「① UGC風動画で話題化」）。NFKC 前の生文字列に当てる
#: （NFKC は ① を "1" に潰すので、正規化した後では見出しと本文の数字を区別できない）。
_HEADING_RE = re.compile(r"^\s*(?P<num>[①-⑳])\s*(?P<text>.+)$")

#: 区切りスライドに出ない語（短い「施策結果：TikTok内での占有状況」を区切りと誤認しない）。
_NOT_DIVIDER_RE = re.compile(r"施策|結果|[:：]")
_DIVIDER_MAX_CHARS = 30
#: 「成果の数字」とみなす短い shape（「380%達成」「751件のCVを獲得」）の上限。
_RESULT_MAX_CHARS = 30
#: 小見出し（「◯◯ TikTokショート動画施策」）の上限。
_SUBHEADING_MAX_CHARS = 80
#: case_effect の上限（pre_meeting_brief の effect_display と同じ）。
CASE_EFFECT_MAX_CHARS = 160
#: 共通フッタとみなす割合（空でないスライドのこの割合以上に同じ文字列が出る shape は外す）。
_BOILERPLATE_MIN_RATIO = 0.2
_BOILERPLATE_MIN_SLIDES = 5

_DIGITS_ONLY_RE = re.compile(r"^\s*\d{1,3}\s*$")
_HAS_DIGIT_RE = re.compile(r"\d")
_QUOTED_RE = re.compile(r"[「『]([^」』]*)[」』]")


@dataclass(frozen=True)
class DeckCase:
    """deck から切り出した事例 1 件。"""

    company: str  # 表題の会社名（NFKC 済み・法人格はそのまま＝表示用）
    client_name: str  # 照合用（normalize_company）。段1/2 が引くキー
    brand: str  # 表題のブランド/作品（括弧は外す）。無ければ ""
    brand_key: str  # 照合用（normalize_company）。無ければ ""
    product: str  # 表示用の商材/施策（ブランド → 小見出し の順・無ければ ""）
    effect: str  # 成果ハイライトの 1 行（160 字以内・数字の見出しが中心）
    slide_from: int  # 事例の最初のスライド番号（区切りを含む）
    slide_to: int  # 事例の最後のスライド番号
    pages: tuple[tuple[int, str], ...]  # (スライド番号, 本文)。共通フッタ・ページ番号は除去済み


@dataclass(frozen=True)
class DeckSplitResult:
    """切り出しの結果と診断値（件数だけ・社名は持たない＝ログに出してよい）。"""

    cases: tuple[DeckCase, ...]
    slide_count: int
    start_slides: int  # 「全体結果ハイライト」を含むスライド数
    unmatched_titles: int  # 始まりのスライドなのに表題が読めず落とした件数


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _flat(text: str) -> str:
    """shape の段落を空白なしで繋いだ 1 行（表題の照合用）。"""
    return "".join(part.strip() for part in _nfkc(text).split("\n")).strip()


def _boilerplate(slides: Sequence[tuple[int, Sequence[str]]]) -> frozenset[str]:
    """deck の多くのスライドに同じ文字列で出る shape（フッタ住所など）を集める。

    実 deck の住所フッタは全スライドではなく約半数にだけ載っている（コネクタの全文で
    確認）ので、閾値は「2 割以上 かつ 5 枚以上」（事例の続きで表題を 3〜4 枚繰り返す型を
    共通フッタと誤認しない）。事例の始まりの印（全体結果ハイライト）は
    事例の数だけ繰り返すが、印そのものは共通フッタとして消さない。
    """
    non_empty = [shapes for _, shapes in slides if shapes]
    if len(non_empty) < _BOILERPLATE_MIN_SLIDES:
        return frozenset()
    counter: Counter[str] = Counter()
    for shapes in non_empty:
        counter.update({_flat(s) for s in shapes if _flat(s)})
    threshold = max(_BOILERPLATE_MIN_SLIDES, int(len(non_empty) * _BOILERPLATE_MIN_RATIO))
    return frozenset(
        text
        for text, count in counter.items()
        if count >= threshold and not CASE_START_MARKER_RE.search(text)
    )


def _content_shapes(shapes: Sequence[str], boilerplate: frozenset[str]) -> list[str]:
    """共通フッタとページ番号（数字だけの shape）を除いた shape。"""
    return [
        s
        for s in shapes
        if s.strip() and _flat(s) not in boilerplate and not _DIGITS_ONLY_RE.match(_nfkc(s))
    ]


def _is_start(shapes: Sequence[str]) -> bool:
    return any(CASE_START_MARKER_RE.search(_nfkc(s)) for s in shapes)


def _is_divider(shapes: Sequence[str]) -> bool:
    text = "".join(_flat(s) for s in shapes)
    return 0 < len(text) <= _DIVIDER_MAX_CHARS and not _NOT_DIVIDER_RE.search(text)


def _unwrap_brand(raw: str) -> str:
    """「「羊の箱」」→「羊の箱」。括弧の外に文字があれば括弧ごと残す。"""
    text = raw.strip()
    m = _QUOTED_RE.fullmatch(text)
    if m:
        return m.group(1).strip()
    return text


def _brand_key(brand: str) -> str:
    """照合キー: 括弧の外の主語（「白樺スリープ「深呼吸まくら」」→「白樺スリープ」）。"""
    head = _QUOTED_RE.split(brand, maxsplit=1)[0].strip() or brand
    return normalize_company(head)


def parse_case_title(shape_text: str) -> tuple[str, str] | None:
    """表題 shape → (会社名, ブランド)。表題の型でなければ None（推測しない）。"""
    flat = _flat(shape_text)
    if not flat or len(flat) > 120:
        return None
    m = CASE_TITLE_RE.match(flat)
    if not m:
        return None
    company = m.group("company").strip()
    brand_raw = m.group("brand_colon") or m.group("brand_quoted") or m.group("brand_space") or ""
    brand = _unwrap_brand(brand_raw)
    if not normalize_company(company):
        return None
    return company, brand


def _find_title(shapes: Sequence[str]) -> tuple[str, str] | None:
    for shape in shapes:
        parsed = parse_case_title(shape)
        if parsed is not None:
            return parsed
    return None


def _short_result(text: str) -> str:
    """「最も高い\\n指名検索を獲得」→「最も高い指名検索を獲得」。条件外は ""。"""
    joined = "".join(line.strip() for line in text.split("\n")).strip()
    if (
        not joined
        or len(joined) > _RESULT_MAX_CHARS
        or "。" in joined
        or _HEADING_RE.match(joined)
        or _DIGITS_ONLY_RE.match(_nfkc(joined))
        or CASE_START_MARKER_RE.search(_nfkc(joined))
    ):
        return ""
    return joined


def _compose_effect(shapes: Sequence[str]) -> str:
    """成果ハイライト（①②③）を「見出し：成果」で 1 行に。無ければ概要文の数字入りの文。"""
    parts: list[str] = []
    for idx, shape in enumerate(shapes):
        lines = [line for line in shape.split("\n") if line.strip()]
        if not lines:
            continue
        m = _HEADING_RE.match(lines[0])
        if not m:
            continue
        heading = m.group("text").strip()
        result = _short_result("\n".join(lines[1:])) if len(lines) > 1 else ""
        if not result and idx + 1 < len(shapes):
            result = _short_result(shapes[idx + 1])
        parts.append(f"{heading}：{result}" if result else heading)
    if parts:
        return truncate_graphemes("／".join(parts), CASE_EFFECT_MAX_CHARS)
    # 見出しが無い型: 概要文（。を含む最長の shape）から数字を含む文だけを拾う。
    prose = [s for s in shapes if "。" in s]
    if not prose:
        return ""
    body = max(prose, key=len).replace("\n", "")
    sentences = [s.strip() + "。" for s in body.split("。") if s.strip()]
    picked = [s for s in sentences if _HAS_DIGIT_RE.search(s)] or sentences[:1]
    return truncate_graphemes("".join(picked), CASE_EFFECT_MAX_CHARS)


def _subheading(shapes: Sequence[str]) -> str:
    """「全体結果ハイライト」の直後の小見出し（同じ shape の 2 行目 or 次の shape）。"""
    for idx, shape in enumerate(shapes):
        if not CASE_START_MARKER_RE.search(_nfkc(shape)):
            continue
        lines = [line.strip() for line in shape.split("\n") if line.strip()]
        candidates = lines[1:2]
        if idx + 1 < len(shapes):
            candidates.append(shapes[idx + 1].split("\n", 1)[0].strip())
        for cand in candidates:
            if cand and len(cand) <= _SUBHEADING_MAX_CHARS and "。" not in cand:
                return cand
        return ""
    return ""


def split_case_deck(slides: Sequence[tuple[int, Sequence[str]]]) -> DeckSplitResult:
    """``extract_pptx_slide_shapes`` の結果を事例ごとに切る（モジュール冒頭の手順どおり）。"""
    boilerplate = _boilerplate(slides)
    raw = [(num, list(shapes)) for num, shapes in slides]
    cleaned = [(num, _content_shapes(shapes, boilerplate)) for num, shapes in raw]
    # 始まりの判定と表題の照合は **生の shape** で行う（共通フッタ判定に巻き込まれない）。
    starts = [i for i, (_, shapes) in enumerate(raw) if _is_start(shapes)]

    # 各事例の先頭位置（区切りスライドがあれば区切りから）。
    heads: list[int] = []
    for pos, i in enumerate(starts):
        head = i
        prev_start = starts[pos - 1] if pos else -1
        if i - 1 > prev_start and i - 1 >= 0 and _is_divider(cleaned[i - 1][1]):
            head = i - 1
        heads.append(head)

    cases: list[DeckCase] = []
    unmatched = 0
    for pos, i in enumerate(starts):
        start_shapes = cleaned[i][1]
        title = _find_title(raw[i][1])
        if title is None:
            unmatched += 1
            continue
        company, brand = title
        head = heads[pos]
        tail = (heads[pos + 1] - 1) if pos + 1 < len(heads) else len(cleaned) - 1
        span = cleaned[head : tail + 1]
        pages = tuple((num, "\n".join(shapes)) for num, shapes in span if shapes)
        subheading = _subheading(start_shapes)
        cases.append(
            DeckCase(
                company=company,
                client_name=normalize_company(company),
                brand=brand,
                brand_key=_brand_key(brand) if brand else "",
                product=brand or subheading,
                effect=_compose_effect(start_shapes),
                slide_from=cleaned[head][0],
                slide_to=cleaned[tail][0],
                pages=pages,
            )
        )
    return DeckSplitResult(
        cases=tuple(cases),
        slide_count=len(slides),
        start_slides=len(starts),
        unmatched_titles=unmatched,
    )


def _norm_key_part(value: str) -> str:
    return re.sub(r"\s+", " ", _nfkc(value)).strip().casefold()


def case_external_id(file_id: str, client_name: str, brand_key: str, ordinal: int = 1) -> str:
    """並び替えで変わらない external_id（file_id ＋ 正規化した 会社×ブランド の sha1）。

    同じ 会社×ブランド が deck に 2 回出たときだけ、2 件目以降に ``#2`` … を付ける
    （順序依存になるのはその同名事例どうしの間だけ）。
    """
    key = f"{_norm_key_part(client_name)}\n{_norm_key_part(brand_key)}"
    if ordinal > 1:
        key += f"\n#{ordinal}"
    # ID の安定化のためのハッシュ（セキュリティ用途ではない）。
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"{file_id}:case:{digest}"


def assign_external_ids(file_id: str, cases: Sequence[DeckCase]) -> tuple[list[str], int]:
    """事例ごとの external_id と「同名で枝番を付けた件数」を返す。"""
    seen: Counter[str] = Counter()
    ids: list[str] = []
    duplicates = 0
    for case in cases:
        base = f"{_norm_key_part(case.client_name)}\n{_norm_key_part(case.brand_key)}"
        seen[base] += 1
        if seen[base] > 1:
            duplicates += 1
        ids.append(case_external_id(file_id, case.client_name, case.brand_key, seen[base]))
    return ids, duplicates


def case_label(case: DeckCase) -> str:
    """「会社名（ブランド）」。ブランドが無ければ会社名だけ。"""
    return f"{case.company}（{case.brand}）" if case.brand else case.company


def case_title(case: DeckCase, deck_name: str) -> str:
    """documents.title（検索結果・出典行に出る）。"""
    return f"事例 {case_label(case)}｜{deck_name}"


def format_case_pages(
    case: DeckCase,
    *,
    deck_name: str,
    external_use_note: str = "",
) -> list[tuple[int, str]]:
    """chunk 化する本文（スライドごと）。どの chunk にも事例名が入るよう各ページに前置する。

    先頭ページの前に「事例 / 出典 / 対外利用 / 成果」の見出し行を置く（検索で事例名と
    数字だけ当たっても、どの deck の何枚目かが分かるように）。
    """
    label = case_label(case)
    header = [
        f"事例: {label}",
        f"出典: {deck_name} スライド {case.slide_from}〜{case.slide_to}",
    ]
    if external_use_note:
        header.append(f"対外利用: {external_use_note}")
    if case.effect:
        header.append(f"成果: {case.effect}")
    out: list[tuple[int, str]] = []
    for idx, (num, text) in enumerate(case.pages):
        body = f"{label}の事例（スライド {num}）\n{text}"
        if idx == 0:
            body = "\n".join(header) + "\n" + body
        out.append((num, body))
    if not out:
        out.append((case.slide_from, "\n".join(header)))
    return out


__all__ = [
    "CASE_EFFECT_MAX_CHARS",
    "CASE_START_MARKER_RE",
    "CASE_TITLE_RE",
    "DeckCase",
    "DeckSplitResult",
    "assign_external_ids",
    "case_external_id",
    "case_label",
    "case_title",
    "format_case_pages",
    "parse_case_title",
    "split_case_deck",
]
