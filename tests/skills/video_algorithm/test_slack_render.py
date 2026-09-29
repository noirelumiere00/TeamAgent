"""動画分析の完了の直接投稿（Block Kit）のテスト。本番の形のデータ（C・13:51）で組む。

確かめること:
(1) 生の記法（``**``・``[名](url)``・行頭の「- 」）が出ない (2) リンクは自前の URL だけ
(3) 第三者の文字列（KW・Gemini の出力・アカウント名）が無害化される (4) 上限の内側
(5) 最上位の text（スクリーンリーダーは blocks を読まずこれだけを読む）に blocks の全文
あわせて、共通点に本数と段階（4/5本・多数派）と選び方（6 割以上・全体でも多い点は除く）を付けること・
「最も多く見られた」と言い切らないこと・下位からの繰上げと分析できなかった本数を書くこと・
「平均保存率」の語（cross.avg_save_rate は平均）・Gemini の自由記述（cross.summary）と「勝ち筋」の
二重表記を出さないことを固定する。
"""

from __future__ import annotations

import json
import statistics

from teamagent.skills._shared.slack_blocks import MAX_FALLBACK_TEXT, block_text
from teamagent.skills.video_algorithm.schema import VideoAlgorithmOutput, WinFactor
from teamagent.skills.video_algorithm.slack_render import (
    FACTOR_RULE,
    NO_ANALYZED,
    NO_FACTOR,
    REPORT_FAILED,
    completion_message,
)
from tests.skills._shared.slack_blocks_checks import (
    assert_limits,
    assert_only_own_links,
    assert_well_formed,
    body,
    header_texts,
    mrkdwn_texts,
)
from tests.skills.search_surface_check import slack_prod_shape as shape


def _links(out: VideoAlgorithmOutput) -> set[str]:
    return {v.meta.url for v in out.videos} | {shape.C_REPORT, shape.C_SLIDES}


def test_fixture_averages_match_the_rows() -> None:
    """見本の平均保存率・尺の中央値は、並べた 5 本の値から計算した値と同じ（実物の 0.806%・59秒）。"""
    out = shape.c_output()
    rates = [v.meta.save_rate() for v in out.videos]
    assert round(statistics.mean(rates), 3) == out.cross.avg_save_rate == 0.806
    durations = [v.analysis.duration_sec for v in out.videos if v.analysis]
    assert statistics.median(durations) == out.cross.median_duration_sec == 59.0


def test_c_is_well_formed_block_kit() -> None:
    out = shape.c_output()
    msg = completion_message(out)
    assert msg is not None
    assert_well_formed(msg.blocks, _links(out))
    assert header_texts(msg.blocks) == [f"VSEO動画アルゴリズム分析「{shape.KEYWORD}」"]


def test_c_about_line_has_counts_and_the_correlation_caveat() -> None:
    msg = completion_message(shape.c_output())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        "TikTok 検索上位から5本（分析できた 5本）・n=5 の観測（相関であって因果ではありません）"
    )


def test_c_factor_has_count_stage_and_how_it_was_chosen() -> None:
    """共通点は「6 割以上に出た点（全体でも多い点は除く）」なので「最も多く」とは言い切らない。"""
    msg = completion_message(shape.c_output())
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    assert (
        ":mag: *上位に多く見られた共通点*\n• テロップ(焼き込み)に検索KWが出る（4/5本・多数派）\n"
        f"{FACTOR_RULE}" in texts
    )
    assert "最も多く" not in body(msg.blocks) and "最も多く" not in msg.text
    numbers = next(b for b in msg.blocks if b.get("fields"))
    assert numbers["text"]["text"] == ":bar_chart: *分析できた5本の数字*"
    fields = numbers["fields"]
    assert [f["text"] for f in fields] == [
        "*平均エンゲージメント率*\n2.75%",
        "*平均保存率*\n0.806%",
        "*尺の中央値*\n59秒",
        "*分析できた本数*\n5/5本",
    ]
    whole = body(msg.blocks)
    assert "勝ち筋" not in whole and "最有力" not in whole
    assert "尺中央値 59.0秒。" not in whole  # Gemini ではなく横断集計の自由記述も出さない


