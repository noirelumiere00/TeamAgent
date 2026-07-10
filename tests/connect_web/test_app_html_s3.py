"""/app の app.html S3 ホットスワップと /healthz 拡張のテスト（実 S3 0・実 boto3 0）。

boto3 は sys.modules への fake 注入で排除し、次を検証する:
  - env 未設定 → 従来どおりイメージ同梱（baked）/ 無ければ「準備中」（missing）
  - S3 成功 → source="s3"・配信バイト列と healthz の sha256 先頭12hex が一致
  - S3 失敗 → ERROR ログの上 baked へフォールバック（サービスは落ちない）
  - 取得はプロセス内で1回だけ（モジュールキャッシュ）
  - /healthz に app_html_sha256 / app_html_source が載る（既存 ok は維持）
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import teamagent.connect_web.app as app_module
from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"unit-test-apphtml-secret-32bytes!"
_EMAIL = "s-komata@vectorinc.co.jp"

_BAKED_HTML = "<html><body>baked版 app.html</body></html>"
_S3_HTML = "<html><body>S3版 app.html（ホットスワップ）</body></html>"
_S3_URI = "s3://teamagent-dev-raw-files/codebuild/connect-web-app.html"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """各テストの前後でモジュールキャッシュと env をクリアする（テスト間の汚染防止）。"""
    app_module._reset_app_html_cache()
    monkeypatch.delenv("CONNECT_APP_HTML_S3_URI", raising=False)
    yield
    app_module._reset_app_html_cache()


def _config() -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({_EMAIL}),
        allowed_hd=None,
        google_client_id="cid-123",
        session_secret=_SECRET,
        dev_bypass=False,
        cookie_secure=False,
    )


def _client() -> TestClient:
    return TestClient(create_app(search_config=_config()))


def _auth_cookie() -> dict[str, str]:
    return {"ta_search_session": make_session(_EMAIL, _SECRET, ttl_s=3600)}


class _FakeS3Client:
    """get_object の呼び出しを記録し、固定 HTML（または例外）を返す fake。"""

    def __init__(self, *, body: bytes, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append((Bucket, Key))
        if self._error is not None:
            raise self._error
        return {"Body": io.BytesIO(self._body)}


def _install_fake_boto3(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes = _S3_HTML.encode("utf-8"),
    error: Exception | None = None,
) -> _FakeS3Client:
    """sys.modules に fake boto3 を注入する（遅延 import 側でこれが解決される）。"""
    s3 = _FakeS3Client(body=body, error=error)
    fake = types.ModuleType("boto3")
    fake.client = lambda service: s3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return s3


def _use_baked(monkeypatch: pytest.MonkeyPatch) -> None:
    """イメージ同梱 static/app.html の有無に依存しないよう固定 HTML を注入する。"""
    monkeypatch.setattr(app_module, "_static_app_html", lambda: _BAKED_HTML)


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------- env 未設定（従来挙動） ----------------


def test_env_unset_serves_baked(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 未設定なら従来どおりイメージ同梱版を配信し source="baked"。"""
    _use_baked(monkeypatch)
    client = _client()
    r = client.get("/app", cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.text == _BAKED_HTML
    h = client.get("/healthz").json()
    assert h["app_html_source"] == "baked"
    assert h["app_html_sha256"] == _sha12(_BAKED_HTML)


def test_env_unset_without_baked_serves_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 未設定かつ同梱ファイルも無ければ従来どおり「準備中」で source="missing"。"""
    monkeypatch.setattr(app_module, "_static_app_html", lambda: app_module._APP_HTML_MISSING)
    client = _client()
    r = client.get("/app", cookies=_auth_cookie())
    assert r.status_code == 200
    assert "準備中" in r.text
    assert client.get("/healthz").json()["app_html_source"] == "missing"


# ---------------- S3 オーバーライド ----------------


def test_s3_success_serves_s3_html_and_sha_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 取得成功なら S3 版を配信し、healthz の sha が配信バイト列と一致する。"""
    _use_baked(monkeypatch)
    monkeypatch.setenv("CONNECT_APP_HTML_S3_URI", _S3_URI)
    s3 = _install_fake_boto3(monkeypatch)
    client = _client()
    r = client.get("/app", cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.text == _S3_HTML
    # URI が bucket/key に正しく分解されて get_object に渡ること。
    assert s3.calls == [("teamagent-dev-raw-files", "codebuild/connect-web-app.html")]
    h = client.get("/healthz").json()
    assert h["ok"] is True
    assert h["app_html_source"] == "s3"
    assert h["app_html_sha256"] == _sha12(_S3_HTML)
    assert h["app_html_sha256"] == hashlib.sha256(r.content).hexdigest()[:12]


def test_s3_fetch_happens_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 取得はプロセス内で1回だけ（以降はモジュールキャッシュ配信）。"""
    _use_baked(monkeypatch)
    monkeypatch.setenv("CONNECT_APP_HTML_S3_URI", _S3_URI)
    s3 = _install_fake_boto3(monkeypatch)
    client = _client()
    client.get("/healthz")
    client.get("/app", cookies=_auth_cookie())
    client.get("/app", cookies=_auth_cookie())
    assert len(s3.calls) == 1


def test_s3_failure_falls_back_to_baked(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3 取得失敗はサービスを落とさず baked へフォールバック（healthz で検知可能）。"""
    _use_baked(monkeypatch)
    monkeypatch.setenv("CONNECT_APP_HTML_S3_URI", _S3_URI)
    _install_fake_boto3(monkeypatch, error=RuntimeError("s3 down"))
    client = _client()
    r = client.get("/app", cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.text == _BAKED_HTML
    h = client.get("/healthz").json()
    assert h["ok"] is True  # サービス自体は健康（劣化は source で観測）
    assert h["app_html_source"] == "baked"
    assert h["app_html_sha256"] == _sha12(_BAKED_HTML)


def test_s3_bad_uri_falls_back_to_baked(monkeypatch: pytest.MonkeyPatch) -> None:
    """URI 不正（s3:// でない・key 欠落）も落とさず baked へフォールバック。"""
    _use_baked(monkeypatch)
    monkeypatch.setenv("CONNECT_APP_HTML_S3_URI", "https://example.com/app.html")
    client = _client()
    assert client.get("/healthz").json()["app_html_source"] == "baked"


# ---------------- /healthz フィールド ----------------


def test_healthz_has_app_html_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """/healthz に app_html_sha256（12hex）と app_html_source が載り、既存 ok は維持。"""
    _use_baked(monkeypatch)
    h = _client().get("/healthz").json()
    assert h["ok"] is True
    assert h["app_html_source"] in {"s3", "baked", "missing"}
    assert re.fullmatch(r"[0-9a-f]{12}", h["app_html_sha256"])
