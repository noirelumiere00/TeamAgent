"""Block Kit の直接投稿を確かめる共通の検査（A/B/C と配送の経路のテストで使う）。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from teamagent.skills._shared.slack_blocks import (
    MAX_BLOCKS,
    MAX_CONTEXT_ELEMENTS,
    MAX_FIELD_TEXT,
    MAX_FIELDS,
    MAX_HEADER_TEXT,
    MAX_SECTION_TEXT,
)

# mrkdwn の山かっこの列（リンク・メンション・特殊コマンド）。自前のリンクだけが残るはず。
_ANGLE = re.compile(r"<([^<>]*)>")
# 本番以外の注記（設計の見本だけにあった言い回し）と、使わないと決めた断定語。
FORBIDDEN_WORDS = ("勝ち筋", "空白:", "打ち手", "最有力", "見本", "samples.md", "〔", "未掲載")


def mrkdwn_objects(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict) and text.get("type") == "mrkdwn":
            out.append(text)
        out += [f for f in block.get("fields", []) if f.get("type") == "mrkdwn"]
        out += [e for e in block.get("elements", []) if e.get("type") == "mrkdwn"]
    return out


def mrkdwn_texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [o["text"] for o in mrkdwn_objects(blocks)]


def header_texts(blocks: list[dict[str, Any]]) -> list[str]:
    return [b["text"]["text"] for b in blocks if b["type"] == "header"]


def body(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(header_texts(blocks) + mrkdwn_texts(blocks))


def angle_targets(text: str) -> list[str]:
    """``<…>`` の中身（リンクの宛先 URL）。表示名つきなら ``|`` の前だけ。"""
    return [m.group(1).split("|", 1)[0] for m in _ANGLE.finditer(text)]


def assert_only_own_links(texts: Iterable[str], allowed: set[str]) -> None:
    """山かっこは自前のリンクだけ（メンション・特殊コマンド・偽装リンクが無い）。"""
    for text in texts:
        assert "<!" not in text and "<@" not in text and "<#" not in text, text
        for target in angle_targets(text):
            assert target in allowed, (target, text)
        # 開きと閉じの数がそろう（エスケープ漏れの ``<`` が残っていない）
        assert text.count("<") == text.count(">") == len(angle_targets(text)), text


def assert_no_markdown(texts: Iterable[str]) -> None:
    """標準 Markdown の記法（``**太字**``・``[名](url)``・行頭の ``- ``）が出ていない。"""
    for text in texts:
        assert "**" not in text, text
        assert "](" not in text, text
        assert not any(line.startswith("- ") for line in text.splitlines()), text


def assert_bold_is_closed_before_space(texts: Iterable[str]) -> None:
    """太字 ``*語*`` の閉じの直後は改行・半角空白・文末だけ（和文が続くと太字にならない）。"""
    for text in texts:
        stars = [i for i, ch in enumerate(text) if ch == "*"]
        assert len(stars) % 2 == 0, text
        for open_i, close_i in zip(stars[::2], stars[1::2], strict=True):
            assert open_i == 0 or text[open_i - 1] in " \n", text
            after = text[close_i + 1 : close_i + 2]
            assert after in ("", " ", "\n"), (after, text)


def assert_limits(blocks: list[dict[str, Any]]) -> None:
    """Block Kit の上限（Slack の仕様）。"""
    assert 0 < len(blocks) <= MAX_BLOCKS
    for block in blocks:
        if block["type"] == "header":
            assert block["text"]["type"] == "plain_text"
            assert 0 < len(block["text"]["text"]) <= MAX_HEADER_TEXT
        elif block["type"] == "section":
            assert len(block["text"]["text"]) <= MAX_SECTION_TEXT
            assert len(block.get("fields", [])) <= MAX_FIELDS
            assert all(len(f["text"]) <= MAX_FIELD_TEXT for f in block.get("fields", []))
        elif block["type"] == "context":
            assert 0 < len(block["elements"]) <= MAX_CONTEXT_ELEMENTS
        else:
            assert block["type"] == "divider"
        assert "accessory" not in block and block["type"] != "actions"  # ボタンは使わない


def assert_verbatim(blocks: list[dict[str, Any]]) -> None:
    for obj in mrkdwn_objects(blocks):
        assert obj["verbatim"] is True, obj


def assert_well_formed(blocks: list[dict[str, Any]], allowed_links: set[str]) -> None:
    """(1)(2)(4) と verbatim・太字の決まりをまとめて確かめる。"""
    texts = mrkdwn_texts(blocks)
    assert_limits(blocks)
    assert_verbatim(blocks)
    assert_no_markdown(texts)
    assert_only_own_links(texts, allowed_links)
    assert_bold_is_closed_before_space(texts)
    whole = body(blocks)
    for word in FORBIDDEN_WORDS:
        assert word not in whole, word


__all__ = [
    "FORBIDDEN_WORDS",
    "angle_targets",
    "assert_bold_is_closed_before_space",
    "assert_limits",
    "assert_no_markdown",
    "assert_only_own_links",
    "assert_verbatim",
    "assert_well_formed",
    "body",
    "header_texts",
    "mrkdwn_texts",
]
