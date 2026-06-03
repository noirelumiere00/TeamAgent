"""per-user OAuth トークンストアのテスト（課金0）。

repr によるトークン秘匿（G8）と、email 正規化込みの get/put/has を検証する。
"""

from __future__ import annotations

from teamagent.adapters.oauth_token_store import (
    InMemoryTokenStore,
    OAuthToken,
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
