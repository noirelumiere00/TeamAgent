"""メール系 Skill の「連携状態」を構造化して返すための共有部品（P0-3 / P0-4）。

## なぜ要るか（実測された事故）

### P0-4: 未連携シグナルが機械可読でない

未連携・再連携要のとき、mail_summary / mail_followup は
``raise PermissionError("メール連携が未完了です（/teamagent connect で…）")`` していた。

1. MCP 境界（``mcp_gateway/server.py`` の ``_err``）はこれを **例外名＋和文の 1 本の文字列**へ
   潰すため、LLM から見て機械可読な手がかりが無い。SOUL は「``error=not_connected`` なら
   oauth_connect（@Aico に『連携』）へ誘導」という契約を持つのに、その ``error`` が届かない。
2. 案内語「/teamagent connect」は **その語では起動しない**（実際の導線は「@Aico に
   『連携』と話しかける」）＝断絶した導線を案内していた。

そこで calendar_freebusy（``CalendarFreeBusyOutput(error="not_connected", message=…)``）と
**同型の構造化 return** に寄せ、文言の後半（:data:`CONNECT_SUFFIX`）も共有する。

### P0-3: 0 件の理由を LLM が創作する

0 件応答には「連携」の語が一文字も無く、MCP が LLM へ渡す JSON にも 0 件の意味づけが無い。
空白地帯を埋めるために LLM が「Google 連携が未完了かもしれません」と創作していた。
:func:`searched_inbox_prefix` は「実際に受信箱を検索した」という事実を **サーバ側で確定**させ、
その創作の余地を潰すための決定論プレフィクス。

### 残る限界（2026-08-20 レビュー 要修正3）: トークンの**生死**は検証していない

:func:`resolve_gmail_for_user` はネットワーク I/O をしない（＝client_name ガードより前に
呼んでも「Gmail を 1 回も叩かない」不変量を壊さない）設計だが、その裏返しとして
**失効・revoke 済みトークンでも成功扱い**になる。したがってガード経路の「連携は正常です」は
厳密には「連携の**配線**は解決できた（まだ受信箱は見ていない）」の意味でしかない。

失効が実際に露見するのは受信箱を叩いた瞬間なので、そこを **例外のまま MCP の汎用エラーへ
落とさず** :func:`classify_gmail_failure` で ``reauth_needed`` / ``gmail_api_failed`` に
落とし、SOUL の「error=reauth_needed なら再連携へ誘導」契約に載せる。

## fail-closed は維持している

構造化 return にしても受信箱には 1 度も触れない（G1/G2）。「例外を投げるか値を返すか」の
違いだけで権限判定そのものは変えていない。ただし **TokenStore 自体が未設定**（＝配線ミス＝
運用バグ）は利用者向けメッセージに落とさず ``PermissionError`` のまま残す。
"""

from __future__ import annotations

import re
from typing import Final, Literal

from teamagent.adapters.gmail_client import GmailClient
from teamagent.adapters.oauth_token_store import TokenStore

# ``calendar_freebusy/skill.py`` の ``_ERR_MSG["not_connected"]`` と **同じ後半**。
# 導線（@Aico に『連携』）を 1 か所に集約し、片方だけ古くなるのを防ぐ。
CONNECT_SUFFIX: Final[str] = (
    " Google の連携が必要です（@Aico に『連携』と話しかけて許可してください）。"
)

NOT_CONNECTED_MESSAGE: Final[str] = "メールの確認には" + CONNECT_SUFFIX
REAUTH_NEEDED_MESSAGE: Final[str] = (
    "メール連携の認証情報を解決できませんでした。もう一度" + CONNECT_SUFFIX
)

# 受信箱は叩けたが API が失敗した（＝「メールが 0 件」ではない）。
GMAIL_FAILED_MESSAGE: Final[str] = (
    "受信箱の検索に失敗しました。時間をおいて再度お試しください"
    "（メールが 0 件という意味ではありません）。"
)

MESSAGE_BY_CONNECTION_ERROR: Final[dict[str, str]] = {
    "not_connected": NOT_CONNECTED_MESSAGE,
    "reauth_needed": REAUTH_NEEDED_MESSAGE,
    "gmail_api_failed": GMAIL_FAILED_MESSAGE,
}

# 例外の型名・文面に現れたら「認証をやり直せば直る」とみなす目印（小文字で照合）。
# google.auth.exceptions.RefreshError / 401 / invalid_grant などを skill 層から
# google ライブラリを import せずに拾うための決定論ルール（3 層分離を壊さない）。
_REAUTH_MARKERS: Final[tuple[str, ...]] = (
    "refresherror",
    "invalid_grant",
    "invalid_credentials",
    "invalid_token",
    "unauthorized",
    "401",
    "insufficient",
    "expired",
    "revoked",
)


