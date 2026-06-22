"""ingest.classify: 資料自動分類のテスト（Bedrock はモック）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from teamagent.ingest.classify import (
    DocClassification,
    DocClassifier,
    build_classifier_from_env,
)


def _fake_bedrock(text: str) -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = SimpleNamespace(text=text)
    return mock


def test_classify_normal() -> None:
    bedrock = _fake_bedrock(
        '{"project": "アース製薬", "industry": "日用品", "doc_type": "提案書", "phase": "提案"}'
    )
    clf = DocClassifier(bedrock)
    cls = clf.classify(title="アース製薬_提案.pdf", text="本文...", request_id="r1")
    assert cls == DocClassification(
        project="アース製薬", industry="日用品", doc_type="提案書", phase="提案"
    )


def test_as_metadata_mirrors_industry() -> None:
    cls = DocClassification(project="A社", industry="食品", doc_type="議事録", phase="不明")
    md = cls.as_metadata()
    assert md["cls_project"] == "A社"
    assert md["cls_industry"] == "食品"
    assert md["industry"] == "食品"  # 既存の業界フィルタと整合
    assert md["cls_doc_type"] == "議事録"
    assert md["cls_phase"] == "不明"


def test_as_metadata_omits_empty() -> None:
    cls = DocClassification(industry="IT")
    assert cls.as_metadata() == {"cls_industry": "IT", "industry": "IT"}


def test_classify_salvages_first_object_when_array_like_breaks() -> None:
    # 完結オブジェクト + 末尾に壊れたオブジェクト → 救済フォールバックで先頭を拾う。
    bedrock = _fake_bedrock(
        '{"project": "B社", "industry": "小売", "doc_type": "報告書"} {"project": "X'
    )
    clf = DocClassifier(bedrock)
    cls = clf.classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.project == "B社"
    assert cls.doc_type == "報告書"


def test_classify_normalizes_doc_type_and_phase() -> None:
    bedrock = _fake_bedrock(
        '{"project": "", "industry": "", "doc_type": "提案書（最終）", "phase": "提案"}'
    )
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.doc_type == "提案書"  # 部分一致で正規化
    assert cls.phase == "提案"


def test_classify_unknown_choices_drop_to_empty() -> None:
    bedrock = _fake_bedrock(
        '{"project": "C社", "industry": "金融", "doc_type": "雑メモ", "phase": "謎"}'
    )
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.doc_type == ""  # 語彙外は落とす
    assert cls.phase == ""


def test_classify_bedrock_error_returns_none() -> None:
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError("bedrock down")
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is None  # fail-open（取り込みは継続）


def test_classify_garbage_returns_none() -> None:
    cls = DocClassifier(_fake_bedrock("no json at all")).classify(
        title="t", text="x", request_id="r"
    )
    assert cls is None


def test_classify_all_empty_returns_none() -> None:
    bedrock = _fake_bedrock('{"project": "", "industry": "", "doc_type": "", "phase": ""}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is None  # 何も取れなければ None


def test_classify_empty_input_returns_none() -> None:
    cls = DocClassifier(_fake_bedrock("{}")).classify(title="", text="   ", request_id="r")
    assert cls is None  # 本文もタイトルも無ければ Bedrock を呼ばず None


def test_build_classifier_disabled_by_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("USE_DOC_CLASSIFY", raising=False)
    assert build_classifier_from_env() is None


def test_build_classifier_enabled(monkeypatch: Any) -> None:
    monkeypatch.setenv("USE_DOC_CLASSIFY", "1")
    import teamagent.adapters.bedrock_client as bc

    monkeypatch.setattr(bc.BedrockClient, "from_env", classmethod(lambda cls: MagicMock()))
    clf = build_classifier_from_env()
    assert isinstance(clf, DocClassifier)


def test_build_classifier_init_failure_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setenv("USE_DOC_CLASSIFY", "true")
    import teamagent.adapters.bedrock_client as bc

    def _boom(cls: type) -> None:
        raise RuntimeError("no creds")

    monkeypatch.setattr(bc.BedrockClient, "from_env", classmethod(_boom))
    assert build_classifier_from_env() is None  # 初期化失敗でも取り込みは止めない
