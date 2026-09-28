"""横断シンセシス v3 の出力の検査（仕様 v3 §3-3）。LLM を使わない。

「常時」の検査は構造の規則なので、数字の照合（env GROUNDING_MODE_VIDEO_ALGORITHM）が shadow でも
効く。入力に無い数字の文を落とす照合（drop 系）だけは、従来どおり enforce のときだけ落とす
（shadow ではログだけ）。

常時の検査（finalize）:
- R1 本数タグ〔上位 2/5〕・ρ・n= を剥がす。ρ・相関・係数を含む文は落とす
- R3/R5 「勝ち筋」「勝ちパターン」と、断定・保証の語（勝つ・取れる・獲る・必ず・確実・鉄板）を
  言い換える。見出しに断定語があれば、コードの代わりの文にする
- R4 見出し（type_line）: feature_ids の段階を照合し、事例が混じる・数の主張が多数派でない
  ときは、コードが必須条件と多数派から作った文に代える
- R6 同じ助数詞（種・分・秒・枚・つ）に付く数が欄の間で食い違えば、後の方の文を落とす
  （作り直しの 1 回は synthesis.synthesize が LLM に頼む）
- R7 仮説・切り口の該当動画は match_terms でコードが数え直し、1 本以下なら出さない
- R8 指示・やらないこと・絵コンテの refs を evidence.verify_ref で照合し、合格 0 件の項目は捨てる。
  段階の名前は合格した refs の順位の数から tier() で付ける
- R9 再生が最少で中央値の 0.2 倍未満の 1 本だけにある指示は「やらないこと（実績が伴わない事例）」へ
- R11 クライアント未指定なら「御社・貴社・弊社」を「（クライアント商品）」に置き換える（指定ありなら
  クライアント名）。避けたい訴求の語は、指示・絵コンテ・クライアントの次の一手・仮説・盗める点から
  落とす（事実の引用欄＝refs・やらないことは対象外）
- R12 測っていない指標（視聴維持率・離脱・完了率・視聴時間・CTR）を含む文は落とす
- R13 タイアップ表記のある動画を根拠にした文に「（タイアップ投稿 #n）」を足す
- R14 画角の欄（framing）が無いのに、寄り・アップ・表情を含む指示・カットは落とす
- R16 切り口（v2 の angle）は許可値だけ。ほぼ全部の動画を含むクラスタは捨てる
- M7 事実の指示（0 秒のテロップなど）は、コードが特徴の表から作って先頭に置く
- 仕上げに、今の描画が読む v2 の欄（headline・creative_brief など）を v3 の欄とコードの事実から
  作り直す（LLM が v2 の形で返した文は、照合できないので使わない）

enforce のときだけ落とす（数字の照合）: 全欄は本文全体の数字、per_video は動画ごとの個票の数字
（R15）、best_reason は最も見られた 1 本の個票の数字（R10）。

finalize は冪等（検査済みのものをもう一度通しても同じ）。再描画（rebuild.rebuild_cross）が
キャッシュ済みの synthesis に掛け直すため。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

from teamagent.skills._shared.grounding import DropLedger, DropSink, NumberGrounder, tone_down
from teamagent.skills.search_surface_check.video_structure import QTY_RE
from teamagent.skills.video_algorithm.evidence import (
    TIER_CASE,
    TIER_MAJORITY,
    TIER_REQUIRED,
    Ref,
    fold,
    majority_min,
    norm,
    ranks_text,
    tier,
    tier_text,
    verify_ref,
)
from teamagent.skills.video_algorithm.schema import (
    AvoidItem,
    CrossSynthesis,
    Directive,
    HypothesisV3,
    PerVideoNote,
    Storyboard,
    StoryboardCut,
    SummaryLines,
    SynthRef,
    WinHypothesis,
)
from teamagent.skills.video_algorithm.synthesis_input import (
    CORR_MIN_N,
    UNSPECIFIED_CLIENT,
    Grounders,
    SynthesisContext,
)

logger = structlog.get_logger(__name__)

SYNTHESIS_V3 = "v3"
MAX_DIRECTIVES = 6
MAX_CODE_DIRECTIVES = 3
MAX_AVOID = 4
MAX_STORYBOARDS = 2
MAX_HYPOTHESES = 3
MAX_STEAL = 2
MAX_BOARD_ANGLES = 5
GUESS_PREFIX = "推測:"
ALLOWED_ANGLES = frozenset(
    {"price_volume", "aesthetic", "convenience", "authority", "empathy", "novelty", "other"}
)
# R1: 文に書かせない統計の語（ρ・相関係数は _shared/grounding.RHO_TERMS と同じ）。
STAT_WORDS: tuple[str, ...] = ("ρ", "相関", "係数")
# R12: 測っていない指標（効果として書いた文を落とす）。
UNMEASURED_WORDS: tuple[str, ...] = (
    "視聴維持率",
    "維持率",
    "離脱",
    "完了率",
    "視聴完了",
    "視聴時間",
    "平均視聴",
    "CTR",
    "クリック率",
)
# R5: 見出しにあればコードの代わりの文にする語（本文は言い換える）。「必ずしも」「確実性」は除く。
ASSERTIVE_WORDS: tuple[str, ...] = (
    "勝つ",
    "勝て",
    "取れる",
    "獲る",
    "獲れ",
    "必ず",
    "確実",
    "鉄板",
    "支配",
    "独占",
    "圧倒",
)
_ASSERTIVE_RE = re.compile(
    "|".join(
        re.escape(w) + ("(?!しも)" if w == "必ず" else "(?!性)" if w == "確実" else "")
        for w in ASSERTIVE_WORDS
    )
)
# R3・R5 の言い換え（長い語から）。「を取る」は「バランスを取る」があるので面・上位だけにする。
_REPHRASE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"勝ちパターン|勝ち筋"), "共通点"),
    (re.compile(r"勝てる"), "上位を狙える"),
    (re.compile(r"勝つ"), "上位を狙う"),
    (re.compile(r"(面|上位|枠)を(?:取|獲)りに"), r"\1を狙いに"),
    (re.compile(r"(面|上位|枠)を(?:取|獲)れる"), r"\1を狙える"),
    (re.compile(r"(面|上位|枠)を(?:取|獲)る"), r"\1を狙う"),
    (re.compile(r"獲れる"), "狙える"),
    (re.compile(r"獲る"), "狙う"),
    (re.compile(r"必ず(?!しも)"), ""),
    (re.compile(r"確実に|確実な|確実(?!性)"), ""),
    (re.compile(r"鉄板"), "定番"),
)
# R3: 事例（多数派未満）の項目で「型」「傾向」と呼んだ言い方。
_CASE_WORDS = re.compile(r"(?:という|の)(?:型|傾向)")
# R14: 画角の語（寄り・アップ・表情）。アップデート・ピックアップ等は除く。
FRAMING_RE = re.compile(
    r"(?<!タイ)(?<!ピック)(?<!レベル)(?<!ステップ)(?<!セット)(?<!バック)(?<!ライン)(?<!ワン)"
    r"アップ(?!デート|ロード|グレード)"
    r"|クローズアップ|寄りの|寄りで|寄り画|表情|俯瞰|引きの画|引きで"
)
# R11
_CLIENT_WORD_RE = re.compile(r"(?:御社|貴社|弊社)(?:様)?")
_CLIENT_PRODUCT_RE = re.compile(r"(?:御社|貴社|弊社)(?:様)?の?(?:商品|製品)")
# R1: 剥がすタグ（LLM が付けた本数・相関の根拠）。
_TAG_RE = re.compile(r"〔[^〔〕]*〕")
_BRACKET_TAG_RE = re.compile(
    r"[\[［][^\[\]［］]*(?:\d\s*/\s*\d|[nｎ]\s*[=＝]|ρ|上位)[^\[\]［］]*[\]］]"
)
_PAREN_COUNT_RE = re.compile(r"[（(]\s*(?:上位|全)?\s*\d+\s*/\s*\d+\s*(?:本中|本)?\s*[）)]")
_N_EQ_RE = re.compile(r"[、,，]?\s*[nｎ]\s*[=＝]\s*\d+")
_RHO_EXPR_RE = re.compile(r"[、,，]?\s*ρ\s*[=＝]?\s*[−\-+ー－]?\s*\d*\.?\d+")
_FRACTION_RE = re.compile(r"(?<!さじ)(?<!カップ)(?:上位|全)?\s*(\d+)\s*/\s*(\d+)\s*(?:本中|本)?")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]*(?:[。！？!?\n]+|$)")
_EMPTY_BRACKETS = re.compile(r"[（(]\s*[）)]|〔\s*〕|[\[［]\s*[\]］]")
# 避けたい訴求の語を、文字の種類の切れ目で分けた片（「ルー卒業」→「ルー」「卒業」）。
_SEGMENT_RE = re.compile(
    r"[ァ-ヶー]+|[一-龥々〆]+|[ぁ-ゖ]+|[a-z0-9]+|[^\sァ-ヶー一-龥々〆ぁ-ゖa-z0-9]+"
)
# 片と片のあいだに挟まってよい字数（「カレールーはもう卒業」の「はもう」）。文の区切りはまたがない。
_AVOID_GAP = 4

# R6: 数の主張（助数詞のまとまり）。種・種類・選・品は「何種」、つは「いくつ」。
_COUNTER = r"種類|種|選|品|つ|分|秒|枚"
_NUM = r"\d+(?:\.\d+)?"
_GROUP = {"種類": "種", "種": "種", "選": "種", "品": "種", "つ": "つ", "分": "分", "秒": "秒"}
_GROUP["枚"] = "枚"
# 対象（「冒頭3秒」「スパイス4つ」）を見ないと比べられない助数詞。種・分は同じ対象とみなす。
# 秒は「0秒台」「3秒以内」のように時点の違う事実が並ぶので、尺（「尺60秒」）だけを比べる。
_OBJECT_GROUPS = frozenset({"つ", "秒", "枚"})
_RANGE_CLAIM_RE = re.compile(
    rf"({_NUM})\s*(?:{_COUNTER})?\s*(?:から|〜|～|~|-|−|－)\s*({_NUM})\s*({_COUNTER})"
)
_ALT_CLAIM_RE = re.compile(
    rf"({_NUM})\s*({_COUNTER})\s*(?:版)?\s*(?:と|か|や|vs\.?|VS|対)\s*({_NUM})\s*({_COUNTER})"
)
_SINGLE_CLAIM_RE = re.compile(rf"(?<![\d.])({_NUM})\s*({_COUNTER})")
_OBJ_CHARS = r"[゠-ヿー㐀-鿿々A-Za-z]"
_OBJ_AFTER_RE = re.compile(rf"\s*の\s*({_OBJ_CHARS}{{1,8}})")
_OBJ_BEFORE_RE = re.compile(rf"({_OBJ_CHARS}{{1,6}})$")
_OBJ_TRAIL_RE = re.compile(r"[をはがのでにもへと、\s]+$")
_GROUP_COUNTERS = {"種": "種類|種|選|品|つ", "つ": "つ", "分": "分", "秒": "秒", "枚": "枚"}


# ── 記録 ────────────────────────────────────────────────────────────────


@dataclass
class CheckLog:
    """常時の検査で直した・捨てた記録（ログ synthesis_checked。本文は出さない）。"""

    request_id: str = ""
    sink: DropSink | None = None
    count: int = 0
    fields: list[str] = field(default_factory=list)

    def __call__(self, field_name: str, reason: str) -> None:
        self.count += 1
        self.fields.append(field_name)
        logger.info(
            "synthesis_checked",
            skill="video_algorithm",
            field=field_name,
            reason=reason,
            request_id=self.request_id,
        )
        if self.sink is not None:
            self.sink(field_name, reason)


# ── 文の掃除（R1・R3・R5・R11・R12）────────────────────────────────────


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_RE.findall(text) if s.strip()]


def _tidy(text: str) -> str:
    t = _EMPTY_BRACKETS.sub("", text)
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\s*([。、，,！？!?])", r"\1", t)
    t = re.sub(r"、+。", "。", t)
    t = re.sub(r"。{2,}", "。", t)
    return t.strip(" 　、,")


def strip_count_tags(text: str, n: int, board_size: int = 0) -> str:
    """本数タグ（〔上位 2/5〕・［…］・（2/5本））・n=・ρ=…・本数の分数（分母が n か上位の本数）。"""
    t = _TAG_RE.sub("", text)
    t = _BRACKET_TAG_RE.sub("", t)
    t = _PAREN_COUNT_RE.sub("", t)
    t = _N_EQ_RE.sub("", t)
    t = _RHO_EXPR_RE.sub("", t)
    dens = {d for d in (n, board_size) if d > 0}

    def frac(m: re.Match[str]) -> str:
        num, den = int(m.group(1)), int(m.group(2))
        return "" if den in dens and num <= den else m.group(0)

    return _FRACTION_RE.sub(frac, t)


def replace_client_words(text: str, label: str) -> str:
    """「御社・貴社・弊社」をクライアント名（未指定なら「（クライアント商品）」）へ。"""
    if label == UNSPECIFIED_CLIENT:
        text = _CLIENT_PRODUCT_RE.sub(label, text)
    return _CLIENT_WORD_RE.sub(label, text)


def rephrase(text: str) -> str:
    for pat, plain in _REPHRASE:
        text = pat.sub(plain, text)
    return tone_down(text)


def deny_hit(text: str) -> str | None:
    """落とす語（統計の語・測っていない指標）の最初の 1 つ。"""
    for word in (*STAT_WORDS, *UNMEASURED_WORDS):
        if word in text:
            return word
    return None


def clean_text(text: str, ctx: SynthesisContext, field_name: str, log: CheckLog) -> str:
    """LLM の文を掃除する。

    タグ・本数・ρ を剥がし、御社・断定語を直し、統計語・未計測指標の文を落とす。
    """
    if not text:
        return ""
    t = strip_count_tags(text, ctx.n, len(ctx.board))
    if t != text:
        log(field_name, "tag_stripped")
    replaced = replace_client_words(t, ctx.client_label)
    if replaced != t:
        log(field_name, "client_word")
    toned = rephrase(replaced)
    if toned != replaced:
        log(field_name, "assertive")
    kept: list[str] = []
    for sentence in _sentences(toned):
        hit = deny_hit(sentence)
        if hit is not None:
            log(field_name, f"deny:{hit}")
            continue
        kept.append(sentence)
    return _tidy("".join(kept))


def has_assertive(text: str) -> bool:
    return _ASSERTIVE_RE.search(text) is not None


def has_framing_words(text: str) -> bool:
    return FRAMING_RE.search(text) is not None


def _avoid_pattern(term: str) -> re.Pattern[str] | None:
    """語を文字の種類の切れ目で分け、片が順に・短い間隔で並ぶ形（片が 1 つなら None）。"""
    segments = _SEGMENT_RE.findall(norm(term))
    if len(segments) <= 1:
        return None
    gap = rf"[^。！？!?]{{0,{_AVOID_GAP}}}?"
    return re.compile(gap.join(re.escape(seg) for seg in segments))


def has_avoid(text: str, terms: Iterable[str]) -> bool:
    """避けたい訴求の語があるか（NFKC・大小・空白を無視）。

    「ルー卒業」は「カレールーは卒業」「カレールーはもう卒業」も拾う（片のあいだに 4 字まで）。
    """
    body = norm(text)
    for term in terms:
        t = norm(term)
        if not t:
            continue
        if t in body:
            return True
        pat = _avoid_pattern(term)
        if pat is not None and pat.search(body):
            return True
    return False


def _case_words(text: str) -> str:
    return _CASE_WORDS.sub("の例", text)


# ── 数の主張（R4・R6・R7）──────────────────────────────────────────────


@dataclass(frozen=True)
class Claim:
    group: str
    obj: str
    values: frozenset[str]
    start: int
    end: int


def _canon(num: str) -> str:
    return num.rstrip("0").rstrip(".") if "." in num else num.lstrip("0") or "0"


def _object(text: str, start: int, end: int, group: str) -> str:
    if group not in _OBJECT_GROUPS:
        return ""
    if group == "つ":
        m = _OBJ_AFTER_RE.match(text, end)
        if m:
            return m.group(1)
    before = _OBJ_TRAIL_RE.sub("", text[:start])
    m = _OBJ_BEFORE_RE.search(before)
    return m.group(1) if m else ""


def extract_claims(text: str) -> list[Claim]:
    """数の主張（範囲「4〜5種」は 1 つ・比較「4種と5種」は除く・単独「4種」）。"""
    t = unicodedata.normalize("NFKC", text or "")
    claims: list[Claim] = []
    masked = list(t)

    def mask(m: re.Match[str]) -> None:
        for i in range(m.start(), m.end()):
            masked[i] = " "

    for m in _RANGE_CLAIM_RE.finditer(t):
        group = _GROUP[m.group(3)]
        values = frozenset({_canon(m.group(1)), _canon(m.group(2))})
        claims.append(
            Claim(group, _object(t, m.start(), m.end(), group), values, m.start(), m.end())
        )
        mask(m)
    rest = "".join(masked)
    for m in _ALT_CLAIM_RE.finditer(rest):
        if _GROUP[m.group(2)] == _GROUP[m.group(4)]:
            mask(m)
    rest = "".join(masked)
    for m in _SINGLE_CLAIM_RE.finditer(rest):
        group = _GROUP[m.group(2)]
        claims.append(
            Claim(
                group,
                _object(t, m.start(), m.end(), group),
                frozenset({_canon(m.group(1))}),
                m.start(),
                m.end(),
            )
        )
    return sorted(claims, key=lambda c: c.start)


def _claim_key(c: Claim) -> tuple[str, str] | None:
    if c.group in _OBJECT_GROUPS and not c.obj:
        return None  # 対象が分からない秒・枚・つは比べない
    if c.group == "秒" and not c.obj.endswith("尺"):
        return None
    return (c.group, c.obj if c.group in _OBJECT_GROUPS else "")


@dataclass(frozen=True)
class Conflict:
    field: str
    group: str
    obj: str
    first: frozenset[str]
    later: frozenset[str]


def find_conflicts(fields: Iterable[tuple[str, str]]) -> list[Conflict]:
    """同じ助数詞（と対象）の数の集合が、先に出た欄と違う欄（後の方）。"""
    ref: dict[tuple[str, str], frozenset[str]] = {}
    out: list[Conflict] = []
    for name, text in fields:
        by_key: dict[tuple[str, str], set[str]] = {}
        for c in extract_claims(text):
            key = _claim_key(c)
            if key is not None:
                by_key.setdefault(key, set()).update(c.values)
        for key, values in by_key.items():
            vals = frozenset(values)
            if key not in ref:
                ref[key] = vals
            elif vals != ref[key]:
                out.append(Conflict(name, key[0], key[1], ref[key], vals))
    return out


def conflict_note(conflicts: Iterable[Conflict]) -> str:
    """作り直しを頼む一文（どの欄の、どの助数詞の数が食い違ったか）。"""
    parts = []
    for c in conflicts:
        what = f"{c.obj}の" if c.obj else ""
        parts.append(
            f"・{what}「{c.group}」の数が欄によって"
            f"{'・'.join(sorted(c.first))}と{'・'.join(sorted(c.later))}で食い違っている"
        )
    return (
        "# 前回の出力の食い違い（直して JSON を出し直す）\n"
        + "\n".join(parts)
        + "\n同じ対象の数は欄の間で揃え、該当動画のテロップかキャプションにある数だけを書く。"
    )


def _drop_claim_sentences(text: str, keys: set[tuple[str, str]]) -> str:
    kept = []
    for sentence in _sentences(text):
        if any(_claim_key(c) in keys for c in extract_claims(sentence)):
            continue
        kept.append(sentence)
    return _tidy("".join(kept))


def claim_matcher(claim: Claim) -> re.Pattern[str]:
    values = "|".join(re.escape(v) for v in sorted(claim.values, key=len, reverse=True))
    return re.compile(rf"(?<![\d.])(?:{values})\s*(?:{_GROUP_COUNTERS[claim.group]})")


def _video_texts(ctx: SynthesisContext, rank: int) -> list[str]:
    a = ctx.analysis(rank)
    f = ctx.fact(rank)
    texts = [t.text for t in (a.telops if a else [])]
    if f is not None:
        texts.append(f.desc)
    return [unicodedata.normalize("NFKC", t) for t in texts if t]


def _whole_claim(word: str) -> Claim | None:
    """語がまるごと数の主張（「30分」「4種類」「4〜5種」）なら、その主張。"""
    text = unicodedata.normalize("NFKC", word).strip()
    claims = extract_claims(text)
    if len(claims) == 1 and claims[0].start == 0 and claims[0].end == len(text):
        return claims[0]
    return None


def term_ranks(terms: Iterable[str], ctx: SynthesisContext) -> list[int]:
    """語がテロップかキャプションにある動画（数の語は同じ助数詞のまとまりで当てる）。"""
    words = [t for t in terms if norm(t)]
    out: list[int] = []
    for rank in ctx.ranks:
        texts = _video_texts(ctx, rank)
        for word in words:
            claim = _whole_claim(word)
            if claim is not None:
                pat = claim_matcher(claim)
                hit = any(pat.search(t) for t in texts)
            else:
                hit = any(norm(word) in norm(t) for t in texts)
            if hit:
                out.append(rank)
                break
    return out


# ── 照合（R8・R9・R13）─────────────────────────────────────────────────


def verify_refs(
    refs: Iterable[SynthRef], ctx: SynthesisContext, field_name: str, log: CheckLog
) -> list[SynthRef]:
    """照合に合格した refs だけ（同じ場所の重複は 1 つ）。source / found_sec はコードが書く。"""
    out: list[SynthRef] = []
    seen: set[tuple[int, float | None, str]] = set()
    for r in refs:
        facts = ctx.fact(r.rank)
        if facts is None:
            log(field_name, f"ref_rank:{r.rank}")
            continue
        vr = verify_ref(Ref(r.rank, r.sec, r.quote), facts, ctx.analysis(r.rank))
        if vr is None:
            log(field_name, "ref_unverified")
            continue
        key = (vr.rank, vr.found_sec, norm(vr.quote))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            SynthRef(
                rank=vr.rank,
                sec=vr.sec,
                quote=vr.quote.strip(),
                source=vr.source,
                found_sec=vr.found_sec,
            )
        )
    return out


def _ranks_of(refs: Iterable[SynthRef]) -> list[int]:
    return sorted({r.rank for r in refs})


def pr_ranks(ranks: Iterable[int], ctx: SynthesisContext) -> list[int]:
    return [r for r in ranks if (f := ctx.fact(r)) is not None and f.pr]


def with_pr_note(text: str, ranks: Iterable[int], ctx: SynthesisContext) -> str:
    """R13: タイアップ表記のある動画を根拠にした文に「（タイアップ投稿 #n）」を足す。"""
    ranks = list(ranks)
    pr = pr_ranks(ranks, ctx)
    if not pr or "タイアップ" in text:
        return text
    tail = "" if len(pr) == len(set(ranks)) else "を含む"
    return f"{text}（タイアップ投稿 {ranks_text(pr)}{tail}）"


