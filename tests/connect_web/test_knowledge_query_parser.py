"""条件検索 parser の実測済みゴールデン値と未対応構文の検証。"""

from __future__ import annotations

import pytest

from teamagent.knowledge_query import KnowledgeQueryError, parse_knowledge_query


def test_expected_decomposition() -> None:
    assert parse_knowledge_query(
        'Ａ -Ｂ "引用 句" tag:食品 -tag:IT file:"Case Study" -file:旧'
    ) == {
        "plain": ["ａ", "引用 句"],
        "neg": ["ｂ"],
        "tag": ["食品"],
        "ntag": ["it"],
        "file": ["case study"],
        "nfile": ["旧"],
    }


# Captured from the static JavaScript parser before removing the runtime dependency.
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {"plain": [], "neg": [], "tag": [], "file": [], "ntag": [], "nfile": []}),
        (
            "Aico 食品 -下書き",
            {
                "plain": ["aico", "食品"],
                "neg": ["下書き"],
                "tag": [],
                "file": [],
                "ntag": [],
                "nfile": [],
            },
        ),
        (
            '"引用 句" file:提案 -file:下書き',
            {
                "plain": ["引用 句"],
                "neg": [],
                "tag": [],
                "file": ["提案"],
                "ntag": [],
                "nfile": ["下書き"],
            },
        ),
        (
            'tag:食品 -tag:議事録 tag:"Case Study" -file:"Old Copy"',
            {
                "plain": [],
                "neg": [],
                "tag": ["食品", "case study"],
                "file": [],
                "ntag": ["議事録"],
                "nfile": ["old copy"],
            },
        ),
        (
            '"tag:食品" - x',
            {"plain": ["tag:食品", "x"], "neg": [], "tag": [], "file": [], "ntag": [], "nfile": []},
        ),
        (
            'file:"" tag:"" -file:""',
            {"plain": [], "neg": [], "tag": [], "file": [], "ntag": [], "nfile": []},
        ),
        (
            "TAG:食品 -FILE:x",
            {
                "plain": ["tag:食品"],
                "neg": ["file:x"],
                "tag": [],
                "file": [],
                "ntag": [],
                "nfile": [],
            },
        ),
        (
            "50% A_B C:\\資料",
            {
                "plain": ["50%", "a_b", "c:\\資料"],
                "neg": [],
                "tag": [],
                "file": [],
                "ntag": [],
                "nfile": [],
            },
        ),
        (
            "ＦＵＬＬＷＩＤＴＨ\u3000日本語",
            {
                "plain": ["ｆｕｌｌｗｉｄｔｈ", "日本語"],
                "neg": [],
                "tag": [],
                "file": [],
                "ntag": [],
                "nfile": [],
            },
        ),
        (
            'file:foo""',
            {"plain": [], "neg": [], "tag": [], "file": ['foo"'], "ntag": [], "nfile": []},
        ),
        (
            "tag:業界/食品",
            {"plain": [], "neg": [], "tag": ["業界/食品"], "file": [], "ntag": [], "nfile": []},
        ),
        (
            'file:提案 -語 "日本 ガイシ" tag:食品 -tag:IT',
            {
                "plain": ["日本 ガイシ"],
                "neg": ["語"],
                "tag": ["食品"],
                "file": ["提案"],
                "ntag": ["it"],
                "nfile": [],
            },
        ),
    ],
)
def test_query_decomposition(query: str, expected: dict[str, list[str]]) -> None:
    assert parse_knowledge_query(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "path:docs",
        "-path:clients",
        "[業種:食品]",
        "[フェーズ:null]",
        "[担当]",
        "/提案.*/",
        r"/a\/b/",
        "/[/",
    ],
)
def test_unsupported_syntax_is_explicit(query: str) -> None:
    with pytest.raises(KnowledgeQueryError, match="この書き方はまだ使えません"):
        parse_knowledge_query(query)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("x" * 301, "300文字"),
        ("x" * 101, "1語100文字"),
        (" ".join(["x"] * 11), "10語"),
        ("x\x00", "制御文字"),
        ("x\n", "制御文字"),
    ],
)
def test_query_limits(query: str, message: str) -> None:
    with pytest.raises(KnowledgeQueryError, match=message):
        parse_knowledge_query(query)


def test_query_limit_boundaries() -> None:
    assert len(parse_knowledge_query(" ".join(["x"] * 10))["plain"]) == 10
    assert parse_knowledge_query("x" * 100)["plain"] == ["x" * 100]
    query = "a" * 100 + " " + "b" * 100 + " " + "c" * 98
    assert len(query) == 300 and len(parse_knowledge_query(query)["plain"]) == 3
