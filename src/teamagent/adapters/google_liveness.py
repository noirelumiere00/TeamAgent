"""保存済み Google トークンの生存確認（oauth_connect が「連携済み」と答える前に 1 回だけ）。

背景（F0・2026-09-29）:
  oauth_connect は保存行のスコープだけで「連携済み」と判定しており、パスワード変更などで
  refresh token が失効（invalid_grant）した人が「連携」と送っても「連携済み・操作不要」と返り、
  再連携リンクを手に入れる方法が無かった（行き止まり）。そこで「連携済み」と答える前に、
  本番の各アダプタと**同じ組み立て**（``build_user_credentials`` → ``Credentials.refresh``）で
  token endpoint を 1 回だけ叩き、生きているかを確かめる。

結果は 4 つ:
  - ``alive``: refresh が通り、許可された範囲も足りている。
  - ``token_dead``: ``invalid_grant``（失効・取り消し）または refresh token が空。
  - ``scope_missing``: ``invalid_scope``（保存した範囲の一部が実は許可されていない）、または
    refresh 応答の許可済み範囲が WORKSPACE_SCOPES に足りない。
  - ``unknown``: それ以外すべて（``invalid_client``・``admin_policy_enforced``・5xx・通信断・
    時間切れ・設定不備など）。呼び出し側は「判定不能＝リンクを出す」安全側に倒す。

分類は厳しめ: token_dead と判定するのは ``invalid_grant`` だけ。``RefreshError`` 一般や
``invalid_client``（クライアントの secret 不正＝全員に同時に起きる）は unknown に置く。

守ること:
  - 呼び出しは token endpoint への 1 回だけ（Gmail/Calendar の API は呼ばない・書き込みなし）。
  - 全体で ``timeout`` 秒（既定 5 秒）で打ち切る（google-auth の再試行を含めて上限を守るため、
    別スレッドで走らせて待つ時間を区切る）。
  - ログ・戻り値には分類コードと例外の型名だけを入れる。refresh token・access token・
    例外の文面・メールアドレスは入れない（G8）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Literal

from teamagent.adapters.google_auth import build_user_credentials
from teamagent.adapters.google_oauth_flow import (
    WORKSPACE_SCOPES,
    missing_workspace_scopes,
    normalize_granted_scopes,
)
from teamagent.adapters.oauth_token_store import OAuthToken

LivenessStatus = Literal["alive", "token_dead", "scope_missing", "unknown"]

DEFAULT_TIMEOUT_S = 5.0

# Google の token endpoint が返す error（RFC 6749 §5.2 の形・英小文字と _ だけ）。
# これに合うものだけを reason としてログに出す（任意の文面をログに流さない）。
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class LivenessResult:
    """生存確認の結果。``reason`` は分類の根拠コード（例外の文面は入れない）。"""

    status: LivenessStatus
    reason: str
    missing_scopes: tuple[str, ...] = ()
    granted_count: int | None = None
    elapsed_ms: int = 0


def _error_code(exc: BaseException) -> str | None:
    """RefreshError から token endpoint の ``error`` を取り出す（取れなければ None）。

    google-auth は ``RefreshError("<error>: <error_description>", response_data)`` の形で投げる
    （google/oauth2/_client.py の ``_handle_error_response``）。構造化された ``response_data``
    を優先し、無ければ文面の先頭（``<error>:``）を見る。
    """
    args: tuple[Any, ...] = exc.args
    if len(args) >= 2 and isinstance(args[1], dict):
        code = args[1].get("error")
        if isinstance(code, str) and _ERROR_CODE_RE.match(code.strip().lower()):
            return code.strip().lower()
    if args and isinstance(args[0], str):
        head = args[0].split(":", 1)[0].strip().lower()
        if _ERROR_CODE_RE.match(head):
            return head
    return None


def classify_refresh_failure(exc: BaseException) -> tuple[LivenessStatus, str]:
    """refresh の失敗を (status, reason) に分ける（厳しめ）。

    - ``invalid_grant`` → token_dead（失効・取り消し。再連携で直る）。
    - ``invalid_scope`` → scope_missing（保存した範囲の一部が許可されていない。再連携で直る）。
    - それ以外 → unknown（再連携では直らない失敗を「再連携して」に混ぜない）。
    """
    from google.auth import exceptions as gexc

    if isinstance(exc, gexc.RefreshError):
        code = _error_code(exc)
        text = str(exc).lower()
        if code == "invalid_grant" or "invalid_grant" in text:
            return "token_dead", "invalid_grant"
        if code == "invalid_scope" or "invalid_scope" in text:
            return "scope_missing", "invalid_scope"
        return "unknown", code or "RefreshError"
    if isinstance(exc, gexc.TransportError):
        return "unknown", "TransportError"
    return "unknown", type(exc).__name__


def _default_request(timeout: float, *, session: Any | None = None) -> Callable[..., Any]:
    """1 回の HTTP を ``timeout`` 秒で区切る google-auth のトランスポート。

    ``google.auth.transport.requests.Request`` の既定の待ち時間は 120 秒なので、そのままでは
    「連携」の返事が長く止まりうる。呼び出し側が渡す timeout を上書きして区切る。
    """
    import google.auth.transport.requests as gatr

    base = gatr.Request(session=session)
    per_call = max(0.1, float(timeout))

    def _call(
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        timeout: Any = None,  # 呼び出し側の値は使わず、下の per_call で区切る
        **kwargs: Any,
    ) -> Any:
        return base(url, method=method, body=body, headers=headers, timeout=per_call, **kwargs)

    return _call


def probe(
    token: OAuthToken | None,
    *,
    required: Iterable[str] = WORKSPACE_SCOPES,
    timeout: float = DEFAULT_TIMEOUT_S,
    request: Callable[..., Any] | None = None,
) -> LivenessResult:
    """保存済みトークンで refresh を 1 回試し、生きているかを返す（例外は投げない）。

    Args:
        token: ストアから復号した本人のトークン（``None``＝行が無い）。
        required: 足りているべき範囲（既定 WORKSPACE_SCOPES）。
        timeout: 全体の上限秒（google-auth の再試行込み）。
        request: google-auth のトランスポート（テストで token endpoint の応答を差し替える口）。
            省略時は requests ベースで 1 回の HTTP を ``timeout`` 秒に区切ったもの。
    """
    started = time.monotonic()

    def _done(
        status: LivenessStatus,
        reason: str,
        *,
        missing: tuple[str, ...] = (),
        granted_count: int | None = None,
    ) -> LivenessResult:
        return LivenessResult(
            status=status,
            reason=reason,
            missing_scopes=missing,
            granted_count=granted_count,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if token is None:
        return _done("unknown", "no_row")
    if not token.refresh_token:
        # 保存行はあるが refresh token が空＝本人が未認可のまま。再連携で直る。
        return _done("token_dead", "refresh_token_empty")
    try:
        creds = build_user_credentials(token)
    except Exception as exc:  # 連携用クライアント未設定など（再連携では直らない）
        return _done("unknown", f"credentials_{type(exc).__name__}")

    req = request if request is not None else _default_request(timeout)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google-liveness")
    try:
        future = executor.submit(creds.refresh, req)
        try:
            future.result(timeout=max(0.01, float(timeout)))
        except FutureTimeoutError:
            return _done("unknown", "timeout")
        except Exception as exc:
            status, reason = classify_refresh_failure(exc)
            return _done(status, reason)
    finally:
        # 時間切れのときは待たずに戻る（走っている 1 回の HTTP は per-call timeout で終わる）。
        executor.shutdown(wait=False)

    granted = normalize_granted_scopes(getattr(creds, "granted_scopes", None))
    if not granted:
        # refresh は通ったが応答に scope が無い／空（"scope": ""）＝範囲の根拠が無い。保存行の
        # 範囲は呼び出し側で確認済みなので生きている扱いにする（範囲不足と決めつけない）。
        # connect-web の exchange() が空を「根拠なし」として要求した範囲に戻すのと同じ判断。
        return _done("alive", "refreshed")
    missing = missing_workspace_scopes(granted, required)
    if missing:
        return _done("scope_missing", "granted_subset", missing=missing, granted_count=len(granted))
    return _done("alive", "refreshed", granted_count=len(granted))


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "LivenessResult",
    "LivenessStatus",
    "classify_refresh_failure",
    "probe",
]
