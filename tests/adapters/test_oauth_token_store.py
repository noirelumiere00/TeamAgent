"""per-user OAuth トークンストアのテスト（課金0）。

repr によるトークン秘匿（G8）と、email 正規化込みの get/put/has を検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import (
    InMemoryTokenStore,
    OAuthToken,
    RdsTokenStore,
    SlackTokenStore,
    TokenStore,
)


def test_token_repr_hides_secret() -> None:
    t = OAuthToken(
        refresh_token="super-secret-refresh",
        scopes=("gmail.readonly",),
        id_token="super-secret-id-token",
    )
    r = repr(t)
    assert "super-secret-refresh" not in r  # 誤ログ防止（G8）
    assert "super-secret-id-token" not in r
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
    """テスト用 cipher: 平文と異なる ciphertext を返し（暗号化を模す）、context を記録する。"""

    def __init__(self) -> None:
        self.enc_context: dict[str, str] | None = None
        self.dec_context: dict[str, str] | None = None

    def encrypt(self, plaintext: str, *, context: dict[str, str] | None = None) -> bytes:
        self.enc_context = context
        return b"ENC:" + plaintext.encode("utf-8")[::-1]  # 反転で平文 substring を残さない

    def decrypt(self, ciphertext: bytes, *, context: dict[str, str] | None = None) -> str:
        self.dec_context = context
        assert ciphertext.startswith(b"ENC:"), "cipher を通っていない平文が来ている"
        return ciphertext[4:][::-1].decode("utf-8")


class _FakeCursor:
    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        statements: list[tuple[str, Any]],
    ) -> None:
        self._rows = rows
        self._statements = statements
        self._result: dict[str, Any] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._statements.append((s, params))
        if s.startswith("SELECT"):
            r = self._rows.get(params[0])
            self._result = dict(r) if r else None
        elif s.startswith("INSERT"):
            email, enc, scopes = params
            self._rows[email] = {"refresh_token_enc": enc, "scopes": scopes}

    def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _FakeConn:
    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        statements: list[tuple[str, Any]],
    ) -> None:
        self._rows = rows
        self._statements = statements
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self._statements)

    def commit(self) -> None:
        self.committed = True


class _FakePgvector:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.statements: list[tuple[str, Any]] = []
        self.last_user_email: str | None = None
        self.last_conn: _FakeConn | None = None

    def connection(self, *, app_role: str, user_email: str) -> _FakeConn:
        self.last_user_email = user_email  # RLS GUC 用に渡される本人 email
        self.last_conn = _FakeConn(self.rows, self.statements)
        return self.last_conn


def test_rds_token_store_roundtrip() -> None:
    pg = _FakePgvector()
    cipher = _FakeCipher()
    store = RdsTokenStore(pg, cipher)
    assert not store.has("A@X.com")
    assert store.get("A@X.com") is None

    store.put("A@X.com", OAuthToken(refresh_token="secret-rt", scopes=("gmail.readonly",)))
    # put は commit する（最後の get で last_conn が上書きされる前に確認）
    assert pg.last_conn is not None and pg.last_conn.committed is True
    assert store.has("a@x.com")  # email 正規化
    got = store.get("  A@X.COM  ")
    assert got is not None
    assert got.refresh_token == "secret-rt"
    assert got.scopes == ("gmail.readonly",)
    # 平文が DB に残らない（cipher 通過後の ciphertext のみ）— G8
    stored = pg.rows["a@x.com"]["refresh_token_enc"]
    assert b"secret-rt" not in stored
    assert stored.startswith(b"ENC:")
    # KMS EncryptionContext に本人 email が綴じられる（per-user 暗号束縛・#1）
    assert cipher.enc_context == {"user_email": "a@x.com"}
    assert cipher.dec_context == {"user_email": "a@x.com"}
    # RLS 用に本人 email が connection() に渡る
    assert pg.last_user_email == "a@x.com"


def test_rds_token_store_upsert_updates_existing() -> None:
    """同一 email に2回 put → 上書き更新（再認可でのトークンローテーション）。"""
    pg = _FakePgvector()
    store = RdsTokenStore(pg, _FakeCipher())
    store.put("a@x.com", OAuthToken(refresh_token="old-rt"))
    store.put("a@x.com", OAuthToken(refresh_token="new-rt", scopes=("drive.readonly",)))
    assert len(pg.rows) == 1  # 行は増えない（PK upsert）
    got = store.get("a@x.com")
    assert got is not None and got.refresh_token == "new-rt"
    assert got.scopes == ("drive.readonly",)


class _StubKms:
    """boto3 KMS client の最小 stub（課金0）。EncryptionContext を ciphertext に綴じ込み、
    復号時に不一致なら拒否する＝実 KMS の AAD 挙動を模す。"""

    def __init__(self) -> None:
        self.last_encrypt: dict[str, Any] = {}
        self.last_decrypt: dict[str, Any] = {}

    @staticmethod
    def _ctx_bytes(kw: dict[str, Any]) -> bytes:
        import json

        return json.dumps(kw.get("EncryptionContext", {}), sort_keys=True).encode("utf-8")

    def encrypt(self, **kw: Any) -> dict[str, Any]:
        self.last_encrypt = kw
        return {"CiphertextBlob": b"K:" + self._ctx_bytes(kw) + b"::" + kw["Plaintext"]}

    def decrypt(self, **kw: Any) -> dict[str, Any]:
        self.last_decrypt = kw
        blob = bytes(kw["CiphertextBlob"])
        ctx_part, pt = blob[2:].split(b"::", 1)
        if ctx_part != self._ctx_bytes(kw):
            raise ValueError("InvalidCiphertextException: EncryptionContext mismatch")
        return {"Plaintext": pt}


def test_kms_cipher_roundtrip_and_encryption_context() -> None:
    """KmsCipher が KeyId/EncryptionContext を正しく渡し round-trip する。context 不一致は復号拒否。"""
    from teamagent.adapters.oauth_token_store import KmsCipher

    stub = _StubKms()
    cipher = KmsCipher("key-1", client=stub)
    blob = cipher.encrypt("super-secret", context={"user_email": "a@x.com"})
    assert isinstance(blob, bytes)
    assert stub.last_encrypt["KeyId"] == "key-1"
    assert stub.last_encrypt["Plaintext"] == b"super-secret"  # bytes で渡る
    assert stub.last_encrypt["EncryptionContext"] == {"user_email": "a@x.com"}
    # round-trip（同一 context）
    assert cipher.decrypt(blob, context={"user_email": "a@x.com"}) == "super-secret"
    assert stub.last_decrypt["KeyId"] == "key-1"  # decrypt も KeyId をガードに渡す
    # 他人の context では復号できない（per-user 束縛の暗号レイヤ担保）
    with pytest.raises(ValueError):
        cipher.decrypt(blob, context={"user_email": "b@x.com"})


def test_rds_token_store_satisfies_protocol() -> None:
    assert isinstance(RdsTokenStore(_FakePgvector(), _FakeCipher()), TokenStore)


def test_slack_token_store_reads_existing_token() -> None:
    """変更前に保存済みの行を、従来どおり復号して読める。"""
    pg = _FakePgvector()
    cipher = _FakeCipher()
    pg.rows["a@x.com"] = {
        "xoxp_token_enc": cipher.encrypt(
            "xoxp-existing",
            context={"user_email": "a@x.com"},
        ),
        "scopes": ["search:read", "users:read"],
        "slack_user_id": "U123",
        "team_id": "T123",
    }

    token = SlackTokenStore(pg, cipher).get(" A@X.COM ")

    assert token is not None
    assert token.access_token == "xoxp-existing"
    assert token.scopes == ("search:read", "users:read")
    assert token.slack_user_id == "U123"
    assert token.team_id == "T123"
    assert cipher.dec_context == {"user_email": "a@x.com"}


def test_slack_token_store_slack_user_id_does_not_decrypt_xoxp() -> None:
    pg = _FakePgvector()
    pg.rows["a@x.com"] = {"slack_user_id": "U123"}
    cipher = _FakeCipher()
    store = SlackTokenStore(pg, cipher)

    assert store.slack_user_id(" A@X.COM ") == "U123"
    assert cipher.dec_context is None
    assert pg.statements[-1] == (
        "SELECT slack_user_id FROM slack_oauth_tokens WHERE user_email = %s",
        ("a@x.com",),
    )
    assert pg.last_user_email == "a@x.com"


def test_slack_token_store_slack_user_id_preserves_empty_and_missing() -> None:
    pg = _FakePgvector()
    pg.rows["empty@x.com"] = {"slack_user_id": ""}
    store = SlackTokenStore(pg, _FakeCipher())

    assert store.slack_user_id("empty@x.com") == ""
    assert store.slack_user_id("missing@x.com") is None
