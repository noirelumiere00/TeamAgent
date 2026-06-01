"""runtime/slack_bot.py のユニットテスト。

mention テキストの parsing と SearchOutput のフォーマットを検証する。
Bolt App 自体の起動テストはネットワーク必須なので含めない。
"""

from __future__ import annotations

from typing import Any

from teamagent.runtime.slack_bot import (
    _asyncio_exception_handler,
    _slack_thread_permalink,
    build_ack_message,
    build_search_blocks,
    format_search_response,
    parse_command_text,
    strip_mention,
)
from teamagent.skills.search.schema import SearchHitOut, SearchOutput


def test_build_ack_message_tiktok() -> None:
    """TikTok 検索意図には TikTok 用の受付文 (ブラウザ収集を明示) を返す。"""
    msg = build_ack_message("TikTokで新宿 ランチ で検索して")
    assert "受け付けました" in msg
    assert "TikTok" in msg


def test_build_ack_message_video() -> None:
    """動画 URL には動画分析の受付文を返す。"""
    msg = build_ack_message("https://www.tiktok.com/@u/video/123 を分析して")
    assert "受け付けました" in msg
    assert "動画" in msg


def test_build_ack_message_search_default() -> None:
    """通常の質問は検索の受付文。"""
    msg = build_ack_message("飲食店のPR事例を教えて")
    assert "受け付けました" in msg
    assert "検索" in msg


def test_build_ack_message_always_acknowledges() -> None:
    """どんな入力でも必ず『受け付けました』を含む (空・記号でも落ちない)。"""
    for s in ["", "   ", "！？", "提案を作って", "マンダムの状況教えて"]:
        assert "受け付けました" in build_ack_message(s)


def test_strip_mention_removes_leading_at() -> None:
    """先頭の `<@USERID> ` を取り除くこと。"""
    assert strip_mention("<@U082ABC> A社の前回提案は？") == "A社の前回提案は？"


def test_strip_mention_handles_multiple_spaces() -> None:
    """`<@USERID>` の後に複数の空白があっても削れること。"""
    assert strip_mention("<@U082ABC>   hello") == "hello"


def test_strip_mention_only_strips_first() -> None:
    """テキスト中の別ユーザー mention は残ること。"""
    assert strip_mention("<@U082ABC> ping <@U999XYZ>") == "ping <@U999XYZ>"


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
    button_sections = [b for b in blocks if b.get("type") == "section" and "accessory" in b]
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


def test_format_response_uses_structured_file_name_and_page() -> None:
    """file_name + page_num が設定されていれば 📄 *file* (p.N) で表示する。"""
    output = SearchOutput(
        answer="ans",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="...",
                score=0.93,
                file_name="proposal_a.pdf",
                page_num=5,
                source=None,
            ),
        ],
        total_cost_usd=0.0,
    )
    formatted = format_search_response(output)
    assert "📄 *proposal_a.pdf*" in formatted
    assert "(p.5)" in formatted
    assert "score=0.93" in formatted


def test_format_response_falls_back_to_source_string() -> None:
    """file_name が無いが source だけある旧形式でも壊れない。"""
    output = SearchOutput(
        answer="ans",
        hits=[SearchHitOut(chunk_id=1, content="...", score=0.7, source="legacy.pdf (p.2)")],
        total_cost_usd=0.0,
    )
    formatted = format_search_response(output)
    assert "legacy.pdf (p.2)" in formatted


def test_build_blocks_uses_file_name_page() -> None:
    """Block Kit でも file_name / page_num が構造化表示される。"""
    output = SearchOutput(
        answer="x",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="...",
                score=0.88,
                file_name="b.pdf",
                page_num=3,
                drive_url=None,
            )
        ],
        total_cost_usd=0.0,
    )
    blocks = build_search_blocks(output)
    section_texts = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and isinstance(b.get("text"), dict)
    ]
    assert any("📄 *b.pdf*" in t and "(p.3)" in t for t in section_texts)


# -----------------------------------------------------------
# _slack_thread_permalink
# -----------------------------------------------------------
def test_slack_thread_permalink_generates_url(monkeypatch: Any) -> None:
    """SLACK_WORKSPACE が設定されているとき permalink が生成される。"""
    monkeypatch.setenv("SLACK_WORKSPACE", "vectorinc")
    url = _slack_thread_permalink("slack://C091ZSVTKF1/1748244936.050099")
    assert url == "https://vectorinc.slack.com/archives/C091ZSVTKF1/p1748244936050099"


def test_slack_thread_permalink_no_domain_returns_none(monkeypatch: Any) -> None:
    """SLACK_WORKSPACE 未設定のとき None を返す。"""
    monkeypatch.delenv("SLACK_WORKSPACE", raising=False)
    assert _slack_thread_permalink("slack://C091ZSVTKF1/1748244936.050099") is None


def test_slack_thread_permalink_non_slack_uri_returns_none(monkeypatch: Any) -> None:
    """slack:// でない URI は None を返す。"""
    monkeypatch.setenv("SLACK_WORKSPACE", "vectorinc")
    assert _slack_thread_permalink("gdrive://abc123") is None


