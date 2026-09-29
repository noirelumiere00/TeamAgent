"""サムネ（一覧の表紙）の事実・特徴・差・指示（LLM を使わない・純関数）。

Gemini の読み取り（CoverRead）から、コードが次を決める:
- 1 枚ずつの事実（CoverFacts）: 文字の中身（検索語・単位つきの数字・問い・警告・手間なし）、
  大きさ・位置・行数（box_2d と改行から計算。AI の自己申告は使わない）、冒頭のテロップと
  キャプションとの一致（検索語だけの共通は別の値）、商品名の照合と区分、顔・寄り・質感など。
- 特徴の表（cover_feature_table）: 母数は欄ごと（読めなかった欄はその欄の母数から外す）。段階は
  evidence.tier。表紙を読めた本数が上位の本数より少ないときは「必須条件」と呼ばない（多数派まで）。
- 上位 n 本とほか m 本の差（cover_gap）: 6〜30 位を読んだとき（mode=board）だけ。比べる行は
  データを見る前に固定する。Fisher の正確検定（両側）を Holm で補正し、割合の差 0.4 以上・
  上位 3 本以上・ほか 10 本以上・読めた割合 7 割以上・同じ取り方の画像のときだけ
  「差が大きい（参考）」の印を付ける（因果ではない）。
- コードが作る表紙の指示（code_cover_directives）: 差の印のある行 → 上位で多数派かつほかで少ない行
  → 具体的な言い方（検索語・数字）→ ほかの多数派の順に 3 つまで。「文字がある」だけの指示は
  作らない。
  6〜30 位と比べていない指示には、そう書く。

表紙の群は表示順（上位 n 本＝group "top"）で決める。動画の分析の成否（ctx.facts）では切らない
（#2 の動画の取得に失敗しても、#2 の表紙は上位の群に入る）。冒頭のテロップとの一致だけは、動画を
見て分析できた本に限る。
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from teamagent.skills.video_algorithm.evidence import (
    MIN_QUOTE_CHARS,
    TIER_MAJORITY,
    TIER_REQUIRED,
    Roster,
    at_least_majority,
    norm,
    query_terms,
    tier,
)
from teamagent.skills.video_algorithm.facts import OPENING_SEC, Feature, is_watched, rank_runs
from teamagent.skills.video_algorithm.schema import (
    COVER_KIND,
    AnalyzedVideo,
    CoverRead,
    Directive,
    SynthRef,
    VideoMeta,
)

# 一致の判定の最短の長さ（検索語を除いたあとの字数）。
MATCH_MIN_CHARS = 4
# キャプションの冒頭として比べる字数（一覧のタイルの下に出る部分。表示される長さは未確認）。
CAPTION_HEAD = 40
# 「一覧のタイルで読める大きさ」: 文字 1 行の高さが表紙の幅の 1/10 以上。
LARGE_LINE_OF_WIDTH = 0.10
# 画像の縦横が分からないときに仮に置く縦横比（TikTok の表紙の 9:16）。
DEFAULT_ASPECT = 16 / 9
# 差の印の条件。
GAP_MIN_DIFF = 0.4
GAP_ALPHA = 0.10
GAP_MIN_TOP = 3
GAP_MIN_REST = 10
GAP_MIN_READ_SHARE = 0.7
GAP_MAX_TERMS = 2
_P_TOLERANCE = 1 + 1e-7
MAX_CODE_COVER_DIRECTIVES = 3
NOT_COMPARED = "6〜30位とは比べていない"

MatchKind = str  # same / partial / kw_only / different / no_text / no_telop / no_caption / ""
MATCH_LABEL: dict[str, str] = {
    "same": "同じ",
    "partial": "一部同じ",
    "kw_only": "検索語だけ共通",
    "different": "違う",
    "no_text": "表紙に文字なし",
    "no_telop": "冒頭にテロップなし",
    "no_caption": "キャプションなし",
    "": "—",
}
POSITION_JP = {"top": "上", "center": "中央", "bottom": "下", "unknown": "不明"}
ELEMENT_LABEL: dict[str, str] = {
    "person": "人",
    "product": "商品・パッケージ",
    "result": "完成品・仕上がり",
    "process": "工程・使う途中",
    "before_after": "使用前後・比較",
    "text_main": "文字が主",
    "scene": "場所・景色",
}
SIZZLE_LABEL: dict[str, str] = {
    "steam": "湯気",
    "gloss": "照り・つや",
    "cross_section": "断面",
    "pour": "注ぐ・とろみ",
    "foam": "泡",
    "skin": "肌の質感",
    "hair": "髪の質感",
    "texture": "質感",
}
FACE_KIND_LABEL = {
    "real": "実写の人",
    "illustration": "イラスト",
    "in_media": "画面・パッケージの中の顔",
    "none": "なし",
    "unknown": "不明",
}
EXPRESSION_LABEL = {
    "smile": "笑顔",
    "surprise": "驚き",
    "serious": "真顔",
    "other": "そのほか",
    "none": "—",
    "unknown": "不明",
}
GAZE_LABEL = {
    "camera": "カメラ目線",
    "subject": "主役を見る",
    "away": "よそを見る",
    "none": "—",
    "unknown": "不明",
}
PRODUCT_LABEL = {"hero": "主役", "visible": "見える", "none": "なし", "unknown": "不明"}
CLUTTER_LABEL = {"simple": "すっきり", "moderate": "ふつう", "busy": "物が多い", "unknown": "不明"}
LEGIBILITY_LABEL = {
    "good": "読みやすい",
    "ok": "読める",
    "poor": "読みにくい",
    "none": "文字なし",
    "unknown": "不明",
}
STYLE_LABEL = {"outline": "縁取り", "box": "座布団", "shadow": "影", "plain": "飾りなし"}
STATUS_LABEL = {
    "ok": "読めた",
    "no_cover": "表紙のURLなし",
    "skipped": "画像投稿のため読まない",
    "fetch_failed": "表紙を取得できず",
    "read_failed": "AIが読めず",
    "timeout": "時間内に読めず",
}
NUMBER_KIND_LABEL = {
    "time": "時間",
    "count": "数",
    "price": "価格",
    "amount": "量",
    "percent": "割合",
    "rank": "順位",
    "ratio": "倍率",
    "age": "年齢",
}
_UNIT_KIND: dict[str, str] = {
    **dict.fromkeys(("分", "秒", "時間", "日", "日間", "週", "週間", "ヶ月", "か月"), "time"),
    **dict.fromkeys(
        ("選", "個", "品", "種類", "種", "つ", "本", "枚", "回", "杯", "ステップ", "工程"), "count"
    ),
    **dict.fromkeys(("円", "万円"), "price"),
    **dict.fromkeys(("kg", "g", "キロ", "グラム", "ml", "cm", "kcal"), "amount"),
    **dict.fromkeys(("%", "割"), "percent"),
    "位": "rank",
    "倍": "ratio",
    "歳": "age",
}
_NUMBER_RE = re.compile(
    r"(?<![\d.])(\d{1,4}(?:\.\d+)?)\s*("
    + "|".join(sorted((re.escape(u) for u in _UNIT_KIND), key=len, reverse=True))
    + r")",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"[?？]|なぜ|なんで|どうして|どっち|どれが|知って(?:る|た)|って何")
# 警告型（失敗・注意を先に言う）。「間違いない」（太鼓判）・「やめられない」（ほめ言葉）・
# 「失敗しない」（成功の約束）は拾わない。
_WARNING_RE = re.compile(
    r"NG|注意|禁止|危険|ダメ|だめ|やめ(?:て|な[!！。]|とけ|ろ)|"
    r"間違(?:い|え)(?:方|がち|てる|ている|やすい)|失敗(?!しない|なし|ゼロ|知らず)|損(?:する|して)",
    re.IGNORECASE,
)
# 手間なし型（手間・道具・失敗が要らないことを言う）。
_EFFORTLESS_RE = re.compile(
    r"いらない|要らない|いらず|不要|なしで|(?:だけ|のみ)(?:で|[!！。]|$)|ほったらかし|放置|"
    r"洗い物(?:なし|ゼロ|少な)|失敗しない|失敗なし|簡単|かんたん|手軽|ズボラ|ずぼら|時短",
    re.MULTILINE,
)


# ── 文字の照合 ──────────────────────────────────────────────────────────


def match_norm(text: str | None) -> str:
    """照合用: NFKC・大小を無視し、文字（かな・漢字・英字）と数字だけを残す。

    記号・絵文字・【】・！は落とす。
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] in ("L", "N"))


