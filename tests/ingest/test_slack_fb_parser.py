"""Slack 営業 FB parser のユニットテスト。

実投稿 (4 件) を fixture 化して構造化結果を検証する。
PII (実顧客名・実営業担当者名) は仮名化する。
"""

from __future__ import annotations

from teamagent.ingest.slack_fb_parser import (
    extract_client_name,
    map_fb_fields,
    parse_fb_post,
)


def _fb_sample_hearing() -> str:
    """商談フェーズ=ヒアリング のサンプル (SCSK/スモカ歯磨 ベース、仮名化)。"""
    return (
        "[2026-05-27 10:00] <B0ATTE7LXME>: <!channel>\n"
        "\n"
        "<@U999XXXXX> さんからの共有です！\n"
        "\n"
        "\n"
        "*商流*\n"
        "代理店\n"
        "*顧客名*\n"
        "アルファ広告社\n"
        "*顧客名/案件名*\n"
        "ベータ商事 / ガンマ製品\n"
        "*商談フェーズ*\n"
        "ヒアリング\n"
        "*提案メニュー*\n"
        "UGC（TTO，切り抜きなど）\n"
        "*商談感触（BANT）*\n"
        "B（前向き）\n"
        "*顧客反応（ポジティブ）*\n"
        "ショート動画戦略は特定製品への適合性が高いと評価され、"
        "従来のB2B広告手法を補完する選択肢として具体的な検討が進められる。\n"
        "*顧客反応（質問事項、ネガティブ）*\n"
        "どの商材をどう組み合わせるかによって見積もりが異なる点、"
        "また進行管理費用として別途15%が必要となる点について確認があった。\n"
        "*ネクストアクション*\n"
        "事例動画・サービス資料の共有、CLさんへ一次提案後に具体的なPKGの調整\n"
    )


def _fb_sample_keipa_multiline() -> str:
    """商談フェーズ=ケイパ のサンプル (日本ガイシ/リクルーティング ベース、仮名化、
    ポジ/ネガ反応が箇条書き複数行)。"""
    return (
        "[2026-05-19 11:08] <B0AKBPSAHD0>: <!channel>\n"
        "\n"
        "<@U999YYYYY> さんからの共有です！\n"
        "\n"
        "\n"
        "*商流*\n"
        "代理店\n"
        "*顧客名*\n"
        "デルタ広告中部\n"
        "*顧客名/案件名*\n"
        "エプシロン工業/リクルーティング\n"
        "*商談フェーズ*\n"
        "ケイパ\n"
        "*提案メニュー*\n"
        "UGC（TTO、切り抜きなど）\n"
        "*商談感触（BANT）*\n"
        "B（前向き）\n"
        "*顧客反応（ポジティブ）*\n"
        "・既存のTTO（BtoC向け）をBtoB向けに活用できる可能性を認識\n"
        "・年齢層ターゲティングへの不安が解消：\n"
        "ゼータ案件の実績事例（40代以上層へのリーチ成功）を示すことで、"
        "TikTokでもターゲット層への配信が可能であることを確信\n"
        "・PDCA運用の柔軟性を高く評価\n"
        "*顧客反応（質問事項、ネガティブ）*\n"
        "・テレビCMとの予算配分に関する懸念：\n"
        "クライアントがテレビCMに大きな予算を配分している状況では、新規予算獲得は難しい見通し\n"
        "・BtoB企業の難解な製品紹介の難しさ\n"
        "*ネクストアクション*\n"
        "・クライアント提案準備：\n"
        "営業チーム内で情報共有し、適合するクライアントにアプローチ\n"
        "・イータ案件等の事例共有：\n"
        "クイズ形式など難解な製品でも視聴者を引き付ける工夫事例を別途共有\n"
        "*共有メモ*\n"
        "\n"
    )


