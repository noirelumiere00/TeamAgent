"""connect_diagnostics（連携失敗の診断コード・単一情報源）の単体テスト。

固定するもの:
- 全コードに DiagSpec（意味・対処・ログ event）がある＝runbook と同じ表がコードで引ける。
- 利用者向け定型文は「対処 → 転送案内（管理者名）→ 診断行」で、診断行に
  コード・時刻(JST)・マスク済み識別子・request_id が入る。
- 診断行に秘匿値（state / code / token / 素のメール）が入らない。素のメールが渡されても
  マスクされる（二重防御）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from teamagent.connect_diagnostics import (
    ADMIN_FORWARD_HINT,
    ADMIN_NAME,
    DIAG_SPECS,
    IDENTITY_REJECT_REASON_CODES,
    ConnectDiag,
    format_diag_line,
    format_user_message,
    format_when,
    identity_reject_code,
    mask_email,
    now_jst,
)

_WHEN = datetime(2026, 9, 3, 1, 15, 42, tzinfo=UTC)  # = 2026-09-03 10:15 JST
_WHEN_JST_TEXT = "2026-09-03 10:15 JST"
_STATE_LIKE = "dGFyb0B2ZWN0b3JpbmMuY28uanB8MTc1NjkwMDAwMHxub25jZXxkZWFkYmVlZg=="


def test_every_code_has_a_spec_and_matching_code() -> None:
    assert set(DIAG_SPECS) == set(ConnectDiag)
    for code, spec in DIAG_SPECS.items():
        assert spec.code is code
        assert spec.meaning and spec.user_action and spec.log_events
        assert code.value.startswith("CONNECT-")


def test_codes_are_unique_strings() -> None:
    values = [c.value for c in ConnectDiag]
    assert len(values) == len(set(values))
    # 仕様の 14 コード（S01-S06 / I01a-c / I02 / I03 / L01 / T01 / T02）
    assert len(values) == 14


@pytest.mark.parametrize("code", list(ConnectDiag))
def test_user_message_carries_code_time_masked_email_and_request_id(code: ConnectDiag) -> None:
    msg = format_user_message(
        code,
        when=_WHEN,
        request_id="req-abc123",
        masked_email=mask_email("taro@vectorinc.co.jp"),
    )
    lines = msg.splitlines()
    assert lines[0] == DIAG_SPECS[code].user_action
    assert lines[1] == ADMIN_FORWARD_HINT
    assert ADMIN_NAME in ADMIN_FORWARD_HINT
    diag = lines[-1]
    assert diag == f"診断: {code.value} {_WHEN_JST_TEXT} t***@vectorinc.co.jp req-abc123"


def test_diag_line_never_contains_raw_email_even_if_caller_forgot_to_mask() -> None:
    line = format_diag_line(ConnectDiag.S01, when=_WHEN, masked_email="taro@vectorinc.co.jp")
    assert "taro@" not in line
    assert "t***@vectorinc.co.jp" in line


def test_diag_line_uses_slack_user_id_when_no_email() -> None:
    line = format_diag_line(ConnectDiag.I01A, when=_WHEN, extra="U0123456789")
    assert line == f"診断: CONNECT-I01a {_WHEN_JST_TEXT} U0123456789"


def test_diag_line_without_identifiers_and_request_id_still_well_formed() -> None:
    line = format_diag_line(ConnectDiag.S06, when=_WHEN)
    assert line == f"診断: CONNECT-S06 {_WHEN_JST_TEXT} -"


def test_diag_line_does_not_embed_anything_but_given_tokens() -> None:
    """state のような値は引数に無い＝出力に現れない（呼び出し側の責務も含めて固定）。"""
    msg = format_user_message(
        ConnectDiag.S01, when=_WHEN, request_id="r1", masked_email="t***@vectorinc.co.jp"
    )
    assert _STATE_LIKE not in msg
    assert "state" not in msg.lower()
    assert "token" not in msg.lower()


def test_format_when_converts_to_jst_and_treats_naive_as_utc() -> None:
    assert format_when(_WHEN) == _WHEN_JST_TEXT
    assert format_when(_WHEN.replace(tzinfo=None)) == _WHEN_JST_TEXT
    ny = _WHEN.astimezone(timezone(timedelta(hours=-4)))
    assert format_when(ny) == _WHEN_JST_TEXT


def test_now_jst_is_aware_jst() -> None:
    now = now_jst()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=9)


def test_mask_email_matches_skill_convention() -> None:
    assert mask_email("taro@vectorinc.co.jp") == "t***@vectorinc.co.jp"
    assert mask_email("@vectorinc.co.jp") == "***@vectorinc.co.jp"
    assert mask_email("not-an-email") == "***"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("missing_verified_caller", ConnectDiag.I01A),
        ("resolver_error", ConnectDiag.I01B),
        ("resolve_none", ConnectDiag.I01C),
        ("something_new", ConnectDiag.I01A),
    ],
)
def test_identity_reject_code_maps_gateway_reasons(reason: str, code: ConnectDiag) -> None:
    assert identity_reject_code(reason) is code
    if reason in IDENTITY_REJECT_REASON_CODES:
        assert IDENTITY_REJECT_REASON_CODES[reason] is code


def test_user_actions_point_to_reissue_or_admin() -> None:
    """利用者の対処は『連携し直す/許可し直す』か『管理者へ』のどれかに必ず着地する。"""
    for spec in DIAG_SPECS.values():
        assert any(w in spec.user_action for w in ("連携", "許可", "管理者")), spec.code