def _longest_common(a: str, b: str) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _drop_terms(text: str, terms: Iterable[str]) -> str:
    for t in sorted((match_norm(x) for x in terms), key=len, reverse=True):
        if t:
            text = text.replace(t, "")
    return text


_MATCH_ORDER = {"same": 0, "partial": 1, "kw_only": 2, "different": 3}


def text_match(cover: str, other: str, terms: Sequence[str]) -> MatchKind:
    """表紙の文字と別の文字の一致。検索語を除いてから比べ、検索語だけの共通は kw_only。

    same＝一方がもう一方を含む（短い方が 4 字以上）・partial＝共通の部分が 4 字以上・
    kw_only＝共通は検索語だけ・different＝それ以外。other が空なら ""（呼んだ側が理由を付ける）。
    """
    a0, b0 = match_norm(cover), match_norm(other)
    if not a0:
        return "no_text"
    if not b0:
        return ""
    a1, b1 = _drop_terms(a0, terms), _drop_terms(b0, terms)
    short = min(len(a1), len(b1))
    if short >= MATCH_MIN_CHARS and (a1 in b1 or b1 in a1):
        return "same"
    if _longest_common(a1, b1) >= MATCH_MIN_CHARS:
        return "partial"
    if any((t := match_norm(term)) and t in a0 and t in b0 for term in terms):
        return "kw_only"
    return "different"


def best_match(cover: str, others: Sequence[str], terms: Sequence[str]) -> MatchKind:
    kinds = [k for k in (text_match(cover, o, terms) for o in others) if k in _MATCH_ORDER]
    return min(kinds, key=lambda k: _MATCH_ORDER[k]) if kinds else ""


# ── 文字の中身 ──────────────────────────────────────────────────────────


def number_claims(text: str) -> tuple[tuple[str, str], ...]:
    """単位つきの数字（種類, 原文）。年号・ID のような単位の無い数字は数えない。"""
    body = unicodedata.normalize("NFKC", text or "")
    out: list[tuple[str, str]] = []
    for m in _NUMBER_RE.finditer(body):
        kind = _UNIT_KIND.get(m.group(2)) or _UNIT_KIND.get(m.group(2).lower(), "")
        if kind:
            out.append((kind, m.group(0).strip()))
    return tuple(dict.fromkeys(out))


def is_question(text: str) -> bool:
    return _QUESTION_RE.search(unicodedata.normalize("NFKC", text or "")) is not None


def is_warning(text: str) -> bool:
    return _WARNING_RE.search(unicodedata.normalize("NFKC", text or "")) is not None


def is_effortless(text: str) -> bool:
    return _EFFORTLESS_RE.search(unicodedata.normalize("NFKC", text or "")) is not None


# ── 枠（box_2d）からの計算 ─────────────────────────────────────────────


def _lines(text: str) -> int:
    return max(1, min(6, len([x for x in text.split("\n") if x.strip()]) or 1))


def _position(box: tuple[int, int, int, int] | None) -> str:
    if box is None:
        return "unknown"
    center = (box[0] + box[2]) / 2000
    if center < 1 / 3:
        return "top"
    if center > 2 / 3:
        return "bottom"
    return "center"


def _area(box: tuple[int, int, int, int] | None) -> float:
    if box is None:
        return 0.0
    return (box[2] - box[0]) / 1000 * (box[3] - box[1]) / 1000


