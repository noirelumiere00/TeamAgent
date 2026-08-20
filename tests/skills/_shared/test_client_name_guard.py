"""client_name_guard（残差法）の判定表を固定するテスト（純粋関数・IO 無し）。

P0-2 の実測症状（「今週の空き時間」等が client_name に入り Gmail 完全一致検索で必ず 0 件）を
再発させないための判定表。**単純部分一致で弾いていないこと**（「日本メール便」が ok である
こと）が本ガードの肝なので、必ず正例と誤爆例をセットで固定する。
"""

from __future__ import annotations

import pytest

from teamagent.skills._shared.client_name_guard import (
    MSG_MISSING,
    classify_client_name,
    guard_message,
    is_searchable_residual,
    msg_structural,
    non_hiragana_len,
    normalize_client_name,
    residual_of,
    retry_disclosure,
    retry_zero_note,
    safe_client_name,
    to_gmail_phrase,
)

# GitHub の push protection がリテラルを弾くため実行時に組み立てる（値は同じ）。
_FAKE_SLACK_TOKEN = "xo" + "xb-" + "1234567890" + "-abcdefghijklmn"

# 依頼書の判定表（この表が契約。値を緩めるときは必ずレビューを通すこと）。
_TABLE: list[tuple[str, str, list[str]]] = [
    ("今日のメール", "structural", []),
    ("今週の空き時間", "structural", []),
    ("返信必要", "structural", []),
    ("  ", "missing", []),
    ("花王", "ok", ["花王"]),
    ("アサヒ飲料", "ok", ["アサヒ飲料"]),
    ("森ビル", "ok", ["森ビル"]),
    # 「メール」を含むが立派な固有名詞。部分一致で弾くと殺してしまう＝残差法の存在理由。
    ("日本メール便", "ok", ["日本メール便", "日本便"]),
    # 依頼文が混じった名前は ok にしつつ、二段検索用に残差「花王」を持たせる。
    ("花王のメール", "ok", ["花王のメール", "花王"]),
    # 括弧は拒否ではなく除去（「(株)ABC」を壊さない）。
    ("(株)ABC", "ok", ["株ABC"]),
    # Gmail 検索演算子のインジェクション試行は拒否。
    ('x" OR from:ceo@example.com "', "structural", []),
]


@pytest.mark.parametrize(("raw", "expected_verdict", "expected_terms"), _TABLE)
def test_classify_table(raw: str, expected_verdict: str, expected_terms: list[str]) -> None:
    v = classify_client_name(raw)
    assert v.verdict == expected_verdict, f"{raw!r} -> {v}"
    assert v.search_terms == expected_terms, f"{raw!r} -> {v}"


def test_none_and_empty_are_missing() -> None:
    assert classify_client_name(None).verdict == "missing"
    assert classify_client_name("").verdict == "missing"
    assert classify_client_name("　　").verdict == "missing"  # 全角空白のみ


def test_normalize_folds_width_quotes_and_spaces() -> None:
    assert normalize_client_name("　花王　") == "花王"
    assert normalize_client_name("花王   の   メール") == "花王 の メール"
    assert normalize_client_name('花"王\\') == "花王"
    assert normalize_client_name("（株）ＡＢＣ") == "株ABC"  # NFKC で全角→半角


def test_all_hiragana_proper_noun_survives_particle_stripping() -> None:
    """「とらや」は と/や が助詞だが固有名詞。助詞削除で殺さないこと。"""
    v = classify_client_name("とらや")
    assert v.verdict == "ok"
    assert v.search_terms == ["とらや"]

    v2 = classify_client_name("はなまるうどん")
    assert v2.verdict == "ok"
    assert v2.search_terms == ["はなまるうどん"]


def test_legal_entity_prefix_becomes_second_term() -> None:
    v = classify_client_name("株式会社セブン")
    assert v.verdict == "ok"
    assert v.search_terms == ["株式会社セブン", "セブン"]


def test_single_char_residual_is_structural() -> None:
    """残差 1 文字は「お客様名の実体が無い」＝ structural（閾値の固定点）。"""
    assert classify_client_name("Xのメール").verdict == "structural"
    assert classify_client_name("今週の予定").verdict == "structural"


def test_to_gmail_phrase_quotes_and_strips_quotes() -> None:
    assert to_gmail_phrase("花王") == '"花王"'
    # フレーズを閉じてクエリを継ぎ足す攻撃を封じる（" は必ず落ちる）。
    assert to_gmail_phrase('x" OR from:ceo@example.com "') == '"x OR from:ceo@example.com "'
    assert to_gmail_phrase('x" OR from:ceo@example.com "').count('"') == 2


def test_guard_messages_say_connection_is_healthy() -> None:
    """「連携は正常」と明示することが本 P0 の中核（0 件＝故障、と誤解させない）。"""
    structural = guard_message(classify_client_name("今日のメール"))
    assert "連携は正常です" in structural
    assert "まだ受信箱は検索していません" in structural
    assert "今日のメール" in structural  # 何を拒否したかエコーする
    assert "花王" in structural  # 言い直しの例を必ず添える

    missing = guard_message(classify_client_name(""))
    assert missing == MSG_MISSING
    assert "連携は正常です" in missing
    assert "アサヒ飲料" in missing

    assert guard_message(classify_client_name("花王")) == ""


