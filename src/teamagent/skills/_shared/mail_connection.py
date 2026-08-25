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

from typing import Final

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
    "GMAIL_FAILED_MESSAGE",
    "MESSAGE_BY_CONNECTION_ERROR",
    "NOT_CONNECTED_MESSAGE",
    "REAUTH_NEEDED_MESSAGE",
    "MailConnectionError",
    "classify_gmail_failure",
    "resolve_gmail_for_user",
    "searched_inbox_prefix",
]
