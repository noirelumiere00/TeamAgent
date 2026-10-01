"""truncate_graphemes（書記素クラスタを割らない切り詰め）のテスト。

期待値は「手で区切ったクラスタの列」から作る（実装と独立した正解）。各クラスタが 1 個の
拡張書記素クラスタであることは UAX #29 実装（regex パッケージの ``\\X``）で確認済み。
"""

from __future__ import annotations

import pytest

from teamagent.skills._shared.grapheme_cut import truncate_graphemes

FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # 👨 ZWJ 👩 ZWJ 👧
TECHNOLOGIST = "\U0001f468\U0001f3fd\u200d\U0001f4bb"  # 👨 肌色 ZWJ 💻
RAINBOW_FLAG = "\U0001f3f3\ufe0f\u200d\U0001f308"  # 🏳 VS16 ZWJ 🌈
FLAG_JP = "\U0001f1ef\U0001f1f5"  # 地域指示子 J P
FLAG_US = "\U0001f1fa\U0001f1f8"
FLAG_FR = "\U0001f1eb\U0001f1f7"
THUMBS_SKIN = "\U0001f44d\U0001f3fd"  # 👍 肌色
KEYCAP_1 = "1\ufe0f\u20e3"  # 1 VS16 囲み
KEYCAP_HASH = "#\ufe0f\u20e3"
HEART = "❤\ufe0f"  # ❤ VS16
ENGLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"  # 🏴 タグ列
GA_DECOMPOSED = "か\u3099"  # か＋結合濁点
IVS_KANJI = "葛\U000e0100"  # 異体字セレクタ付きの漢字
HALFWIDTH_GA = "ｶ\uff9e"  # 半角カナ＋半角濁点
E_ACUTE = "e\u0301"
DEVANAGARI_KA = "\u0915\u093e"  # 子音＋母音記号（SpacingMark＝Mc）
CRLF = "\r\n"

EMOJI_CLUSTERS = [
    FAMILY,
    TECHNOLOGIST,
    RAINBOW_FLAG,
    FLAG_JP,
    THUMBS_SKIN,
    KEYCAP_1,
    KEYCAP_HASH,
    HEART,
    ENGLAND,
]
OTHER_CLUSTERS = [GA_DECOMPOSED, IVS_KANJI, HALFWIDTH_GA, E_ACUTE, DEVANAGARI_KA, CRLF]


def _expected(clusters: list[str], max_chars: int) -> str:
    """max_chars 以内に収まる、クラスタを丸ごと並べた最長の先頭部分。"""
    out = ""
    for cluster in clusters:
        if len(out) + len(cluster) > max_chars:
            break
        out += cluster
    return out


_SEQUENCES = {
    "mixed": ["【", "定", "例", "】", *EMOJI_CLUSTERS, *OTHER_CLUSTERS, "a", " ", "終"],
    # 地域指示子は先頭から 2 個ずつ組になる（連続した国旗の途中で切っても片割れを残さない）。
    "flags_in_a_row": ["定", FLAG_JP, FLAG_US, FLAG_FR, FLAG_JP, "例"],
    "emoji_only": [FAMILY, FAMILY, THUMBS_SKIN, FLAG_US, KEYCAP_1, ENGLAND, FAMILY],
    "japanese": ["株", "式", "会", "社", GA_DECOMPOSED, "様", IVS_KANJI, HALFWIDTH_GA, "定", "例"],
}


@pytest.mark.parametrize("name", sorted(_SEQUENCES))
def test_every_cut_position_keeps_clusters_whole(name: str) -> None:
    clusters = _SEQUENCES[name]
    text = "".join(clusters)
    for max_chars in range(len(text) + 2):
        got = truncate_graphemes(text, max_chars)
        assert got == _expected(clusters, max_chars), (name, max_chars)
        assert len(got) <= max_chars


@pytest.mark.parametrize("cluster", EMOJI_CLUSTERS + OTHER_CLUSTERS)
def test_cut_inside_a_cluster_drops_the_whole_cluster(cluster: str) -> None:
    """件名 60 字の切り口がクラスタの途中に来たら、そのクラスタは丸ごと落とす
    （孤立した地域指示子・宙ぶらりんの ZWJ・肌色の抜けた 👍・囲みの無い 1 を残さない）。"""
    for inside in range(1, len(cluster)):
        head = "定" * (60 - inside)
        assert truncate_graphemes(head + cluster + "例", 60) == head
    head = "定" * (60 - len(cluster))
    assert truncate_graphemes(head + cluster + "例", 60) == head + cluster


def test_regional_indicator_pairs_are_counted_from_the_start_of_the_run() -> None:
    # 57 字 + 🇯🇵🇺🇸（4 字）を 60 字で切る → 🇯🇵 は残し、🇺 だけの片割れは残さない。
    head = "定" * 57
    assert truncate_graphemes(head + FLAG_JP + FLAG_US, 60) == head + FLAG_JP
    # 56 字 + 🇯🇵🇺🇸 はちょうど 60 字＝境界で切れる。
    head = "定" * 56
    assert truncate_graphemes(head + FLAG_JP + FLAG_US + "例", 60) == head + FLAG_JP + FLAG_US


def test_dropping_one_cluster_at_a_time() -> None:
    """event_token の縮めループと同じ使い方（len - 1 を渡す）で末尾 1 クラスタずつ落ちる。"""
    clusters = ["定", FAMILY, FLAG_JP, THUMBS_SKIN, KEYCAP_1, ENGLAND, GA_DECOMPOSED]
    text = "".join(clusters)
    for keep in range(len(clusters) - 1, -1, -1):
        text = truncate_graphemes(text, len(text) - 1)
        assert text == "".join(clusters[:keep])


def test_short_text_and_non_positive_limits() -> None:
    assert truncate_graphemes("定例", 60) == "定例"
    assert truncate_graphemes("", 60) == ""
    assert truncate_graphemes("定例", 0) == ""
    assert truncate_graphemes("定例", -1) == ""
    # 上限より長い 1 クラスタしか無ければ空になる（呼び出し側が空の件名を扱う）。
    assert truncate_graphemes(ENGLAND, 6) == ""
    # 収まる文字列は（壊れていても）触らない。切り詰めていないものは直さない。
    assert truncate_graphemes("定例\U0001f1ef", 60) == "定例\U0001f1ef"