# ── 1 枚の事実 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CoverFacts:
    """1 枚の表紙の事実（コードが決めた値・AI の読み取りは read に残す）。"""

    rank: int
    group: str
    status: str
    reason: str
    read: CoverRead
    plays: int
    watched: bool  # 動画を見て分析できた（冒頭のテロップと比べられる）
    orientation: str  # portrait / landscape / square / unknown（読み取りに渡した画像の縦横）
    aspect_known: bool
    texts: tuple[str, ...]  # AI の読み取り（改行は残す）
    main_text: str  # いちばん大きい文字のまとまり（枠の面積が最大）
    chars: int
    lines: int
    has_text: bool | None  # None＝文字の欄が読めていない
    line_h_pct: float | None  # 大きい文字 1 行の高さ（表紙の高さに対する %・枠から計算）
    large_text: bool | None
    position: str
    kw_terms: tuple[str, ...]
    numbers: tuple[tuple[str, str], ...]
    question: bool
    warning: bool
    effortless: bool
    appeals: tuple[str, ...] | None
    elements: tuple[str, ...] | None
    face_kind: str
    face_real: bool | None
    expression: str
    gaze: str
    face_area_pct: float | None
    action: str
    closeup: bool | None
    sizzle: tuple[str, ...] | None
    product: str
    brands: tuple[
        tuple[str, str], ...
    ]  # (名前, client／competitor／other／unspecified／unverified)
    clutter: str
    legibility: str
    styles: tuple[str, ...] | None
    subject_note: str
    opening_match: MatchKind  # 動画を見た本だけ（見ていなければ ""）
    caption_match: MatchKind
    own_text: bool | None  # 表紙だけの文字（冒頭 0〜3 秒のどのテロップとも違う）
    caption_head: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def flat_texts(self) -> tuple[str, ...]:
        return tuple(" ".join(t.split()) for t in self.texts)

    @property
    def main_flat(self) -> str:
        return " ".join(self.main_text.split())


def _aliases(entry: str | None) -> list[str]:
    return [a for a in (norm(x) for x in (entry or "").split("|")) if a]


def brand_relations(
    names: Sequence[str],
    meta: VideoMeta,
    analysis_brands: Sequence[str],
    roster: Roster | None,
) -> tuple[tuple[str, str], ...]:
    """表紙の商品名（AI の読み取り）を照合し、照合できたものだけ区分を付ける。

    照合先: 動画の分析で検出したブランド名・キャプション・ハッシュタグ。照合できないものは
    unverified（区分の判定にも指示にも使わない）。区分は名簿の別名が名前に含まれるかで決める。
    """
    roster = roster or Roster()
    sources = [norm(meta.desc), *(norm(h) for h in meta.hashtags)]
    found = [norm(b) for b in analysis_brands if norm(b)]
    out: list[tuple[str, str]] = []
    for name in names:
        n = norm(name)
        if len(n) < MIN_QUOTE_CHARS:
            continue
        verified = any(n in s for s in sources if s) or any(n in b or b in n for b in found)
        if not verified:
            out.append((name, "unverified"))
            continue
        if not roster.specified:
            out.append((name, "unspecified"))
        elif any(a in n for a in _aliases(roster.client_name)):
            out.append((name, "client"))
        elif any(a in n for c in roster.competitors for a in _aliases(c)):
            out.append((name, "competitor"))
        else:
            out.append((name, "other"))
    return tuple(out)


def cover_facts(
    read: CoverRead,
    meta: VideoMeta,
    video: AnalyzedVideo | None,
    query: str,
    roster: Roster | None = None,
) -> CoverFacts:
    """1 枚の表紙の事実（読めていなければ、読み取りの欄は全部「分からない」）。"""
    terms = query_terms(query)
    ok = read.status == "ok"
    watched = video is not None and is_watched(video)
    analysis = video.analysis if video is not None else None
    blocks = list(read.texts or []) if ok else []
    texts = tuple(t.text for t in blocks)
    has_text: bool | None = (bool(blocks) if read.texts is not None else None) if ok else None
    if has_text is False and read.unreadable_text:
        has_text = None  # 文字らしいが読めない＝「文字なし」とは数えない（文字の欄の母数から外す）
    boxed = [t for t in blocks if t.box is not None]
    main = max(boxed, key=lambda t: _area(t.box)) if boxed else (blocks[0] if blocks else None)
    main_text = main.text if main is not None else ""
    lines = _lines(main_text) if main is not None else 0
    aspect_known = read.img_w > 0 and read.img_h > 0
    aspect = read.img_h / read.img_w if aspect_known else DEFAULT_ASPECT
    line_h_pct: float | None = None
    large: bool | None = False if has_text is False else None
    if main is not None and main.box is not None:
        per_line = (main.box[2] - main.box[0]) / 1000 / lines
        line_h_pct = round(per_line * 100, 1)
        large = per_line * aspect >= LARGE_LINE_OF_WIDTH
    joined = "\n".join(texts)
    flat = match_norm(joined)
    kw = tuple(t for t in terms if match_norm(t) and match_norm(t) in flat)
    face = read.face if ok else None
    face_kind = face.kind if face is not None else "unknown"
    face_real = None if face is None or face_kind == "unknown" else face_kind == "real"
    face_area = (
        round(_area(face.box) * 100, 1) if face is not None and face.box is not None else None
    )
    if read.img_w and read.img_h:
        w, h = read.img_w, read.img_h
        orientation = "landscape" if w > h * 1.05 else "portrait" if h > w * 1.05 else "square"
    else:
        orientation = "unknown"
    opening = [t.text for t in (analysis.telops if analysis else []) if t.sec <= OPENING_SEC]
    opening = [t for t in opening if t.strip()]
    if not watched or not ok or has_text is None:
        opening_match: MatchKind = ""
    elif not has_text:
        opening_match = "no_text"
    elif not opening:
        opening_match = "no_telop"
    else:
        opening_match = best_match(joined, [*opening, "".join(opening)], terms)
    own_text: bool | None = None
    if opening_match in ("same", "partial"):
        own_text = False
    elif opening_match in ("kw_only", "different", "no_telop"):
        own_text = True
    head = " ".join((meta.desc or "").split())[:CAPTION_HEAD]
    if not ok or has_text is None:
        caption_match: MatchKind = ""
    elif not has_text:
        caption_match = "no_text"
    elif not head:
        caption_match = "no_caption"
    else:
        caption_match = text_match(joined, head, terms) or "no_caption"
    brands = (
        brand_relations(
            read.brand_text,
            meta,
            [b.brand_name for b in (analysis.brand_detections if analysis else [])],
            roster,
        )
        if ok
        else ()
    )
    return CoverFacts(
        rank=meta.rank,
        group=read.group,
        status=read.status,
        reason=read.reason,
        read=read,
        plays=meta.play_count,
        watched=watched,
        orientation=orientation,
        aspect_known=aspect_known,
        texts=texts,
        main_text=main_text,
        chars=len(flat),
        lines=lines,
        has_text=has_text,
        line_h_pct=line_h_pct,
        large_text=large,
        position=_position(main.box) if main is not None else "unknown",
        kw_terms=kw,
        numbers=number_claims(joined),
        question=is_question(joined),
        warning=is_warning(joined),
        effortless=is_effortless(joined),
        appeals=tuple(read.appeals) if ok and read.appeals is not None else None,
        elements=tuple(read.elements) if ok and read.elements is not None else None,
        face_kind=face_kind,
        face_real=face_real,
        expression=face.expression if face is not None else "unknown",
        gaze=face.gaze if face is not None else "unknown",
        face_area_pct=face_area,
        action=read.action if ok else "unknown",
        closeup=read.closeup if ok else None,
        sizzle=tuple(read.sizzle) if ok and read.sizzle is not None else None,
        product=read.product if ok else "unknown",
        brands=brands,
        clutter=read.clutter if ok else "unknown",
        legibility=read.legibility if ok else "unknown",
        styles=tuple(sorted({s for t in blocks for s in (t.style or [])})) if blocks else None,
        subject_note=read.subject_note if ok else "",
        opening_match=opening_match,
        caption_match=caption_match,
        own_text=own_text,
        caption_head=head,
    )


