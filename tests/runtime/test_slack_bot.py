"""runtime/slack_bot.py のユニットテスト。

mention テキストの parsing と SearchOutput のフォーマットを検証する。
Bolt App 自体の起動テストはネットワーク必須なので含めない。
"""

from __future__ import annotations

from teamagent.runtime.slack_bot import (
    build_search_blocks,
    format_search_response,
    strip_mention,
)
from teamagent.skills.search.schema import SearchHitOut, SearchOutput


def test_strip_mention_removes_leading_at() -> None:
    """先頭の `<@USERID> ` を取り除くこと。"""
    assert strip_mention("<@U082ABC> A社の前回提案は？") == "A社の前回提案は？"


def test_strip_mention_handles_multiple_spaces() -> None:
    """`<@USERID>` の後に複数の空白があっても削れること。"""
    assert strip_mention("<@U082ABC>   hello") == "hello"


def test_strip_mention_only_strips_first() -> None:
    """テキスト中の別ユーザー mention は残ること。"""
    assert (
        strip_mention("<@U082ABC> ping <@U999XYZ>")
        == "ping <@U999XYZ>"
    )


def test_strip_mention_no_mention_returns_trimmed() -> None:
    """mention 無しでも例外を投げず、前後の空白を取って返す。"""
    assert strip_mention("hello") == "hello"
    assert strip_mention("  spaced  ") == "spaced"


def test_format_search_response_with_hits() -> None:
    """SearchOutput に hits があれば参考資料が列挙される。"""
    output = SearchOutput(
        answer="PR代行は飲食・コスメ業界で実績あり [chunk_id: 1]",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="飲食業の事例詳細...",
                score=0.91,
                source="proposal_drink_2024.pdf (p.3)",
            ),
            SearchHitOut(
                chunk_id=7,
                content="コスメ業の事例詳細...",
                score=0.84,
                source="proposal_cosme.pdf (p.1)",
            ),
        ],
        total_cost_usd=0.0021,
    )
    formatted = format_search_response(output)
    assert "PR代行は飲食・コスメ業界で実績あり" in formatted
    assert "*参考資料:*" in formatted
    assert "proposal_drink_2024.pdf (p.3)" in formatted
    assert "score=0.91" in formatted
    assert "$0.0021" in formatted


def test_format_search_response_no_hits() -> None:
    """SearchOutput.hits が空でも整形できること。"""
    output = SearchOutput(
        answer="該当する資料が見つかりませんでした。",
        hits=[],
        total_cost_usd=0.0,
    )
    formatted = format_search_response(output)
    assert "見つかりませんでした" in formatted
    assert "*参考資料:*" not in formatted
    assert "$0.0000" in formatted


def test_format_search_response_source_fallback_to_chunk_id() -> None:
    """source が None なら chunk #N で表示。"""
    output = SearchOutput(
        answer="ans",
        hits=[
            SearchHitOut(chunk_id=42, content="...", score=0.5, source=None),
        ],
        total_cost_usd=0.0001,
    )
    formatted = format_search_response(output)
    assert "chunk #42" in formatted


def test_format_search_response_includes_drive_link() -> None:
    """drive_url がある場合、text の参考資料行に Slack リンク記法が含まれる。"""
    output = SearchOutput(
        answer="x",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="...",
                score=0.9,
                source="a.pdf",
                drive_url="https://drive.google.com/file/d/abc/view",
            )
        ],
        total_cost_usd=0.0,
    )
    formatted = format_search_response(output)
    assert "<https://drive.google.com/file/d/abc/view|Drive で開く>" in formatted


def test_build_search_blocks_with_drive_url() -> None:
    """drive_url 付き hit から Block Kit のボタン accessory が生成される。"""
    output = SearchOutput(
        answer="検索結果サマリ",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="...",
                score=0.91,
                source="proposal_a.pdf",
                drive_url="https://drive.google.com/file/d/abc/view",
            ),
            SearchHitOut(
                chunk_id=2,
                content="...",
                score=0.80,
                source="proposal_b.pdf",
                drive_url=None,
            ),
        ],
        total_cost_usd=0.01,
    )
    blocks = build_search_blocks(output)

    # 1 つ目のセクションは answer
    assert blocks[0]["type"] == "section"
    assert "検索結果サマリ" in blocks[0]["text"]["text"]

    # drive_url がある hit にはボタン accessory
    button_sections = [
        b
        for b in blocks
        if b.get("type") == "section" and "accessory" in b
    ]
    assert len(button_sections) == 1
    btn = button_sections[0]["accessory"]
    assert btn["type"] == "button"
    assert btn["url"] == "https://drive.google.com/file/d/abc/view"
    assert btn["text"]["text"] == "📎 Drive で開く"


def test_build_search_blocks_without_hits() -> None:
    """hits 0 件でも壊れない（answer + cost のみ）。"""
    output = SearchOutput(answer="該当なし", hits=[], total_cost_usd=0.0)
    blocks = build_search_blocks(output)
    assert blocks[0]["text"]["text"] == "該当なし"
    # divider や参考資料セクションは出ない
    assert not any(b.get("type") == "divider" for b in blocks)
