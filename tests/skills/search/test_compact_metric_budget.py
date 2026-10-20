"""数字欄の掲載行だけに出典を付け、コードの注意書きも本文予算へ含める。"""

from dataclasses import replace

from teamagent.skills.search.campaign_files import VIDEO_METRICS_HEADING
from teamagent.skills.search.compact import build, render
from teamagent.skills.search.result_guard import WEAK_RESULT_NOTICE
from tests.skills.search.test_campaign_files import _video, _video_campaign


def metric_block(label: str, url: str) -> str:
    return f"{label}\n1. 1,000 再生・保存 10　投稿文 {url}"


def test_no_video_rows_means_no_unused_metric_source():
    answer = render("", {"1": ("資料1", "https://example.test/source1", "")}, metric_keys=["1"])
    assert "出典:" not in answer
    assert "📊" not in answer


def test_oversized_video_row_drops_its_heading_and_source_together():
    answer = render(
        "",
        {"1": ("資料1", "https://example.test/source1", "")},
        metrics=VIDEO_METRICS_HEADING
        + "\n"
        + metric_block("施策A", "https://example.test/" + "x" * 2600),
        metric_keys=["1"],
    )
    assert len(answer) <= 2500
    assert "📊" not in answer and "施策A" not in answer
    assert "出典:" not in answer


def test_budget_keeps_only_sources_of_campaigns_with_visible_video_rows():
    answer = render(
        "",
        {
            "1": ("掲載元", "https://example.test/source1", ""),
            "2": ("非掲載元", "https://example.test/source2", ""),
        },
        metrics=VIDEO_METRICS_HEADING
        + "\n"
        + metric_block("掲載施策", "https://example.test/video1")
        + "\n\n"
        + metric_block("非掲載施策", "https://example.test/" + "x" * 2600),
        metric_keys=["1", "2"],
    )
    assert "掲載施策" in answer and "source1" in answer
    assert "非掲載施策" not in answer and "非掲載元" not in answer and "source2" not in answer
    assert "https://example.test/video1" in answer
    assert len(answer) <= 2500


def test_metric_source_cannot_displace_three_already_used_body_sources():
    sources = {
        str(i): (f"資料{i}", f"https://example.test/source{i}", "内容です。") for i in range(1, 5)
    }
    answer = render(
        "内容です。[S:1]\n内容です。[S:2]\n内容です。[S:3]",
        sources,
        metrics=VIDEO_METRICS_HEADING
        + "\n"
        + metric_block("別施策", "https://example.test/video4"),
        metric_keys=["4"],
    )
    assert all(f"source{i}" in answer for i in (1, 2, 3))
    assert "source4" not in answer and "video4" not in answer and "📊" not in answer


def test_build_pairs_metric_sources_after_dedup_and_industry_scope():
    first = _video_campaign(
        2, "食品A", "施策A", [_video("https://example.test/video2", "調理", 1000, 10)]
    )
    duplicate = replace(first, chunk_id=22)
    other = _video_campaign(
        3, "化粧品A", "施策C", [_video("https://example.test/video3", "保湿", 2000, 20)]
    )
    other.metadata["cls_industry"] = "化粧品"
    last = _video_campaign(
        4, "食品B", "施策B", [_video("https://example.test/video4", "調理", 3000, 30)]
    )
    no_rows = replace(
        first,
        chunk_id=1,
        content="施策実績: 食品Z / 施策Z",
        metadata={**first.metadata, "advertiser": "食品Z", "campaign": "施策Z"},
    )
    answer = build(
        "",
        [no_rows, first, duplicate, other, last],
        [],
        lambda hit: f"https://example.test/source{hit.chunk_id}",
        query="動画ごとの数字を教えて",
        asked_industry="食品",
    )
    assert "source2" in answer and "source4" in answer
    assert "source1" not in answer and "source22" not in answer and "source3" not in answer
    assert "video2" in answer and "video4" in answer and "video3" not in answer


def test_code_notice_survives_inside_the_body_and_sentence_budgets():
    answer = render(
        "\n".join("参考情報の内容です。[S:1]" for _ in range(7)),
        {"1": ("資料", "https://example.test/source", "参考情報の内容です。")},
        notice=WEAK_RESULT_NOTICE,
    )
    assert answer.startswith(WEAK_RESULT_NOTICE)
    assert answer.split("出典:")[0].count("。") <= 6
    assert len(answer.split("出典:")[0].strip()) <= 550
    assert "https://example.test/source" in answer


def test_build_accepts_a_source_independent_code_notice():
    answer = build(
        "", [], [], lambda hit: None, query="状況は？", notice="🔒 非公開に一致があります。"
    )
    assert answer == "🔒 非公開に一致があります。"
