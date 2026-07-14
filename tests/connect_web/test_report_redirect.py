"""connect-web /r/<token> 短縮リダイレクトのテスト（Part2）。

署名トークン→都度新鮮な presigned へ 302（no-store）・ログイン不要・fail-closed(404)。
presign_get は S3 を叩かないよう monkeypatch する。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app

_BUCKET = "teamagent-dev-raw-files"
_KEY = "vseo-reports/abc123.html"
_FAKE_PRESIGNED = (
    "https://s3.ap-northeast-1.amazonaws.com/teamagent-dev-raw-files/"
    "vseo-reports/abc123.html?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "test-secret-xyz")
    monkeypatch.setenv("VSEO_REPORT_BUCKET", _BUCKET)
    return TestClient(create_app())


def _patch_presign(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    import teamagent.adapters.report_publish as rp

    monkeypatch.setattr(rp, "presign_get", lambda bucket, key, **kw: url)


def test_valid_token_redirects_302_with_no_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.adapters.report_link_token import encode_report_token

    _patch_presign(monkeypatch, _FAKE_PRESIGNED)
    token = encode_report_token(_BUCKET, _KEY)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == _FAKE_PRESIGNED  # 都度生成した新鮮な presigned へ
    assert r.headers["cache-control"] == "no-store"  # 期限切れURLを中間キャッシュに残さない


def test_no_login_required(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cookie 無し（未ログイン）でも 302（/login への 303 ではない）＝Slack受信者がそのまま開ける。"""
    from teamagent.adapters.report_link_token import encode_report_token

    _patch_presign(monkeypatch, "https://example.test/presigned")
    token = encode_report_token(_BUCKET, "vseo-proposals/deck.pdf")
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302


def test_invalid_token_404(client: TestClient) -> None:
    r = client.get("/r/not-a-valid-token", follow_redirects=False)
    assert r.status_code == 404


def test_expired_token_404(client: TestClient) -> None:
    from teamagent.adapters.report_link_token import encode_report_token

    token = encode_report_token(_BUCKET, _KEY, now=int(time.time()) - 100, ttl_s=10)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 404


def test_foreign_prefix_token_404(client: TestClient) -> None:
    """署名は正しくても許可プレフィックス外の key は 404（任意オブジェクト読取の転用防止）。"""
    from teamagent.adapters.report_link_token import encode_report_token

    token = encode_report_token(_BUCKET, "secrets/leak.txt")
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 404


def test_presign_failure_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.report_link_token import encode_report_token

    _patch_presign(monkeypatch, None)  # S3 権限不足等で presign 失敗
    token = encode_report_token(_BUCKET, _KEY)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 404
