"""書記素クラスタ（見た目の 1 文字）を割らずに文字列を切り詰める（標準ライブラリのみ）。

``text[:n]`` はコードポイント単位で切る。次のような絵文字・文字の途中で切れると、
切り口に片割れが残る（孤立した地域指示子は□で囲んだ英字に見える・宙ぶらりんの ZWJ・
肌色の抜けた 👍・囲みの無い 1 など）:
ZWJ 連結（👨+ZWJ+👩+ZWJ+👧 の家族）・国旗（🇯🇵 は地域指示子 2 個）・肌色（👍🏽）・
キーキャップ（1+FE0F+U+20E3）・異体字セレクタ・結合文字（か＋結合濁点）。
ここでは UAX #29 の拡張書記素クラスタ規則のうち件名に現れるものだけを実装し、
「クラスタ境界でしか切らない」ことを保証する（``regex`` は直接依存でないので使わない）。

簡略化はいずれも「多めに落とす」側に倒す（割れたクラスタは残さない）:
- ZWJ の直後は、相手が絵文字でなくても境界にしない（GB11 を広めに取る）。
- 制御文字の直後の結合文字も繋がったものとみなす（GB4/GB5 を省略）。
扱わないもの（件名に現れることはまれ）: Prepend（GB9b）・ハングル字母の連なり（GB6-8。
ふつうは合成済みの 1 字で届く）・インド系文字の子音結合（GB9c）。
"""

from __future__ import annotations

import unicodedata

_ZWJ = "\u200d"


def _is_extend(ch: str) -> bool:
    """直前の文字に繋がる（＝この文字の前では切れない）文字か。

    GB9/GB9a の Extend・ZWJ・SpacingMark にあたる。
    """
    cp = ord(ch)
    return (
        # 結合文字（Mn/Me/Mc）。異体字セレクタ（絵文字表示の FE0F・漢字の IVS）、濁点 U+3099、
        # キーキャップの囲み U+20E3 もここに入る。
        unicodedata.category(ch).startswith("M")
        # 以下は M* 以外の分類だが直前の文字に繋がるもの。
        or cp in (0x200C, 0x200D)  # ZWNJ・ZWJ
        or 0x1F3FB <= cp <= 0x1F3FF  # 絵文字の肌色修飾子
        or 0xE0020 <= cp <= 0xE007F  # タグ文字（🏴 に続けて地域旗を作る）
        or 0xFF9E <= cp <= 0xFF9F  # 半角の濁点・半濁点（ｶﾞ）
    )


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_boundary(text: str, i: int) -> bool:
    """``text[:i]`` と ``text[i:]`` の間がクラスタ境界か（``0 < i < len(text)``）。"""
    prev, cur = text[i - 1], text[i]
    if prev == "\r" and cur == "\n":  # GB3
        return False
    if _is_extend(cur):  # GB9/GB9a
        return False
    if prev == _ZWJ:  # GB11（広め）
        return False
    if _is_regional_indicator(prev) and _is_regional_indicator(cur):  # GB12/GB13
        # 地域指示子は先頭から 2 個ずつ組になる。直前までの連なりが奇数個なら cur はその相方。
        run = 0
        j = i - 1
        while j >= 0 and _is_regional_indicator(text[j]):
            run += 1
            j -= 1
        return run % 2 == 0
    return True


def truncate_graphemes(text: str, max_chars: int) -> str:
    """``text`` を ``max_chars`` コードポイント以内に、書記素クラスタを割らずに切り詰める。

    収まればそのまま返す。収まらなければ、``max_chars`` 以内で最も長い「クラスタ境界で終わる
    先頭部分」を返す（切り口にかかったクラスタは丸ごと落とす）。長さの単位は ``len()``
    （pydantic の ``max_length`` と同じコードポイント数）のままなので、既存の上限も守られる。
    ``truncate_graphemes(s, len(s) - 1)`` とすると末尾の 1 クラスタだけが落ちる。
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    end = max_chars
    while end > 0 and not _is_boundary(text, end):
        end -= 1
    return text[:end]


__all__ = ["truncate_graphemes"]
