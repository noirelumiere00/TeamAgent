"""Sentry SDK 初期化 + PII / シークレットスクラブ。

Sprint 2 / 2.6 「runtime/slack_bot.py に Sentry SDK 組込（PII scrubber 有効化）」。

設計指針（Agent 調査 + Sentry 公式 docs 2026/5 確認済）:
1. **DSN 未設定なら init() を完全スキップ** — テスト・dev 環境で副作用ゼロ
2. **send_default_pii=False だけでは不十分** — extra/breadcrumbs/message の
   値（PDF 全文・xoxb- など）はスクラブされないため `before_send` で値走査
3. **LoggingIntegration(event_level=None)** — breadcrumb のみ。例外は
   `capture_exception()` で一本化（二重送信防止）
4. **AsyncioIntegration() は async 文脈内で init すること** — import 時に呼ぶと
   event loop を掴み損ねる（sentry-python issue #2328 / #2333）
5. **request_id を tag に昇格** — CLAUDE.md 6-bis のトレーサビリティ維持

参考:
- https://docs.sentry.io/platforms/python/data-management/sensitive-data/
- https://docs.sentry.io/platforms/python/integrations/asyncio/
- https://docs.sentry.io/platforms/python/integrations/logging/
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Final, cast

import structlog

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# スクラブ対象パターン
# -----------------------------------------------------------
# シークレット（必ず redact）
_SECRET_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack bot/user/refresh/legacy
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),  # Slack app-level
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic API key
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API key
    re.compile(r"1//0[A-Za-z0-9_\-]{20,}"),  # Google OAuth refresh token (1//0...)
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}"),  # Google OAuth client secret (GOCSPX-)
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),  # Google OAuth access token (ya29.)
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"),
    # URI userinfo (postgresql://user:PASS@host 等の接続文字列パスワード)。
    # ://直後〜@手前の user:pass 全体を redact。パスワードに : を含むケースも許容。
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
]

# PII（メール / 電話）— redact だが分類は別タグ
_PII_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"0\d{1,4}-\d{1,4}-\d{4}"),  # 日本の電話番号（ハイフン区切り）
    re.compile(r"0[789]0\d{8}"),  # 日本の携帯（ハイフン無し 070/080/090）
    re.compile(r"\+\d{1,3}[\d\- ]{7,}\d"),  # 国際電話（+81-90-... 等）
]

# 1 フィールドの最大長（提案 PDF 全文や会話履歴の混入を防ぐ hard cap）
_MAX_FIELD_LEN: Final[int] = 2000


def _scrub_str(s: str) -> str:
    """文字列に対してシークレット/PII マスクと長さ制限をかける。"""
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED_SECRET]", s)
    for pat in _PII_PATTERNS:
        s = pat.sub("[REDACTED_PII]", s)
    if len(s) > _MAX_FIELD_LEN:
        s = s[:_MAX_FIELD_LEN] + f"...[TRUNCATED:{len(s)} chars]"
    return s


def redact_secrets(text: str) -> str:
    """シークレットだけを redact する（**長さ制限も PII マスクもしない**）。

    ``scrub_value`` は 1 フィールド 2000 文字の hard cap を持つ（Sentry イベントに
    PDF 全文が乗るのを防ぐため）。資料本文をそのまま LLM へ渡す用途に ``scrub_value``
    を使うと **本文が黙って 2000 文字で切れる**ので、シークレット除去だけが要る
    経路（attachment_assist の抽出本文）はこちらを使う。文字数の上限は呼び出し側が
    自分の要件で明示的に切る（切ったことを利用者に伝える責務も呼び出し側）。
    """
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED_SECRET]", text)
    return text


def scrub_value(value: Any) -> Any:
    """任意の値を再帰的にスクラブする。

    Sentry イベントの extra / contexts / breadcrumbs.data など、
    dict / list / str が混在する構造を素通り対応する。
    """
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(x) for x in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(x) for x in value)
    return value


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Sentry 送信前フック。

    フロー:
      1. event 内の主要セクションを再帰スクラブ
      2. breadcrumbs の message / data も対象
      3. event.message （文字列 or {"formatted": "..."}）もスクラブ
      4. request_id を tag に昇格して filter しやすくする
    """
    # トップレベルセクションのスクラブ
    for key in ("extra", "contexts", "tags", "request", "user"):
        if key in event:
            event[key] = scrub_value(event[key])

    # breadcrumbs は dict 構造で values[] にぶら下がる
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        for b in breadcrumbs.get("values", []) or []:
            if "message" in b:
                b["message"] = scrub_value(b["message"])
            if "data" in b:
                b["data"] = scrub_value(b["data"])

    # event.message は文字列 or {"formatted":"...","message":"...","params":[...]}
    msg = event.get("message")
    if isinstance(msg, str):
        event["message"] = _scrub_str(msg)
    elif isinstance(msg, dict):
        for k in ("formatted", "message"):
            if k in msg and isinstance(msg[k], str):
                msg[k] = _scrub_str(msg[k])
        if "params" in msg:
            msg["params"] = scrub_value(msg["params"])

    # exception.values[].value（例外メッセージ自体）と stacktrace の frame locals をスクラブ。
    # attach_stacktrace=True で全フレームのローカル変数 (vars) が添付されるため、
    # DB 接続文字列を握ったローカル変数経由のシークレット漏れをここで塞ぐ。
    exception = event.get("exception")
    if isinstance(exception, dict):
        for ex in exception.get("values", []) or []:
            if isinstance(ex.get("value"), str):
                ex["value"] = _scrub_str(ex["value"])
            stacktrace = ex.get("stacktrace")
            if isinstance(stacktrace, dict):
                for frame in stacktrace.get("frames", []) or []:
                    if isinstance(frame, dict) and isinstance(frame.get("vars"), dict):
                        frame["vars"] = scrub_value(frame["vars"])

    # request_id を tag に昇格
    extra = event.get("extra") or {}
    if isinstance(extra, dict):
        rid = extra.get("request_id")
        if rid:
            event.setdefault("tags", {})["request_id"] = str(rid)

    return event