def test_c_lists_analyzed_videos_with_links_and_report_links() -> None:
    msg = completion_message(shape.c_output())
    assert msg is not None
    top = next(t for t in mrkdwn_texts(msg.blocks) if "*対象の5本*" in t)
    assert top.splitlines()[1] == (
        "*1位* <https://www.tiktok.com/@gonosara/video/7165798692291644674|@gonosara>"
        " 35.1万回・保存0.80%・59秒"
    )
    texts = mrkdwn_texts(msg.blocks)
    # 長い署名 URL が並んでも 3000 字に収まるよう、リンクは 1 つずつ別の section
    assert (
        f":page_facing_up: <{shape.C_REPORT}|詳細レポートを開く>"
        " （タイムライン・テロップ位置・ブランド検出ほか）" in texts
    )
    assert f":pencil2: <{shape.C_SLIDES}|編集用スライドを開く> （ブラウザで直接編集）" in texts
    assert texts[-1] == "概算 $0.4176"


def test_c_long_presigned_urls_each_get_their_own_section() -> None:
    long_url = "https://bucket.s3.amazonaws.com/vseo-proposals/x.pptx?X-Amz-Security-Token=" + (
        "A" * 2500
    )
    out = shape.c_output().model_copy(update={"pptx_url": long_url, "slides_url": long_url})
    msg = completion_message(out)
    assert msg is not None
    assert_limits(msg.blocks)
    assert sum(long_url in t for t in mrkdwn_texts(msg.blocks)) == 2


def test_c_text_has_everything_in_the_blocks() -> None:
    out = shape.c_output()
    msg = completion_message(out)
    assert msg is not None
    assert msg.text.splitlines()[0] == (
        f"VSEO動画アルゴリズム分析「{shape.KEYWORD}」完了（5本／分析成功5本）: "
        "上位に多く見られた共通点は『テロップ(焼き込み)に検索KWが出る（4/5本・多数派）』"
    )
    assert len(msg.text) <= MAX_FALLBACK_TEXT
    for block in msg.blocks[1:]:
        flat = block_text(block)
        if flat:
            assert flat in msg.text, flat
    for needle in ("*平均保存率* 0.806%", "*尺の中央値* 59秒", "*平均エンゲージメント率* 2.75%"):
        assert needle in msg.text
    assert f"<{shape.C_REPORT}|詳細レポートを開く>" in msg.text
    assert all(f"<{v.meta.url}|" in msg.text for v in out.videos)
    assert_only_own_links([msg.text], _links(out))


def test_c_backfilled_videos_are_counted_and_not_called_top_n() -> None:
    """分析できなかった上位を下位で繰り上げたら、その本数を書く（元の文面の「下位繰上げ」）。"""
    msg = completion_message(shape.c_backfilled_output())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        "TikTok 検索上位から5本（分析できた 5本・うち下位からの繰上げ 1本）"
        "・n=5 の観測（相関であって因果ではありません）"
    )
    assert msg.text.splitlines()[0].startswith(
        f"VSEO動画アルゴリズム分析「{shape.KEYWORD}」完了（5本／分析成功5本・下位繰上げ1本）"
    )
    top = next(t for t in mrkdwn_texts(msg.blocks) if "*対象の5本*" in t).splitlines()
    assert [line.split("*")[1] for line in top[1:]] == ["1位", "2位", "3位", "5位", "6位"]


def test_c_one_failed_video_is_not_counted_as_analyzed() -> None:
    msg = completion_message(shape.c_one_failed_output())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        "TikTok 検索上位から5本（分析できた 4本）・n=4 の観測（相関であって因果ではありません）"
    )
    numbers = next(b for b in msg.blocks if b.get("fields"))
    assert numbers["text"]["text"] == ":bar_chart: *分析できた4本の数字*"
    assert numbers["fields"][-1]["text"] == "*分析できた本数*\n4/5本"
    top = next(t for t in mrkdwn_texts(msg.blocks) if "*対象の5本*" in t).splitlines()
    assert top[4] == (
        "*4位* <https://www.tiktok.com/@sample_influencer4/video/7400000000000000204"
        "|@sample_influencer4>　分析できませんでした"
    )


