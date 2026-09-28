"""共通部品（_shared/grounding.py）の数字の読み方の改良が、検索上位チェックに与える影響を固定する。

改良は 4 点（先頭ゼロ・画面比・タイムコード・万/億の展開）。付け替え前の _numbers
（conclusion.py:54-61 の写し）での結果と並べ、何が変わったかを 1 件ずつ書く。
正しい文を落とさない方向の変化（0:05・9:16・2.50）と、単位の取り違えを通さない方向の変化
（1.2万 と 1.2% の偶然一致・入力に無い「1万人」）の両方がある。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pytest

from teamagent.skills.search_surface_check.conclusion import _numbers, ground_conclusion
from teamagent.skills.search_surface_check.insights import compute_facts
from tests.skills.search_surface_check.fixtures import GROUNDED_CONCLUSION, KEYWORD, NOW

_ALWAYS = frozenset({str(i) for i in range(11)} | {"90", "100"})
_LEGACY_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _legacy_numbers(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).replace(",", "")
    out: set[str] = set()
    for raw in _LEGACY_NUM_RE.findall(normalized):
        out.add(raw)
        if "." in raw:
            out.add(raw.rstrip("0").rstrip("."))
    return out


def _legacy_kept(text: str, input_text: str) -> bool:
    return not (_legacy_numbers(text) - _legacy_numbers(input_text) - _ALWAYS)


def _kept_now(text: str, input_text: str) -> bool:
    raw: dict[str, Any] = {"winning": {"text": text, "ranks": [1]}}
    c = ground_conclusion(raw, allowed_numbers=_numbers(input_text), valid_ranks={1})
    return c is not None and c.winning is not None


@pytest.mark.parametrize(
    ("text", "input_text", "before", "after"),
    [
        # 正しい文を落とさなくなった
        ("冒頭0:05にKWのテロップ", '{"sec": 23}', False, True),  # "05" を入力に無いと判定していた
        ("縦型9:16の全画面", '{"sec": 23}', False, True),  # "16" を入力に無いと判定していた
        ("保存率2.50%が上位", '{"save_rate%": 2.5}', False, True),  # 末尾ゼロの違い
        ("1:30の尺で締める", '{"sec": 90}', False, True),  # タイムコードは秒で照合
        # 単位の取り違えを通さなくなった
        ("検索量1.2万のKW", '{"保存率の中央値%": 1.2}', True, False),  # 1.2% と偶然一致していた
        ("フォロワー1万人未満が中心", '{"本数": 3}', True, False),  # 入力に 1万 が無い
        # 変わらない
        ("再生の中央値は8.2万", '{"再生の中央値": "8.2万"}', True, True),
        ("再生の中央値は82,000", '{"再生の中央値": "8.2万"}', False, True),  # 万の展開で一致
        ("再生の83%を占める", '{"本数の割合%": 47}', False, False),
    ],
)
def test_number_reading_change_is_pinned(
    text: str, input_text: str, before: bool, after: bool
) -> None:
    assert _legacy_kept(text, input_text) is before
    assert _kept_now(text, input_text) is after


def test_real_fixture_conclusion_still_grounds_after_the_change() -> None:
    """実物再現の結論（1万〜10万人・2位と9位・68%・90日）は改良後も 1 件も落ちない。"""
    from teamagent.skills.search_surface_check.conclusion import build_prompt
    from tests.skills.search_surface_check.test_surface_analysis import _posts

    posts = _posts()
    facts = compute_facts(posts, keyword=KEYWORD, client_name="GABAN", now_epoch=NOW)
    _, allowed = build_prompt(
        "{keyword}{platform}{client_name}{facts_json}{posts_json}",
        keyword=KEYWORD,
        platform="tiktok",
        client_name="GABAN",
        facts=facts,
        posts=posts,
        now_epoch=NOW,
    )
    dropped: list[tuple[str, str]] = []
    c = ground_conclusion(
        GROUNDED_CONCLUSION,
        allowed_numbers=allowed,
        valid_ranks={p.rank for p in posts},
        on_drop=lambda f, r: dropped.append((f, r)),
    )
    assert c is not None
    # 捨てるのは実在しない順位だけの切り口 1 件（数字では 1 件も捨てない）
    assert dropped == [("angles", "ranks<2")]
    assert "10万" in c.actions[0].text
