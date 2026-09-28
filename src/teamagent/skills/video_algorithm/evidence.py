"""入力に実在するかの照合（検索語・引用 refs・ブランド名簿）と、段階の名前。LLM を使わない。

- 検索語（KW）: テロップは本文（NFKC）に語があれば完全一致。動画分析 AI（Gemini）の
  ``kw_match`` と、テロップ層の ``keyword_matches`` は**そのまま信じない**。言い換え
  （surface_text）は、その秒 ±2 のテロップに実在するときだけ残す（本番で、実在しない
  テロップ「工程」への一致や、KW を含まないテロップへの ✓ があった）。ハッシュタグは
  取得したハッシュタグ（無ければキャプションの ``#…語``）で判定する。発話は照合できないので
  「AI 聞き取り（未照合）」として分ける（``verified=False``）。
- 引用 refs（``verify_ref``）: 「#n の何秒の何」を、その動画のテロップ（±2 秒）・場面・フック・
  ブランド・キャプションに文字として実在するかで確かめる。
- ブランド名簿（``Roster``）: 区分（クライアント／競合）はコードが名簿で決める。Gemini の
  ``brand_relation`` は使わない。名簿が無ければ「未指定」。
- 段階（``tier``）: 本数 c/n から 必須条件・多数派・事例 を付ける。LLM には付けさせない。

search_surface_check（video_structure）からも使うので、video_algorithm.schema 以外を実行時に
import しない（葉のモジュール。循環 import を作らない）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from teamagent.skills.video_algorithm.schema import (
    FrameShot,
    KeywordMatch,
    VideoMeta,
    VideoVSEOAnalysis,
)

if TYPE_CHECKING:
    from teamagent.skills.video_algorithm.facts import VideoFacts

# 秒の許容（Gemini の秒は実際の映像より 1〜2 秒ずれる。目視で確認済み）。
SEC_TOLERANCE = 2.0
# 根拠に添えるコマの許容（既に抜いたコマのうち、この秒以内で最も近いもの）。
FRAME_TOLERANCE = 3.0
# フック（hook_summary）で照合する秒の上限。
HOOK_MAX_SEC = 3.0
# 引用として照合する最短の長さ（1 文字は何にでも含まれてしまう）。
MIN_QUOTE_CHARS = 2
_EPS = 1e-9

TIER_REQUIRED = "必須条件"
TIER_MAJORITY = "多数派"
TIER_CASE = "事例"
TIER_NAMES: tuple[str, ...] = (TIER_REQUIRED, TIER_MAJORITY, TIER_CASE)
# 多数派の下限（本数の割合）。n=5 なら 3 本。
MAJORITY_SHARE = 0.6

KwLayer = Literal["telop", "caption", "hashtag", "speech"]
KwMatchKind = Literal["exact", "synonym"]
Relation = Literal["client", "competitor", "other", "unspecified"]
RefSource = Literal["telop", "scene", "hook", "brand", "caption"]

KW_LAYERS: tuple[KwLayer, ...] = ("telop", "caption", "hashtag", "speech")
KW_LAYER_LABEL: dict[str, str] = {
    "telop": "テロップ",
    "caption": "キャプション",
    "hashtag": "ハッシュタグ",
    "speech": "発話（AI聞き取り）",
}
RELATION_LABEL: dict[str, str] = {
    "client": "クライアント",
    "competitor": "競合",
    "other": "その他",
    "unspecified": "未指定",
}
UNIDENTIFIED_LOGO = "unidentified_logo"

_TERM_SPLIT = re.compile(r"[\s　,、]+")
# 言い換え（surface_text）を語の単位に分ける区切り。「作れます、レシピ」→「作れます」「レシピ」。
_SURFACE_SPLIT = re.compile(r"[、,，/／・;；|｜\s]+")
_SPEECH_LAYERS = ("narration", "dialogue")


# ── 正規化 ──────────────────────────────────────────────────────────────


def fold(text: str | None) -> str:
    """NFKC＋大文字小文字を無視（空白は残す。ハッシュタグの区切りに使う）。"""
    return unicodedata.normalize("NFKC", text or "").casefold()


def norm(text: str | None) -> str:
    """照合用: NFKC＋大文字小文字を無視＋空白を除く。"""
    return "".join(fold(text).split())


def contains(haystack: str | None, needle: str | None) -> bool:
    """正規化した haystack に正規化した needle が含まれるか（needle が空なら False）。"""
    n = norm(needle)
    return bool(n) and n in norm(haystack)


# ── 段階の名前 ──────────────────────────────────────────────────────────


def majority_min(n: int) -> int:
    """多数派とみなす最小本数（ceil(0.6n)）。"""
    return max(1, math.ceil(MAJORITY_SHARE * n))


def tier(c: int, n: int) -> str:
    """本数 c/n の段階: 全部＝必須条件、ceil(0.6n) 以上＝多数派、それ以外＝事例。"""
    if n > 0 and c >= n:
        return TIER_REQUIRED
    if n > 0 and c >= majority_min(n):
        return TIER_MAJORITY
    return TIER_CASE


def ranks_text(ranks: Iterable[int]) -> str:
    """#1・#3 の形（重複なし・昇順）。"""
    return "・".join(f"#{r}" for r in sorted(set(ranks)))


