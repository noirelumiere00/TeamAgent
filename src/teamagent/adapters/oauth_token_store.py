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

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, repr=False)
class OAuthToken:
    """1ユーザー分の OAuth リフレッシュトークン（＋認可済みスコープ）。

    repr で refresh_token を伏せる（誤ってログ/例外に出るのを防ぐ・G8）。
    """

    refresh_token: str
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"OAuthToken(refresh_token=***, scopes={self.scopes!r})"


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


@runtime_checkable
class TokenCipher(Protocol):
    """refresh token の暗号化/復号（at-rest 暗号化・G8）。本番は KMS 実装を注入する。"""

    def encrypt(self, plaintext: str) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> str: ...


class KmsCipher:
    """AWS KMS で refresh token を暗号化/復号する（直接 KMS・boto3 は遅延 import）。

    refresh token は十分小さく KMS 直接暗号化(最大4KB)に収まる。復号には KMS Decrypt の
    IAM 権限が必要＝DB を読めても token は復号できない（G8 を真に満たす）。
    """

    def __init__(self, key_id: str, client: Any | None = None) -> None:
        self._key_id = key_id
        self._client = client

    def _kms(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("kms")
        return self._client

    def encrypt(self, plaintext: str) -> bytes:
        resp = self._kms().encrypt(KeyId=self._key_id, Plaintext=plaintext.encode("utf-8"))
        return bytes(resp["CiphertextBlob"])

    def decrypt(self, ciphertext: bytes) -> str:
        resp = self._kms().decrypt(CiphertextBlob=ciphertext)
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
        refresh = self._cipher.decrypt(bytes(row["refresh_token_enc"]))
        return OAuthToken(refresh_token=refresh, scopes=tuple(row["scopes"] or ()))

    def put(self, user_email: str, token: OAuthToken) -> None:
        email = self._norm(user_email)
        enc = self._cipher.encrypt(token.refresh_token)
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
        return self.get(user_email) is not None


__all__ = [
    "InMemoryTokenStore",
    "KmsCipher",
    "OAuthToken",
    "RdsTokenStore",
    "TokenCipher",
    "TokenStore",
]
