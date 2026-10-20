"""短文の数値比較は、同じ指標・集計単位の取得値からコードで作る。"""

from collections.abc import Callable
from typing import Any

import pytest

from teamagent.skills.search import comparison
from tests.skills.search.test_comparison import _campaign


def renderer() -> Callable[..., tuple[str, list[int]]]:
    render: Any = getattr(comparison, "compact_comparison", None)
    assert callable(render), "短文の同指標比較をコードで生成する処理がない"
    return render


def test_compact_comparison_uses_same_metric_and_aggregation_for_both_campaigns() -> None:
    first = _campaign(
        11, "施策A", "再生数: 合計 1,230,000、平均 205,000、最大 507,700\n反応: 保存 平均 50"
    )
    second = _campaign(
        22, "施策B", "再生数: 合計 685,300、平均 171,325、最大 400,000\n反応: 保存 平均 25"
    )
    text, source_ids = renderer()("比較して何が効いた？", [first, second, first])
    assert "再生平均" in text
    assert "食品 / 施策Aが205,000回" in text
    assert "食品 / 施策Bが171,325回" in text
    assert "保存平均" in text and "施策Aが50件" in text and "施策Bが25件" in text
    assert text.count("施策Aが上です。") == 2
    assert source_ids == [11, 22]
    assert text.count("。") <= 2
    assert "1,230,000" not in text and "685,300" not in text


def test_compact_comparison_never_pairs_play_median_with_save_total() -> None:
    first = _campaign(1, "施策A", "再生数: 中央値 153,300\n反応: 保存 合計 261")
    second = _campaign(2, "施策B", "再生数: 合計 1,230,000\n反応: 保存 平均 25")
    assert renderer()("比較して何が効いた？", [first, second]) == ("", [])


def test_compact_comparison_skips_missing_metric_and_preserves_a_tie() -> None:
    first = _campaign(1, "施策A", "再生数: 平均 1,200.5\n反応: 保存 平均 5")
    second = _campaign(2, "施策B", "再生数: 平均 1,200.5")
    text, source_ids = renderer()("どちらが効いた？", [first, second])
    assert "1,200.5回" in text and "同率です。" in text
    assert "保存" not in text
    assert source_ids == [1, 2]


@pytest.mark.parametrize("count", [0, 1, 4])
def test_compact_comparison_requires_two_or_three_citable_campaigns(count: int) -> None:
    hits = [_campaign(i, f"施策{i}", "再生数: 平均 100") for i in range(1, count + 1)]
    assert renderer()("比較して", hits) == ("", [])


def test_compact_comparison_keeps_three_campaigns_and_excludes_noncomparison_query() -> None:
    hits = [_campaign(i, f"施策{i}", f"再生数: 平均 {i * 100}") for i in (1, 2, 3)]
    text, source_ids = renderer()("比較して", hits)
    assert all(f"施策{i}が{i * 100}回" in text for i in (1, 2, 3))
    assert "施策3が上です。" in text
    assert source_ids == [1, 2, 3]
    assert renderer()("今どうなっている？", hits) == ("", [])
