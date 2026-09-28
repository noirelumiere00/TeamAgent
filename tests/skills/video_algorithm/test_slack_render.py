"""動画分析の完了の直接投稿（Block Kit）のテスト。本番の形のデータ（C・13:51）で組む。

確かめること:
(1) 生の記法（``**``・``[名](url)``・行頭の「- 」）が出ない (2) リンクは自前の URL だけ
(3) 第三者の文字列（KW・Gemini の出力・アカウント名）が無害化される (4) 上限の内側
(5) 通知文に結論（最も多く見られた共通点）とレポートの URL
あわせて、共通点に本数と段階（4/5本・多数派）を付けること・「平均保存率」の語（cross.avg_save_rate
は平均）・Gemini の自由記述（cross.summary）と「勝ち筋」の二重表記を出さないことを固定する。
"""

from __future__ import annotations

import json

from teamagent.skills.video_algorithm.schema import VideoAlgorithmOutput, WinFactor
from teamagent.skills.video_algorithm.slack_render import (
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


def test_c_is_well_formed_block_kit() -> None:
    out = shape.c_output()
    msg = completion_message(out)
    assert msg is not None
    assert_well_formed(msg.blocks, _links(out))
    assert header_texts(msg.blocks) == [f"VSEO動画アルゴリズム分析「{shape.KEYWORD}」"]


def test_c_factor_has_count_and_stage_and_numbers_are_labeled() -> None:
    msg = completion_message(shape.c_output())
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    assert (
        ":mag: *最も多く見られた共通点*\nテロップ(焼き込み)に検索KWが出る（4/5本・多数派）" in texts
    )
    fields = next(b for b in msg.blocks if b.get("fields"))["fields"]
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
    top = next(t for t in mrkdwn_texts(msg.blocks) if "*分析した上位5本*" in t)
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


def test_c_fallback_text() -> None:
    out = shape.c_output()
    msg = completion_message(out)
    assert msg is not None
    assert msg.text.splitlines()[0] == (
        f"VSEO動画アルゴリズム分析「{shape.KEYWORD}」完了（上位5本／分析成功5本）: "
        "最も多く見られた共通点は『テロップ(焼き込み)に検索KWが出る（4/5本・多数派）』"
    )
    assert f"詳細レポート: <{shape.C_REPORT}>" in msg.text
    assert_only_own_links([msg.text], _links(out))


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
    assert f":mag: *共通点*\n{NO_FACTOR}" in mrkdwn_texts(msg.blocks)


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
