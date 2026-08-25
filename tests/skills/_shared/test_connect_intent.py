"""連携依頼の決定論判定（`_shared/connect_intent.py`）の判定表テスト。

本番実測（2026-08）: 利用者が「連携」と言っても外側 LLM が `oauth_connect` を選ばず、
1・2 ターン目はツール呼び出しゼロ、3 ターン目は `search` に落ちた。判定を純関数に切り出し、
**発火する表記ゆれ**と**発火してはいけない文**の両方を表で凍結する。

⚠️ ここが赤くなったら「表を直す」のではなく、**判定を緩めた/締めすぎたのが本当に正しいか**を
先に確認すること（誤爆＝資料検索の依頼を連携リンクで潰す、は発火漏れより明確に悪い）。
"""

from __future__ import annotations

import pytest

from teamagent.skills._shared.connect_intent import (
    REASON_EMPTY,
    REASON_TOO_LONG,
    detect_connect_intent,
    detect_connect_intent_in_args,
    normalize_connect_text,
)

# ── 発火する表記ゆれ（10 件以上）──────────────────────────────────────────────
FIRES: list[tuple[str, str]] = [
    ("一語だけ", "連携"),
    ("依頼形", "連携して"),
    ("丁寧形", "連携してください"),
    ("お願い形", "連携お願いします"),
    ("サービス名つき", "Google連携したい"),
    ("メール文脈", "メール連携して"),
    ("助詞つき", "アカウント連携をお願いします"),
    ("再連携", "再連携したい"),
    ("接続", "接続して"),
    ("英語", "connect"),
    ("和語動詞", "つないでください"),
    ("メンションつき", "<@U0B990FG03T> 連携"),
    ("記号と絵文字つき", "連携！！🙏"),
    ("認証語", "認証して"),
    ("全角/半角ゆれ", "ｇｏｏｇｌｅ連携したいです"),
    ("長めの丁寧文", "Googleアカウントの連携をお願いしたいです"),
]

# ── 発火してはいけない文（8 件以上）──────────────────────────────────────────
DOES_NOT_FIRE: list[tuple[str, str]] = [
    ("連携が修飾語（提案書）", "〇〇社との連携について提案書を作って"),
    ("連携が修飾語（事例検索）", "他社との連携事例を検索して"),
    ("連携が話題（資料探し）", "連携についての資料を探して"),
    ("長文の中の連携して", "先方と連携して進める件、議事録まとめて"),
    ("施策名としての連携", "連携強化の施策を提案して"),
    ("状態の照会", "Slackの連携状況を教えて"),
    ("そもそも無関係", "この動画を分析して"),
    ("接続の確認依頼", "接続を確認して"),
    ("提案書レビュー", "システム連携の提案書をレビューして"),
    ("空文字", ""),
]


@pytest.mark.parametrize(("label", "text"), FIRES, ids=[label for label, _ in FIRES])
def test_connect_requests_fire(label: str, text: str) -> None:
    assert detect_connect_intent(text).matched, f"発火すべき表記が落ちた: {label} / {text!r}"


@pytest.mark.parametrize(
    ("label", "text"), DOES_NOT_FIRE, ids=[label for label, _ in DOES_NOT_FIRE]
)
def test_non_connect_requests_do_not_fire(label: str, text: str) -> None:
    assert not detect_connect_intent(text).matched, f"誤爆した: {label} / {text!r}"


def test_reason_codes_are_deterministic_and_carry_no_text() -> None:
    """ログへ出すのは理由コードだけ。入力本文が混ざらないことを固定する。"""
    assert detect_connect_intent("").reason == REASON_EMPTY
    assert detect_connect_intent("あ" * 40).reason == REASON_TOO_LONG
    for _, text in FIRES + DOES_NOT_FIRE:
        reason = detect_connect_intent(text).reason
        assert reason.isascii(), f"理由コードは ASCII の決定論コードに保つこと: {reason!r}"
        assert not text or text not in reason, f"理由コードに本文が混ざった: {reason!r}"


def test_long_request_never_fires_even_with_core_word() -> None:
    """『短い依頼文で主辞のときだけ』を長さでも宣言している（多層の 1 枚目）。"""
    padded = "これはとても長い依頼文でありまして本当に長いのですがところで連携して"
    assert detect_connect_intent(padded).reason == REASON_TOO_LONG
    assert not detect_connect_intent(padded).matched


def test_normalization_drops_mentions_urls_symbols_and_case() -> None:
    assert normalize_connect_text("<@U09CX1CCBLN> 連携！") == "連携"
    assert normalize_connect_text("連携 https://example.com/x?y=1") == "連携"
    assert normalize_connect_text("@aico 連携") == "連携"
    assert normalize_connect_text("CONNECT") == "connect"
    assert normalize_connect_text(None) == ""


# ── 引数からの判定（MCP 境界が使う口）────────────────────────────────────────


def test_args_detection_reads_free_text_fields_only() -> None:
    assert detect_connect_intent_in_args({"query": "連携して"}).matched
    assert detect_connect_intent_in_args({"goal": "連携"}).matched
    assert detect_connect_intent_in_args({"query": "花王の資料"}).matched is False


def test_args_detection_ignores_client_name() -> None:
    """『コネクト』『連動』は実在しうる社名。固有名詞欄は連携依頼に化けさせない。"""
    assert not detect_connect_intent_in_args({"client_name": "コネクト"}).matched
    assert not detect_connect_intent_in_args({"client_name": "連携"}).matched


def test_args_detection_reports_field_without_the_value() -> None:
    intent = detect_connect_intent_in_args({"query": "連携してください"})
    assert intent.field == "query"
    assert intent.matched


def test_args_detection_ignores_non_string_values() -> None:
    assert not detect_connect_intent_in_args({"query": 123, "goal": None}).matched