# ── 特徴の表 ────────────────────────────────────────────────────────────

Check = Callable[[CoverFacts], "bool | None"]


@dataclass(frozen=True)
class CoverSpec:
    id: str
    label: str
    check: Check
    ai: bool


def _has(c: CoverFacts) -> bool | None:
    return c.has_text


def _text_rule(rule: Callable[[CoverFacts], bool]) -> Check:
    def check(c: CoverFacts) -> bool | None:
        if c.has_text is None:
            return None
        return bool(c.has_text) and rule(c)

    return check


def _kw_rule(term: str) -> Check:
    def rule(c: CoverFacts) -> bool:
        return term in c.kw_terms

    return _text_rule(rule)


def _element(name: str) -> Check:
    return lambda c: None if c.elements is None else name in c.elements


def _match_ok(field: str) -> Check:
    def check(c: CoverFacts) -> bool | None:
        kind = getattr(c, field)
        if kind in ("", "no_text", "no_telop", "no_caption"):
            return None
        return kind in ("same", "partial")

    return check


def cover_specs(query: str, roster: Roster | None = None) -> list[CoverSpec]:
    """特徴の定義（並びは描画と指示の優先に使う）。"""
    specs = [
        CoverSpec("cover:text", "表紙に文字がある", _has, False),
        CoverSpec(
            "cover:text_large",
            "一覧のタイルで読める大きさの文字（枠から計算）",
            lambda c: c.large_text,
            False,
        ),
        CoverSpec(
            "cover:text_2lines",
            "表紙の文字が2行以内",
            lambda c: (c.lines <= 2) if c.has_text else None,
            False,
        ),
    ]
    for term in query_terms(query)[:GAP_MAX_TERMS]:
        specs.append(
            CoverSpec(f"cover:kw:{term}", f"表紙の文字に「{term}」", _kw_rule(term), False)
        )
    specs += [
        CoverSpec(
            "cover:number",
            "表紙の文字に単位つきの数字",
            _text_rule(lambda c: bool(c.numbers)),
            False,
        ),
        CoverSpec(
            "cover:question", "表紙の文字が問いかけ", _text_rule(lambda c: c.question), False
        ),
        CoverSpec(
            "cover:effortless",
            "表紙の文字で手間の少なさを言う（いらない・だけ・簡単など）",
            _text_rule(lambda c: c.effortless),
            False,
        ),
        CoverSpec(
            "cover:warning",
            "表紙の文字で失敗や注意を言う（NG・注意など）",
            _text_rule(lambda c: c.warning),
            False,
        ),
        CoverSpec(
            "cover:appeal:benefit",
            "表紙の文字で得られることを言う（AI判定）",
            lambda c: None if c.appeals is None else "benefit" in c.appeals,
            True,
        ),
        CoverSpec(
            "cover:own_text",
            "表紙だけの文字（冒頭0〜3秒のテロップと違う）",
            lambda c: c.own_text,
            False,
        ),
        CoverSpec(
            "cover:opening_match",
            "表紙の文字が冒頭のテロップ（AIの読み取り同士）と同じか一部同じ",
            _match_ok("opening_match"),
            False,
        ),
        CoverSpec(
            "cover:caption_match",
            "表紙の文字がキャプションの冒頭（実データ）と重なる",
            _match_ok("caption_match"),
            False,
        ),
    ]
    for name in ("result", "person", "product", "process", "before_after", "text_main"):
        specs.append(
            CoverSpec(
                f"cover:el:{name}",
                f"表紙に{ELEMENT_LABEL[name]}（AI判定）"
                if name != "text_main"
                else "表紙は画より文字が主（AI判定）",
                _element(name),
                True,
            )
        )
    specs += [
        CoverSpec("cover:face", "実写の人の顔（AI判定）", lambda c: c.face_real, True),
        CoverSpec(
            "cover:gaze_camera",
            "顔がカメラ目線（AI判定）",
            lambda c: (c.gaze == "camera") if c.face_real and c.gaze != "unknown" else None,
            True,
        ),
        CoverSpec("cover:closeup", "主役に寄った画（AI判定）", lambda c: c.closeup, True),
        CoverSpec(
            "cover:sizzle",
            "湯気・照り・断面・質感などの見せ場（AI判定）",
            lambda c: None if c.sizzle is None else bool(c.sizzle),
            True,
        ),
        CoverSpec(
            "cover:product",
            "商品・パッケージが見える（AI判定）",
            lambda c: None if c.product == "unknown" else c.product in ("hero", "visible"),
            True,
        ),
        CoverSpec(
            "cover:simple_bg",
            "背景がすっきり（AI判定）",
            lambda c: None if c.clutter == "unknown" else c.clutter == "simple",
            True,
        ),
        CoverSpec(
            "cover:legible",
            "文字が背景から読みやすい（AI判定）",
            lambda c: (
                None
                if not c.has_text or c.legibility in ("unknown", "none")
                else c.legibility == "good"
            ),
            True,
        ),
        CoverSpec(
            "cover:outline",
            "文字に縁取りか座布団（AI判定）",
            lambda c: (
                None
                if not c.has_text or c.styles is None
                else any(s in ("outline", "box") for s in c.styles)
            ),
            True,
        ),
    ]
    if roster is not None and _aliases(roster.client_name):
        specs.append(
            CoverSpec(
                "cover:brand_client",
                "クライアントの商品名が読める（照合済み）",
                lambda c: None if not c.ok else any(r == "client" for _n, r in c.brands),
                False,
            )
        )
    return specs


