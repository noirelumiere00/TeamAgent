"""Block Kit の部品（_shared/slack_blocks.py）のテスト。

確かめること:
- 第三者の文字列は ``<!here>``・``<@U…>``・``<url|偽名>`` を作れない（``& < >`` は実体参照）
- リンクにできるのは https の自前 URL と、許可ホストの SNS 投稿 URL だけ
- Block Kit の上限（50 blocks・section 3000 字・field 2000 字×10・header 150 字・context 10）を
  超えない。切るときはリンクと実体参照の途中で切らない。溢れても末尾（レポートのリンク）は残す
- メッセージ全体の大きさ（text object の合計 12,000 字＝msg_blocks_too_long の手前）も超えない
- 最上位の text（スクリーンリーダーは blocks を読まずこれだけを読む）に blocks の全文が入り、
  4,000 字を超えるときもどの節も頭の行とレポートのリンクは残る
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
    # 公式には非公開（msg_blocks_too_long）。約 13,200 字で弾かれた報告より小さく取る
    assert sb.MAX_TOTAL_TEXT == 12_000
    # chat.postMessage の text は 4,000 字以内が推奨
    assert sb.MAX_FALLBACK_TEXT == 4000


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
    assert blocks[0]["text"]["text"].endswith("…")
    sb.validate(blocks[:1] + blocks[3:])
    # block ごとには上限の内側でも、合計が大きすぎれば弾く（Slack の msg_blocks_too_long）
    with pytest.raises(ValueError, match="total"):
        sb.validate(blocks)


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


def test_assemble_keeps_the_total_within_the_message_budget() -> None:
    """block ごとの上限の内側でも、合計が 12,000 字を超えるなら後ろの block を削る。"""
    head = [sb.header("見出し"), *[sb.section(f"節{i}\n" + "あ" * 2900) for i in range(10)]]
    tail = [sb.section("<https://s3.example/r|レポートを開く>"), sb.context("概算 $0.1")]
    blocks = sb.assemble(head, tail)
    assert sb.text_size(blocks) <= sb.MAX_TOTAL_TEXT
    assert blocks[-2:] == tail  # レポートのリンクは残す
    assert blocks[-3] == sb.context(sb.MORE_IN_REPORT)
    assert blocks[:4] == head[:4]  # 前から順に残す
    assert sb.text_size(sb.assemble(head[:3], tail)) == sb.text_size(head[:3] + tail)


def test_assemble_does_not_end_the_body_with_a_divider() -> None:
    head = [sb.section("あ" * 3000) for _ in range(3)] + [sb.divider()]
    head += [sb.section("い" * 3000) for _ in range(3)]  # 4 つ目の節で 12,000 字を超える
    blocks = sb.assemble(head, [sb.context("概算 $0.1")])
    assert [b["type"] for b in blocks] == ["section"] * 3 + ["context", "context"]


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
    with pytest.raises(ValueError, match="total"):
        sb.validate([sb.section("x" * 3000) for _ in range(5)])  # 15,000 字


# ── 最上位の text（スクリーンリーダー・通知・会話の履歴）───────────────────


def test_block_text_flattens_every_block_kind() -> None:
    assert sb.block_text(sb.header("KW <x> & *y*")) == "KW ＜x＞ &amp; ＊y＊"  # mrkdwn へ入れる
    assert sb.block_text(sb.section("本文", fields=["*題*\n値", "欄2"])) == "本文\n*題* 値\n欄2"
    assert sb.block_text(sb.context("注1", "注2")) == "注1\n注2"
    assert sb.block_text(sb.divider()) == ""


def test_message_text_has_every_block_and_keeps_lead_and_tail() -> None:
    body = [sb.context("TikTok 上位30本"), sb.section("*常連*\n• @a 3枠"), sb.divider()]
    tail = [sb.section("<https://s3.example/r|レポートを開く>"), sb.context("概算 $0.1")]
    text = sb.message_text(["要点"], body, tail)
    assert text == (
        "要点\nTikTok 上位30本\n*常連*\n• @a 3枠\n<https://s3.example/r|レポートを開く>\n概算 $0.1"
    )


def test_message_text_clips_long_sections_fairly_and_adds_lost_post_urls() -> None:
    url = "https://www.tiktok.com/@a/video/1"
    body = [sb.section(f"*節{i}*\n" + "あ" * 1500) for i in range(6)]
    body.append(sb.section("*上位*\n" + "い" * 1500 + f"<{url}|1位>"))  # リンクは節の末尾
    tail = [sb.section("<https://s3.example/r|レポートを開く>"), sb.context("概算 $0.1")]
    text = sb.message_text(["要点"], body, tail, urls=[("1位", url)])
    assert len(text) <= sb.MAX_FALLBACK_TEXT
    assert text.startswith("要点\n")
    assert all(f"*節{i}*" in text for i in range(6)) and "*上位*" in text  # どの節も頭は残す
    assert text.endswith("<https://s3.example/r|レポートを開く>\n概算 $0.1")
    assert text.count("<") == text.count(">")  # リンクの途中で切らない
    # 投稿の URL が本文から削れたら最後に足す（会話の履歴から辿れるように）
    assert f"<{url}|1位>" not in text
    assert f"上位の投稿: 1位 <{url}>" in text
    # 削れていなければ足さない（二重に並べない）
    small = sb.message_text(["要点"], [sb.section(f"<{url}|1位>")], tail, urls=[("1位", url)])
    assert "上位の投稿" not in small


def test_message_text_with_a_huge_tail_still_fits() -> None:
    long_url = "https://s3.example/x?X-Amz-Security-Token=" + "A" * 2700
    tail = [sb.section(f"<{long_url}|レポートを開く>"), sb.section(f"<{long_url}|スライドを開く>")]
    text = sb.message_text(["要点"], [sb.section("本文")], tail)
    assert len(text) <= sb.MAX_FALLBACK_TEXT
    assert text.startswith("要点") and text.count("<") == text.count(">")


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