def _low_reason(rank: int, ctx: SynthesisContext) -> str:
    return f"実績が伴わない事例（#{rank}・再生が{ctx.n}本の中央値の2割未満）"


def _sec_text(sec: float | None) -> str:
    return "キャプション" if sec is None else f"{sec:g}秒"


def evidence_text(ref: SynthRef) -> str:
    """「#4 25秒『大さじ8杯』」の形（コードが照合した根拠）。"""
    sec = ref.found_sec if ref.found_sec is not None else ref.sec
    return f"#{ref.rank} {_sec_text(sec if ref.source != 'caption' else None)}「{ref.quote}」"


# ── 事実の指示（M7）────────────────────────────────────────────────────


def _by_plays(ranks: Iterable[int], ctx: SynthesisContext) -> list[int]:
    order = {f.rank: (-f.plays, f.rank) for f in ctx.facts}
    return sorted(ranks, key=lambda r: order.get(r, (0, r)))


def _example_ref(fid: str, rank: int, ctx: SynthesisContext) -> SynthRef | None:
    f = ctx.fact(rank)
    if f is None:
        return None
    if fid == "first_telop_0s" and f.opening_telops:
        sec, text = f.opening_telops[0]
        return SynthRef(rank=rank, sec=sec, quote=text, source="telop", found_sec=sec)
    if fid.startswith("kw_telop_3s:"):
        term = fid.split(":", 1)[1]
        for sec, text in f.opening_telops:
            if norm(term) in norm(text):
                return SynthRef(rank=rank, sec=sec, quote=text, source="telop", found_sec=sec)
        return None
    if fid == "qty_anywhere" and f.qty_telops:
        sec, text = f.qty_telops[0]
        return SynthRef(rank=rank, sec=sec, quote=text, source="telop", found_sec=sec)
    return None