def cover_tier(count: int, n: int, n_top: int) -> str:
    """段階（evidence.tier）。表紙を読めた本数が上位の本数より少なければ、必須条件と呼ばない。"""
    name = tier(count, n)
    if name == TIER_REQUIRED and n < n_top:
        return TIER_MAJORITY
    return name


def _hits(spec: CoverSpec, covers: Iterable[CoverFacts]) -> tuple[list[int], list[int]]:
    """(当てはまる順位, 母数の順位)。分からない欄（None）は母数から外す。"""
    hit: list[int] = []
    base: list[int] = []
    for c in covers:
        if not c.ok:
            continue
        value = spec.check(c)
        if value is None:
            continue
        base.append(c.rank)
        if value:
            hit.append(c.rank)
    return hit, base


def cover_feature_table(
    top: Sequence[CoverFacts], query: str, roster: Roster | None = None, *, n_top: int = 0
) -> list[Feature]:
    """上位の群の特徴（1 本以上で観測したものだけ）。母数は欄ごと・段階は cover_tier。"""
    n_top = n_top or len(top)
    out: list[Feature] = []
    for spec in cover_specs(query, roster):
        hit, base = _hits(spec, top)
        if not hit:
            continue
        n = len(base)
        uniq = tuple(sorted(set(hit)))
        out.append(Feature(spec.id, spec.label, uniq, n, cover_tier(len(uniq), n, n_top)))
    return out


# ── 分布 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CoverDist:
    n: int  # 文字のある表紙の本数
    chars: tuple[float, int, int] | None  # (中央値, 最小, 最大)
    lines: tuple[float, int, int] | None
    line_h_pct: tuple[float, float] | None  # 大きい文字 1 行の高さ（表紙の高さの %）の最小・最大
    position: str  # 大きい文字の位置の最頻（同数なら ""）
    number_kinds: tuple[str, ...]


def cover_dist(covers: Sequence[CoverFacts]) -> CoverDist | None:
    texted = [c for c in covers if c.ok and c.has_text]
    if not texted:
        return None
    chars = [c.chars for c in texted]
    lines = [c.lines for c in texted]
    heights = [c.line_h_pct for c in texted if c.line_h_pct is not None]
    pos = Counter(c.position for c in texted if c.position != "unknown").most_common(2)
    mode = pos[0][0] if pos and (len(pos) == 1 or pos[0][1] > pos[1][1]) else ""
    kinds = Counter(k for c in texted for k, _raw in c.numbers)
    return CoverDist(
        n=len(texted),
        chars=(float(statistics.median(chars)), min(chars), max(chars)),
        lines=(float(statistics.median(lines)), min(lines), max(lines)),
        line_h_pct=(min(heights), max(heights)) if heights else None,
        position=mode,
        number_kinds=tuple(k for k, _c in kinds.most_common()),
    )


def dist_text(d: CoverDist | None) -> str:
    if d is None:
        return "表紙の文字なし"
    parts: list[str] = []
    if d.chars:
        med, lo, hi = d.chars
        parts.append(f"字数 中央値{med:g}字（{lo}〜{hi}字）")
    if d.lines:
        med, lo, hi = d.lines
        parts.append(f"行数 中央値{med:g}行（{lo}〜{hi}行）")
    if d.line_h_pct:
        lo_h, hi_h = d.line_h_pct
        parts.append(f"大きい文字1行の高さ 表紙の高さの{lo_h:g}〜{hi_h:g}%")
    if d.position:
        parts.append(f"大きい文字は{POSITION_JP.get(d.position, '不明')}寄り")
    if d.number_kinds:
        parts.append("数字: " + "・".join(NUMBER_KIND_LABEL.get(k, k) for k in d.number_kinds))
    return f"文字のある{d.n}本: " + "・".join(parts)


# ── 上位とほかの差（Fisher の正確検定と Holm）──────────────────────────


def fisher_two_sided(a: int, n: int, b: int, m: int) -> float:
    """2×2 表（上位 a/n・ほか b/m）の Fisher の正確検定（両側・math.comb だけ）。"""
    if n <= 0 or m <= 0:
        return 1.0
    k = a + b
    total = math.comb(n + m, k)
    lo, hi = max(0, k - m), min(k, n)

    def prob(x: int) -> float:
        return math.comb(n, x) * math.comb(m, k - x) / total

    observed = prob(a)
    return min(1.0, sum(p for x in range(lo, hi + 1) if (p := prob(x)) <= observed * _P_TOLERANCE))


