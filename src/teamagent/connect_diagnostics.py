"""連携（oauth_connect → Google/Slack 認可 → connect-web callback）失敗の診断コード。

**単一情報源**: 連携が失敗したときに利用者へ見せる「診断: CONNECT-xxx …」行と、その意味・
利用者の対処・対応するログ event は、すべてここで定義する。mcp(oauth_connect / gateway) と
connect-web(callback) の両方がこのモジュールを使い、文言を個別に持たない。

なぜ必要か（2026-09-03 実測）:
  連携の失敗は 9 型あるが、利用者に見える文言は「検証に失敗しました。リンクが古いか不正です」
  等の数種類しかなく、利用者にも管理者にも原因が分からない。管理者へは「うまくいかない」とだけ
  問い合わせが来て、ログを時刻と名前から手探りで引くことになる。
  → 失敗経路ごとに固定の **診断コード** を振り、利用者がそのまま転送できる 1 行
  （コード・時刻(JST)・マスク済み識別子・request_id）を全経路の末尾に付ける。
  管理者は runbook（docs/runbooks/connect_diagnostics.md）のコード表から意味とログの引き方へ
  直行できる。

秘匿値の扱い（不変条件）:
  診断行には state / code / token / secret を **絶対に含めない**。含めてよいのは
  コード・時刻・マスク済みメール（``mask_email``）・Slack user ID・request_id だけ。
  ``format_user_message`` は渡された値以外を文字列化しない（呼び出し側が秘匿値を渡さない責務）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

JST = timezone(timedelta(hours=9), name="JST")

# 管理者名（利用者向け定型文に載せる転送先）。env CONNECT_ADMIN_NAME で差し替え可能。
ADMIN_NAME_ENV = "CONNECT_ADMIN_NAME"
DEFAULT_ADMIN_NAME = "小俣"


def admin_name() -> str:
    """転送先の管理者名（mcp / connect-web 共通・``CONNECT_ADMIN_NAME``・既定 ``小俣``）。"""
    return os.environ.get(ADMIN_NAME_ENV, "").strip() or DEFAULT_ADMIN_NAME


def admin_forward_hint() -> str:
    """利用者向け定型文（全経路共通・診断行の直前に必ず置く）。"""
    return f"解決しない場合は、次の 1 行をそのまま管理者（{admin_name()}）へ送ってください:"


# 「連携」と話しかけて新しいリンクを取り直す、という最も多い対処。
_REISSUE_ACTION = "Slack で Aico に『連携』と送って、新しいリンクを使ってください。"
_CONTACT_ADMIN_ACTION = "利用者側の操作では直りません。管理者へご連絡ください。"


class ConnectDiag(StrEnum):
    """連携失敗の診断コード（利用者に見せる固定文字列）。

    体系: ``CONNECT-<系統><番号>``
      S = state / Google callback（connect-web）
      I = identity（本人特定・mcp 側）
      L = link（連携リンク生成・mcp 側）
      T = Slack callback（connect-web）
    """

    S01 = "CONNECT-S01"  # state 署名不一致（リンクが途中で改変された）
    S02 = "CONNECT-S02"  # state 期限切れ（発行から 30 分超）
    S03 = "CONNECT-S03"  # 使用済みリンク
    S04 = "CONNECT-S04"  # Google アカウント不一致
    S05 = "CONNECT-S05"  # 利用者が許可画面で拒否
    S06 = "CONNECT-S06"  # サーバ側障害（設定不備・交換失敗・保存失敗・id_token 不正）
    I01A = "CONNECT-I01a"  # 本人特定失敗: missing_verified_caller
    I01B = "CONNECT-I01b"  # 本人特定失敗: resolver_error
    I01C = "CONNECT-I01c"  # 本人特定失敗: resolve_none
    I02 = "CONNECT-I02"  # メール未取得（no_user_email）
    I03 = "CONNECT-I03"  # Slack 再連携が必要（slack_rebind_needed）
    L01 = "CONNECT-L01"  # 連携リンク生成失敗
    T01 = "CONNECT-T01"  # Slack 側 state 不正/期限切れ/使用済み
    T02 = "CONNECT-T02"  # Slack team 不一致 / identity mismatch / uid collision


@dataclass(frozen=True)
class DiagSpec:
    """1 コードぶんの意味・利用者の対処・対応するログ event（runbook と同じ内容）。"""

    code: ConnectDiag
    meaning: str
    user_action: str
    log_events: tuple[str, ...]


DIAG_SPECS: dict[ConnectDiag, DiagSpec] = {
    ConnectDiag.S01: DiagSpec(
        ConnectDiag.S01,
        "state 署名不一致（リンクが途中で改変された。LLM の再タイプ・コピー欠け）",
        _REISSUE_ACTION,
        ("connect_callback_bad_state", "connect_start_bad_state"),
    ),
    ConnectDiag.S02: DiagSpec(
        ConnectDiag.S02,
        "state 期限切れ（発行から 30 分超）",
        _REISSUE_ACTION,
        ("connect_callback_bad_state", "connect_start_bad_state"),
    ),
    ConnectDiag.S03: DiagSpec(
        ConnectDiag.S03,
        "使用済みリンク（同じ state の 2 回目以降）",
        _REISSUE_ACTION,
        ("connect_callback_reused_state",),
    ),
    ConnectDiag.S04: DiagSpec(
        ConnectDiag.S04,
        "Google アカウント不一致（別アカウントで許可された）",
        "会社アカウント（表示されているメール）で Google にログインし直してから許可してください。",
        ("connect_callback_account_mismatch",),
    ),
    ConnectDiag.S05: DiagSpec(
        ConnectDiag.S05,
        "利用者が許可画面で拒否（キャンセル）",
        "もう一度 Slack で Aico に『連携』と送り、許可画面で『許可』を押してください。",
        ("connect_callback_user_denied", "connect_slack_callback_user_denied"),
    ),
    ConnectDiag.S06: DiagSpec(
        ConnectDiag.S06,
        "サーバ側障害（state 保管先未設定・消費失敗・token 交換失敗・保存失敗・"
        "id_token 不正・client_id 未設定）",
        _CONTACT_ADMIN_ACTION,
        (
            "connect_callback_state_store_unconfigured",
            "connect_callback_state_consume_failed",
            "connect_callback_exchange_failed",
            "connect_callback_store_failed",
            "connect_callback_id_token_missing",
            "connect_callback_id_token_invalid",
            "connect_callback_client_id_missing",
            "connect_slack_callback_state_store_unconfigured",
            "connect_slack_callback_state_consume_failed",
            "connect_slack_callback_exchange_failed",
            "connect_slack_callback_store_failed",
            "connect_start_url_failed",
            "connect_slack_start_url_failed",
        ),
    ),
    ConnectDiag.I01A: DiagSpec(
        ConnectDiag.I01A,
        "本人特定失敗: 署名済み Slack caller が無い（missing_verified_caller・署名 claim 拒否）",
        _CONTACT_ADMIN_ACTION,
        ("caller_claim_rejected", "identity_spoof_rejected"),
    ),
    ConnectDiag.I01B: DiagSpec(
        ConnectDiag.I01B,
        "本人特定失敗: Slack プロフィール解決でエラー（resolver_error）",
        _CONTACT_ADMIN_ACTION,
        ("identity_spoof_rejected",),
    ),
    ConnectDiag.I01C: DiagSpec(
        ConnectDiag.I01C,
        "本人特定失敗: Slack ユーザーを会社メンバーへ解決できない（resolve_none）",
        _CONTACT_ADMIN_ACTION,
        ("identity_spoof_rejected",),
    ),
    ConnectDiag.I02: DiagSpec(
        ConnectDiag.I02,
        "本人メール未取得（oauth_connect fail-closed・no_user_email）",
        "Slack プロフィールのメールアドレスが会社メールになっているか確認し、"
        "管理者へご連絡ください。",
        ("oauth_connect_fail_closed",),
    ),
    ConnectDiag.I03: DiagSpec(
        ConnectDiag.I03,
        "Slack 再連携が必要（保存済み Slack ID と現在の ID が不一致・または未保存）",
        "上の案内文に従って Slack を連携し直してください。",
        ("oauth_connect_slack_rebind_needed",),
    ),
    ConnectDiag.L01: DiagSpec(
        ConnectDiag.L01,
        "連携リンク生成失敗（OAuth 系 env 不備・検証済み Slack ID 無し）",
        _CONTACT_ADMIN_ACTION,
        (
            "oauth_connect_url_failed",
            "oauth_connect_slack_url_failed",
            "oauth_connect_slack_url_suppressed",
        ),
    ),
    ConnectDiag.T01: DiagSpec(
        ConnectDiag.T01,
        "Slack 側 state 不正/期限切れ/使用済み",
        _REISSUE_ACTION,
        (
            "connect_slack_callback_bad_state",
            "connect_slack_callback_reused_state",
            "connect_slack_state_unbound_rejected",
            "connect_slack_start_bad_state",
            "connect_slack_start_unbound_rejected",
        ),
    ),
    ConnectDiag.T02: DiagSpec(
        ConnectDiag.T02,
        "Slack team 不一致 / 許可した Slack アカウントの不一致 / Slack ID の重複",
        _CONTACT_ADMIN_ACTION,
        (
            "connect_slack_callback_team_mismatch",
            "connect_slack_callback_identity_mismatch",
            "connect_slack_callback_identity_missing",
            "slack_oauth_uid_collision",
        ),
    ),
}

# gateway の identity_spoof_rejected reason → I01 サブコード。
IDENTITY_REJECT_REASON_CODES: dict[str, ConnectDiag] = {
    "missing_verified_caller": ConnectDiag.I01A,
    "resolver_error": ConnectDiag.I01B,
    "resolve_none": ConnectDiag.I01C,
}


def identity_reject_code(reason: str) -> ConnectDiag:
    """gateway の拒否 reason を I01 サブコードへ（未知 reason は I01a に寄せる）。"""
    return IDENTITY_REJECT_REASON_CODES.get(reason, ConnectDiag.I01A)


def mask_email(email: str) -> str:
    """メールをマスクする（skills の ``_mask_email`` と同じ流儀: 先頭 1 文字 + ``***@domain``）。"""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1] if local else ''}***@{domain}"


def now_jst() -> datetime:
    """診断行の時刻（JST・aware）。"""
    return datetime.now(tz=JST)


def format_when(when: datetime) -> str:
    """時刻を ``YYYY-MM-DD HH:MM JST`` にする（naive は UTC とみなす）。"""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def format_diag_line(
    code: ConnectDiag,
    *,
    when: datetime,
    request_id: str | None = None,
    masked_email: str | None = None,
    extra: str | None = None,
) -> str:
    """利用者が転送する 1 行: ``診断: <CODE> <時刻 JST> <識別子> [<request_id>]``。

    識別子はマスク済みメール、無ければ ``extra``（Slack user ID 等）、どちらも無ければ ``-``。
    ``masked_email`` に素のメールが来ても必ずマスクして出す（秘匿値の二重防御）。
    """
    subject_parts: list[str] = []
    if masked_email:
        subject_parts.append(masked_email if "***" in masked_email else mask_email(masked_email))
    if extra:
        subject_parts.append(extra.strip())
    subject = " ".join(subject_parts) or "-"
    line = f"診断: {code.value} {format_when(when)} {subject}"
    if request_id:
        line = f"{line} {request_id.strip()}"
    return line


def format_user_message(
    code: ConnectDiag,
    *,
    when: datetime,
    request_id: str | None = None,
    masked_email: str | None = None,
    extra: str | None = None,
) -> str:
    """利用者向け全文: 対処 → 定型文 → 診断行（全経路共通・末尾に付ける）。"""
    spec = DIAG_SPECS[code]
    line = format_diag_line(
        code,
        when=when,
        request_id=request_id,
        masked_email=masked_email,
        extra=extra,
    )
    return f"{spec.user_action}\n{admin_forward_hint()}\n{line}"


__all__ = [
    "ADMIN_NAME_ENV",
    "DEFAULT_ADMIN_NAME",
    "DIAG_SPECS",
    "IDENTITY_REJECT_REASON_CODES",
    "JST",
    "ConnectDiag",
    "DiagSpec",
    "admin_forward_hint",
    "admin_name",
    "format_diag_line",
    "format_user_message",
    "format_when",
    "identity_reject_code",
    "mask_email",
    "now_jst",
]