def _caption_qty_ref(rank: int, ctx: SynthesisContext) -> SynthRef | None:
    """分量がキャプションにしか無いときの例（キャプションの分量の前後）。"""
    f = ctx.fact(rank)
    if f is None:
        return None
    body = unicodedata.normalize("NFKC", f.desc)
    m = QTY_RE.search(fold(body))
    if m is None or len(fold(body)) != len(body):
        return None
    lo = max(0, m.start() - 8)
    quote = " ".join(body[lo : m.end()].split())
    return SynthRef(rank=rank, sec=None, quote=quote, source="caption", found_sec=None)


def code_directives(ctx: SynthesisContext) -> list[Directive]:
    """特徴の表の多数派以上から、コードが作る事実の指示（最大 3 つ・例は再生の多い順に 2 本）。"""
    out: list[Directive] = []
    for feature, text, kind in ctx.code_directive_features()[:MAX_CODE_DIRECTIVES]:
        ordered = _by_plays(feature.ranks, ctx)
        refs = [r for r in (_example_ref(feature.id, rank, ctx) for rank in ordered) if r][:2]
        if not refs and feature.id == "qty_anywhere":
            refs = [r for r in (_caption_qty_ref(rank, ctx) for rank in ordered) if r][:1]
        out.append(
            Directive(
                text=text,
                kind=kind,
                refs=refs,
                origin="code",
                tier=feature.tier,
                ranks=list(feature.ranks),
            )
        )
    return out