def _fb_sample_with_meta_url() -> str:
    """共有メモ欄が空 + 末尾に Google Spreadsheet リンクが付くケース (実投稿フォーマット)。"""
    return (
        "<@U999ZZZZZ> さんからの共有です！\n"
        "\n"
        "*商流*\n"
        "直販\n"
        "*顧客名*\n"
        "アルファ広告社\n"
        "*顧客名/案件名*\n"
        "シータ食品\n"
        "*商談フェーズ*\n"
        "2回目以降提案\n"
        "*提案メニュー*\n"
        "【メディア】グレースモード（EMMEなど）\n"
        "*商談感触（BANT）*\n"
        "B（前向き）\n"
        "*顧客反応（ポジティブ）*\n"
        "EMMEメイトで、Xでの話題形成をご提案したが、施策の考え方や話題形成の手法としては◯\n"
        "*顧客反応（質問事項、ネガティブ）*\n"
        "ブランドの現状的には少し早いかも\n"
        "*ネクストアクション*\n"
        "10月までの半期でのご提案で認知率向上に向けて広告施策とPRをかけ合わせたご提案\n"
        "*共有メモ*\n"
        "\n"
        "\n"
        "これまで共有されたフィードバックは"
        "<https://docs.google.com/spreadsheets/d/REDACTED/edit|こちら>\n"
    )


def _non_fb_chitchat() -> str:
    """営業 FB じゃない通常の Slack 投稿 (雑談・告知)。parse は空 dict を返すべき。"""
    return (
        "[2026-05-27 09:00] <@U123XYZ>: みなさんおはようございます！\n"
        "今日の朝会は10時からです。Zoom リンクは下記。\n"
        "https://zoom.us/j/XXXX\n"
    )


def _non_fb_with_one_bold() -> str:
    """`*foo*` が 1 個だけ含まれる通常投稿 (誤判定しないこと)。"""
    return "*重要* 明日は休業日です。緊急連絡は私の携帯まで。\n"


# ==================================================================
# parse_fb_post
# ==================================================================
def test_parse_hearing_post_extracts_all_known_fields() -> None:
    meta = parse_fb_post(_fb_sample_hearing())
    assert meta["channel_type"] == "代理店"
    assert meta["agency_name"] == "アルファ広告社"
    assert meta["client_case"] == "ベータ商事 / ガンマ製品"
    assert meta["deal_phase"] == "ヒアリング"
    assert meta["proposed_menu"] == "UGC（TTO，切り抜きなど）"
    assert meta["bant_score"] == "B（前向き）"
    assert "従来のB2B広告手法を補完" in meta["positive_reaction"]
    assert "進行管理費用として別途15%" in meta["negative_reaction"]
    assert "CLさんへ一次提案" in meta["next_action"]


def test_parse_keipa_post_preserves_multiline_bullets() -> None:
    meta = parse_fb_post(_fb_sample_keipa_multiline())
    assert meta["deal_phase"] == "ケイパ"
    # 箇条書き複数行が値内に保持される
    assert meta["positive_reaction"].count("\n") >= 3
    assert "・既存のTTO" in meta["positive_reaction"]
    assert "・年齢層ターゲティング" in meta["positive_reaction"]
    assert "・PDCA運用の柔軟性" in meta["positive_reaction"]
    assert "・テレビCMとの予算配分" in meta["negative_reaction"]
    assert "・BtoB企業の難解な製品紹介" in meta["negative_reaction"]
    # 空欄ラベル (共有メモ) は出力に含めない (値が空)
    assert "shared_memo" not in meta


def test_parse_post_with_trailing_url_block() -> None:
    """末尾の Spreadsheet リンクブロックは無視して既知ラベルだけ拾う。"""
    meta = parse_fb_post(_fb_sample_with_meta_url())
    assert meta["agency_name"] == "アルファ広告社"
    assert meta["client_case"] == "シータ食品"
    assert meta["deal_phase"] == "2回目以降提案"
    # 末尾 URL は metadata に混入しない
    for v in meta.values():
        assert "spreadsheets" not in v


def test_non_fb_post_returns_empty_dict() -> None:
    """営業 FB じゃない通常投稿は空 dict を返す (副作用ゼロ)。"""
    assert parse_fb_post(_non_fb_chitchat()) == {}