# 重複初期化防止フラグ（プロセス内 idempotent）
_INITIALIZED: bool = False


def is_initialized() -> bool:
    """Sentry が init 済みかを返す。テスト用。"""
    return _INITIALIZED


def _reset_for_tests() -> None:
    """テスト専用：再 init できる状態に戻す。本番では呼ばない。"""
    global _INITIALIZED
    _INITIALIZED = False


def init_sentry(
    *,
    dsn: str | None = None,
    environment: str | None = None,
    release: str | None = None,
    traces_sample_rate: float = 0.05,
) -> bool:
    """Sentry を初期化する。DSN が空 / 未設定なら no-op で False を返す。

    Args:
        dsn: 明示 DSN（None なら env SENTRY_DSN を読む）
        environment: 環境名（None なら APP_ENV、既定 "dev"）
        release: リリース識別（None なら GIT_SHA）
        traces_sample_rate: パフォーマンストレース sample 率（無料枠考慮 0.05）

    Returns:
        True: init された / False: skip された（DSN 無 or 既に init 済）

    Note:
        AsyncioIntegration が event loop を取りこぼさないよう、本関数は
        **async 関数内**から呼ぶことを推奨（slack_bot._run() 内）。
    """
    global _INITIALIZED
    if _INITIALIZED:
        logger.debug("sentry_already_initialized")
        return True

    dsn = (dsn or os.environ.get("SENTRY_DSN", "")).strip()
    if not dsn:
        logger.info("sentry_skip", reason="SENTRY_DSN is not set")
        return False

    # 遅延 import: sentry-sdk が optional になっても落ちないように
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.scrubber import (
        DEFAULT_DENYLIST,
        DEFAULT_PII_DENYLIST,
        EventScrubber,
    )

    env = environment or os.environ.get("APP_ENV", "dev")
    rel = release or os.environ.get("GIT_SHA")

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=rel,
        send_default_pii=False,
        # key 名ベースのスクラブ（EventScrubber） + 値ベースのスクラブ（before_send）の二重化
        event_scrubber=EventScrubber(
            denylist=list(DEFAULT_DENYLIST)
            + list(DEFAULT_PII_DENYLIST)
            + [
                # TeamAgent 固有の機密フィールド名
                "pdf_text",
                "customer_name",
                "slack_token",
                "slack_bot_token",
                "slack_app_token",
                "bedrock_input",
                "bedrock_output",
                "query",
                "content",
                "answer",
                "raw_text",
            ],
            recursive=True,
        ),
        # sentry-sdk は TypedDict Event を期待する。before_send は dict[str, Any] で
        # 実装しているので cast で型を合わせる（runtime 互換）
        before_send=cast("Any", before_send),
        integrations=[
            AsyncioIntegration(),
            # event_level=None → 例外は LoggingIntegration から自動送信しない
            # （明示的に capture_exception() を呼ぶ箇所で一本化）
            LoggingIntegration(level=logging.INFO, event_level=None),
        ],
        traces_sample_rate=traces_sample_rate,
        profiles_sample_rate=0.0,  # 無料枠（5K errors/月）では OFF
        max_breadcrumbs=30,
        attach_stacktrace=True,
    )
    _INITIALIZED = True
    logger.info(
        "sentry_init",
        environment=env,
        release=rel,
        traces_sample_rate=traces_sample_rate,
    )
    return True


def capture_skill_exception(
    exc: BaseException,
    *,
    request_id: str,
    skill: str,
    user_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Skill 層の例外を Sentry に送る。DSN 未設定なら no-op。

    呼び出し側は try/except でこれを呼んでから自前の構造化ログを出す。
    Sentry 内では tag.skill / tag.request_id でフィルタ可能。

    Args:
        exc: 補足した例外
        request_id: トレース ID（必ず）
        skill: Skill 名
        user_id: Slack user id（任意）
        extra: 追加コンテキスト（生入力は入れない）
    """
    if not _INITIALIZED:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("skill", skill)
        scope.set_tag("request_id", request_id)
        if user_id:
            scope.set_user({"id": user_id})
        if extra:
            # 念のためここでも scrub（before_send でもう一度走るので二重）
            scope.set_context("skill_extra", scrub_value(extra))
        sentry_sdk.capture_exception(exc)


def capture_event_exception(
    exc: BaseException,
    *,
    event_type: str,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Runtime 層（Slack イベントハンドラ等）の例外を送る。

    Skill 外（DM 受信 / mention parsing / Bolt middleware）で起きた例外用。
    """
    if not _INITIALIZED:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("event_type", event_type)
        if request_id:
            scope.set_tag("request_id", request_id)
        if extra:
            scope.set_context("event_extra", scrub_value(extra))
        sentry_sdk.capture_exception(exc)