# ── 見出し（R4・R5）────────────────────────────────────────────────────


_HEADLINE_PRIORITY: tuple[str, ...] = (
    "first_telop_0s",
    "kw_telop_3s:",
    "qty_anywhere",
    "kw_telop:",
    "narration",
    "qty_telop",
    "kw_caption:",
)


def headline_features(ctx: SynthesisContext) -> list[str]:
    """代わりの見出しに使う特徴（必須条件→多数派・固定の優先順で 2 つまで）。"""
    picked: list[str] = []
    for want in (TIER_REQUIRED, TIER_MAJORITY):
        for prefix in _HEADLINE_PRIORITY:
            for f in ctx.features:
                if f.tier != want or f.id in picked:
                    continue
                if f.id == prefix or (prefix.endswith(":") and f.id.startswith(prefix)):
                    picked.append(f.id)
                    break
            if len(picked) >= 2:
                return picked
    return picked


def alt_type_line(ctx: SynthesisContext) -> tuple[str, list[str]]:
    """コードの代わりの見出し（必須条件と多数派の特徴だけ）。特徴が無ければ空。"""
    ids = headline_features(ctx)
    labels = [f.label for i in ids if (f := ctx.feature(i)) is not None]
    if not labels:
        return "", []
    return f"上位{ctx.n}本の共通点（仮説）: {'、'.join(labels)}", ids