def test_build_search_blocks_slack_source_shows_thread_button(monkeypatch: Any) -> None:
    """source_type='slack' + SLACK_WORKSPACE 設定済みのとき Slack ボタンが生成される。"""
    monkeypatch.setenv("SLACK_WORKSPACE", "vectorinc")
    output = SearchOutput(
        answer="Slack から見つかりました",
        hits=[
            SearchHitOut(
                chunk_id=99,
                content="...",
                score=0.92,
                source="#proj-ナレッジ共有",
                source_type="slack",
                source_uri="slack://C091ZSVTKF1/1748244936.050099",
                channel_name="#proj-ナレッジ共有",
            ),
        ],
        total_cost_usd=0.0,
    )
    blocks = build_search_blocks(output)

    button_sections = [b for b in blocks if b.get("type") == "section" and "accessory" in b]
    assert len(button_sections) == 1
    btn = button_sections[0]["accessory"]
    assert btn["type"] == "button"
    assert "C091ZSVTKF1" in btn["url"]
    assert btn["text"]["text"] == "💬 Slack で開く"


def test_format_search_response_slack_source_shows_channel(monkeypatch: Any) -> None:
    """source_type='slack' のとき 💬 channel_name で表示される。"""
    monkeypatch.delenv("SLACK_WORKSPACE_DOMAIN", raising=False)
    output = SearchOutput(
        answer="ans",
        hits=[
            SearchHitOut(
                chunk_id=1,
                content="...",
                score=0.9,
                source_type="slack",
                channel_name="#proj-ナレッジ共有",
                source_uri="slack://C091ZSVTKF1/1748244936.050099",
            )
        ],
        total_cost_usd=0.0,
    )
    formatted = format_search_response(output)
    assert "💬" in formatted
    assert "#proj-ナレッジ共有" in formatted


# -----------------------------------------------------------
# parse_command_text — /teamagent_search のオプション抽出
# -----------------------------------------------------------
def test_parse_command_text_pure_query() -> None:
    """key=value が無いケース: 文字列はそのまま query、options は空。"""
    assert parse_command_text("飲食店PR事例") == ("飲食店PR事例", {})


def test_parse_command_text_with_industry() -> None:
    """末尾の industry=飲食 が options に行き、query から外れる。"""
    assert parse_command_text("案件 industry=飲食") == ("案件", {"industry": "飲食"})


def test_parse_command_text_with_top_k() -> None:
    """top_k=10 もオプションとして抽出される（値は str のまま）。"""
    q, opts = parse_command_text("案件 top_k=10")
    assert q == "案件"
    assert opts == {"top_k": "10"}


def test_parse_command_text_options_first() -> None:
    """先頭にオプションがあっても query が正しく残る（順不同許容）。"""
    q, opts = parse_command_text("industry=飲食 top_k=10 飲食店PR事例")
    assert q == "飲食店PR事例"
    assert opts == {"industry": "飲食", "top_k": "10"}


def test_parse_command_text_quoted_value_with_space() -> None:
    """\"...\" でクォートされた値の中の空白は値の一部として扱う。"""
    q, opts = parse_command_text('industry="飲食 業界" 案件')
    assert opts["industry"] == "飲食 業界"
    assert q == "案件"


def test_parse_command_text_unknown_key_stays_in_query() -> None:
    """ホワイトリスト外のキー (foo=bar) は options に取らず query に残す。"""
    q, opts = parse_command_text("foo=bar 案件 industry=飲食")
    assert "foo=bar" in q
    assert opts == {"industry": "飲食"}


def test_parse_command_text_empty_string() -> None:
    """空文字を渡しても落ちず ('', {}) を返す。"""
    assert parse_command_text("") == ("", {})


def test_parse_command_text_only_options_returns_empty_query() -> None:
    """オプションだけ渡された場合 query は空文字（呼び出し側で「使い方」を返す）。"""
    q, opts = parse_command_text("industry=飲食 top_k=5")
    assert q == ""
    assert opts == {"industry": "飲食", "top_k": "5"}


# -----------------------------------------------------------
# asyncio exception handler — Sentry no-op 経路
# -----------------------------------------------------------
def test_asyncio_exception_handler_no_init_no_raise() -> None:
    """Sentry 未 init 状態でも _asyncio_exception_handler が例外を投げない。

    context.exception が BaseException でも None でも安全であることを確認。
    """

    class _DummyLoop:
        """asyncio loop の最低限スタブ（context.message ログにしか触らない）。"""

    loop: Any = _DummyLoop()

    # 1) exception を伴うケース
    _asyncio_exception_handler(
        loop, {"message": "task fail", "exception": RuntimeError("fake-token-leak-test")}
    )

    # 2) exception 無しのケース（message だけ）
    _asyncio_exception_handler(loop, {"message": "no exc"})

    # 3) 空 context
    _asyncio_exception_handler(loop, {})