def test_single_bold_marker_post_returns_empty_dict() -> None:
    """`*重要*` 等のラベル 1 個だけの通常投稿は誤分類しない。"""
    assert parse_fb_post(_non_fb_with_one_bold()) == {}


def test_empty_string_returns_empty_dict() -> None:
    assert parse_fb_post("") == {}


def test_parse_empty_client_case_does_not_eat_next_label() -> None:
    """Day 8 (2026-05-28) 回帰テスト: 空 value のとき次ラベル行を吸い込まない。

    `*顧客名/案件名*` が空のとき、本番 RDS 19 件で client_name に
    `*商談フェーズ*\\nケイパ` のような壊れた値が入っていたバグの再発防止。
    """
    content = (
        "*商流*\n直販\n"
        "*顧客名*\nアルファ広告社\n"
        "*顧客名/案件名*\n"  # 値なし、すぐ次のラベル行
        "*商談フェーズ*\nケイパ\n"
        "*提案メニュー*\n"  # こちらも値なし
        "*商談感触（BANT）*\nB（前向き）\n"
        "*顧客反応（ポジティブ）*\n良い反応\n"
    )
    meta = parse_fb_post(content)
    # client_case は空欄なので metadata に含まれない（空 value は drop される）
    assert "client_case" not in meta
    # 重要: deal_phase が正しく 'ケイパ' のみ（前 label を吸い込んでない）
    assert meta["deal_phase"] == "ケイパ"
    assert meta["channel_type"] == "直販"
    assert meta["agency_name"] == "アルファ広告社"
    assert meta["bant_score"] == "B（前向き）"
    assert meta["positive_reaction"] == "良い反応"
    # proposed_menu も空欄なので metadata に含まれない
    assert "proposed_menu" not in meta


def test_extract_client_name_from_empty_client_case_falls_back() -> None:
    """空 client_case のとき extract_client_name は agency_name にフォールバックする。"""
    meta = {"agency_name": "アルファ広告社"}  # client_case 無し
    assert extract_client_name(meta) == "アルファ広告社"


def test_unknown_labels_are_ignored() -> None:
    """未知ラベル (例: `*天気*`) は metadata 化されない。"""
    content = (
        "*商流*\n直販\n"
        "*顧客名*\nアルファ\n"
        "*顧客名/案件名*\nベータ\n"
        "*商談フェーズ*\nヒアリング\n"
        "*天気*\n晴れ\n"
        "*謎ラベル*\n値\n"
    )
    meta = parse_fb_post(content)
    assert "weather" not in meta
    assert meta["channel_type"] == "直販"
    # 未知ラベルが metadata に key として現れない
    assert all(k in {"channel_type", "agency_name", "client_case", "deal_phase"} for k in meta)


# ==================================================================
# map_fb_fields (gsheets フォーム回答行との共通コア)
# ==================================================================
def test_map_fb_fields_sheet_headers_with_variants() -> None:
    """フォーム回答シートの実ヘッダ (表記ゆれ込み) を metadata に写像する。

    シート特有の表記ゆれ (2026-07-03 実データ確認):
    - 「顧客名・案件名」 ('・' 区切り) → client_case
    - 「顧客反応(ポジ・ネガ)」 (半角括弧・ポジネガ統合 1 列) → client_reaction
    """
    fields = {
        "タイムスタンプ": "2026/06/30 10:00:00",
        "商流": "代理店",
        "顧客名": "アルファ広告社",
        "顧客名・案件名": "ベータ商事 / ガンマ製品",
        "商談フェーズ": "ヒアリング",
        "商談感触（BANT）": "B（前向き）",
        "顧客反応(ポジ・ネガ)": "ポジ: 適合性を評価 / ネガ: 予算感に懸念",
        "提案メニュー": "UGC（TTO、切り抜きなど）",
    }
    meta = map_fb_fields(fields)
    assert meta["channel_type"] == "代理店"
    assert meta["agency_name"] == "アルファ広告社"
    assert meta["client_case"] == "ベータ商事 / ガンマ製品"
    assert meta["deal_phase"] == "ヒアリング"
    assert meta["bant_score"] == "B（前向き）"
    assert meta["client_reaction"] == "ポジ: 適合性を評価 / ネガ: 予算感に懸念"
    assert meta["proposed_menu"] == "UGC（TTO、切り抜きなど）"
    # 未知ヘッダ (タイムスタンプ) は写像されない
    assert set(meta) == {
        "channel_type",
        "agency_name",
        "client_case",
        "deal_phase",
        "bant_score",
        "client_reaction",
        "proposed_menu",
    }
    # extract_client_name も従来どおり導出できる ('/' 左側)
    assert extract_client_name(meta) == "ベータ商事"


