"""直接回答の比較表と結論は同じ集計値から作る。"""

from __future__ import annotations

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.comparison import comparison_table


def _campaign(chunk_id: int, campaign: str, metrics: str) -> SearchHit:
    return SearchHit(
        chunk_id,
        metrics,
        0.9,
        {"campaign_aggregate": "true", "advertiser": "食品", "campaign": campaign},
    )


def test_comparison_starts_with_table_and_concludes_from_both_campaigns() -> None:
    first = _campaign(
        1,
        "施策A",
        "投稿本数: 6 本。\n再生数: 合計 1,230,000、平均 205,000、最大 507,700\n反応: 保存 平均 50",
    )
    second = _campaign(
        2,
        "施策B",
        "投稿本数: 4 本。\n再生数: 合計 685,300、平均 171,325、最大 400,000\n反応: 保存 平均 25",
    )
    answer = comparison_table("食品の施策Aと施策Bを比較して何が効いた？", [first, second, first])

    assert answer.startswith("# 施策比較表")
    assert answer.count("| 食品 / 施策A |") == 1
    assert "| 食品 / 施策B | 4 | 685,300 | 171,325 | 400,000 | 25 |" in answer
    assert "結論: 再生合計・再生平均・再生最大・保存平均は食品 / 施策Aが上です。" in answer
    assert "施策の因果効果を示すものではありません。" in answer


@pytest.mark.parametrize(
    ("first_metrics", "second_metrics", "conclusion"),
    [
        (
            "再生数: 合計 100、平均 10、最大 50\n反応: 保存 平均 5",
            "再生数: 合計 100、平均 10、最大 50\n反応: 保存 平均 5",
            "結論: 再生合計・再生平均・再生最大・保存平均は同率です。",
        ),
        (
            "再生数: 合計 100、平均 10、最大 50",
            "再生数: 合計 200、平均 10、最大 40\n反応: 保存 平均 5",
            "結論: 再生合計は食品 / 施策Bが上、再生平均は同率、"
            "再生最大は食品 / 施策Aが上、保存平均は比較不可（欠測あり）です。",
        ),
    ],
)
def test_comparison_conclusion_preserves_ties_and_missing_metrics(
    first_metrics: str, second_metrics: str, conclusion: str
) -> None:
    hits = [_campaign(1, "施策A", first_metrics), _campaign(2, "施策B", second_metrics)]

    assert conclusion in comparison_table("どちらが効いた？", hits)
    assert comparison_table("今どうなっている？", hits) == ""
