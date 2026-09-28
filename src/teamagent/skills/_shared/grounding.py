"""LLM の文に「入力に無い数字」が混ざったら捨てる照合（スキル横断の共通部品）。

照合は、LLM に渡した入力の文字列に同じ数字が現れるかどうかで行う。計算して作った数字
（「再生の71%」など）は入力に現れないので落ちる。0〜10 と 90・100 は数えなくても書ける
小さい数・区切りの数なので無条件に通す。

使い方:
    grounder = NumberGrounder.from_inputs(prompt_body, valid_ranks={1, 2, 3})
    grounder.stray("再生の71%")          # -> {"71"}
    grounder.keep_sentences("A。B71%。")  # -> ("A。", ["number:71"])
    grounder.filter_ranks([1, 9, 1])     # -> [1]

数字の読み方（入力側・出力側で同じ規則）:
    - 全角は半角に、桁区切りのカンマは外す。先頭ゼロと小数の末尾ゼロは揃える（05→5・2.50→2.5）
    - 「万」「億」は展開する（1.2万→12000）。単位の違う「1.2%」とは一致しない
    - 画面比（9:16・16:9・1:1・4:5・3:4・4:3）は数字として扱わない
    - 「0:05」のようなタイムコード（分が 1 桁）は秒に直す（0:05→5・1:30→90）
    - from_inputs(rounding=True) は入力の小数の切り捨て・切り上げ・四捨五入（整数と小数 1 桁）と、
      1 万以上の整数の「万」単位の丸め（12,345→1.2万=12000 など）も入力にあったものとみなす

捨てた記録（DropSink）には欄名と「入力に無い数字」だけを渡し、本文は渡さない
（ログに LLM の文をそのまま出さないため）。

mode:
    enforce … 捨てる（本番の既定にするのは件数を測ってから）
    shadow  … 捨てずにログだけ出す。env ``GROUNDING_MODE_<SKILL>`` で切り替える。
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

Mode = Literal["enforce", "shadow"]
# (欄名, 理由) を受け取る。理由は "number:71,83" "rank:9" "deny:ρ" の形で本文を含めない。
DropSink = Callable[[str, str], None]

# 数えなくても書ける小さい数（「2つ」「1万人未満」の 1 など）と、帯・期間の境目の数。
ALWAYS_ALLOWED: frozenset[str] = frozenset({str(i) for i in range(11)} | {"90", "100"})
# 相関係数は結論・指示に使わない（video_algorithm synthesis.md の統計ガードレール）。
RHO_TERMS: tuple[str, ...] = ("ρ", "相関係数")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_SUFFIX_RE = re.compile(r"\s*(倍|%|万|億)")
_SCALE = {"万": Decimal(10_000), "億": Decimal(100_000_000)}
# 画面比。数字として照合しない（プロンプトが 9:16 の記載を許している）。
_RATIO_RE = re.compile(r"(?<![\d.:])(?:9:16|16:9|1:1|4:5|3:4|4:3)(?![\d.:])")
# 「0:05」「1:30」のタイムコード（分が 1 桁）。時刻の「18:00」は対象外。
_TIMECODE_RE = re.compile(r"(?<![\d.:])(\d):([0-5]\d)(?![\d.:])")
# 「#3」「rank3」。数字で始まるハッシュタグ（#100均・#30代ランチ・#2025新宿・#3coins）は
# 順位ではないので、数字の直後に漢字・カタカナ・英字が続くものは除く（「#3の」「#3と」は順位）。
_RANK_REF_RE = re.compile(
    r"(?:#|rank\s*)(\d+)(?![\d.]|[A-Za-z\u30a0-\u30ff\u3400-\u9fff\uf900-\ufaff々〆])",
    re.IGNORECASE,
)
# 「3位」。範囲の「上位10位」「トップ10位」「10位以内」などは実在の順位を指さないので除く。
_RANK_POS_RE = re.compile(r"(?<!上位)(?<!トップ)(?<![\d.])(\d+)\s*位(?!以内|以下|以上|まで|圏)")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]*(?:[。！？!?\n]+|$)")

# 誇張語の言い換え（プロンプトで禁じても実機の Haiku が「検索面を支配」と書いた）。
# 長い語から順に当てる（「を支配」を「支配」より先に）。
_TONE_DOWN: tuple[tuple[str, str], ...] = (
    ("を支配", "の中心"),
    ("支配的", "中心的"),
    ("支配", "中心"),
    ("を独占", "の多くを占める"),
    ("独占", "多数"),
    ("圧倒的な", "大きな"),
    ("圧倒的に", "大きく"),
    ("圧倒", "上回"),
    ("爆発的な", "大きな"),
    ("爆発的に", "大きく"),
)


def tone_down(text: str) -> str:
    """誇張語を言い換える（数字の照合とは独立）。"""
    for word, plain in _TONE_DOWN:
        text = text.replace(word, plain)
    return text


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(",", "")


def _canonical(raw: str) -> str:
    """先頭ゼロと小数の末尾ゼロを揃える（05→5・2.50→2.5・10.0→10）。"""
    if "." in raw:
        whole, frac = raw.split(".", 1)
        whole = whole.lstrip("0") or "0"
        frac = frac.rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return raw.lstrip("0") or "0"


def _decimal_text(value: Decimal) -> str:
    return _canonical(format(value, "f"))


@dataclass(frozen=True)
class NumberToken:
    """文中の数字 1 つ。

    key: 照合に使う正規形（先頭ゼロ・末尾ゼロを揃え、万・億は展開、タイムコードは秒）。
    values: extract_numbers が返す表記（書かれたままの形と正規形）。
    suffix: 直後の単位（倍・%・万・億）。無ければ空。
    """

    key: str
    values: tuple[str, ...]
    suffix: str = ""


def extract_tokens(text: str) -> list[NumberToken]:
    """文中の数字を、単位つきで取り出す。"""
    normalized = _RATIO_RE.sub(" ", _normalize(text))
    tokens: list[NumberToken] = []

    def timecode(m: re.Match[str]) -> str:
        seconds = str(int(m.group(1)) * 60 + int(m.group(2)))
        tokens.append(NumberToken(key=seconds, values=(seconds,)))
        return " "

    normalized = _TIMECODE_RE.sub(timecode, normalized)
    for m in _NUM_RE.finditer(normalized):
        raw = m.group(0)
        sm = _SUFFIX_RE.match(normalized, m.end())
        suffix = sm.group(1) if sm else ""
        if suffix in _SCALE:
            expanded = _decimal_text(Decimal(raw) * _SCALE[suffix])
            tokens.append(NumberToken(key=expanded, values=(expanded,), suffix=suffix))
            continue
        key = _canonical(raw)
        values = [raw]
        if "." in raw:
            values.append(raw.rstrip("0").rstrip("."))
        values.append(key)
        tokens.append(NumberToken(key=key, values=tuple(dict.fromkeys(values)), suffix=suffix))
    return tokens


def _rounded(key: str) -> set[str]:
    """入力の数字から、丸めて書いてよい形（小数→整数・小数 1 桁、1 万以上→万単位）。"""
    value = Decimal(key)
    if "." in key:
        base, scale = value, Decimal(1)
    elif value >= _SCALE["万"]:
        base, scale = value / _SCALE["万"], _SCALE["万"]
    else:
        return set()
    candidates = (
        base.to_integral_value(rounding=ROUND_FLOOR),
        base.to_integral_value(rounding=ROUND_CEILING),
        base.quantize(Decimal(1), rounding=ROUND_HALF_UP),
        base.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
    )
    return {_decimal_text(c * scale) for c in candidates}


def extract_numbers(text: str) -> set[str]:
    """文中の数字の集合（全角・カンマを正規化し、小数の末尾ゼロ違いも入れる）。"""
    out: set[str] = set()
    for token in extract_tokens(text):
        out.update(token.values)
    return out


def grounding_mode(skill: str, *, default: Mode = "shadow") -> Mode:
    """env ``GROUNDING_MODE_<SKILL>`` を読む。enforce / shadow 以外は既定に倒す。"""
    raw = os.environ.get(f"GROUNDING_MODE_{skill.upper()}", "").strip().lower()
    if raw == "enforce":
        return "enforce"
    if raw == "shadow":
        return "shadow"
    return default


def _is_rank(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class NumberGrounder:
    """入力に現れた数字の集合と、実在する順位で LLM の文を照合する。

    strict_suffixes: この単位が付いた数字は ALWAYS_ALLOWED でも無条件には通さない
    （「保存率が3倍」を入力に 3 が無いのに通さないため）。
    """

    allowed: frozenset[str]
    valid_ranks: frozenset[int] | None = None
    always_allowed: frozenset[str] = ALWAYS_ALLOWED
    strict_suffixes: frozenset[str] = frozenset()

    @classmethod
    def from_inputs(
        cls,
        *texts: str,
        valid_ranks: Iterable[int] | None = None,
        rounding: bool = False,
        always_allowed: frozenset[str] = ALWAYS_ALLOWED,
        strict_suffixes: frozenset[str] = frozenset(),
    ) -> NumberGrounder:
        allowed: set[str] = set()
        for text in texts:
            for token in extract_tokens(text):
                allowed.update(token.values)
                if rounding:
                    allowed |= _rounded(token.key)
        return cls(
            allowed=frozenset(allowed),
            valid_ranks=frozenset(valid_ranks) if valid_ranks is not None else None,
            always_allowed=always_allowed,
            strict_suffixes=strict_suffixes,
        )

    def _value_ok(self, value: str, suffix: str) -> bool:
        if value in self.allowed:
            return True
        return suffix not in self.strict_suffixes and value in self.always_allowed

    def stray(self, text: str) -> set[str]:
        """入力に無い数字（正規形）。"""
        return {t.key for t in extract_tokens(text) if not self._value_ok(t.key, t.suffix)}

    def bad_rank_refs(self, text: str) -> set[int]:
        """「#N」「rankN」「N位」の N のうち、実在しない順位。valid_ranks が無ければ検査しない。"""
        if self.valid_ranks is None:
            return set()
        normalized = _normalize(text)
        refs = {int(m.group(1)) for m in _RANK_REF_RE.finditer(normalized)}
        refs |= {int(m.group(1)) for m in _RANK_POS_RE.finditer(normalized)}
        return {r for r in refs if r not in self.valid_ranks}

    def rank_refs_ok(self, text: str) -> bool:
        return not self.bad_rank_refs(text)

    def reason(self, text: str, *, deny: Iterable[str] = ()) -> str | None:
        """捨てる理由（本文を含めない）。問題が無ければ None。"""
        parts: list[str] = []
        stray = self.stray(text)
        if stray:
            parts.append("number:" + ",".join(sorted(stray)))
        bad = self.bad_rank_refs(text)
        if bad:
            parts.append("rank:" + ",".join(str(r) for r in sorted(bad)))
        hits = [term for term in deny if term in text]
        if hits:
            parts.append("deny:" + ",".join(hits))
        return ";".join(parts) or None

    def ok(self, text: str, *, deny: Iterable[str] = ()) -> bool:
        return self.reason(text, deny=deny) is None

    def keep_sentences(self, text: str, *, deny: Iterable[str] = ()) -> tuple[str, list[str]]:
        """文単位で照合し、問題のある文だけ捨てる。(残した文, 捨てた理由の一覧)。"""
        deny = tuple(deny)
        kept: list[str] = []
        dropped: list[str] = []
        for sentence in _SENTENCE_RE.findall(text):
            if not sentence.strip():
                continue
            why = self.reason(sentence, deny=deny)
            if why is None:
                kept.append(sentence)
            else:
                dropped.append(why)
        return "".join(kept).strip(), dropped

    def filter_ranks(self, values: Any) -> list[int]:
        """実在する順位だけ・重複なし・順序はそのまま。valid_ranks が無ければ int だけ残す。"""
        out: list[int] = []
        for value in values if isinstance(values, list) else []:
            if not _is_rank(value):
                continue
            if self.valid_ranks is not None and value not in self.valid_ranks:
                continue
            if value not in out:
                out.append(value)
        return out


@dataclass
class DropLedger:
    """捨てた（shadow では捨てるはずだった）件数を数え、ログと DropSink へ流す。

    ログ ``grounding_dropped`` に出すのは欄名と理由（入力に無い数字・順位）だけ。
    """

    skill: str
    mode: Mode
    request_id: str = ""
    sink: DropSink | None = None
    count: int = 0
    fields: list[str] = field(default_factory=list)

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    def __call__(self, field_name: str, reason: str) -> None:
        self.count += 1
        self.fields.append(field_name)
        logger.info(
            "grounding_dropped",
            skill=self.skill,
            field=field_name,
            reason=reason,
            mode=self.mode,
            request_id=self.request_id,
        )
        if self.sink is not None:
            self.sink(field_name, reason)


__all__ = [
    "ALWAYS_ALLOWED",
    "RHO_TERMS",
    "DropLedger",
    "DropSink",
    "Mode",
    "NumberGrounder",
    "NumberToken",
    "extract_numbers",
    "extract_tokens",
    "grounding_mode",
    "tone_down",
]
