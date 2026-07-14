"""名寄せタグ抽出（entity_extract）の単体テスト。

2026-07-14。資料に登場する取引先/代理店/ブランド/コラボ名を抽出・正規化する。
LLM は fake bedrock で制御（外部 I/O 無し）。
"""

from __future__ import annotations

from typing import Any

from teamagent.ingest.entity_extract import (
    extract_entities,
    normalize_entity,
)


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeBedrock:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kw: Any) -> _Resp:
        self.calls.append(kw)
        return _Resp(self._text)


class _BoomBedrock:
    def converse(self, **kw: Any) -> _Resp:
        raise RuntimeError("bedrock down")


# ---- normalize_entity ----


def test_normalize_strips_legal_and_honorific() -> None:
    assert normalize_entity("株式会社サンマルクカフェ 御中") == "サンマルクカフェ"
    assert normalize_entity("（株）祇園辻利様") == "祇園辻利"
    assert normalize_entity("  ユニー  ") == "ユニー"


def test_normalize_removes_comma_to_protect_csv() -> None:
    # CSV 保存の区切りと衝突しないよう ASCII/全角カンマは除去（M2）。
    assert "," not in normalize_entity("ABC, Inc.")
    assert "、" not in normalize_entity("日本、ABC")


# ---- extract_entities ----


def test_extract_collab_both_sides() -> None:
    fake = _FakeBedrock('{"entities": ["株式会社サンマルクカフェ", "祇園辻利"]}')
    ents = extract_entities(
        title="0115_祇園辻利プロモーション",
        text="サンマルクカフェ×祇園辻利のコラボ",
        bedrock=fake,
        request_id="r",
    )
    assert ents == ["サンマルクカフェ", "祇園辻利"]  # 法人格除去・両者を保持


def test_extract_dedup_and_cap() -> None:
    raw = '{"entities": ["サンマルク", "株式会社サンマルク", "サンマルク　", "AA", "BB", "CC", "DD", "EE", "FF", "GG"]}'
    fake = _FakeBedrock(raw)
    # 本文に全エンティティを含める（M3 の実在フィルタを通すため）。
    body = "サンマルク AA BB CC DD EE FF GG が登場する資料"
    ents = extract_entities(title="t", text=body, bedrock=fake, request_id="r")
    assert ents.count("サンマルク") == 1  # 表記ゆれ畳み込み
    assert len(ents) <= 8  # 上限


def test_extract_filters_names_not_in_text() -> None:
    """本文に無い名前（LLM のインジェクション/幻覚）は落とす（M3）。"""
    fake = _FakeBedrock('{"entities": ["サンマルクカフェ", "存在しない競合社"]}')
    ents = extract_entities(
        title="提案", text="サンマルクカフェのPR施策", bedrock=fake, request_id="r"
    )
    assert ents == ["サンマルクカフェ"]  # 本文に無い『存在しない競合社』は除外


def test_extract_empty_when_none() -> None:
    fake = _FakeBedrock('{"entities": []}')
    assert extract_entities(title="t", text="b", bedrock=fake, request_id="r") == []


def test_extract_failopen_on_bedrock_error() -> None:
    assert extract_entities(title="t", text="b", bedrock=_BoomBedrock(), request_id="r") == []


def test_extract_failopen_on_bad_json() -> None:
    fake = _FakeBedrock("これは JSON ではない")
    assert extract_entities(title="t", text="b", bedrock=fake, request_id="r") == []


def test_extract_empty_input_returns_empty() -> None:
    fake = _FakeBedrock('{"entities": ["x"]}')
    # タイトルも本文も空 → LLM を呼ばず []（fake は呼ばれない）。
    assert extract_entities(title="", text="", bedrock=fake, request_id="r") == []
    assert fake.calls == []


def test_extract_result_has_no_comma() -> None:
    """抽出結果に区切り(カンマ)が残らない＝CSV cls_entities が壊れない（M2）。"""
    fake = _FakeBedrock('{"entities": ["ABC, Inc."]}')
    ents = extract_entities(title="t", text="ABC, Inc. の資料", bedrock=fake, request_id="r")
    assert ents and all("," not in e for e in ents)
