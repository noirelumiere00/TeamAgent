"""per-user OAuth トークンストアのテスト（課金0）。

repr によるトークン秘匿（G8）と、email 正規化込みの get/put/has を検証する。
"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.oauth_token_store import (
    InMemoryTokenStore,
    OAuthToken,
    RdsTokenStore,
    TokenStore,
)


def test_token_repr_hides_secret() -> None:
    t = OAuthToken(refresh_token="super-secret-refresh", scopes=("gmail.readonly",))
    r = repr(t)
    assert "super-secret-refresh" not in r  # 誤ログ防止（G8）
    assert "***" in r
    assert "gmail.readonly" in r


def test_put_get_has() -> None:
    store = InMemoryTokenStore()
    assert not store.has("a@vectorinc.co.jp")
    assert store.get("a@vectorinc.co.jp") is None
    tok = OAuthToken(refresh_token="rt", scopes=("drive.readonly",))
    store.put("a@vectorinc.co.jp", tok)
    assert store.has("a@vectorinc.co.jp")
    assert store.get("a@vectorinc.co.jp") is tok


def test_email_normalization() -> None:
    store = InMemoryTokenStore({"S-Komata@Vectorinc.co.jp ": OAuthToken("rt")})
    # 大小文字・前後空白を正規化して同一視
    assert store.has("s-komata@vectorinc.co.jp")
    assert store.get("  S-KOMATA@VECTORINC.CO.JP  ") is not None


def test_satisfies_protocol() -> None:
    # InMemoryTokenStore が TokenStore Protocol を満たす（runtime_checkable）。
    assert isinstance(InMemoryTokenStore(), TokenStore)


# ── RdsTokenStore（fake pgvector + fake cipher・課金0）────────────────────────


class _FakeCipher:
    """テスト用: encode/decode を暗号化に見立てる（cipher 経路が通ることの確認用）。"""

    def encrypt(self, plaintext: str) -> bytes:
        return plaintext.encode("utf-8")

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")


class _FakeCursor:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows
        self._result: dict[str, Any] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT"):
            r = self._rows.get(params[0])
            self._result = dict(r) if r else None
        elif s.startswith("INSERT"):
            email, enc, scopes = params
            self._rows[email] = {"refresh_token_enc": enc, "scopes": scopes}

    def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _FakeConn:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        self.committed = True


class _FakePgvector:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.last_user_email: str | None = None

    def connection(self, *, app_role: str, user_email: str) -> _FakeConn:
        self.last_user_email = user_email  # RLS GUC 用に渡される本人 email
        return _FakeConn(self.rows)


def test_rds_token_store_roundtrip() -> None:
    pg = _FakePgvector()
    store = RdsTokenStore(pg, _FakeCipher())
    assert not store.has("A@X.com")
    assert store.get("A@X.com") is None

    store.put("A@X.com", OAuthToken(refresh_token="secret-rt", scopes=("gmail.readonly",)))
    assert store.has("a@x.com")  # email 正規化
    got = store.get("  A@X.COM  ")
    assert got is not None
    assert got.refresh_token == "secret-rt"
    assert got.scopes == ("gmail.readonly",)
    # 平文でなく cipher 通過後の bytes が格納されている（G8）
    assert pg.rows["a@x.com"]["refresh_token_enc"] == b"secret-rt"
    # RLS 用に本人 email が connection() に渡る
    assert pg.last_user_email == "a@x.com"


def test_rds_token_store_satisfies_protocol() -> None:
    assert isinstance(RdsTokenStore(_FakePgvector(), _FakeCipher()), TokenStore)