def tier_text(ranks: Iterable[int], n: int) -> str:
    """「必須条件 5/5」「多数派 3/5（#1・#3・#5）」「事例 2/5（#1・#3）」。"""
    uniq = sorted(set(ranks))
    name = tier(len(uniq), n)
    base = f"{name} {len(uniq)}/{n}"
    if name == TIER_REQUIRED or not uniq:
        return base
    return f"{base}（{ranks_text(uniq)}）"


# ── 検索語（KW）の照合 ─────────────────────────────────────────────────


def query_terms(query: str) -> list[str]:
    """検索 KW を語に分ける（空白・読点・カンマ。重複は 1 回）。"""
    return list(dict.fromkeys(t for t in _TERM_SPLIT.split((query or "").strip()) if t))


def analysis_terms(a: VideoVSEOAnalysis) -> list[str]:
    """検索 KW が渡されないとき、動画分析 AI が記録した keyword（= 検索 KW）から語を取る。"""
    terms: list[str] = []
    for k in (*a.keyword_matches, *a.spoken_keywords):
        terms.extend(query_terms(k.keyword))
    return list(dict.fromkeys(terms))


@dataclass(frozen=True)
class KwHit:
    """検索語 1 つが、ある層に、完全一致か言い換えで出たこと（秒はテロップと発話だけ）。"""

    term: str
    layer: KwLayer
    match: KwMatchKind
    secs: tuple[float, ...]
    verified: bool  # speech は常に False（AI 聞き取り・未照合）


def _belongs(k: KeywordMatch, term: str, n_terms: int) -> bool:
    """その一致がどの語のものか。keyword が語そのもの（または語が 1 つで keyword 空）なら True。

    keyword が検索 KW 全体（複数の語）のときは、surface_text に語があるときだけその語に数える
    （言い換えをどの語のものか決められないため）。
    """
    kw = norm(k.keyword)
    t = norm(term)
    if kw == t:
        return True
    if not kw and n_terms == 1:
        return True
    if not kw or t in kw:
        return contains(k.surface_text, term)
    return False


def _surface_pieces(surface: str | None, term: str) -> list[str]:
    """言い換えの語（語そのものを含む片は除く＝それは完全一致で数える）。"""
    t = norm(term)
    return [
        p
        for p in (norm(x) for x in _SURFACE_SPLIT.split(fold(surface)))
        if len(p) >= MIN_QUOTE_CHARS and t not in p
    ]


def _telop_synonym_secs(a: VideoVSEOAnalysis, term: str, n_terms: int) -> list[float]:
    """テロップ層の言い換えのうち、その秒 ±2 のテロップに実在するものの秒。"""
    t = norm(term)
    secs: set[float] = set()
    for k in a.keyword_matches:
        if not k.matched or k.layer != "telop" or not _belongs(k, term, n_terms):
            continue
        pieces = _surface_pieces(k.surface_text, term)
        if not pieces:
            continue
        for sec in k.appear_sec:
            for tel in a.telops:
                text = norm(tel.text)
                if abs(tel.sec - sec) > SEC_TOLERANCE + _EPS or t in text:
                    continue
                if any(p in text for p in pieces):
                    secs.add(tel.sec)
    return sorted(secs)


