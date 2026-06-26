"""search skill の内部マーカー除去（_strip_internal_markers）テスト。"""

from __future__ import annotations

from teamagent.skills.search.skill import _strip_internal_markers


def test_strip_chunk_id_bracket() -> None:
    assert _strip_internal_markers("訴求 [chunk_id: 123, 456] が効いた") == "訴求 が効いた"


def test_strip_chunk_id_paren() -> None:
    assert _strip_internal_markers("動詞 + 内容 (根拠 chunk_id: 999)") == "動詞 + 内容"


def test_strip_multiple() -> None:
    s = "刺さった [chunk_id: 1] と 避けたい [chunk_id: 2, 3] 点"
    assert _strip_internal_markers(s) == "刺さった と 避けたい 点"


def test_strip_low_confidence_tag() -> None:
    assert _strip_internal_markers("結果（関連度低・参考）です") == "結果です"


def test_no_markers_unchanged() -> None:
    assert (
        _strip_internal_markers("普通の営業向け文章。chunk は出ない")
        == "普通の営業向け文章。chunk は出ない"
    )


def test_empty() -> None:
    assert _strip_internal_markers("") == ""
