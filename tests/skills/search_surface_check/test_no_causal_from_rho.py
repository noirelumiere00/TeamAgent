"""一致度（順位と再生の相関）から原因を言う文を落とす（09-29 本番の実機で見つかった形）。"""

from __future__ import annotations

import pytest

from teamagent.skills.search_surface_check.conclusion import (
    drop_causal_from_rho,
    ground_conclusion,
)

# 09-29 13:01 の本番の AI の読み（2 つ目の打ち手）そのままの形
PROD_ACTION = (
    "本文に『スパイスカレー 作り方』を明記し、#スパイスカレー #カレーレシピなどのタグを活用。"
    "順位と再生数の一致度0.31から、KW関連性が順位に影響している可能性。"
    "PR表記投稿（1位、7位、11位、29位）が上位に複数あるため、PR枠での出稿も検討。"
)


def test_the_production_sentence_is_dropped_and_the_rest_is_kept() -> None:
    """壊し方: drop_causal_from_rho を素通しにする → 本番の文が残って赤。"""
    out = drop_causal_from_rho(PROD_ACTION)
    assert "一致度0.31から" not in out and "影響" not in out
    assert out.startswith("本文に『スパイスカレー 作り方』を明記し")
    assert out.endswith("PR枠での出稿も検討。")


@pytest.mark.parametrize(
    "text",
    [
        "相関が高いので保存率が順位を左右している。",
        "一致度が低いことから、KWが順位を決めていると言える。",
        "一致度0.2のため上位はKWの要因が大きい。",
        "相関から見て、尺が効いている。",
    ],
)
def test_causal_claims_from_correlation_are_dropped(text: str) -> None:
    assert drop_causal_from_rho(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "順位と再生数の一致度は0.31で、再生の多い順には弱く並ぶ。",  # 並び方の説明は残す
        "保存率が高い投稿は手順を見返す需要に応えている。",  # 一致度の話ではない
        "PR表記の投稿が上位に複数ある。",
    ],
)
def test_descriptions_without_causal_claims_are_kept(text: str) -> None:
    assert drop_causal_from_rho(text) == text


def test_ground_conclusion_drops_only_the_causal_sentence_and_reports_it() -> None:
    drops: list[tuple[str, str]] = []
    c = ground_conclusion(
        {
            "headline": "インフルエンサーが再生の55%を占める",
            "actions": [{"text": PROD_ACTION, "ranks": [1, 7]}],
        },
        allowed_numbers={"55", "0.31", "1", "7", "11", "29"},
        valid_ranks={1, 7, 11, 29},
        on_drop=lambda field, reason: drops.append((field, reason)),
    )
    assert c is not None
    assert len(c.actions) == 1
    assert "影響" not in c.actions[0].text
    assert ("actions", "causal_from_rho") in drops


def test_a_causal_headline_is_emptied() -> None:
    c = ground_conclusion(
        {
            "headline": "一致度が低くKWが順位を決めている面",
            "winning": {"text": "インフルエンサーが再生の55%を占める。", "ranks": []},
        },
        allowed_numbers={"55"},
        valid_ranks=set(),
    )
    assert c is not None
    assert c.headline == ""
    assert c.winning is not None