def holm(ps: Sequence[float]) -> list[float]:
    """Holm の補正後の p（行を増やしたぶんの偶然を差し引く）。"""
    k = len(ps)
    order = sorted(range(k), key=lambda i: ps[i])
    adjusted = [1.0] * k
    running = 0.0
    for j, i in enumerate(order):
        running = max(running, min(1.0, (k - j) * ps[i]))
        adjusted[i] = running
    return adjusted


@dataclass(frozen=True)
class GapRow:
    id: str
    label: str
    a: int
    n: int
    b: int
    m: int
    p_holm: float | None
    marked: bool

    @property
    def top_rate(self) -> float:
        return self.a / self.n if self.n else 0.0

    @property
    def rest_rate(self) -> float:
        return self.b / self.m if self.m else 0.0


# 比べる行（データを見る前に固定する・検索語は最大 2 語）。
GAP_IDS: tuple[str, ...] = (
    "cover:text",
    "cover:text_large",
    "cover:kw:",
    "cover:number",
    "cover:question",
    "cover:effortless",
    "cover:warning",
    "cover:el:person",
    "cover:el:product",
    "cover:el:result",
    "cover:el:text_main",
    "cover:face",
    "cover:closeup",
    "cover:sizzle",
    "cover:product",
    "cover:simple_bg",
    "cover:legible",
    "cover:caption_match",
)


def _gap_specs(query: str, roster: Roster | None) -> list[CoverSpec]:
    specs = cover_specs(query, roster)
    out: list[CoverSpec] = []
    for want in GAP_IDS:
        for s in specs:
            if s.id == want or (want.endswith(":") and s.id.startswith(want)):
                out.append(s)
    return out


def _read_share(group: Sequence[CoverFacts]) -> float:
    tried = [c for c in group if c.status not in ("skipped", "no_cover")]
    return sum(1 for c in tried if c.ok) / len(tried) if tried else 0.0


def _same_inputs(covers: Iterable[CoverFacts]) -> bool:
    """群の間で取り方と画像の幅が同じか（解像度の違いが偽の差になるのを避ける）。"""
    ways = {c.read.via for c in covers if c.ok}
    widths = [c.read.img_w for c in covers if c.ok and c.read.img_w > 0]
    if len(ways) > 1:
        return False
    return not widths or max(widths) <= min(widths) * 1.1


def cover_gap(
    top: Sequence[CoverFacts],
    rest: Sequence[CoverFacts],
    query: str,
    roster: Roster | None = None,
) -> tuple[list[GapRow], str]:
    """上位とほかの差の行と注記。ほかの群を読んでいなければ ([], 注記)。"""
    if not any(c.ok for c in rest):
        return [], "6〜30位の表紙はまだ読んでいない（比べていない）"
    notes: list[str] = []
    allow = True
    if _read_share(top) < GAP_MIN_READ_SHARE or _read_share(rest) < GAP_MIN_READ_SHARE:
        allow = False
        notes.append("読めた本数が少ない（7割未満）ため差の印は付けない")
    if not _same_inputs([*top, *rest]):
        allow = False
        notes.append("群の間で取り方か画像の幅が違うため差の印は付けない")
    rows: list[tuple[CoverSpec, int, int, int, int]] = []
    for spec in _gap_specs(query, roster):
        hit_t, base_t = _hits(spec, top)
        hit_r, base_r = _hits(spec, rest)
        if not base_t or not base_r:
            continue
        rows.append((spec, len(hit_t), len(base_t), len(hit_r), len(base_r)))
    ps = [fisher_two_sided(a, n, b, m) for _s, a, n, b, m in rows]
    adjusted = holm(ps)
    out: list[GapRow] = []
    for (spec, a, n, b, m), p in zip(rows, adjusted, strict=True):
        marked = (
            allow
            and abs(a / n - b / m) >= GAP_MIN_DIFF
            and p <= GAP_ALPHA
            and n >= GAP_MIN_TOP
            and m >= GAP_MIN_REST
        )
        out.append(GapRow(spec.id, spec.label, a, n, b, m, round(p, 4), marked))
    notes.append(
        f"上位{rank_runs(c.rank for c in top)}とほか{rank_runs(c.rank for c in rest)}の比較"
        "・行を増やした偶然を差し引いても残る差だけに印・因果ではない"
    )
    return out, "。".join(notes)


# ── まとめ（SynthesisContext・描画が使う）──────────────────────────────


@dataclass(frozen=True)
class CoverView:
    """表紙の読み取りのまとめ（上位ボードの各行の cover_read が唯一の出どころ）。"""

    mode: str  # ""（読み取りなし）／top／board（ほかの群も読んだ）
    top: tuple[CoverFacts, ...]  # 表示順の上位 n 本（読めなかった本も含む）
    rest: tuple[CoverFacts, ...]
    features: tuple[Feature, ...]
    dist: CoverDist | None
    gap: tuple[GapRow, ...]
    gap_note: str
    query: str = ""

    @property
    def n_top(self) -> int:
        return len(self.top)

    @property
    def top_ok(self) -> tuple[CoverFacts, ...]:
        return tuple(c for c in self.top if c.ok)

    @property
    def any_ok(self) -> bool:
        return any(c.ok for c in self.top)

    def by_rank(self, rank: int) -> CoverFacts | None:
        return next((c for c in (*self.top, *self.rest) if c.rank == rank), None)

    def feature(self, fid: str) -> Feature | None:
        return next((f for f in self.features if f.id == fid), None)

    def gap_row(self, fid: str) -> GapRow | None:
        return next((g for g in self.gap if g.id == fid), None)


EMPTY_VIEW = CoverView("", (), (), (), None, (), "")