def headline_problem(text: str, feature_ids: Iterable[str], ctx: SynthesisContext) -> str | None:
    """見出しを使えない理由（使えるなら None）。"""
    if not text:
        return "empty"
    if has_assertive(text):
        return "assertive"
    if has_avoid(text, ctx.avoid_terms):
        return "avoid_term"
    ids = list(feature_ids)
    if not ids:
        return "feature_ids_missing"
    for fid in ids:
        f = ctx.feature(fid)
        if f is None:
            return "feature_unknown"
        if f.tier == TIER_CASE:
            return "feature_case"
    need = majority_min(ctx.n)
    # 特徴の名前にある数（「0秒台」「3秒以内」）は特徴の表で数えてあるので、そのまま通す。
    label_nums = {
        v
        for fid in ids
        if (f := ctx.feature(fid)) is not None
        for c in extract_claims(f.label)
        for v in c.values
    }
    for claim in extract_claims(text):
        if claim.values <= label_nums:
            continue
        pat = claim_matcher(claim)
        hits = [r for r in ctx.ranks if any(pat.search(t) for t in _video_texts(ctx, r))]
        if len(hits) < need:
            return f"claim_minority:{claim.group}"
    return None


# ── 本体 ────────────────────────────────────────────────────────────────


def _ground_text(
    text: str,
    grounder: NumberGrounder | None,
    field_name: str,
    ledger: DropLedger | None,
    *,
    by_sentence: bool,
) -> str:
    """数字の照合（enforce は落とす・shadow はログだけ）。"""
    if not text or grounder is None or ledger is None:
        return text
    if by_sentence:
        kept, reasons = grounder.keep_sentences(text)
        for reason in reasons:
            ledger(field_name, reason)
        return kept if (reasons and ledger.enforce) else text
    why = grounder.reason(text)
    if why is None:
        return text
    ledger(field_name, why)
    return "" if ledger.enforce else text


