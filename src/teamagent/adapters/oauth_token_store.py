"""per-user OAuth トークンストア（Workspace 5サービス × 個人認可の中核）。

各個人が OAuth 同意で得た refresh token を user_email 単位で保管し、リクエスト時に
**発行者本人のトークン**を選ぶ。これにより各アダプタは「本人のデータにしか触れない」
（G1 本人限定）を構造的に保証する（DWD の代理権限と違い、越権が原理的に起きない）。

⚠️ refresh token は機微シークレット（G8）: ログ / プロンプト / Sentry に絶対出さない。
本番バックエンド（暗号化ファイル / SecretsManager / RDS 暗号化列）は設計 W1 確定後に
差し替える。本モジュールは Protocol ＋ dev/test 用の InMemory 実装を提供する。

設計: docs/poc/workspace_integration_design.md §3。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, repr=False)
class OAuthToken:
    """1ユーザー分の OAuth リフレッシュトークン（＋認可済みスコープ）。

    id_token は callback で同意アカウントを照合する交換時だけ保持し、TokenStore は永続化しない。
    repr では両 token を伏せる（誤ってログ/例外に出るのを防ぐ・G8）。
    """

    refresh_token: str
    scopes: tuple[str, ...] = ()
    id_token: str | None = None

    def __repr__(self) -> str:
        id_token = "***" if self.id_token else None
        return f"OAuthToken(refresh_token=***, scopes={self.scopes!r}, id_token={id_token})"


@dataclass(frozen=True, repr=False)
class SlackOAuthToken:
    """1ユーザー分の Slack user token(xoxp)（＋認可済みスコープ・本人 Slack id）。

    repr で access_token を伏せる（誤ってログ/例外に出るのを防ぐ・G8）。xoxp は
    本人なりすまし級の高感度資格情報なので Google の OAuthToken より厳に扱う。
    """

    access_token: str  # xoxp-...
    scopes: tuple[str, ...] = ()
    slack_user_id: str = ""
    team_id: str = ""

    def __repr__(self) -> str:
        return (
            "SlackOAuthToken(access_token=***, "
            f"scopes={self.scopes!r}, slack_user_id={self.slack_user_id!r}, "
            f"team_id={self.team_id!r})"
        )


@runtime_checkable
class TokenStore(Protocol):
    """user_email → OAuthToken の差し替え可能な保管口。

    本番は暗号化永続バックエンドを実装して注入する（本 Protocol を満たすだけ）。
    """

    def get(self, user_email: str) -> OAuthToken | None: ...

    def put(self, user_email: str, token: OAuthToken) -> None: ...

    def has(self, user_email: str) -> bool: ...


class InMemoryTokenStore:
    """dev/test 用のメモリ実装（本番は暗号化永続バックエンドへ差し替え）。

    email は大小文字・前後空白を正規化して引く（認可フローと参照で表記揺れに耐える）。
    """

    def __init__(self, initial: dict[str, OAuthToken] | None = None) -> None:
        self._tokens: dict[str, OAuthToken] = {}
        if initial:
            for email, token in initial.items():
                self._tokens[self._norm(email)] = token

    @staticmethod
    def _norm(email: str) -> str:
        return email.strip().lower()

    def get(self, user_email: str) -> OAuthToken | None:
        return self._tokens.get(self._norm(user_email))

    def put(self, user_email: str, token: OAuthToken) -> None:
        self._tokens[self._norm(user_email)] = token

    def has(self, user_email: str) -> bool:
        return self._norm(user_email) in self._tokens

    def scopes(self, user_email: str) -> tuple[str, ...] | None:
        """認可済みスコープのみ返す（行なしは None）。RdsTokenStore と対称。"""
        token = self._tokens.get(self._norm(user_email))
        return None if token is None else token.scopes


@runtime_checkable
class TokenCipher(Protocol):
    """refresh token の暗号化/復号（at-rest 暗号化・G8）。本番は KMS 実装を注入する。"""

    def encrypt(self, plaintext: str, *, context: dict[str, str] | None = None) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, context: dict[str, str] | None = None) -> str: ...


class KmsCipher:
    """AWS KMS で refresh token を暗号化/復号する（直接 KMS・boto3 は遅延 import）。

    refresh token は十分小さく KMS 直接暗号化(最大4KB)に収まる。復号には KMS Decrypt の
    IAM 権限が必要＝DB を読めても token は復号できない（G8 を真に満たす）。
    """

    def __init__(
        self, key_id: str, client: Any | None = None, *, region: str | None = None
    ) -> None:
        self._key_id = key_id
        self._client = client
        # KMS鍵のregion。鍵はRDSと同じ東京、Bedrockはus-east-1で食い違うため明示pinが必要。
        self._region = region or os.environ.get("OAUTH_KMS_REGION") or "ap-northeast-1"

    def _kms(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("kms", region_name=self._region)
        return self._client

    def encrypt(self, plaintext: str, *, context: dict[str, str] | None = None) -> bytes:
        kwargs: dict[str, Any] = {"KeyId": self._key_id, "Plaintext": plaintext.encode("utf-8")}
        if context:
            kwargs["EncryptionContext"] = context  # AAD: 復号時に同一 context 必須（per-user 束縛）
        resp = self._kms().encrypt(**kwargs)
        return bytes(resp["CiphertextBlob"])

    def decrypt(self, ciphertext: bytes, *, context: dict[str, str] | None = None) -> str:
        kwargs: dict[str, Any] = {"CiphertextBlob": ciphertext, "KeyId": self._key_id}
        if context:
            kwargs["EncryptionContext"] = context  # encrypt 時と不一致なら KMS が復号拒否
        resp = self._kms().decrypt(**kwargs)
        return str(resp["Plaintext"].decode("utf-8"))


class RdsTokenStore:
    """RDS(oauth_tokens) に refresh token を保管する TokenStore（per-user・暗号化・RLS）。

    migration 0006_oauth_tokens.sql のテーブルを使う。refresh token は cipher で暗号化して
    BYTEA 格納。`pgvector.connection(app_role, user_email)` が app.user_email GUC を立て、
    RLS が「本人行のみ」を保証する（アプリのバグでも他人の token に触れない）。
    """

    def __init__(
        self, pgvector: Any, cipher: TokenCipher, *, app_role: str = "teamagent_app"
    ) -> None:
        self._pgvector = pgvector
        self._cipher = cipher
        self._app_role = app_role

    @staticmethod
    def _norm(email: str) -> str:
        return email.strip().lower()

    def get(self, user_email: str) -> OAuthToken | None:
        email = self._norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT refresh_token_enc, scopes FROM oauth_tokens WHERE user_email = %s",
                    (email,),
                )
                row = cur.fetchone()
        if not row:
            return None
        refresh = self._cipher.decrypt(
            bytes(row["refresh_token_enc"]), context={"user_email": email}
        )
        return OAuthToken(refresh_token=refresh, scopes=tuple(row["scopes"] or ()))

    def put(self, user_email: str, token: OAuthToken) -> None:
        email = self._norm(user_email)
        enc = self._cipher.encrypt(token.refresh_token, context={"user_email": email})
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO oauth_tokens (user_email, refresh_token_enc, scopes)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_email) DO UPDATE
                      SET refresh_token_enc = EXCLUDED.refresh_token_enc,
                          scopes = EXCLUDED.scopes
                    """,
                    (email, enc, list(token.scopes)),
                )
            conn.commit()

    def has(self, user_email: str) -> bool:
        return self.scopes(user_email) is not None

    def scopes(self, user_email: str) -> tuple[str, ...] | None:
        """認可済みスコープ列のみ読む（KMS 復号なし・行なしは None）。

        oauth_connect の連携状態チェック用。has()/連携判定のためだけに refresh token を
        KMS 復号するのは無駄＋監査ログ汚染なので、scopes 列だけを SELECT する。
        """
        email = self._norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scopes FROM oauth_tokens WHERE user_email = %s",
                    (email,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return tuple(row["scopes"] or ())


class SlackTokenStore:
    """RDS(slack_oauth_tokens) に xoxp を保管する（per-user・KMS暗号化・RLS）。

    migration 0018_slack_oauth_tokens.sql のテーブルを使う。RdsTokenStore と対称で、
    `pgvector.connection(app_role, user_email)` が app.user_email GUC を立て、RLS が
    「本人行のみ」を保証する。xoxp は cipher で暗号化して BYTEA 格納（平文は持たない・G8）。
    """

    def __init__(
        self, pgvector: Any, cipher: TokenCipher, *, app_role: str = "teamagent_app"
    ) -> None:
        self._pgvector = pgvector
        self._cipher = cipher
        self._app_role = app_role

    @staticmethod
    def _norm(email: str) -> str:
        return email.strip().lower()

    def get(self, user_email: str) -> SlackOAuthToken | None:
        email = self._norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT xoxp_token_enc, scopes, slack_user_id, team_id "
                    "FROM slack_oauth_tokens WHERE user_email = %s",
                    (email,),
                )
                row = cur.fetchone()
        if not row:
            return None
        xoxp = self._cipher.decrypt(bytes(row["xoxp_token_enc"]), context={"user_email": email})
        return SlackOAuthToken(
            access_token=xoxp,
            scopes=tuple(row["scopes"] or ()),
            slack_user_id=str(row["slack_user_id"] or ""),
            team_id=str(row["team_id"] or ""),
        )

    def put(self, user_email: str, token: SlackOAuthToken) -> None:
        email = self._norm(user_email)
        enc = self._cipher.encrypt(token.access_token, context={"user_email": email})
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO slack_oauth_tokens
                        (user_email, xoxp_token_enc, slack_user_id, team_id, scopes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_email) DO UPDATE
                      SET xoxp_token_enc = EXCLUDED.xoxp_token_enc,
                          slack_user_id  = EXCLUDED.slack_user_id,
                          team_id        = EXCLUDED.team_id,
                          scopes         = EXCLUDED.scopes
                    """,
                    (email, enc, token.slack_user_id, token.team_id, list(token.scopes)),
                )
            conn.commit()

    def has(self, user_email: str) -> bool:
        return self.get(user_email) is not None


__all__ = [
    "InMemoryTokenStore",
    "KmsCipher",
    "OAuthToken",
    "RdsTokenStore",
    "SlackOAuthToken",
    "SlackTokenStore",
    "TokenCipher",
    "TokenStore",
]