def classify_gmail_failure(exc: BaseException) -> str:
    """受信箱アクセス中の例外を Output.error の決定論コードへ落とす。

    失効トークンは ``reauth_needed``（＝oauth_connect へ誘導できる）に寄せ、それ以外の
    API 障害は ``gmail_api_failed``。**どちらも「0 件」とは別物**として返すことが肝で、
    ここで例外のまま抜けると MCP 境界で和文 1 本に潰れ、LLM が「連携が未完了かも」と
    創作する余地（P0-3 で塞いだはずの穴）が復活する。
    """
    blob = f"{type(exc).__name__} {exc}".lower()
    if any(marker in blob for marker in _REAUTH_MARKERS):
        return "reauth_needed"
    return "gmail_api_failed"


# ─────────────────────────────────────────────────────────────────────────────
# F0（連携切れの見える化）: 朝ダイジェストと calendar_freebusy 専用の **厳しめの分類器**
# ─────────────────────────────────────────────────────────────────────────────
#
# 上の classify_gmail_failure は "refresherror" / "unauthorized" / "401" まで再連携扱いにする。
# 対話の 1 回なら「念のため再連携」でも害は小さいが、朝ダイジェストは **全員へ同時に** 出る。
# 連携用クライアントの secret 不正（invalid_client）や管理者のアプリ制限（admin_policy_enforced）
# も RefreshError なので、緩い分類のままだと 23 人全員へ「再連携してください」と出し、
# 全員に無駄な操作をさせたうえで直らない。そこで本人の操作で直るものだけを拾う:
#
#   token_expired: RefreshError の error が invalid_grant／refresh token が空
#   scope_missing: HTTP 403 かつ「スコープ不足」の印（insufficientPermissions 等）
#   temporary    : 上記以外すべて（invalid_client・admin_policy_enforced・5xx・429・
#                  タイムアウト・通信断・設定不備の ValueError・Bedrock の失敗 …）
#
# 判定材料は Google の API / token endpoint が返す例外の型と構造化フィールドだけ。
# メール本文や件名は一切見ない（第三者が分類結果を操作できない）。google ライブラリは
# import せず、型名と属性で判定する（skill 層の 3 層分離を崩さない）。

#: 取得状態（MorningDigestOutput.mail_fetch / calendar_fetch の値）。
#: ⚠️ ``Final`` に型を書かない（mypy が Literal として推論し、schema の FetchStatus に代入できる）。
FETCH_OK: Final = "ok"
FETCH_TOKEN_EXPIRED: Final = "token_expired"
FETCH_SCOPE_MISSING: Final = "scope_missing"
FETCH_TEMPORARY: Final = "temporary"
#: 取得を試みたかどうかも分からない（既定値）。描画は「確認できませんでした」に倒す。
FETCH_UNKNOWN: Final = "unknown"

#: 分類器の戻り値（ok と unknown は返さない）。
FetchFailure = Literal["token_expired", "scope_missing", "temporary"]

#: 本人の再連携で直る状態（＝「この DM で『連携』」へ案内してよい状態）。
FETCH_NEEDS_RECONNECT: Final[frozenset[str]] = frozenset({FETCH_TOKEN_EXPIRED, FETCH_SCOPE_MISSING})

# HTTP 403 のうち「トークンのスコープが足りない」ことを示す印（小文字で照合）。
# 403 には rateLimitExceeded / domainPolicy / accessNotConfigured もあり、それらは
# 再連携では直らないので拾わない。
_SCOPE_MISSING_MARKERS: Final[tuple[str, ...]] = (
    "access_token_scope_insufficient",
    "insufficientpermissions",
    "insufficient authentication scopes",
)

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TYPE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _type_names(exc: BaseException) -> tuple[str, ...]:
    return tuple(cls.__name__ for cls in type(exc).__mro__)


def _refresh_error_code(exc: BaseException) -> str:
    """RefreshError の OAuth エラーコード（invalid_grant 等）。取れなければ空。

    google-auth は ``RefreshError("invalid_grant: …", {"error": "invalid_grant", …})`` の形で
    投げる（google/oauth2/_client.py の _handle_error_response）。構造化 dict を優先し、
    無ければ先頭文字列の「コード:」を読む。値は [a-z_] の短い識別子だけ受け付ける。
    """
    args = getattr(exc, "args", ()) or ()
    for arg in args[1:]:
        if isinstance(arg, dict):
            code = str(arg.get("error", "") or "").strip().lower()
            if _CODE_RE.match(code):
                return code
    if args and isinstance(args[0], str):
        head = args[0].split(":", 1)[0].strip().lower()
        if _CODE_RE.match(head):
            return head
    return ""


