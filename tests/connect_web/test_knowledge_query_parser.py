"""条件検索 parser と静的 Vault の対応構文の互換性。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from teamagent.knowledge_query import KnowledgeQueryError, parse_knowledge_query

QUERIES = [
    "",
    "Aico 食品 -下書き",
    '"引用 句" file:提案 -file:下書き',
    'tag:食品 -tag:議事録 tag:"Case Study" -file:"Old Copy"',
    '"tag:食品" - x',
    'file:"" tag:"" -file:""',
    "TAG:食品 -FILE:x",
    r"50% A_B C:\資料",
    "ＦＵＬＬＷＩＤＴＨ　日本語",
    'file:foo""',
    "tag:業界/食品",
    'file:提案 -語 "日本 ガイシ" tag:食品 -tag:IT',
]


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


@pytest.mark.skipif(shutil.which("node") is None, reason="static parser parity requires node")
def test_same_decomposition_as_actual_app_parse_query() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts/build_app_html.py").read_text()
    parser = source[source.index("function parseQuery(q)") : source.index("\nfunction tagMatch")]
    script = (
        parser
        + "\nconst fs=require('node:fs');process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(parseQuery)));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(QUERIES),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    for query, actual in zip(QUERIES, json.loads(result.stdout), strict=True):
        parsed = parse_knowledge_query(query)
        assert parsed == {key: actual[key] for key in parsed}
        assert all(actual[key] == [] for key in ("path", "npath", "props", "regex"))


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