def cover_view(
    board: Sequence[VideoMeta],
    videos: Sequence[AnalyzedVideo],
    query: str,
    roster: Roster | None = None,
) -> CoverView:
    """上位ボードの cover_read から、表紙の事実・特徴・差をまとめる（読み取りが無ければ空）。"""
    reads = [(m, m.cover_read) for m in board if m.cover_read is not None]
    if not reads:
        return EMPTY_VIEW
    by_rank = {v.meta.rank: v for v in videos}
    facts = [
        cover_facts(read, meta, by_rank.get(meta.rank), query, roster)
        for meta, read in sorted(reads, key=lambda x: x[0].rank)
    ]
    top = tuple(c for c in facts if c.group == "top")
    rest = tuple(c for c in facts if c.group == "rest")
    mode = "board" if rest else "top"
    gap, note = cover_gap(top, rest, query, roster) if mode == "board" else ([], "")
    if mode == "top":
        note = "6〜30位の表紙はまだ読んでいない（比べていない）"
    return CoverView(
        mode=mode,
        top=top,
        rest=rest,
        features=tuple(cover_feature_table(top, query, roster, n_top=len(top))),
        dist=cover_dist(top),
        gap=tuple(gap),
        gap_note=note,
        query=query,
    )


# ── 照合（refs の on="cover"）─────────────────────────────────────────


def verify_cover_ref(ref: SynthRef, cover: CoverFacts | None) -> SynthRef | None:
    """表紙の文字か主役の説明（AI の読み取り）に引用が文字として実在すれば合格。

    キャプションへは逃がさない（表紙とキャプションに同じ文字があっても表紙として照合する）。
    """
    if cover is None or not cover.ok or ref.rank != cover.rank:
        return None
    q = norm(ref.quote)
    if len(q) < MIN_QUOTE_CHARS:
        return None
    for text in cover.texts:
        if q in norm(text):
            return SynthRef(rank=ref.rank, quote=ref.quote.strip(), on="cover", source="cover_text")
    if q in norm(cover.subject_note):
        return SynthRef(rank=ref.rank, quote=ref.quote.strip(), on="cover", source="cover_note")
    return None


def cover_quote_ok(quote: str, ranks: Iterable[int], view: CoverView) -> bool:
    """文の中の引用が、その順位の表紙の文字か説明にあるか。"""
    return any(
        verify_cover_ref(SynthRef(rank=r, quote=quote, on="cover"), view.by_rank(r)) is not None
        for r in ranks
    )


# ── コードが作る表紙の指示 ─────────────────────────────────────────────

# 指示の文（{term}・{kinds}・{sizzle}・{shape} はコードが埋める）。並びは優先の順。
_DIRECTIVE_TEXT: dict[str, str] = {
    "cover:kw:": "表紙の文字に「{term}」を入れる",
    "cover:number": "表紙の文字に{kinds}の数字を入れる",
    "cover:text_large": "表紙の文字は一覧のタイルで読める大きさにする（{shape}）",
    "cover:own_text": "表紙には冒頭のテロップと別の、表紙だけの文字を入れる",
    "cover:opening_match": "表紙の文字と冒頭のテロップを同じ言葉にそろえる",
    "cover:el:result": "表紙の主役は完成品・仕上がりにする",
    "cover:el:person": "表紙に人を入れる",
    "cover:el:product": "表紙に商品・パッケージを入れる",
    "cover:el:process": "表紙に工程・使っている途中を入れる",
    "cover:el:before_after": "表紙で使用前後を並べる",
    "cover:el:text_main": "表紙は画より文字を主にする",
    "cover:face": "表紙に実写の人の顔を入れる",
    "cover:gaze_camera": "表紙の顔はカメラ目線にする",
    "cover:sizzle": "表紙で{sizzle}を見せる",
    "cover:closeup": "表紙は主役に寄った画にする",
    "cover:effortless": "表紙の文字で手間の少なさを言う",
    "cover:question": "表紙の文字を問いかけにする",
    "cover:warning": "表紙の文字で失敗や注意を先に言う",
    "cover:appeal:benefit": "表紙の文字で得られることを言う",
    "cover:outline": "表紙の文字に縁取りか座布団を付けて背景から読めるようにする",
    "cover:legible": "表紙の文字は背景から離れた色にして読めるようにする",
    "cover:simple_bg": "表紙の背景は主役のほかに物を置かない",
    "cover:caption_match": "表紙の文字とキャプションの冒頭をそろえる",
    "cover:product": "表紙に商品が見えるようにする",
}
_TEXT_FEATURES = (
    "cover:kw:",
    "cover:number",
    "cover:text_large",
    "cover:own_text",
    "cover:opening_match",
    "cover:effortless",
    "cover:question",
    "cover:warning",
    "cover:appeal:benefit",
    "cover:outline",
    "cover:legible",
    "cover:caption_match",
    "cover:el:text_main",
)
_CONCRETE = ("cover:kw:", "cover:number")


def _prefix(fid: str) -> str:
    return "cover:kw:" if fid.startswith("cover:kw:") else fid


def _shape(view: CoverView, ranks: Sequence[int]) -> str:
    """大きい文字の形（上位の実測・コードの計算）。"""
    covers = [c for r in ranks if (c := view.by_rank(r)) is not None and c.has_text]
    parts: list[str] = []
    heights = [c.line_h_pct for c in covers if c.line_h_pct is not None]
    if heights:
        parts.append(f"上位の1行の高さは表紙の高さの{min(heights):g}〜{max(heights):g}%")
    chars = [c.chars for c in covers]
    if chars:
        parts.append(f"字数 中央値{statistics.median(chars):g}字（最大{max(chars)}字）")
    lines = [c.lines for c in covers]
    if lines:
        parts.append(f"{max(lines)}行以内")
    pos = Counter(c.position for c in covers if c.position != "unknown").most_common(1)
    if pos:
        parts.append(f"{POSITION_JP.get(pos[0][0], '不明')}寄り")
    return "・".join(parts) or "上位の表紙の文字に合わせる"


def _directive_text(fid: str, view: CoverView, feature: Feature) -> str:
    key = _prefix(fid)
    template = _DIRECTIVE_TEXT.get(key, "")
    if not template:
        return ""
    covers = [c for r in feature.ranks if (c := view.by_rank(r)) is not None]
    kinds = Counter(k for c in covers for k, _raw in c.numbers)
    sizzle = Counter(s for c in covers for s in (c.sizzle or ()))
    return template.format(
        term=fid.split(":", 2)[2] if key == "cover:kw:" else "",
        kinds="・".join(NUMBER_KIND_LABEL.get(k, k) for k, _n in kinds.most_common(2)) or "",
        sizzle="・".join(SIZZLE_LABEL.get(s, s) for s, _n in sizzle.most_common(2)) or "質感",
        shape=_shape(view, feature.ranks),
    )