def _http_status(exc: BaseException) -> int | None:
    """googleapiclient.errors.HttpError の HTTP ステータス（無ければ None）。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_error_blob(exc: BaseException) -> str:
    """HttpError の reason / error_details / 本文を小文字でつないだもの（照合専用）。

    ⚠️ ログにも利用者向け文言にも出さない（URI に thread id 等が入るため str(exc) は使わない）。
    """
    parts: list[str] = [
        str(getattr(exc, "reason", "") or ""),
        str(getattr(exc, "error_details", "")),
    ]
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes | bytearray):
        parts.append(bytes(content[:4096]).decode("utf-8", "replace"))
    elif isinstance(content, str):
        parts.append(content[:4096])
    return " ".join(parts).lower()


def classify_google_fetch_failure(exc: BaseException) -> FetchFailure:
    """Google の取得失敗を token_expired / scope_missing / temporary に分ける（厳しめ）。

    ``token_expired`` と ``scope_missing`` だけが「本人の再連携で直る」。それ以外は
    すべて ``temporary``（再連携へ誘導しない）。迷ったら temporary に倒すのが要点で、
    ここを緩めると設定不備の朝に全員へ「再連携して」と出す事故になる。
    """
    names = _type_names(exc)
    if "MissingRefreshTokenError" in names:
        return FETCH_TOKEN_EXPIRED
    if "RefreshError" in names:
        return (
            FETCH_TOKEN_EXPIRED if _refresh_error_code(exc) == "invalid_grant" else FETCH_TEMPORARY
        )
    if _http_status(exc) == 403:
        blob = _http_error_blob(exc)
        if any(marker in blob for marker in _SCOPE_MISSING_MARKERS):
            return FETCH_SCOPE_MISSING
    return FETCH_TEMPORARY


def google_fetch_failure_detail(exc: BaseException) -> str:
    """管理者向けの内訳コード（例 ``RefreshError:invalid_client`` / ``HttpError:503``）。

    例外の **型名と Google が返す識別子だけ** で作る。例外の文面（URI・件名・アドレスが
    入りうる）は使わない。どの部品も英数字と ``_`` に絞る。
    """
    name = _TYPE_NAME_RE.sub("", type(exc).__name__)[:40] or "Exception"
    if "RefreshError" in _type_names(exc):
        code = _refresh_error_code(exc)
        return (f"{name}:{code}" if code else name)[:80]  # schema の max_length=80 と対
    status = _http_status(exc)
    if status is not None and 100 <= status <= 599:
        return f"{name}:{status}"
    return name


# Output.connection の値。"live" = 実際に Gmail を叩いた（0 件でも連携は正常）。
CONNECTION_LIVE: Final[str] = "live"
# "ok" = 連携は解決済みだが検索はしていない（client_name ガードで止めた）。
CONNECTION_OK: Final[str] = "ok"


class MailConnectionError(Exception):
    """利用者に「連携してください」と返すべき状態（未連携・再連携要）。

    ``PermissionError`` ではなく専用例外にしているのは、呼び出し側 run() が
    **構造化 return へ変換するためだけ**に捕まえる必要があるから。運用バグ
    （TokenStore 未設定）は従来どおり ``PermissionError`` で落とす＝混ぜない。
    """

    def __init__(self, code: str) -> None:
        self.code: Final[str] = code
        self.message: Final[str] = MESSAGE_BY_CONNECTION_ERROR[code]
        super().__init__(code)


def resolve_gmail_for_user(
    token_store: TokenStore | None,
    requester: str,
    *,
    misconfig_message: str,
) -> GmailClient:
    """本人 OAuth トークンから readonly な GmailClient を構築する（G1/G2/G4）。

    **ネットワーク I/O はしない**（TokenStore 参照＋Credentials 構築のみ。refresh は初回の
    API 呼び出し時に遅延実行される）ので、client_name ガードより前に呼んでも「Gmail を
    1 回も叩かない」不変量は壊れない。

    Raises:
        PermissionError: TokenStore 未設定（＝配線ミス。利用者向け文言に落とさない）。
        MailConnectionError: 未連携（``not_connected``）・認証情報の解決失敗（``reauth_needed``）。
    """
    if token_store is None:
        raise PermissionError(misconfig_message)
    token = token_store.get(requester)
    if token is None:
        raise MailConnectionError("not_connected")
    try:
        return GmailClient.from_user_token(token, readonly=True)
    except ValueError as e:
        # 失効/空 refresh token・GOOGLE_CLIENT_ID 未設定などは「再連携してください」に寄せる。
        raise MailConnectionError("reauth_needed") from e


def searched_inbox_prefix(inbox_masked: str) -> str:
    """「0 件」の理由を LLM に創作させないための決定論プレフィクス（P0-3）。

    「連携は正常」「実際に検索した」の 2 つを **サーバが断言**する。これを欠くと LLM は
    空白を埋めようとして「Google 連携が未完了かもしれません」と言い出す（実測）。
    """
    return f"連携は正常です（受信箱 {inbox_masked} を実際に検索しました）。"


__all__ = [
    "CONNECTION_LIVE",
    "CONNECTION_OK",
    "CONNECT_SUFFIX",
    "FETCH_NEEDS_RECONNECT",
    "FETCH_OK",
    "FETCH_SCOPE_MISSING",
    "FETCH_TEMPORARY",
    "FETCH_TOKEN_EXPIRED",
    "FETCH_UNKNOWN",
    "GMAIL_FAILED_MESSAGE",
    "MESSAGE_BY_CONNECTION_ERROR",
    "NOT_CONNECTED_MESSAGE",
    "REAUTH_NEEDED_MESSAGE",
    "MailConnectionError",
    "classify_gmail_failure",
    "classify_google_fetch_failure",
    "google_fetch_failure_detail",
    "resolve_gmail_for_user",
    "searched_inbox_prefix",
]
