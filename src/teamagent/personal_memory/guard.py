"""personal_memory の保存前・読み出し時に使う純粋な安全検査。

このモジュールは入力を保存せず、外部 IO にも触れない。発話から生成したメモ候補は
``check_entry``、学習元に回す発話は ``check_utterance`` で検査する。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from ._threat_patterns import INJECTION_PATTERNS, SECRET_PATTERNS

MAX_ENTRY_CHARS: Final[int] = 200
VERBATIM_MIN_RUN: Final[int] = 25
MAX_UTTERANCE_CHARS: Final[int] = 800

_Scope = Literal["entry", "utterance"]

_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z])",
    re.IGNORECASE,
)
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?<![0-9])0[0-9]{1,3}-[0-9]{1,4}-[0-9]{4}(?![0-9])"
    r"|(?<![0-9])0[0-9]{9,10}(?![0-9])"
    r"|(?<![A-Za-z0-9])\+81(?:[- ]?[0-9]){9,10}(?![0-9])"
    r")"
)
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|(?<![/\w])www\.)",
    re.IGNORECASE,
)
_LONG_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]{8,}")
_FORWARDED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:from:|転送|forwarded|original\s+message|-----original)",
    re.IGNORECASE,
)

_NAME_CHAR_CLASS: Final[str] = r"A-Za-zぁ-ゖァ-ヺー々〆ヶ\u3400-\u4dbf\u4e00-\u9fff"
_NAME_BEFORE_HONORIFIC_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![{_NAME_CHAR_CLASS}])[{_NAME_CHAR_CLASS}]{{1,10}}$"
)
_HONORIFIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?=(さん|様|さま|氏|殿|くん|君|先生|部長|課長|社長|専務|常務|"
    r"取締役|担当|Mr\.|Ms\.|Mrs\.))",
    re.IGNORECASE,
)
_PREFIXED_ENGLISH_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:Mr|Ms|Mrs)\.\s*[A-Za-z]{1,10}\b",
    re.IGNORECASE,
)
_HONORIFIC_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {
        "お客さん",
        "お客様",
        "みなさん",
        "先方さん",
        "担当さん",
        "皆さん",
        "皆様",
    }
)
# 敬称の文字を含むが人名ではない語。前方で終わる形（仕様）と、敬称から始まる形（様式）を分けて持つ
_COMPOUND_ENDING_WITH_HONORIFIC: Final[frozenset[str]] = frozenset(
    {"仕様", "同様", "多様", "模様", "異様", "一様", "有様", "左様", "外様"}
)
_COMPOUND_STARTING_WITH_HONORIFIC: Final[tuple[str, ...]] = (
    "様式",
    "様子",
    "様相",
    "様態",
    "氏名",
    "担当者",
    "担当部署",
)
_WORD_BOUNDARY_PARTICLES: Final[tuple[str, ...]] = (
    "から",
    "まで",
    "より",
    "が",
    "で",
    "と",
    "に",
    "の",
    "は",
    "へ",
    "も",
    "や",
    "を",
)

_INVISIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({"Cf", "Co", "Cn"})
_INVISIBLE_CODEPOINTS: Final[frozenset[int]] = frozenset(
    {
        *range(0xE0000, 0xE0080),
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
        *range(0x200B, 0x200E),
        0x2060,
        0xFEFF,
    }
)


class Reason(StrEnum):
    """検査で拒否した理由。"""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    INVISIBLE = "invisible"
    INJECTION = "injection"
    SECRET = "secret"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    LONG_DIGITS = "long_digits"
    VERBATIM = "verbatim"
    PERSON_NAME = "person_name"
    QUOTED = "quoted"
    FORWARDED = "forwarded"
    CODE_BLOCK = "code_block"
    ATTACHMENT = "attachment"


@dataclass(frozen=True, slots=True)
class Verdict:
    """入力本文を保持しない検査結果。"""

    ok: bool
    reasons: tuple[Reason, ...]


@dataclass(frozen=True, slots=True)
class _Context:
    """ルール評価中だけ使う入力コンテキスト。"""

    scope: _Scope
    raw: str
    normalized: str
    utterances: tuple[str, ...] = ()
    allow_terms: frozenset[str] = frozenset()
    member_names: frozenset[str] = frozenset()
    has_attachment: bool = False


RuleFn = Callable[[_Context], bool]


@dataclass(frozen=True, slots=True)
class Rule:
    """適用先と判定関数をひも付けた単一の検査ルール。"""

    reason: Reason
    applies_to: frozenset[_Scope]
    fn: RuleFn


def normalize(text: str) -> str:
    """NFKC 正規化後に連続空白を畳み、前後の空白を除く。"""
    normalized = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", normalized).strip()


def _is_empty(context: _Context) -> bool:
    return not context.normalized


def _is_too_long(context: _Context) -> bool:
    limit = MAX_ENTRY_CHARS if context.scope == "entry" else MAX_UTTERANCE_CHARS
    return len(context.normalized) > limit


def _has_invisible(context: _Context) -> bool:
    return any(
        ord(char) in _INVISIBLE_CODEPOINTS or unicodedata.category(char) in _INVISIBLE_CATEGORIES
        for char in context.raw
    )


def _has_injection(context: _Context) -> bool:
    return any(pattern.search(context.normalized) for pattern in INJECTION_PATTERNS)


def _has_secret(context: _Context) -> bool:
    return any(pattern.search(context.normalized) for pattern in SECRET_PATTERNS)


def _has_email(context: _Context) -> bool:
    return _EMAIL_RE.search(context.normalized) is not None


def _has_phone(context: _Context) -> bool:
    return _PHONE_RE.search(context.normalized) is not None


def _has_url(context: _Context) -> bool:
    count = sum(1 for _ in _URL_RE.finditer(context.normalized))
    return count >= (1 if context.scope == "entry" else 2)


def _has_long_digits(context: _Context) -> bool:
    return _LONG_DIGITS_RE.search(context.normalized) is not None


def _without_allow_terms(text: str, allow_terms: frozenset[str]) -> tuple[str, ...]:
    """許可語を比較不能な境界として除き、残った文字列を返す。"""
    if not allow_terms:
        return (text,)
    alternatives = (re.escape(term) for term in sorted(allow_terms, key=len, reverse=True))
    pattern = re.compile("|".join(alternatives))
    return tuple(pattern.split(text))


def _has_common_run(left: str, right: str) -> bool:
    """2 文字列に閾値以上の連続一致があるかを返す。"""
    if len(left) < VERBATIM_MIN_RUN or len(right) < VERBATIM_MIN_RUN:
        return False
    if len(left) > len(right):
        left, right = right, left
    windows = {
        left[index : index + VERBATIM_MIN_RUN] for index in range(len(left) - VERBATIM_MIN_RUN + 1)
    }
    return any(
        right[index : index + VERBATIM_MIN_RUN] in windows
        for index in range(len(right) - VERBATIM_MIN_RUN + 1)
    )


def _has_verbatim(context: _Context) -> bool:
    excluded_terms = context.allow_terms | context.member_names
    entry_parts = _without_allow_terms(context.normalized, excluded_terms)
    for utterance in context.utterances:
        utterance_parts = _without_allow_terms(normalize(utterance), excluded_terms)
        if any(
            _has_common_run(entry_part, utterance_part)
            for entry_part in entry_parts
            for utterance_part in utterance_parts
        ):
            return True
    return False


def _has_complete_suffix(text: str, term: str) -> bool:
    """語が文字列末尾にあり、より長い語の一部でないことを確認する。"""
    if not text.endswith(term):
        return False
    term_start = len(text) - len(term)
    if term_start == 0:
        return True
    before_term = text[:term_start]
    if before_term.endswith(_WORD_BOUNDARY_PARTICLES):
        return True
    return re.fullmatch(rf"[{_NAME_CHAR_CLASS}]", before_term[-1]) is None


def _has_person_name(context: _Context) -> bool:
    text = context.normalized
    if _PREFIXED_ENGLISH_NAME_RE.search(text):
        return True

    allowed_names = context.allow_terms | context.member_names
    for honorific_match in _HONORIFIC_RE.finditer(text):
        honorific = honorific_match.group(1)
        start = honorific_match.start()
        prefix = text[:start].rstrip()
        phrase = prefix + honorific
        if any(_has_complete_suffix(phrase, exception) for exception in _HONORIFIC_EXCEPTIONS):
            continue
        if text.startswith(_COMPOUND_STARTING_WITH_HONORIFIC, start) or any(
            phrase.endswith(word) for word in _COMPOUND_ENDING_WITH_HONORIFIC
        ):
            continue
        if any(_has_complete_suffix(prefix, term) for term in allowed_names):
            continue
        if _NAME_BEFORE_HONORIFIC_RE.search(prefix):
            return True
    return False


def _has_quote(context: _Context) -> bool:
    return any(line.startswith((">", "＞")) for line in context.raw.splitlines())


def _is_forwarded(context: _Context) -> bool:
    return _FORWARDED_RE.search(context.normalized) is not None


def _has_code_block(context: _Context) -> bool:
    return "```" in context.raw


def _has_attachment(context: _Context) -> bool:
    return context.has_attachment


_BOTH: Final[frozenset[_Scope]] = frozenset({"entry", "utterance"})
_ENTRY: Final[frozenset[_Scope]] = frozenset({"entry"})
_UTTERANCE: Final[frozenset[_Scope]] = frozenset({"utterance"})

RULES: tuple[Rule, ...] = (
    Rule(Reason.EMPTY, _BOTH, _is_empty),
    Rule(Reason.TOO_LONG, _BOTH, _is_too_long),
    Rule(Reason.INVISIBLE, _BOTH, _has_invisible),
    Rule(Reason.INJECTION, _BOTH, _has_injection),
    Rule(Reason.SECRET, _BOTH, _has_secret),
    Rule(Reason.EMAIL, _ENTRY, _has_email),
    Rule(Reason.PHONE, _ENTRY, _has_phone),
    Rule(Reason.URL, _BOTH, _has_url),
    Rule(Reason.LONG_DIGITS, _ENTRY, _has_long_digits),
    Rule(Reason.VERBATIM, _ENTRY, _has_verbatim),
    Rule(Reason.PERSON_NAME, _ENTRY, _has_person_name),
    Rule(Reason.QUOTED, _UTTERANCE, _has_quote),
    Rule(Reason.FORWARDED, _UTTERANCE, _is_forwarded),
    Rule(Reason.CODE_BLOCK, _UTTERANCE, _has_code_block),
    Rule(Reason.ATTACHMENT, _UTTERANCE, _has_attachment),
)


def _evaluate(context: _Context) -> Verdict:
    reasons = tuple(
        rule.reason for rule in RULES if context.scope in rule.applies_to and rule.fn(context)
    )
    return Verdict(ok=not reasons, reasons=reasons)


def check_entry(
    entry: str,
    *,
    utterances: Sequence[str] = (),
    allow_terms: Collection[str] = (),
    member_names: Collection[str] = (),
) -> Verdict:
    """メモ候補を保存前または読み出し時に検査する。

    ``member_names`` は社内メンバー（同僚）名。敬称付きでも人名として落とさない。
    """
    context = _Context(
        scope="entry",
        raw=entry,
        normalized=normalize(entry),
        utterances=tuple(utterances),
        allow_terms=frozenset(filter(None, (normalize(term) for term in allow_terms))),
        member_names=frozenset(filter(None, (normalize(name) for name in member_names))),
    )
    return _evaluate(context)


def check_utterance(text: str, *, has_attachment: bool = False) -> Verdict:
    """発話をメモ学習へ回す前に検査する。"""
    context = _Context(
        scope="utterance",
        raw=text,
        normalized=normalize(text),
        has_attachment=has_attachment,
    )
    return _evaluate(context)


__all__ = [
    "MAX_ENTRY_CHARS",
    "MAX_UTTERANCE_CHARS",
    "RULES",
    "VERBATIM_MIN_RUN",
    "Reason",
    "Rule",
    "Verdict",
    "check_entry",
    "check_utterance",
    "normalize",
]
