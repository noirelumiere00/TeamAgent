"""サムネ（一覧の表紙）の読み取りの parse（cover_read.parse_cover・schema.CoverRead）。

フェイクは本番の Gemini の崩れ方を再現する: JSON が途中で切れる・```json のフェンスと前置きの文・
知らない列挙の値・texts が文字列の並び・空の JSON・コードだけの欄を AI が書く。
壊し方（→ 赤）は各テストの docstring。
"""

from __future__ import annotations

import json

from teamagent.skills.video_algorithm.cover_read import parse_cover
from teamagent.skills.video_algorithm.schema import CoverRead

_OK = {
    "elements": ["result", "person"],
    "subject_note": "湯気の立つ皿",
    "texts": [
        {"text": "スパイスカレー\n10分", "box_2d": [100, 50, 300, 950], "style": ["outline"]}
    ],
    "unreadable_text": False,
    "face": {
        "kind": "real",
        "expression": "smile",
        "gaze": "camera",
        "box_2d": [400, 300, 600, 700],
    },
    "action": "eating",
    "closeup": True,
    "sizzle": ["steam"],
    "product": "none",
    "brand_text": [],
    "clutter": "simple",
    "legibility": "good",
    "appeals": ["benefit"],
}


def test_fenced_json_with_preamble_is_read() -> None:
    """壊し方: 最初の { から読む処理を json.loads(text) に戻す → 前置きの文で None になり赤。"""
    text = (
        "所見: 文字が大きい表紙です。\n```json\n"
        + json.dumps(_OK, ensure_ascii=False)
        + "\n```\n以上"
    )
    read = parse_cover(text)
    assert read is not None
    assert read.texts is not None and read.texts[0].text == "スパイスカレー\n10分"
    assert read.texts[0].box == (100, 50, 300, 950)
    assert read.face is not None and read.face.kind == "real"


def test_truncated_or_non_object_json_is_none() -> None:
    body = json.dumps(_OK, ensure_ascii=False)
    assert parse_cover(body[: len(body) // 2]) is None  # 途中で切れた
    assert parse_cover("[1, 2, 3]") is None
    assert parse_cover("") is None
    assert parse_cover(None) is None


def test_empty_object_is_not_counted_as_all_absent() -> None:
    """空の JSON（既存のフェイクが返す形）を「文字なし・顔なし」と数えない。

    壊し方: 必須の欄の確認を外す → {} が CoverRead になり赤。
    """
    assert parse_cover("```json\n{}\n```") is None
    missing_face = {k: v for k, v in _OK.items() if k != "face"}
    assert parse_cover(json.dumps(missing_face)) is None


def test_default_status_is_not_ok_and_code_only_fields_are_dropped() -> None:
    """AI が status=ok や rank を書いても使わない（コードだけが決める）。"""
    assert CoverRead().status == "read_failed"
    read = parse_cover(json.dumps({**_OK, "status": "ok", "rank": 99, "group": "rest"}))
    assert read is not None
    assert read.status == "read_failed" and read.rank == 0 and read.group == "top"


def test_unknown_values_become_unknown_and_other_fields_survive() -> None:
    """知らない値は unknown（その欄の母数から外す）。リストは知らない要素だけ捨てる。

    壊し方: 列挙の読み替えを外す（Literal のまま）→ ValidationError で全体が None になり赤。
    """
    broken = {
        **_OK,
        "elements": ["Result", "robot", "person"],
        "clutter": "chaotic",
        "legibility": 3,
        "face": {"kind": "alien", "expression": "grin", "gaze": "camera"},
        "closeup": "たぶん",
        "sizzle": "steam, sparkle",
        "product": None,
    }
    read = parse_cover(json.dumps(broken, ensure_ascii=False))
    assert read is not None
    assert read.elements == ["result", "person"]
    assert read.clutter == "unknown" and read.legibility == "unknown"
    assert read.face is not None and read.face.kind == "unknown" and read.face.gaze == "camera"
    assert read.closeup is None  # 3 値の「分からない」
    assert read.sizzle == ["steam"]
    assert read.product == "unknown"
    assert read.subject_note == "湯気の立つ皿"


def test_texts_as_strings_blocks_are_capped_and_empty_dropped() -> None:
    raw = {**_OK, "texts": ["一行目\\n二行目", "", "b", "c", "d", "e"], "legibility": "good"}
    read = parse_cover(json.dumps(raw, ensure_ascii=False))
    assert read is not None and read.texts is not None
    assert [t.text for t in read.texts] == ["一行目\n二行目", "b", "c", "d"]
    no_text = parse_cover(json.dumps({**_OK, "texts": []}))
    assert no_text is not None and no_text.texts == [] and no_text.legibility == "none"


def test_broken_boxes_are_none() -> None:
    raw = {
        **_OK,
        "texts": [
            {"text": "a", "box_2d": [300, 0, 100, 10]},  # 上下が逆
            {"text": "b", "box_2d": [1, 2, 3]},
            {"text": "c", "box_2d": ["x", 0, 10, 10]},
            {"text": "d", "box_2d": [-5, 0, 1200, 500]},  # 範囲外は 0〜1000 に丸める
        ],
    }
    read = parse_cover(json.dumps(raw))
    assert read is not None and read.texts is not None
    assert [t.box for t in read.texts] == [None, None, None, (0, 0, 1000, 500)]


def test_face_false_means_no_face_not_unknown() -> None:
    read = parse_cover(json.dumps({**_OK, "face": False}))
    assert read is not None and read.face is not None and read.face.kind == "none"
    read = parse_cover(json.dumps({**_OK, "face": {"present": True}}))
    assert read is not None and read.face is not None and read.face.kind == "real"
