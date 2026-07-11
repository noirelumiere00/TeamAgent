"""ingest.classify: ルールブック命名（種別語_クライアント名_内容）決定論パーサのテスト。

パーサ純関数（_parse_rulebook_title）と DocClassifier への配線
（USE_DOC_KIND_RULES gate 配下・ルール確定時の LLM スキップ・LLM 結果より優先）を検証する。
Bedrock はモック（既存 test_classify.py の流儀を踏襲）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from teamagent.ingest.classify import (
    _DOC_TYPES,
    DocClassifier,
    RulebookMatch,
    _parse_rulebook_title,
)


def _fake_bedrock(text: str) -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = SimpleNamespace(text=text)
    return mock


# -----------------------------------------------------------
# パーサ純関数: 6 種別語 × 正常系
# -----------------------------------------------------------


def test_parse_returns_rulebook_match_dataclass() -> None:
    m = _parse_rulebook_title("提案_アース製薬_SNS運用施策.pptx")
    assert m == RulebookMatch(doc_type="提案書", project="アース製薬", is_template=False)


def test_parse_six_kinds_normal() -> None:
    cases = [
        ("提案_アース製薬_SNS運用施策.pptx", "提案書", "アース製薬", False),
        ("提案書_アース製薬_夏キャンペーン", "提案書", "アース製薬", False),
        ("議事録_出光興産_定例MTG0601.docx", "議事録", "出光興産", False),
        ("報告_B社_6月度実績.pdf", "報告書", "B社", False),
        ("報告書_B社_上期まとめ", "報告書", "B社", False),
        ("価格_C社_メニュー一覧.xlsx", "価格表", "C社", False),
        ("価格表_C社_2026年度", "価格表", "C社", False),
        ("契約_D社_基本契約の件", "契約", "D社", False),
        ("契約書_D社_NDA", "契約", "D社", False),
        ("テンプレ_共通_提案書FMT.pptx", "その他", "共通", True),
        ("テンプレート_共通_議事録フォーム", "その他", "共通", True),
    ]
    for title, doc_type, project, is_template in cases:
        m = _parse_rulebook_title(title)
        assert m is not None, title
        assert m.doc_type == doc_type, title
        assert m.project == project, title
        assert m.is_template is is_template, title


def test_parse_doc_types_stay_within_existing_vocab() -> None:
    # マッピング先は既存 _DOC_TYPES 語彙のみ（勝手な新語彙を作らない regression 防止）。
    for title in (
        "提案_A社_x",
        "議事録_A社_x",
        "報告_A社_x",
        "価格_A社_x",
        "契約_A社_x",
        "テンプレ_A社_x",
    ):
        m = _parse_rulebook_title(title)
        assert m is not None, title
        assert m.doc_type in _DOC_TYPES, title


# -----------------------------------------------------------
# 表記ゆれ（全角 ＿ / 半角・全角スペース / 連続区切り / 拡張子）
# -----------------------------------------------------------


def test_parse_fullwidth_underscore() -> None:
    m = _parse_rulebook_title("提案＿アース製薬＿SNS施策")
    assert m is not None
    assert (m.doc_type, m.project) == ("提案書", "アース製薬")


def test_parse_space_separated() -> None:
    # アンダースコア必須: 空白のみ区切りは通常タイトルとみなし不一致（fail-open）。
    assert _parse_rulebook_title("提案 アース製薬 SNS施策") is None


def test_parse_fullwidth_space_separated() -> None:
    # 全角スペースのみ区切りも同様に不一致（アンダースコア必須）。
    assert _parse_rulebook_title("報告書　出光興産　月次まとめ") is None


def test_parse_space_inside_underscore_naming_still_matches() -> None:
    # 命名「内」の空白ゆれは従来どおり許容（アンダースコアが 1 つでもあれば解析対象）。
    m = _parse_rulebook_title("提案_アース製薬 SNS施策")
    assert m is not None
    assert (m.doc_type, m.project) == ("提案書", "アース製薬")


def test_parse_mixed_and_consecutive_separators() -> None:
    m = _parse_rulebook_title("議事録_ 出光興産＿＿キックオフ")
    assert m is not None
    assert (m.doc_type, m.project) == ("議事録", "出光興産")


def test_parse_strips_file_extension() -> None:
    # 2 セグメントのみ + 拡張子: project に ".pdf" が残らない。
    m = _parse_rulebook_title("提案_アース製薬.pdf")
    assert m is not None
    assert m.project == "アース製薬"


# -----------------------------------------------------------
# 不一致（fail-open ＝ None・何も変えない）
# -----------------------------------------------------------


def test_parse_no_match_returns_none() -> None:
    for title in (
        "アース製薬様向けSNS運用提案書",  # 種別語が先頭でない
        "ご提案_アース製薬_夏施策",  # 前置き付きは完全一致しない
        "提案会議メモ_アース製薬",  # 先頭セグメントが種別語+αは不一致
        "月次レポート_6月",  # 種別語なし
        "提案書",  # 第2セグメント（クライアント名）なし
        "テンプレ",  # 同上
        "",
        "   ",
        "2026_アース製薬_提案",  # 種別語が先頭でない（数字 prefix）
    ):
        assert _parse_rulebook_title(title) is None, title


def test_parse_space_separated_ordinary_titles_return_none() -> None:
    # regression: 空白区切りの通常タイトルが cls_project を汚染し LLM 分類まで
    # スキップされていた誤爆（『提案書 v2 最終』→ project='v2' 等）。
    # アンダースコア必須ガードで全て不一致（None）になること。
    for title in (
        "提案書 v2 最終.pptx",  # 旧挙動: project='v2'
        "報告書 2026年度上期 A社.pdf",  # 旧挙動: project='2026年度上期'
        "価格表 改定版.xlsx",  # 旧挙動: project='改定版'
        "議事録 まとめ.docx",  # 旧挙動: project='まとめ'
    ):
        assert _parse_rulebook_title(title) is None, title


def test_parse_numeric_second_segment_returns_none() -> None:
    # 第2セグメントが数字・日付のみはクライアント名不成立 → 不一致（fail-open）。
    # 特に Slack thread タイトル "{channel名} {ts}" で channel 名が種別語のときの誤爆防止
    # （cls_project にタイムスタンプが化けるのを防ぐ）。
    for title in (
        "議事録 1720000000.123456",  # Slack: channel「議事録」+ ts
        "提案 1720000000.123456",
        "報告_2026-06_A社",  # 日付が第2セグメント（命名規則違反）
        "報告_2026/06/30_A社",
        "議事録_2026年6月_定例",
        "価格_123_一覧",
    ):
        assert _parse_rulebook_title(title) is None, title


# -----------------------------------------------------------
# DocClassifier 配線: gate ON でルール確定 → LLM スキップ・LLM より優先
# -----------------------------------------------------------


def test_classify_rulebook_skips_llm_call() -> None:
    # LLM が矛盾する分類（議事録 / 別クライアント）を返す設定でも、そもそも呼ばれない。
    bedrock = _fake_bedrock('{"doc_type": "議事録", "project": "LLM社"}')
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="提案_アース製薬_SNS運用施策.pptx", text="本文...", request_id="r"
    )
    bedrock.converse.assert_not_called()  # 本文を LLM に送らない
    assert cls is not None
    assert cls.doc_type == "提案書"  # ルール確定値が最終値（LLM より優先）
    assert cls.project == "アース製薬"
    assert cls.is_template is False
    assert cls.is_recurring is False


def test_classify_rulebook_metadata_shape() -> None:
    bedrock = _fake_bedrock("{}")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="価格_C社_メニュー.xlsx", text="x", request_id="r"
    )
    assert cls is not None
    # LLM 専用軸（industry 等）は付与されない＝キー自体が出ない（後方互換の形）。
    assert cls.as_metadata() == {"cls_project": "C社", "cls_doc_type": "価格表"}


def test_classify_rulebook_template_sets_flag() -> None:
    bedrock = _fake_bedrock("{}")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="テンプレ_共通_提案書FMT.pptx", text="x", request_id="r"
    )
    bedrock.converse.assert_not_called()
    assert cls is not None
    assert cls.doc_type == "その他"  # テンプレは既存語彙に無いため「その他」+ フラグの 2 段
    assert cls.project == "共通"
    assert cls.is_template is True


def test_classify_rulebook_or_merges_title_recurring() -> None:
    # 第3セグメントの定期報告語（月次+実績データ）はタイトルルールと OR マージされる。
    bedrock = _fake_bedrock("{}")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="報告_A社_月次実績データ.xlsx", text="x", request_id="r"
    )
    bedrock.converse.assert_not_called()
    assert cls is not None
    assert cls.doc_type == "報告書"
    assert cls.is_recurring is True


def test_classify_rulebook_or_merges_folder_rule() -> None:
    # フォルダ決定論ルール（99_テンプレート）も OR マージで生きる。
    bedrock = _fake_bedrock("{}")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="提案_アース製薬_旧版",
        text="x",
        request_id="r",
        folder_name="99_テンプレート",
    )
    assert cls is not None
    assert cls.is_template is True  # フォルダ置き位置シグナルを失わない


def test_classify_rulebook_works_with_empty_text() -> None:
    # タイトルだけで確定するため本文が空でも分類が返る（LLM 経路なら本文必須級）。
    bedrock = _fake_bedrock("{}")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="契約_D社_NDA.pdf", text="", request_id="r"
    )
    assert cls is not None
    assert (cls.doc_type, cls.project) == ("契約", "D社")


# -----------------------------------------------------------
# gate OFF / パターン不一致 → 従来挙動（LLM が呼ばれ、結果がそのまま使われる）
# -----------------------------------------------------------


def test_classify_rulebook_gate_off_uses_llm(monkeypatch: Any) -> None:
    monkeypatch.delenv("USE_DOC_KIND_RULES", raising=False)
    bedrock = _fake_bedrock('{"doc_type": "議事録", "project": "LLM社"}')
    cls = DocClassifier(bedrock).classify(
        title="提案_アース製薬_SNS運用施策.pptx", text="本文", request_id="r"
    )
    bedrock.converse.assert_called_once()  # gate OFF はルールブック無効＝従来どおり LLM
    assert cls is not None
    assert cls.doc_type == "議事録"
    assert cls.project == "LLM社"


def test_classify_rulebook_gate_on_via_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("USE_DOC_KIND_RULES", "1")
    bedrock = _fake_bedrock('{"doc_type": "議事録"}')
    cls = DocClassifier(bedrock).classify(
        title="提案_アース製薬_SNS運用施策", text="本文", request_id="r"
    )
    bedrock.converse.assert_not_called()
    assert cls is not None
    assert cls.doc_type == "提案書"


def test_classify_no_rulebook_match_falls_back_to_llm() -> None:
    # パターン不一致は何もしない（現状と同一挙動）: LLM が呼ばれ、その結果を使う。
    bedrock = _fake_bedrock('{"doc_type": "提案書", "project": "アース製薬"}')
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="アース製薬様向けSNS運用提案書.pptx", text="本文", request_id="r"
    )
    bedrock.converse.assert_called_once()
    assert cls is not None
    assert cls.doc_type == "提案書"
    assert cls.project == "アース製薬"
