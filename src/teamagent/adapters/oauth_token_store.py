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
from typing import Protocol, runtime_checkable


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


__all__ = ["InMemoryTokenStore", "OAuthToken", "TokenStore"]