def _hashtag_hit(meta: VideoMeta, term: str) -> bool:
    t = norm(term)
    tags = [norm(tag.lstrip("#＃")) for tag in meta.hashtags if tag]
    if tags:
        return any(t in tag for tag in tags)
    return re.search(r"#[^\s#]*" + re.escape(fold(term)), fold(meta.desc)) is not None


def kw_hits(
    meta: VideoMeta, analysis: VideoVSEOAnalysis | None, terms: Sequence[str]
) -> tuple[KwHit, ...]:
    """語ごと×層ごと×完全一致/言い換えの一致（照合済み）。analysis が無ければメタの層だけ。"""
    out: list[KwHit] = []
    n_terms = len(terms)
    for term in terms:
        t = norm(term)
        if not t:
            continue
        if analysis is not None:
            exact = sorted({tel.sec for tel in analysis.telops if t in norm(tel.text)})
            if exact:
                out.append(KwHit(term, "telop", "exact", tuple(exact), True))
            syn = _telop_synonym_secs(analysis, term, n_terms)
            if syn:
                out.append(KwHit(term, "telop", "synonym", tuple(syn), True))
        if t in norm(meta.desc):
            out.append(KwHit(term, "caption", "exact", (), True))
        elif analysis is not None and any(
            k.matched
            and k.layer == "caption"
            and _belongs(k, term, n_terms)
            and any(p in norm(meta.desc) for p in _surface_pieces(k.surface_text, term))
            for k in analysis.keyword_matches
        ):
            out.append(KwHit(term, "caption", "synonym", (), True))
        if _hashtag_hit(meta, term):
            out.append(KwHit(term, "hashtag", "exact", (), True))
        if analysis is not None:
            out.extend(_speech_hits(analysis, term, n_terms))
    return tuple(out)


def _speech_hits(a: VideoVSEOAnalysis, term: str, n_terms: int) -> list[KwHit]:
    spoken = [
        *a.spoken_keywords,
        *(k for k in a.keyword_matches if k.layer in _SPEECH_LAYERS),
    ]
    found: dict[KwMatchKind, set[float]] = {}
    for k in spoken:
        if not k.matched or not _belongs(k, term, n_terms):
            continue
        kind: KwMatchKind = (
            "exact"
            if k.match_type in ("exact", "partial") or contains(k.surface_text, term)
            else "synonym"
        )
        found.setdefault(kind, set()).update(k.appear_sec)
    return [
        KwHit(term, "speech", kind, tuple(sorted(secs)), False)
        for kind, secs in sorted(found.items())
    ]


# ── ブランド名簿 ───────────────────────────────────────────────────────


def _aliases(entry: str | None) -> tuple[str, ...]:
    return tuple(a for a in (norm(x) for x in (entry or "").split("|")) if a)


@dataclass(frozen=True)
class Roster:
    """区分の名簿。client_name と competitors の各要素は別名を | で区切れる。"""

    client_name: str | None = None
    competitors: tuple[str, ...] = ()

    @classmethod
    def of(cls, client_name: str | None, competitors: Iterable[str] | None = None) -> Roster:
        return cls(
            client_name=(client_name or "").strip() or None,
            competitors=tuple(c.strip() for c in (competitors or ()) if c and c.strip()),
        )

    @property
    def specified(self) -> bool:
        return bool(_aliases(self.client_name)) or any(_aliases(c) for c in self.competitors)

    def relation(self, brand_name: str) -> Relation:
        """NFKC・大文字小文字・空白を無視した完全一致。名簿が無ければ unspecified。"""
        if not self.specified:
            return "unspecified"
        name = norm(brand_name)
        if not name or name == norm(UNIDENTIFIED_LOGO):
            return "other"
        if name in _aliases(self.client_name):
            return "client"
        if any(name in _aliases(c) for c in self.competitors):
            return "competitor"
        return "other"

    def names(self) -> tuple[str, ...]:
        """名簿の全別名（正規化済み）。"""
        out = list(_aliases(self.client_name))
        for c in self.competitors:
            out.extend(_aliases(c))
        return tuple(dict.fromkeys(out))


