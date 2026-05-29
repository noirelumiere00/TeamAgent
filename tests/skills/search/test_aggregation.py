"""skills/search/aggregation.py の純ロジック単体テスト。

集約・一覧クエリのフィルタ抽出を固定する。DB は使わない。
"""

from __future__ import annotations

from teamagent.skills.search.aggregation import extract_aggregation_filter


def test_extract_bant_score() -> None:
    assert extract_aggregation_filter("BANT A の前向きな案件一覧") == {"bant_score": "A"}
    assert extract_aggregation_filter("BANT:B の案件") == {"bant_score": "B"}
    assert extract_aggregation_filter("bant c の傾向") == {"bant_score": "C"}


def test_extract_channel_type() -> None:
    assert extract_aggregation_filter("代理店経由の案件で刺さる訴求") == {"channel_type": "代理店"}
    assert extract_aggregation_filter("直販案件のフィードバック傾向") == {"channel_type": "直販"}


def test_shitchu_maps_to_bant_c() -> None:
    """失注 は明示フィールドが無いため bant_score=C に近似マッピング。"""
    assert extract_aggregation_filter("失注した案件と失注理由") == {"bant_score": "C"}


def test_bant_takes_precedence_over_shitchu() -> None:
    """BANT 明示があれば失注より優先 (上書きしない)。"""
    assert extract_aggregation_filter("BANT A だが失注した案件") == {"bant_score": "A"}


def test_combined_filters() -> None:
    out = extract_aggregation_filter("代理店経由で BANT B の案件一覧")
    assert out == {"bant_score": "B", "channel_type": "代理店"}


def test_no_signal_returns_none() -> None:
    """集約信号が無い通常クエリは None (= 意味検索にフォールバック)。"""
    assert extract_aggregation_filter("日本ガイシのケイパ提案について") is None
    assert extract_aggregation_filter("飲料メーカー向けの提案実績") is None
