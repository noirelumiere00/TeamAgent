"""資料一覧の条件検索。静的 Vault の parseQuery と同じ対応構文を分解する。"""

from __future__ import annotations

import re

QUERY_MAX_LENGTH = 300
QUERY_MAX_TERMS = 10
QUERY_MAX_TERM_LENGTH = 100
QUICK_FILTERS = {"recent": "最近30日", "proposal": "提案書", "minutes": "議事録"}

_TOKEN = re.compile(
    r'\[([^\]:]+)(?::([^\]]*))?\]|(-?)(tag|path|file):("[^"]*"|\S+)|"([^"]+)"|(\S+)'
)
_REGEX = re.compile(r"(^|\s)/((?:\\.|[^/\\])+)/(?=\s|$)")


class KnowledgeQueryError(ValueError):
    """表示して利用者に修正してもらう入力エラー。"""


def parse_knowledge_query(query: str) -> dict[str, list[str]]:
    if len(query) > QUERY_MAX_LENGTH:
        raise KnowledgeQueryError("条件は全体で300文字までです。")
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise KnowledgeQueryError("条件に制御文字は使えません。")
    if _REGEX.search(query):
        raise KnowledgeQueryError("この書き方はまだ使えません：/正規表現/。")
    parsed: dict[str, list[str]] = {
        key: [] for key in ("plain", "neg", "tag", "file", "ntag", "nfile")
    }
    terms = 0
    for match in _TOKEN.finditer(query):
        prop, _, negative, operator, operand, phrase, word = match.groups()
        if prop is not None:
            raise KnowledgeQueryError("この書き方はまだ使えません：[属性:値]・[属性:null]。")
        if operator == "path":
            raise KnowledgeQueryError(
                "この書き方はまだ使えません：path:。資料一覧にはclients/docsフォルダがありません。"
            )
        if operator:
            key = ("n" if negative else "") + operator
            value = re.sub(r'^"|"$', "", operand or "").lower()
        elif phrase:
            key, value = "plain", phrase.lower()
        else:
            key = "neg" if word.startswith("-") else "plain"
            value = (word[1:] if key == "neg" else word).lower()
        if not value:
            continue
        terms += 1
        if terms > QUERY_MAX_TERMS:
            raise KnowledgeQueryError("条件は10語までです。")
        if len(value) > QUERY_MAX_TERM_LENGTH:
            raise KnowledgeQueryError("条件は1語100文字までです。")
        parsed[key].append(value)
    return parsed