def _example_refs(fid: str, ranks: Iterable[int], view: CoverView) -> list[SynthRef]:
    """根拠: 再生の多い順に 2 本。文字の特徴は表紙の文字、画の特徴は主役の説明（無ければ文字）。"""
    covers = sorted(
        (c for r in ranks if (c := view.by_rank(r)) is not None and c.ok),
        key=lambda c: (-c.plays, c.rank),
    )
    refs: list[SynthRef] = []
    text_first = _prefix(fid) in _TEXT_FEATURES
    for c in covers:
        quote, source = "", ""
        if fid.startswith("cover:kw:"):
            term = fid.split(":", 2)[2]
            quote = next((t for t in c.flat_texts if match_norm(term) in match_norm(t)), "")
            source = "cover_text"
        if not quote and (text_first or not c.subject_note) and c.main_flat:
            quote, source = c.main_flat, "cover_text"
        if not quote and c.subject_note:
            quote, source = c.subject_note, "cover_note"
        if quote:
            refs.append(SynthRef(rank=c.rank, quote=quote, on="cover", source=source))
        if len(refs) >= 2:
            break
    return refs


def _order_key(fid: str) -> int:
    keys = list(_DIRECTIVE_TEXT)
    key = _prefix(fid)
    return keys.index(key) if key in keys else len(keys)


def code_cover_directives(view: CoverView) -> list[Directive]:
    """表紙の事実から、コードが作る指示（最大 3 つ・kind は「表紙」・refs は on="cover"）。

    優先: 差の印がある行（A/B で確かめる）→ 上位で多数派かつほかで少ない行 → 具体的な言い方
    （検索語・数字）→ ほかの多数派（固定の順）。「文字がある」だけの指示は作らない。
    6〜30 位と比べていない指示には「（6〜30位とは比べていない）」を付ける。
    """
    if not view.any_ok:
        return []
    picked: list[tuple[int, Feature, str]] = []
    for f in view.features:
        if f.id == "cover:text" or f.id == "cover:text_2lines" or f.id == "cover:brand_client":
            continue
        text = _directive_text(f.id, view, f)
        if not text:
            continue
        row = view.gap_row(f.id)
        if row is not None and row.marked:
            picked.append((0, f, f"{text}形を A/B で確かめる（6〜30位との差が大きい・参考）"))
            continue
        if not at_least_majority(f.tier):
            continue
        if view.mode == "board" and row is not None:
            if row.rest_rate >= row.top_rate:
                continue  # 6〜30位でも同じくらい多い＝タップの差とは言えない
            picked.append((1, f, f"{text}（6〜30位は{row.b}/{row.m}本）"))
            continue
        rank = 2 if _prefix(f.id) in _CONCRETE else 3
        picked.append((rank, f, f"{text}（{NOT_COMPARED}）"))
    picked.sort(key=lambda x: (x[0], _order_key(x[1].id)))
    out: list[Directive] = []
    for _prio, f, text in picked[:MAX_CODE_COVER_DIRECTIVES]:
        out.append(
            Directive(
                text=text,
                kind=COVER_KIND,
                refs=_example_refs(f.id, f.ranks, view),
                origin="code",
                tier=f.tier,
                ranks=list(f.ranks),
            )
        )
    return out


# ── Slack・結論の 1 行（コードの名前と本数だけ・第三者の文字は入れない）──


_LINE_ORDER = (
    "cover:text",
    "cover:kw:",
    "cover:number",
    "cover:text_large",
    "cover:el:result",
    "cover:el:person",
    "cover:face",
    "cover:own_text",
    "cover:opening_match",
)


def cover_points(view: CoverView, limit: int = 3) -> list[Feature]:
    """多数派以上の特徴（固定の順）。"""
    out: list[Feature] = []
    for want in _LINE_ORDER:
        for f in view.features:
            if f in out or not at_least_majority(f.tier):
                continue
            if f.id == want or (want.endswith(":") and f.id.startswith(want)):
                out.append(f)
    return out[:limit]


def cover_line(view: CoverView) -> str:
    """「サムネ（一覧の表紙）: 表紙に文字がある 5/5（必須条件）・…（6〜30位とは比べていない）」。"""
    if not view.top:
        return ""
    ok = len(view.top_ok)
    if ok == 0:
        return f"サムネ（一覧の表紙）: 上位{view.n_top}本すべて読めず"
    feats = cover_points(view)
    if not feats:
        return f"サムネ（一覧の表紙）: 読めた{ok}/{view.n_top}本に多数派の共通点なし"
    body = "・".join(f"{f.label} {f.count}/{f.n}（{f.tier}）" for f in feats)
    marked = [g for g in view.gap if g.marked]
    tail = (
        f"（6〜30位との差の印 {len(marked)}行・参考）"
        if marked
        else f"（{NOT_COMPARED}）"
        if view.mode != "board"
        else "（6〜30位との差の印なし）"
    )
    return f"サムネ（一覧の表紙）: {body}{tail}"


__all__ = [
    "CAPTION_HEAD",
    "EMPTY_VIEW",
    "MATCH_LABEL",
    "NOT_COMPARED",
    "CoverDist",
    "CoverFacts",
    "CoverSpec",
    "CoverView",
    "GapRow",
    "best_match",
    "brand_relations",
    "code_cover_directives",
    "cover_dist",
    "cover_facts",
    "cover_feature_table",
    "cover_gap",
    "cover_line",
    "cover_points",
    "cover_quote_ok",
    "cover_specs",
    "cover_tier",
    "cover_view",
    "dist_text",
    "fisher_two_sided",
    "holm",
    "is_effortless",
    "is_question",
    "is_warning",
    "match_norm",
    "number_claims",
    "text_match",
    "verify_cover_ref",
]