def _clean_v3(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    """v3 の欄の文を掃除する（照合・構造の検査の前）。"""

    def c(text: str, name: str) -> str:
        return clean_text(text, ctx, name, log)

    if s.summary_lines is not None:
        sl = s.summary_lines
        if has_assertive(sl.type_line):  # R5: 見出しは言い換えず、コードの代わりの文にする
            log("summary_lines.type_line", "headline:assertive")
            sl.type_line = ""
        sl.type_line = c(sl.type_line, "summary_lines.type_line")
        sl.best_reason = c(sl.best_reason, "summary_lines.best_reason")
        sl.client_move = c(sl.client_move, "summary_lines.client_move")
    for p in s.per_video:
        p.win_line = c(p.win_line, "per_video")
        p.why_fact = c(p.why_fact, "per_video")
        p.why_guess = c(p.why_guess, "per_video")
        p.not_to_copy = c(p.not_to_copy, "per_video")
        p.steal = [t for t in (c(x, "per_video.steal") for x in p.steal) if t]
    for d in s.directives:
        if d.origin == "llm":
            d.text = c(d.text, "directives")
    for a in s.avoid:
        a.text = c(a.text, "avoid")
    for sb in s.storyboards:
        sb.name = c(sb.name, "storyboards")
        for cut in sb.cuts:
            cut.show = c(cut.show, "storyboards.cuts")
            cut.telop = c(cut.telop, "storyboards.cuts")
            cut.aim = c(cut.aim, "storyboards.cuts")
    for b in s.board_angles:
        b.label = c(b.label, "board_angles")
    for h in s.hypotheses:
        h.text = c(h.text, "hypotheses")
        h.test = c(h.test, "hypotheses.test")
    if s.posting is not None:
        s.posting.caption_plan = c(s.posting.caption_plan, "posting")
        s.posting.ab_plan = c(s.posting.ab_plan, "posting")
    for cc in s.common_concepts:
        cc.concept = c(cc.concept, "common_concepts")
        cc.gist = c(cc.gist, "common_concepts")
    for ac in s.angle_clusters:
        ac.label_jp = c(ac.label_jp, "angle_clusters")
        ac.why_works = c(ac.why_works, "angle_clusters")
    s.caveat = c(s.caveat, "caveat")


def _ground_v3(s: CrossSynthesis, grounders: Grounders | None, ledger: DropLedger | None) -> None:
    """数字の照合（R15: per_video は動画ごと・R10: best_reason は最も見られた 1 本の個票）。"""
    if grounders is None or ledger is None:
        return
    g = grounders.all
    if s.summary_lines is not None:
        sl = s.summary_lines
        sl.type_line = _ground_text(
            sl.type_line, g, "summary_lines.type_line", ledger, by_sentence=False
        )
        sl.client_move = _ground_text(
            sl.client_move, g, "summary_lines.client_move", ledger, by_sentence=True
        )
    for p in s.per_video:
        pg = grounders.per_video.get(p.rank)
        if pg is None:
            continue
        p.win_line = _ground_text(p.win_line, pg, "per_video", ledger, by_sentence=False)
        p.why_fact = _ground_text(p.why_fact, pg, "per_video", ledger, by_sentence=True)
        p.why_guess = _ground_text(p.why_guess, pg, "per_video", ledger, by_sentence=True)
        p.not_to_copy = _ground_text(p.not_to_copy, pg, "per_video", ledger, by_sentence=False)
        p.steal = [
            t
            for t in (
                _ground_text(x, pg, "per_video.steal", ledger, by_sentence=False) for x in p.steal
            )
            if t
        ]
    for d in s.directives:
        if d.origin == "llm":
            d.text = _ground_text(d.text, g, "directives", ledger, by_sentence=False)
    for a in s.avoid:
        a.text = _ground_text(a.text, g, "avoid", ledger, by_sentence=False)
    for sb in s.storyboards:
        for cut in sb.cuts:
            for attr in ("show", "telop", "aim"):
                value = getattr(cut, attr)
                grounded = _ground_text(value, g, "storyboards.cuts", ledger, by_sentence=False)
                if value and not grounded:
                    cut.show = cut.telop = cut.aim = ""  # カットごと落とす（refs 照合で消える）
                    break
    for b in s.board_angles:
        b.label = _ground_text(b.label, g, "board_angles", ledger, by_sentence=False)
    for h in s.hypotheses:
        h.text = _ground_text(h.text, g, "hypotheses", ledger, by_sentence=False)
        h.test = _ground_text(h.test, g, "hypotheses.test", ledger, by_sentence=False)
    if s.posting is not None:
        s.posting.caption_plan = _ground_text(
            s.posting.caption_plan, g, "posting", ledger, by_sentence=True
        )
        s.posting.ab_plan = _ground_text(s.posting.ab_plan, g, "posting", ledger, by_sentence=True)


def _best_reason_ground(
    s: CrossSynthesis,
    ctx: SynthesisContext,
    grounders: Grounders | None,
    ledger: DropLedger | None,
) -> None:
    if s.summary_lines is None or grounders is None:
        return
    bg = grounders.per_video.get(ctx.best_rank)
    s.summary_lines.best_reason = _ground_text(
        s.summary_lines.best_reason,
        bg,
        "summary_lines.best_reason",
        ledger,
        by_sentence=True,
    )


def conflict_fields(s: CrossSynthesis) -> list[tuple[str, str]]:
    """R6 で比べる欄（見出し→最も見られた 1 本の理由→次の一手→仮説の本文と A/B の順）。"""
    out: list[tuple[str, str]] = []
    if s.summary_lines is not None:
        out += [
            ("summary_lines.type_line", s.summary_lines.type_line),
            ("summary_lines.best_reason", s.summary_lines.best_reason),
            ("summary_lines.client_move", s.summary_lines.client_move),
        ]
    for i, h in enumerate(s.hypotheses):
        out += [(f"hypotheses[{i}].text", h.text), (f"hypotheses[{i}].test", h.test)]
    return out


def _set_field(s: CrossSynthesis, name: str, value: str) -> None:
    if name.startswith("summary_lines.") and s.summary_lines is not None:
        setattr(s.summary_lines, name.split(".", 1)[1], value)
        return
    m = re.fullmatch(r"hypotheses\[(\d+)\]\.(text|test)", name)
    if m:
        setattr(s.hypotheses[int(m.group(1))], m.group(2), value)


def drop_conflicts(s: CrossSynthesis, log: CheckLog) -> None:
    """R6: 食い違った後の方の欄から、その数の文を落とす。"""
    conflicts = find_conflicts(conflict_fields(s))
    if not conflicts:
        return
    by_field: dict[str, set[tuple[str, str]]] = {}
    for c in conflicts:
        by_field.setdefault(c.field, set()).add((c.group, c.obj))
        log(c.field, f"conflict:{c.group}")
    texts = dict(conflict_fields(s))
    for name, keys in by_field.items():
        _set_field(s, name, _drop_claim_sentences(texts[name], keys))


def _summary(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    sl = s.summary_lines or SummaryLines()
    known = [i for i in dict.fromkeys(sl.feature_ids) if ctx.feature(i) is not None]
    alt, alt_ids = alt_type_line(ctx)
    why = headline_problem(sl.type_line, sl.feature_ids, ctx)
    if why is None:
        sl.feature_ids = known
        sl.type_line_by_code = sl.type_line == alt
    else:
        log("summary_lines.type_line", f"headline:{why}")
        sl.type_line, sl.feature_ids = alt, alt_ids
        sl.type_line_by_code = True
    if sl.client_move and (
        has_avoid(sl.client_move, ctx.avoid_terms)
        or (not ctx.framing and has_framing_words(sl.client_move))
    ):
        log("summary_lines.client_move", "avoid_or_framing")
        sl.client_move = ""
    sl.best_rank = ctx.best_rank
    s.summary_lines = sl


def _per_video(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    out: list[PerVideoNote] = []
    seen: set[int] = set()
    for p in s.per_video:
        if p.rank not in ctx.ranks or p.rank in seen:
            log("per_video", f"rank:{p.rank}")
            continue
        seen.add(p.rank)
        guess = p.why_guess.strip()
        for prefix in ("推測:", "推測：", "推測 :", "推測"):
            if guess.startswith(prefix):
                guess = guess[len(prefix) :].strip()
                break
        p.why_guess = f"{GUESS_PREFIX}{guess}" if guess else ""
        steal: list[str] = []
        for t in p.steal:
            if has_avoid(t, ctx.avoid_terms) or (not ctx.framing and has_framing_words(t)):
                log("per_video.steal", "avoid_or_framing")
                continue
            steal.append(t)
        p.steal = steal[:MAX_STEAL]
        if any((p.win_line, p.why_fact, p.why_guess, p.steal, p.not_to_copy)):
            out.append(p)
    s.per_video = sorted(out, key=lambda p: p.rank)


def _directives(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> list[AvoidItem]:
    """指示を照合する。実績が伴わない 1 本だけの指示は、やらないことへ移す（戻り値）。"""
    kept: list[Directive] = []
    moved: list[AvoidItem] = []
    for d in s.directives:
        if d.origin == "code":
            continue  # 作り直す（冪等のため）
        text = d.text
        if not text:
            log("directives", "empty")
            continue
        if has_avoid(text, ctx.avoid_terms):
            log("directives", "avoid_term")
            continue
        if not ctx.framing and has_framing_words(text):
            log("directives", "framing")
            continue
        refs = verify_refs(d.refs, ctx, "directives", log)
        if not refs:
            log("directives", "no_verified_ref")
            continue
        ranks = _ranks_of(refs)
        if ctx.low_rank is not None and ranks == [ctx.low_rank]:
            log("directives", "moved_to_avoid")
            moved.append(
                AvoidItem(
                    text=text,
                    refs=refs,
                    origin="moved",
                    reason=_low_reason(ctx.low_rank, ctx),
                    ranks=ranks,
                )
            )
            continue
        t = tier(len(ranks), ctx.n)
        if t == TIER_CASE:
            text = _case_words(text)
        kept.append(
            Directive(
                text=with_pr_note(text, ranks, ctx),
                kind=d.kind,
                refs=refs,
                origin="llm",
                tier=t,
                ranks=ranks,
            )
        )
    s.directives = (code_directives(ctx) + kept)[:MAX_DIRECTIVES]
    return moved


def _avoid(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog, moved: list[AvoidItem]) -> None:
    out: list[AvoidItem] = []
    for a in [*s.avoid, *moved]:
        if not a.text:
            log("avoid", "empty")
            continue
        refs = verify_refs(a.refs, ctx, "avoid", log)
        if not refs:
            log("avoid", "no_verified_ref")
            continue
        reason = a.reason if a.origin == "moved" else ""
        if a.origin == "moved" and ctx.low_rank is not None and not reason:
            reason = _low_reason(ctx.low_rank, ctx)
        key = norm(a.text)
        if any(norm(o.text) == key for o in out):
            continue
        out.append(
            AvoidItem(text=a.text, refs=refs, origin=a.origin, reason=reason, ranks=_ranks_of(refs))
        )
    s.avoid = out[:MAX_AVOID]


def _storyboards(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    plan = {c.cut: c for c in ctx.cuts}
    out: list[Storyboard] = []
    for sb in s.storyboards:
        if len(out) >= MAX_STORYBOARDS:
            break
        texts = [sb.name, *(t for c in sb.cuts for t in (c.show, c.telop, c.aim))]
        if any(has_avoid(t, ctx.avoid_terms) for t in texts if t):
            log("storyboards", "avoid_term")
            continue
        cuts: dict[int, StoryboardCut] = {}
        for cut in sb.cuts:
            slot = plan.get(cut.cut)
            if slot is None:
                log("storyboards.cuts", f"cut_unknown:{cut.cut}")
                continue
            if cut.cut in cuts:
                continue
            if not (cut.show or cut.telop):
                log("storyboards.cuts", "empty")
                continue
            if not ctx.framing and any(has_framing_words(t) for t in (cut.show, cut.aim)):
                log("storyboards.cuts", "framing")
                continue
            refs = verify_refs(cut.refs, ctx, "storyboards.cuts", log)
            if not refs:
                log("storyboards.cuts", "no_verified_ref")
                continue
            cuts[cut.cut] = StoryboardCut(
                cut=slot.cut,
                show=cut.show,
                telop=cut.telop,
                aim=cut.aim,
                refs=refs,
                start_sec=slot.start,
                end_sec=slot.end,
                stage=slot.stage,
            )
        if not cuts:
            log("storyboards", "no_cut")
            continue
        ordered = [cuts[k] for k in sorted(cuts)]
        basis = sorted({r.rank for c in ordered for r in c.refs})
        note = (
            f"事例1本（#{basis[0]}）にもとづく案"
            if len(basis) == 1
            else f"{ranks_text(basis)}にもとづく案"
        )
        pr = pr_ranks(basis, ctx)
        if pr:
            note += f"（タイアップ投稿 {ranks_text(pr)}を含む）"
        out.append(
            Storyboard(
                name=sb.name,
                basis_ranks=basis,
                cuts=ordered,
                target_sec=ctx.target_sec,
                basis_note=note,
            )
        )
    s.storyboards = out


def _board_angles(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    size = len(ctx.board)
    out = []
    for b in s.board_angles:
        terms = [t for t in dict.fromkeys(b.match_terms) if norm(t)]
        if not b.label or not terms:
            log("board_angles", "no_match_terms")
            continue
        ranks = [m.rank for m in ctx.board if any(norm(t) in norm(m.desc) for t in terms)]
        if len(ranks) <= 1:
            log("board_angles", "match_terms:<=1")
            continue
        if size >= 3 and len(ranks) >= size - 1:
            log("board_angles", "angle_too_wide")
            continue
        b.match_terms = terms
        b.ranks = ranks
        out.append(b)
    s.board_angles = out[:MAX_BOARD_ANGLES]


def stat_tag(feature: str, text: str, ctx: SynthesisContext) -> str:
    """R2: n≥8 で相関を渡したとき、仮説の文に特徴名があれば〔特徴×順位 ρ=…〕をコードが作る。"""
    st = ctx.stats
    if not feature or st is None or ctx.n < CORR_MIN_N or feature not in text:
        return ""
    c = next((c for c in st.correlations if c.feature == feature and c.rho is not None), None)
    if c is None or c.rho is None:
        return ""
    rho = f"{c.rho:+.2f}".replace("-", "−")
    return f"〔{feature}×順位 ρ={rho}, n={c.n_pairs}・単調{c.monotonic_hits}/{c.monotonic_total}〕"


def _hypotheses(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    out: list[HypothesisV3] = []
    for h in s.hypotheses:
        if len(out) >= MAX_HYPOTHESES:
            break
        if not h.text:
            log("hypotheses", "empty")
            continue
        if has_avoid(h.text, ctx.avoid_terms):
            log("hypotheses", "avoid_term")
            continue
        test = h.test
        if test and (
            has_avoid(test, ctx.avoid_terms) or (not ctx.framing and has_framing_words(test))
        ):
            log("hypotheses.test", "avoid_or_framing")
            test = ""
        terms = [t for t in dict.fromkeys(h.match_terms) if norm(t)]
        if not terms:
            log("hypotheses", "no_match_terms")
            continue
        ranks = term_ranks(terms, ctx)
        if len(ranks) <= 1:
            log("hypotheses", "match_terms:<=1")
            continue
        t = tier(len(ranks), ctx.n)
        text = _case_words(h.text) if t == TIER_CASE else h.text
        text = with_pr_note(text, ranks, ctx)
        out.append(
            HypothesisV3(
                text=text,
                match_terms=terms,
                test=test,
                stat_feature=h.stat_feature,
                ranks=ranks,
                tier=t,
                stat_tag=stat_tag(h.stat_feature, text, ctx),
            )
        )
    s.hypotheses = out


def _posting(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    p = s.posting
    if p is None:
        return
    for attr in ("caption_plan", "ab_plan"):
        value = getattr(p, attr)
        if value and (
            has_avoid(value, ctx.avoid_terms) or (not ctx.framing and has_framing_words(value))
        ):
            log(f"posting.{attr}", "avoid_or_framing")
            setattr(p, attr, "")
    s.posting = p if (p.caption_plan or p.ab_plan) else None


def _concepts_and_angles(s: CrossSynthesis, ctx: SynthesisContext, log: CheckLog) -> None:
    """v2 の欄（LLM が返したとき）: 概念は数の語で数え直し、角度は許可値と広さで絞る（R7・R16）。"""
    concepts = []
    for cc in s.common_concepts:
        words = [
            unicodedata.normalize("NFKC", f"{cc.concept} {cc.gist}")[c.start : c.end]
            for c in extract_claims(f"{cc.concept} {cc.gist}")
        ]
        if not cc.concept or not words:
            log("common_concepts", "no_match_terms")
            continue
        ranks = term_ranks(words, ctx)
        if len(ranks) <= 1:
            log("common_concepts", "match_terms:<=1")
            continue
        cc.videos = ranks
        cc.prevalence = f"{len(ranks)}/{ctx.n}"
        concepts.append(cc)
    s.common_concepts = concepts
    angles = []
    for ac in s.angle_clusters:
        if ac.angle not in ALLOWED_ANGLES:
            log("angle_clusters", "angle_value")
            continue
        videos = [r for r in dict.fromkeys(ac.videos) if r in ctx.ranks]
        if not videos:
            log("angle_clusters", "no_valid_rank")
            continue
        if len(videos) >= max(1, ctx.n - 1):
            log("angle_clusters", "angle_too_wide")
            continue
        ac.videos = videos
        angles.append(ac)
    s.angle_clusters = angles


def directive_line(d: Directive, n: int) -> str:
    """描画用の 1 行（指示文〔段階 c/n（#…）｜根拠 #4 25秒「大さじ8杯」〕）。タグはコードだけ。"""
    tag = tier_text(d.ranks, n)
    if d.refs:
        tag += f"｜根拠 {evidence_text(d.refs[0])}"
    return f"{d.text}〔{tag}〕"


def project_v2(s: CrossSynthesis, ctx: SynthesisContext) -> None:
    """今の描画（report / slides）が読む v2 の欄を、v3 の欄とコードの事実から作り直す。"""
    sl = s.summary_lines or SummaryLines()
    s.headline = sl.type_line
    best = ""
    if ctx.best_rank:
        why = f"（{'・'.join(ctx.best_metrics)}が{ctx.n}本で最大）" if ctx.best_metrics else ""
        best = f"最も見られ保存された1本は#{ctx.best_rank}{why}。"
    s.strategy = f"{best}{sl.best_reason}".strip()
    s.creative_brief = [directive_line(d, ctx.n) for d in s.directives]
    s.posting_design = s.posting.caption_plan if s.posting else ""
    s.client_pitch = sl.client_move
    s.win_hypotheses = [
        WinHypothesis(
            hypothesis=h.text,
            supported_by=list(h.ranks),
            confidence="低" if h.tier == TIER_CASE else "中",
            counter_example=None,
            so_what=h.test,
        )
        for h in s.hypotheses
    ]
    s.shared_funnel = None  # 共通の導線は facts.cta_consensus（コード）で描く
    s.differentiators = []
    s.caveat = ""  # 注意書きはコード（stats.caveats・フッタ）が出す


def finalize(
    syn: CrossSynthesis,
    ctx: SynthesisContext,
    *,
    log: CheckLog | None = None,
    grounders: Grounders | None = None,
    ledger: DropLedger | None = None,
) -> CrossSynthesis:
    """v3 の常時の検査を全部通し、v2 の欄を作り直した写しを返す（元は変えない・冪等）。"""
    log = log or CheckLog()
    s = syn.model_copy(deep=True)
    _clean_v3(s, ctx, log)
    _ground_v3(s, grounders, ledger)
    _best_reason_ground(s, ctx, grounders, ledger)
    _summary(s, ctx, log)
    drop_conflicts(s, log)
    _per_video(s, ctx, log)
    moved = _directives(s, ctx, log)
    _avoid(s, ctx, log, moved)
    _storyboards(s, ctx, log)
    _board_angles(s, ctx, log)
    _hypotheses(s, ctx, log)
    _posting(s, ctx, log)
    _concepts_and_angles(s, ctx, log)
    project_v2(s, ctx)
    s.version = SYNTHESIS_V3
    if ledger is not None and ledger.enforce:
        s.grounding_mode = ledger.mode
        s.grounding_dropped = ledger.count
    return s


def conflict_probe(syn: CrossSynthesis, ctx: SynthesisContext) -> list[Conflict]:
    """R6: 作り直しを頼むかの判定（掃除と見出しの検査のあとの文で比べる・記録は出さない）。

    見出しがコードの代わりの文になるなら、LLM の見出しとの食い違いでは作り直させない。
    """
    s = syn.model_copy(deep=True)
    log = CheckLog()
    _clean_v3(s, ctx, log)
    _summary(s, ctx, log)
    return find_conflicts(conflict_fields(s))


__all__ = [
    "ALLOWED_ANGLES",
    "ASSERTIVE_WORDS",
    "FRAMING_RE",
    "GUESS_PREFIX",
    "MAX_DIRECTIVES",
    "STAT_WORDS",
    "SYNTHESIS_V3",
    "UNMEASURED_WORDS",
    "CheckLog",
    "Claim",
    "Conflict",
    "alt_type_line",
    "clean_text",
    "code_directives",
    "conflict_fields",
    "conflict_note",
    "conflict_probe",
    "directive_line",
    "drop_conflicts",
    "evidence_text",
    "extract_claims",
    "finalize",
    "find_conflicts",
    "has_avoid",
    "headline_problem",
    "project_v2",
    "replace_client_words",
    "stat_tag",
    "strip_count_tags",
    "term_ranks",
    "verify_refs",
    "with_pr_note",
]