# ── 引用 refs の照合 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ref:
    """LLM が根拠として挙げた「#n の何秒の何」。sec=None はキャプション。"""

    rank: int
    sec: float | None
    quote: str


@dataclass(frozen=True)
class VerifiedRef:
    """照合に合格した ref。source は見つかった場所、found_sec はその秒（キャプションは None）。"""

    rank: int
    sec: float | None
    quote: str
    source: RefSource
    found_sec: float | None


def _near(a: float, b: float, tol: float = SEC_TOLERANCE) -> bool:
    return abs(a - b) <= tol + _EPS


def verify_ref(
    ref: Ref, facts: VideoFacts, analysis: VideoVSEOAnalysis | None
) -> VerifiedRef | None:
    """quote を正規化（NFKC・空白を除く）し、次のどれかに含まれれば合格。

    | 照合先 | 条件 |
    | テロップ本文 | テロップの秒と ref.sec の差が 2.0 秒以内 |
    | 場面の説明・テロップ・発話 | 場面の開始−2 ≦ ref.sec ≦ 終了+2 |
    | フックの要約 | ref.sec ≦ 3 |
    | ブランド名 | ブランドが映る秒のどれかと ref.sec の差が 2.0 秒以内 |
    | キャプション全文 | ref.sec が None |
    """
    if ref.rank != facts.rank:
        return None
    q = norm(ref.quote)
    if len(q) < MIN_QUOTE_CHARS:
        return None

    def ok(source: RefSource, found: float | None) -> VerifiedRef:
        return VerifiedRef(ref.rank, ref.sec, ref.quote, source, found)

    if ref.sec is None:
        return ok("caption", None) if q in norm(facts.desc) else None
    sec = float(ref.sec)
    if analysis is None:
        return None
    for tel in analysis.telops:
        if _near(tel.sec, sec) and q in norm(tel.text):
            return ok("telop", tel.sec)
    for sc in analysis.scenes:
        end = max(sc.end_sec, sc.start_sec)
        if sc.start_sec - SEC_TOLERANCE - _EPS <= sec <= end + SEC_TOLERANCE + _EPS and any(
            q in norm(text) for text in (sc.desc, sc.telop, sc.speech)
        ):
            return ok("scene", sc.start_sec)
    if sec <= HOOK_MAX_SEC + _EPS and q in norm(analysis.hook_summary):
        return ok("hook", 0.0)
    for b in analysis.brand_detections:
        name = norm(b.brand_name)
        if not name or not (q in name or name in q):
            continue
        hit = next((s for s in b.appear_sec if _near(s, sec)), None)
        if hit is not None:
            return ok("brand", hit)
    return None


def refs_tier(refs: Iterable[VerifiedRef], n: int) -> tuple[str, tuple[int, ...]]:
    """合格した refs の異なる順位の数から段階の名前（順位は昇順）。"""
    ranks = tuple(sorted({r.rank for r in refs}))
    return tier(len(ranks), n), ranks


def ref_frame(
    frames: Iterable[FrameShot], sec: float | None, tol: float = FRAME_TOLERANCE
) -> FrameShot | None:
    """既に抜いたコマのうち ±tol 秒で最も近いもの（同じ距離なら早い方）。無ければ None。"""
    if sec is None:
        return None
    usable = [f for f in frames if f.data_uri and _near(f.sec, sec, tol)]
    if not usable:
        return None
    return min(usable, key=lambda f: (abs(f.sec - sec), f.sec))


__all__ = [
    "FRAME_TOLERANCE",
    "KW_LAYERS",
    "KW_LAYER_LABEL",
    "MAJORITY_SHARE",
    "RELATION_LABEL",
    "SEC_TOLERANCE",
    "TIER_CASE",
    "TIER_MAJORITY",
    "TIER_NAMES",
    "TIER_REQUIRED",
    "KwHit",
    "Ref",
    "Roster",
    "VerifiedRef",
    "analysis_terms",
    "contains",
    "fold",
    "kw_hits",
    "majority_min",
    "norm",
    "query_terms",
    "ranks_text",
    "ref_frame",
    "refs_tier",
    "tier",
    "tier_text",
    "verify_ref",
]