def test_map_fb_fields_normalizes_half_width_parens_and_whitespace() -> None:
    """半角括弧「商談感触(BANT)」・前後空白付きヘッダも canonical 扱いでコア判定に乗る。"""
    fields = {
        " 商流 ": "直販",
        "顧客名": "アルファ広告社",
        "商談フェーズ": "ケイパ",
        "商談感触(BANT)": "B（前向き）",
    }
    meta = map_fb_fields(fields)
    assert meta["channel_type"] == "直販"
    assert meta["bant_score"] == "B（前向き）"
    assert meta["deal_phase"] == "ケイパ"


def test_map_fb_fields_non_fb_headers_return_empty_dict() -> None:
    """ナレッジ共有フォーム等の非 FB ヘッダ (コア列 < 3) は空 dict (副作用ゼロ)。"""
    fields = {
        "タイムスタンプ": "2026/06/30 10:00:00",
        "共有者": "山田太郎",
        "ナレッジ内容": "TikTok の新機能について",
        "カテゴリ": "メディア動向",
    }
    assert map_fb_fields(fields) == {}


def test_map_fb_fields_core_headers_present_but_below_threshold() -> None:
    """コア列 2 個 (< _FB_MIN_CORE_HITS=3) では FB と認定しない。"""
    fields = {"商流": "直販", "顧客名": "アルファ広告社", "備考": "メモ"}
    assert map_fb_fields(fields) == {}


def test_map_fb_fields_drops_empty_values() -> None:
    """FB シートでも空値の列は metadata に含めない (全コア値が空なら空 dict)。"""
    headers_all_empty = {
        "商流": "",
        "顧客名": "",
        "顧客名・案件名": "",
        "商談フェーズ": "",
        "商談感触（BANT）": "",
    }
    # コア列は揃っている (閾値は通過する) が、値が全部空 → 空 dict
    assert map_fb_fields(headers_all_empty) == {}

    partially_filled = dict(headers_all_empty, 商談フェーズ="ヒアリング")
    meta = map_fb_fields(partially_filled)
    assert meta == {"deal_phase": "ヒアリング"}


def test_map_fb_fields_empty_input() -> None:
    assert map_fb_fields({}) == {}


# ==================================================================
# extract_client_name
# ==================================================================
def test_extract_client_name_from_slash_separated() -> None:
    """'SCSK / スモカ歯磨' → 'SCSK' (主クライアント)。"""
    meta = {"client_case": "SCSK / スモカ歯磨"}
    assert extract_client_name(meta) == "SCSK"


def test_extract_client_name_from_single_name() -> None:
    """スラッシュなし → そのまま返す。"""
    meta = {"client_case": "ニチレイ"}
    assert extract_client_name(meta) == "ニチレイ"


def test_extract_client_name_falls_back_to_agency() -> None:
    """client_case 空 → agency_name を返す。"""
    meta = {"agency_name": "アルファ広告社", "client_case": ""}
    assert extract_client_name(meta) == "アルファ広告社"


def test_extract_client_name_returns_none_when_both_empty() -> None:
    assert extract_client_name({}) is None
    assert extract_client_name({"client_case": "", "agency_name": ""}) is None
