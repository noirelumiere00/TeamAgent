"""Block Kit の部品（_shared/slack_blocks.py）のテスト。

確かめること:
- 第三者の文字列は ``<!here>``・``<@U…>``・``<url|偽名>`` を作れない（``& < >`` は実体参照）
- リンクにできるのは https の自前 URL と、許可ホストの SNS 投稿 URL だけ
- Block Kit の上限（50 blocks・section 3000 字・field 2000 字×10・header 150 字・context 10）を
  超えない。切るときはリンクと実体参照の途中で切らない。溢れても末尾（レポートのリンク）は残す
- 段階の語は分母から決まる。描画の例外は None（呼び出し側が文字だけの投稿に戻す）
"""

from __future__ import annotations

import pytest

from teamagent.skills._shared import slack_blocks as sb
from tests.skills._shared.slack_blocks_checks import assert_limits


def test_limits_are_slack_block_kit_limits() -> None:
    assert sb.MAX_BLOCKS == 50
    assert sb.MAX_SECTION_TEXT == 3000
    assert sb.MAX_FIELD_TEXT == 2000
    assert sb.MAX_FIELDS == 10
    assert sb.MAX_HEADER_TEXT == 150
    assert sb.MAX_CONTEXT_ELEMENTS == 10


@pytest.mark.parametrize(
    "raw",
    [
        "<!here>",
        "<!channel>",
        "<@U0EVIL0001>",
        "<#C0123|general>",
        "<https://evil.example/login|公式サイト>",
        "a > b < c & d",
    ],
)
def test_esc_cannot_form_control_sequences(raw: str) -> None:
    out = sb.esc(raw)
    assert "<" not in out and ">" not in out
    assert "&lt;" in out or "&gt;" in out or "&amp;" in out
    assert "|" not in out  # 表示名の区切りも作れない


def test_esc_neutralizes_markup_and_newlines_but_keeps_handles() -> None:
    assert sb.esc("*太字* ~消~ `code`\n2行目\u202eRTL") == "＊太字＊ 〜消〜 'code' 2行目RTL"
    assert sb.esc("@spice_koki") == "@spice_koki"  # _ はコピペのために残す
    assert sb.esc("A&B") == "A&amp;B"


def test_link_url_accepts_only_own_https_urls() -> None:
    ok = "https://connect.newstv.co.jp/r/eyJ.abc-_DEF"
    assert sb.link_url(ok) == ok
    assert sb.link_url("https://s3.example/x?a=1&b=2") == "https://s3.example/x?a=1&amp;b=2"
    for bad in (
        "http://connect.newstv.co.jp/r/x",
        "javascript:alert(1)",
        "https://x.example/a b",
        "https://x.example/a|偽名",
        "https://x.example/a>b",
        "https://x.example/全角",
        "https://" + "a" * 2800,
        "https://",
        "",
        None,
    ):
        assert sb.link_url(bad) is None, bad


def test_post_url_allows_only_known_sns_hosts() -> None:
    url = "https://www.tiktok.com/@spice_koki/video/7504619681093799186"
    assert sb.post_url(url) == url
    assert sb.post_url("https://evil.example/@x/video/1") is None
    assert sb.post_url("https://www.tiktok.com/@x/video/1|偽名>") is None


def test_link_escapes_label_and_falls_back_to_text() -> None:
    url = sb.post_url("https://www.tiktok.com/@a/video/1")
    assert sb.link(url, "@a") == "<https://www.tiktok.com/@a/video/1|@a>"
    assert sb.link(url, "<!here>|x") == "<https://www.tiktok.com/@a/video/1|&lt;!here&gt;｜x>"
    assert sb.link(None, "@a") == "@a"


def test_clip_does_not_cut_links_or_entities() -> None:
    link = "<https://www.tiktok.com/@a/video/1|@a>"
    text = "あ" * 20 + link + "い" * 20
    for limit in range(21, len(text)):
        out = sb.clip(text, limit)
        assert len(out) <= limit
        assert out.count("<") == out.count(">")
    assert sb.clip("abc&amp;def", 7) == "abc…"
    assert sb.clip("short", 10) == "short"
    assert sb.clip("一行目\n二行目\n三行目", 6) == "一行目…"


def test_blocks_are_clipped_to_the_limits() -> None:
    blocks = [
        sb.header("見出し" * 100),
        sb.section("本文" * 2000, fields=["欄" * 3000] * 12),
        sb.context(*(["注記" * 2000] * 12)),
        sb.divider(),
    ]
    assert_limits(blocks)
    sb.validate(blocks)
    assert blocks[0]["text"]["text"].endswith("…")


def test_header_is_plain_text_without_angle_brackets() -> None:
    block = sb.header("KW <!here> & *x*")
    assert block["text"] == {"type": "plain_text", "text": "KW ＜!here＞ & *x*", "emoji": False}


def test_assemble_keeps_the_tail_when_the_head_overflows() -> None:
    head = [sb.section(f"行{i}") for i in range(80)]
    tail = [sb.section("<https://s3.example/r|レポートを開く>"), sb.context("概算 $0.1")]
    blocks = sb.assemble(head, tail)
    assert len(blocks) == sb.MAX_BLOCKS
    assert blocks[-2:] == tail
    assert blocks[-3] == sb.context(sb.MORE_IN_REPORT)
    assert sb.assemble([sb.section("a")], tail) == [sb.section("a"), *tail]


def test_validate_rejects_what_slack_would_reject() -> None:
    too_long = {"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3001}}
    with pytest.raises(ValueError):
        sb.validate([too_long])
    with pytest.raises(ValueError):
        sb.validate([sb.divider()] * 51)
    with pytest.raises(ValueError):
        sb.validate([{"type": "actions", "elements": []}])
    with pytest.raises(ValueError):
        sb.validate([])


@pytest.mark.parametrize(
    ("count", "total", "word"),
    [
        (30, 30, "全員に共通"),
        (4, 4, "全員に共通"),
        (3, 4, "多数派"),
        (16, 30, "多数派"),
        (2, 4, "半数"),
        (15, 30, "半数"),
        (1, 4, "少数派"),
        (7, 30, "少数派"),
        (0, 4, "0本"),
        (1, 1, ""),
        (0, 0, ""),
        (5, 4, ""),
    ],
)
def test_stage_words_come_from_the_denominator(count: int, total: int, word: str) -> None:
    assert sb.stage(count, total) == word


def test_measured_at_is_jst_with_minutes() -> None:
    import datetime as dt

    epoch = int(dt.datetime(2026, 9, 28, 8, 0, tzinfo=dt.UTC).timestamp())
    assert sb.measured_at(epoch) == "2026-09-28 17:00"
    assert sb.measured_at(0) == ""


def test_render_or_none_turns_exceptions_into_none() -> None:
    def boom() -> sb.RichMessage | None:
        raise RuntimeError("render failed")

    assert sb.render_or_none(boom, request_id="r", kind="x") is None
    msg = sb.RichMessage(text="t", blocks=[sb.section("a")])
    assert sb.render_or_none(lambda: msg, request_id="r", kind="x") is msg


def test_count_ja_matches_the_report_wording() -> None:
    from teamagent.skills.search_surface_check.display import fmt_count

    for n in (0, 493, 2_930, 66_000, 351_000, 4_120_000, 150_000_000):
        assert sb.count_ja(n) == fmt_count(n)
