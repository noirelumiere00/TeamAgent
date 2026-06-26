"""json_salvage: 打ち切り耐性のある JSON 取り出しのテスト。"""

from __future__ import annotations

from teamagent.util.json_salvage import salvage_json_array, salvage_json_object


def test_array_normal() -> None:
    text = '[{"importance": "high", "summary": "a"}, {"importance": "low", "summary": "b"}]'
    out = salvage_json_array(text)
    assert out == [
        {"importance": "high", "summary": "a"},
        {"importance": "low", "summary": "b"},
    ]


def test_array_with_prose_around() -> None:
    text = 'はい、結果です:\n[{"x": 1}, {"y": 2}]\n以上です。'
    assert salvage_json_array(text) == [{"x": 1}, {"y": 2}]


def test_array_truncated_salvages_complete_objects() -> None:
    # max_tokens 打ち切りで配列が閉じず最後のオブジェクトも不完全 → 完結分だけ救済。
    text = '[{"importance": "high", "summary": "a"}, {"importance": "medium", "summary": "b"}, {"importance": "lo'
    out = salvage_json_array(text)
    assert out == [
        {"importance": "high", "summary": "a"},
        {"importance": "medium", "summary": "b"},
    ]


def test_array_filters_non_dict_elements() -> None:
    text = '[{"a": 1}, "noise", 42, {"b": 2}]'
    assert salvage_json_array(text) == [{"a": 1}, {"b": 2}]


def test_array_empty_and_garbage() -> None:
    assert salvage_json_array("") == []
    assert salvage_json_array("no json here") == []
    assert salvage_json_array("[]") == []


def test_object_normal() -> None:
    text = '{"project": "アース製薬", "industry": "日用品", "doc_type": "提案書"}'
    obj = salvage_json_object(text)
    assert obj == {"project": "アース製薬", "industry": "日用品", "doc_type": "提案書"}


def test_object_with_prose_and_fence() -> None:
    text = '```json\n{"doc_type": "議事録"}\n```'
    assert salvage_json_object(text) == {"doc_type": "議事録"}


def test_object_truncated_returns_first_complete() -> None:
    # 先頭オブジェクトは完結、続きが壊れている → 先頭を返す。
    text = '{"a": 1} {"b":'
    assert salvage_json_object(text) == {"a": 1}


def test_object_garbage_returns_none() -> None:
    assert salvage_json_object("") is None
    assert salvage_json_object("no json") is None
    assert salvage_json_object("{broken") is None
