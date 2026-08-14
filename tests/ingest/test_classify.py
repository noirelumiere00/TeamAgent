"""ingest.classify: 資料自動分類のテスト（Bedrock はモック）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from teamagent.ingest.classify import (
    _CLASSIFY_SYSTEM_PROMPT,
    DocClassification,
    DocClassifier,
    _kind_from_folder,
    _kind_from_title,
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


def test_as_metadata_emits_new_axes() -> None:
    cls = DocClassification(solution="インフルエンサー", budget="100〜500万", target="若年女性")
    md = cls.as_metadata()
    assert md["cls_solution"] == "インフルエンサー"
    assert md["cls_budget"] == "100〜500万"
    assert md["cls_target"] == "若年女性"
    # 旧軸が空なら出さない（後方互換）。
    assert "cls_project" not in md
    assert "cls_doc_type" not in md


def test_as_metadata_omits_empty_new_axes() -> None:
    # 新軸がすべて空なら新キーは一切出ない。
    cls = DocClassification(project="A社")
    assert cls.as_metadata() == {"cls_project": "A社"}


def test_is_empty_false_with_only_new_axis() -> None:
    assert not DocClassification(solution="動画広告").is_empty()
    assert not DocClassification(budget="不明").is_empty()
    assert not DocClassification(target="シニア").is_empty()
    assert DocClassification().is_empty()


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


def test_classify_reads_new_axes() -> None:
    bedrock = _fake_bedrock(
        '{"project": "", "industry": "", "doc_type": "", "phase": "",'
        ' "solution": "SNS運用", "budget": "100〜500万", "target": "主婦"}'
    )
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.solution == "SNS運用"
    assert cls.budget == "100〜500万"
    assert cls.target == "主婦"


def test_classify_solution_normalizes_to_vocab() -> None:
    bedrock = _fake_bedrock('{"solution": "インフルエンサーマーケティング施策"}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.solution == "インフルエンサー"  # 代表語彙へ部分一致正規化


def test_classify_solution_keeps_raw_when_unknown() -> None:
    bedrock = _fake_bedrock('{"solution": "サンプリング配布"}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.solution == "サンプリング配布"  # 語彙外は生値を短く保持


def test_classify_budget_normalizes_band() -> None:
    bedrock = _fake_bedrock('{"budget": "100〜500万"}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.budget == "100〜500万"


def test_classify_budget_offvocab_drops_to_empty() -> None:
    # 予算帯と無関係な語は _norm_choice で "" に落ちる＝推測で埋めない（fail-open）。
    bedrock = _fake_bedrock('{"budget": "要相談", "solution": "動画広告"}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.budget == ""
    assert cls.solution == "動画広告"


def test_classify_budget_unknown_band_preserved() -> None:
    bedrock = _fake_bedrock('{"budget": "不明", "solution": "SEO"}')
    cls = DocClassifier(bedrock).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.budget == "不明"


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


# ── is_template / is_recurring（決定論タイトルルール + LLM OR マージ） ──────────


def test_kind_from_title_recurring_keywords() -> None:
    for title in (
        "2025年上期売上報告",
        "下期実績まとめ",
        "四半期レビュー資料",
        "月次レポート_6月",
        "週次定例MTG資料",
        "A社_売上データ_2025",
        "実績データ集計",
        "上半期振り返り",
        "通期見通し",
        "月報_営業部",
    ):
        is_template, is_recurring = _kind_from_title(title)
        assert is_recurring is True, title
        assert is_template is False, title


def test_kind_from_title_template_keywords() -> None:
    for title in (
        "提案書テンプレート",
        "提案書テンプレ_v2",
        "proposal_template.pptx",
        "PROPOSAL_TEMPLATE",  # ASCII は大文字小文字無視
        "見積書雛形",
        "議事録ひな形",
        "報告フォーマット",
        "サンプル提案書",
        "新規事業計画（案）",
        "新規事業計画(案)",
        "運用ガイドライン",
    ):
        is_template, _is_recurring = _kind_from_title(title)
        assert is_template is True, title


def test_kind_from_title_short_english_fmt_not_matched() -> None:
    # 短い英語 FMT / format は正規資料名（新提案書FMT 等）に誤爆するため対象外。
    for title in ("新提案書FMT", "report_format_2025", "FMT一覧"):
        is_template, _is_recurring = _kind_from_title(title)
        assert is_template is False, title


def test_kind_from_title_plain_proposal_is_neither() -> None:
    assert _kind_from_title("出光興産様向けSNS運用提案書") == (False, False)
    assert _kind_from_title("") == (False, False)


def test_kind_from_title_period_word_alone_is_not_recurring() -> None:
    # 期間語単独（報告系語なし）の提案書タイトルは recurring にしない
    # （提案書intentクエリからの silent drop 防止）。
    for title in (
        "提案_出光興産_2026上期施策",
        "出光興産様 2026年上期プロモーション提案書",
        "【提案書】下期キャンペーン企画_アース製薬",
        "四半期ごとのSNS運用プラン提案書",
        "前年比120%以上期待できる施策のご提案",  # substring 誤爆（以上期待→上期）
        "不定期開催イベントのご提案",  # 「不定期」の 定期 は除外
        "毎月報告会つき運用プランのご提案",  # 「毎月報告」の 月報 は除外
    ):
        _is_template, is_recurring = _kind_from_title(title)
        assert is_recurring is False, title


def test_kind_from_title_weak_template_word_in_campaign_is_not_template() -> None:
    # 弱語（サンプル/ガイドライン/フォーマット）が施策文脈で出るタイトルは template にしない
    # （exclude_templates 常時ONでの全検索不可視化の防止）。
    for title in (
        "無料サンプル配布キャンペーン提案書",
        "サンプリング施策提案書",  # サンプル ⊄ サンプリング
        "ガイドライン策定支援のご提案",
        "薬機法ガイドライン改定対応のご提案",
        "IR資料フォーマット刷新のご提案",
    ):
        is_template, _is_recurring = _kind_from_title(title)
        assert is_template is False, title


def test_kind_from_title_both_flags() -> None:
    assert _kind_from_title("月次報告テンプレート") == (True, True)


def test_classify_llm_flags_read_without_rules_gate() -> None:
    # LLM 出力の is_template / is_recurring は gate 無関係に読む（プロンプト由来）。
    bedrock = _fake_bedrock('{"doc_type": "報告書", "is_template": false, "is_recurring": true}')
    cls = DocClassifier(bedrock, use_kind_rules=False).classify(
        title="ふつうのタイトル", text="x", request_id="r"
    )
    assert cls is not None
    assert cls.is_recurring is True
    assert cls.is_template is False


def test_classify_rules_gate_off_ignores_title(monkeypatch: Any) -> None:
    # 既定（USE_DOC_KIND_RULES 未設定）はタイトルルール無効＝従来挙動と完全一致。
    monkeypatch.delenv("USE_DOC_KIND_RULES", raising=False)
    bedrock = _fake_bedrock('{"doc_type": "提案書"}')
    cls = DocClassifier(bedrock).classify(
        title="2025年上期売上報告テンプレート", text="x", request_id="r"
    )
    assert cls is not None
    assert cls.is_template is False
    assert cls.is_recurring is False


def test_classify_rules_gate_on_via_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("USE_DOC_KIND_RULES", "1")
    bedrock = _fake_bedrock('{"doc_type": "報告書", "is_template": false, "is_recurring": false}')
    cls = DocClassifier(bedrock).classify(title="月次売上データ", text="x", request_id="r")
    assert cls is not None
    assert cls.is_recurring is True  # ルールが LLM(false) より優先（OR マージ）


def test_classify_rules_or_merge_with_llm() -> None:
    # タイトルはテンプレ語なし・LLM が is_template=true → OR で true。
    bedrock = _fake_bedrock('{"doc_type": "その他", "is_template": true}')
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="会社紹介", text="x", request_id="r"
    )
    assert cls is not None
    assert cls.is_template is True
    assert cls.is_recurring is False


def test_classify_bedrock_failure_with_rules_returns_flags_only() -> None:
    # LLM 失敗でもタイトルルールが立てばフラグだけの分類を返す（従来は None）。
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError("bedrock down")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="提案書テンプレート", text="x", request_id="r"
    )
    assert cls == DocClassification(is_template=True)
    assert cls is not None and cls.should_carry_forward is True


def test_classify_bedrock_failure_without_rules_stays_none() -> None:
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError("bedrock down")
    cls = DocClassifier(bedrock, use_kind_rules=False).classify(
        title="提案書テンプレート", text="x", request_id="r"
    )
    assert cls is None  # gate OFF は従来どおり fail-open で None


def test_classify_llm_bool_string_coerced() -> None:
    bedrock = _fake_bedrock('{"is_template": "true", "is_recurring": "no"}')
    cls = DocClassifier(bedrock, use_kind_rules=False).classify(title="t", text="x", request_id="r")
    assert cls is not None
    assert cls.is_template is True
    assert cls.is_recurring is False


def test_as_metadata_flags_only_when_true() -> None:
    md = DocClassification(is_template=True, is_recurring=True).as_metadata()
    assert md == {"cls_is_template": "true", "cls_is_recurring": "true"}
    # 偽ならキー自体を出さない（後方互換・JSONB migration 不要）。
    md2 = DocClassification(project="A社").as_metadata()
    assert "cls_is_template" not in md2
    assert "cls_is_recurring" not in md2


def test_is_empty_false_with_only_flags() -> None:
    assert not DocClassification(is_template=True).is_empty()
    assert not DocClassification(is_recurring=True).is_empty()


def test_prompt_mentions_flags_and_recurring_rule() -> None:
    # プロンプト回 regression 防止: 2 フラグと「定期報告は報告書」の指示を含むこと。
    assert '"is_template"' in _CLASSIFY_SYSTEM_PROMPT
    assert '"is_recurring"' in _CLASSIFY_SYSTEM_PROMPT
    assert "報告書" in _CLASSIFY_SYSTEM_PROMPT
    assert "提案の事例" in _CLASSIFY_SYSTEM_PROMPT
    # ④: 格納フォルダを判断材料にする指示（本文優先の但し書き込み）を含むこと。
    assert "格納フォルダ" in _CLASSIFY_SYSTEM_PROMPT


# -----------------------------------------------------------
# ④ フォルダ置き位置の決定論ルール（_kind_from_folder）＋ classify の folder_name
# -----------------------------------------------------------
def test_kind_from_folder_template_keywords() -> None:
    # 番号 prefix / 年度 suffix / 表記ゆれに耐える（キーワード search）
    assert _kind_from_folder("99_テンプレート") == (True, False)
    assert _kind_from_folder("テンプレ置き場") == (True, False)
    assert _kind_from_folder("00_雛形(2026)") == (True, False)
    assert _kind_from_folder("ひな形フォルダ") == (True, False)
    assert _kind_from_folder("Templates") == (True, False)
    # パス風の入力でも効く
    assert _kind_from_folder("営業共有/99_テンプレ") == (True, False)


def test_kind_from_folder_recurring_keywords() -> None:
    assert _kind_from_folder("03_定期報告") == (False, True)
    assert _kind_from_folder("定期レポート_2026年度") == (False, True)
    assert _kind_from_folder("売上データ") == (False, True)
    assert _kind_from_folder("実績データ置き場") == (False, True)
    assert _kind_from_folder("週報") == (False, True)
    assert _kind_from_folder("定例MTG") == (False, True)


def test_kind_from_folder_weak_words_do_not_fire() -> None:
    """タイトルルールの弱語はフォルダでは不採用（配下全ファイルへ波及するため誤爆コスト大）。"""
    # サンプル/フォーマット/ガイドライン/（案）はフォルダでは発火しない
    assert _kind_from_folder("サンプル動画") == (False, False)
    assert _kind_from_folder("提案フォーマット刷新PJ") == (False, False)
    assert _kind_from_folder("ガイドライン策定支援") == (False, False)
    # 素の「定期」は使わない（定期便等の商材語に誤爆させない）・期間語単独も不採用
    assert _kind_from_folder("定期便キャンペーン") == (False, False)
    assert _kind_from_folder("2026上期") == (False, False)
    assert _kind_from_folder("不定期報告") == (False, False)
    assert _kind_from_folder("") == (False, False)


def test_classify_folder_rule_or_merged_when_gate_on() -> None:
    """gate ON: フォルダのテンプレ語がタイトル/LLM(false) より優先（OR マージ）。"""
    bedrock = _fake_bedrock('{"doc_type": "提案書", "is_template": false, "is_recurring": false}')
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="アース製薬向け提案", text="x", request_id="r", folder_name="99_テンプレート"
    )
    assert cls is not None
    assert cls.is_template is True
    assert cls.is_recurring is False


def test_classify_folder_rule_ignored_when_gate_off() -> None:
    """gate OFF（既定）: フォルダ決定論は無効（フラグは LLM 出力のみ＝既定でも安全）。"""
    bedrock = _fake_bedrock('{"doc_type": "提案書"}')
    cls = DocClassifier(bedrock, use_kind_rules=False).classify(
        title="アース製薬向け提案", text="x", request_id="r", folder_name="99_テンプレート"
    )
    assert cls is not None
    assert cls.is_template is False
    assert cls.is_recurring is False


def test_classify_folder_name_added_to_prompt_as_hint() -> None:
    """(b) Haiku ヒント: folder_name があれば user prompt に「格納フォルダ: XX」行が入る。"""
    bedrock = _fake_bedrock('{"doc_type": "提案書"}')
    DocClassifier(bedrock, use_kind_rules=False).classify(
        title="t", text="x", request_id="r", folder_name="提案事例アーカイブ"
    )
    user_text = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "格納フォルダ: 提案事例アーカイブ\n" in user_text


def test_classify_without_folder_name_prompt_unchanged() -> None:
    """folder_name 未指定（既定 ""）なら prompt は従来とバイト等価（後方互換）。"""
    bedrock = _fake_bedrock('{"doc_type": "提案書"}')
    DocClassifier(bedrock, use_kind_rules=False).classify(
        title="タイトル", text="本文", request_id="r"
    )
    user_text = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "格納フォルダ" not in user_text
    assert user_text.startswith("資料タイトル: タイトル\n\n本文抜粋")


def test_classify_bedrock_failure_with_folder_rule_returns_flags_only() -> None:
    """LLM 失敗でもフォルダルールが立てばフラグだけの分類を返す（タイトルルールと対称）。"""
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError("bedrock down")
    cls = DocClassifier(bedrock, use_kind_rules=True).classify(
        title="ふつうのタイトル", text="x", request_id="r", folder_name="03_定期報告"
    )
    assert cls == DocClassification(is_recurring=True)
    assert cls is not None and cls.should_carry_forward is True