def test_c_nothing_analyzed_does_not_pretend_to_have_findings() -> None:
    out = shape.c_none_analyzed_output()
    # 横断の集計に共通点が残っていても（作り直し・古い出力）、0 本なら出さない
    out.cross.win_factors = shape.c_output().cross.win_factors
    msg = completion_message(out)
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    assert f":mag: *上位に多く見られた共通点*\n{NO_ANALYZED}" in texts
    numbers = next(b for b in msg.blocks if b.get("fields"))
    assert numbers["text"]["text"] == ":bar_chart: *数字（分析できた動画がありません）*"
    assert [f["text"] for f in numbers["fields"]] == ["*分析できた本数*\n0/5本"]
    whole = body(msg.blocks)
    assert NO_FACTOR not in whole and "平均" not in whole and "n=" not in whole
    assert "テロップ(焼き込み)" not in whole and "テロップ(焼き込み)" not in msg.text


def test_c_notes_quota_and_search_volume() -> None:
    out = shape.c_output().model_copy(
        update={"quota_note": "今月の残りは 3 本です", "search_volume": 12_000}
    )
    msg = completion_message(out)
    assert msg is not None
    notes = next(b for b in msg.blocks if b["type"] == "context" and "月間検索量" in block_text(b))
    assert [e["text"] for e in notes["elements"]] == [
        "今月の残りは 3 本です",
        "月間検索量（手動実測）: 12,000",
    ]


def test_c_missing_report_is_said_in_words_without_a_link() -> None:
    out = shape.c_output().model_copy(update={"report_url": None, "slides_url": None})
    msg = completion_message(out)
    assert msg is not None
    report = next(t for t in mrkdwn_texts(msg.blocks) if t.startswith(":page_facing_up:"))
    assert report == f":page_facing_up: {REPORT_FAILED}"
    assert REPORT_FAILED in msg.text and "<https://connect" not in msg.text


def test_c_without_factors_says_so() -> None:
    out = shape.c_output()
    out.cross.win_factors = []
    msg = completion_message(out)
    assert msg is not None
    assert f":mag: *上位に多く見られた共通点*\n{NO_FACTOR}" in mrkdwn_texts(msg.blocks)


def test_c_more_factors_are_listed_with_counts() -> None:
    out = shape.c_output()
    out.cross.win_factors.append(WinFactor(factor="冒頭3秒にテロップ", observed_in=3, total=5))
    msg = completion_message(out)
    assert msg is not None
    assert "• 冒頭3秒にテロップ（3/5本・多数派）" in body(msg.blocks)


def test_c_hostile_strings_are_neutralized() -> None:
    out = shape.hostile_c()
    msg = completion_message(out)
    assert msg is not None
    allowed = {v.meta.url for v in out.videos if "evil" not in v.meta.url} | {
        shape.C_REPORT,
        shape.C_SLIDES,
    }
    assert_well_formed(msg.blocks, allowed)
    assert_only_own_links([msg.text], allowed)
    raw = json.dumps(msg.payload(), ensure_ascii=False)
    for bad in ("<!here>", "<!channel>", "<@U0EVIL", "<https://evil", "evil.example/@x"):
        assert bad not in raw, bad


def test_c_is_none_for_unexpected_or_empty_outputs() -> None:
    assert completion_message(object()) is None
    assert completion_message(shape.c_output().model_copy(update={"videos": []})) is None


def test_c_ten_videos_stay_within_limits() -> None:
    out = shape.c_output()
    out.videos = [v.model_copy(deep=True) for v in out.videos * 2]
    for i, v in enumerate(out.videos):
        v.meta.rank = i + 1
    msg = completion_message(out)
    assert msg is not None
    assert_limits(msg.blocks)