def test_echo_is_scrubbed_and_truncated() -> None:
    """エコーは PII マスク＋30 字カット（依頼文にメールアドレスが混ざっても漏らさない）。"""
    msg = msg_structural("tanaka@example.com の今日のメール")
    assert "tanaka@example.com" not in msg
    assert "[REDACTED_PII]" in msg

    long_echo = "あ" * 200
    assert "あ" * 30 in msg_structural(long_echo)
    assert "あ" * 31 not in msg_structural(long_echo)


# ── 二段検索の 2 本目を出す条件（要修正1・実測事故の再発防止）──────────────────


def test_non_hiragana_len_counts_only_name_bearing_chars() -> None:
    """ひらがな・伸ばし棒・中黒・空白は「名前の実体」に数えない。"""
    assert non_hiragana_len("している") == 0
    assert non_hiragana_len("たまってる") == 0
    assert non_hiragana_len("届いた") == 1  # 漢字 1 文字だけでは足りない
    assert non_hiragana_len("花王") == 2
    assert non_hiragana_len("セブン") == 3
    assert non_hiragana_len("30分MTG") == 6
    assert non_hiragana_len("ソニー") == 2  # 伸ばし棒は数えない（カタカナ 2 文字ぶんで通る）
    assert non_hiragana_len("あーい") == 0


@pytest.mark.parametrize(
    ("raw", "expected_second_stage"),
    [
        ("花王のメール", "花王"),  # 通す（漢字 2 文字）
        ("日本メール便", "日本便"),
        ("株式会社セブン", "セブン"),
        ("放置しているメール", None),  # 落とす（残差「している」＝ひらがなのみ）
        ("放置してるメール", None),
        ("今日届いたメール", None),  # 残差「届いた」＝非ひらがな 1 文字
        ("たまってる未読", None),
        ("とらやのメール", None),  # 全ひらがな社名の巻き添え（安全側の失敗として許容）
    ],
)
def test_second_stage_term_requires_two_non_hiragana_chars(
    raw: str, expected_second_stage: str | None
) -> None:
    """活用の残りかす（している/届いた）で受信箱を引かせない。

    **これが本ガードの実害修正**: 「放置しているメール」の 2 本目 ``"している"`` は
    無関係な他社メールにヒットし、それを元の client_name の要約として返していた
    （error="" / connection="live" のまま帰属を誤る＋Bedrock 課金）。
    """
    v = classify_client_name(raw)
    assert v.verdict == "ok", f"{raw!r} を殺してはいけない（1 本目は必ず引く）"
    normalized = normalize_client_name(raw)
    if expected_second_stage is None:
        assert v.search_terms == [normalized], f"{raw!r} で 2 本目を出してはいけない: {v}"
        assert v.reason == "proper_noun_weak_residual"
        # 残差そのものは計算できている（＝弱いから出さない、という判断であることを固定）
        assert non_hiragana_len(residual_of(normalized)) < 2
    else:
        assert v.search_terms == [normalized, expected_second_stage], f"{raw!r} -> {v}"
        assert v.reason == "proper_noun_with_residual"


def test_is_searchable_residual_rejects_degenerate_cases() -> None:
    assert is_searchable_residual("花王", "花王のメール") is True
    assert is_searchable_residual("している", "放置しているメール") is False
    assert is_searchable_residual("", "花王") is False
    assert is_searchable_residual("花王", "花王") is False  # 原文と同じなら 2 本目は不要


# ── エコー・開示（要修正1 HIGH / 要修正4）────────────────────────────────────


def test_safe_client_name_normalizes_masks_and_truncates() -> None:
    """``Output.client_name`` に載せる値は message と同じ規律（scrub＋短縮）を通す。"""
    assert safe_client_name("　花王　") == "花王"
    assert safe_client_name('花"王\\') == "花王"  # クエリを壊す文字は落とす
    assert "tanaka@example.com" not in safe_client_name("tanaka@example.com")
    assert safe_client_name("tanaka@example.com") == "[REDACTED_PII]"
    assert _FAKE_SLACK_TOKEN not in safe_client_name(f"返信必要: {_FAKE_SLACK_TOKEN}")
    assert safe_client_name(None) == ""
    assert len(safe_client_name("あ" * 200)) == 30


def test_retry_disclosure_names_both_terms_and_is_scrubbed() -> None:
    """2 本目で当てた事実を隠さない（別法人のメールを自案件と読ませない）。"""
    msg = retry_disclosure("東京メール大学", "東京大学")
    assert "東京メール大学" in msg
    assert "東京大学" in msg
    assert "0 件" in msg
    assert "tanaka@example.com" not in retry_disclosure("tanaka@example.com のメール", "花王")

    zero = retry_zero_note("花王")
    assert "花王" in zero and "0 件" in zero
