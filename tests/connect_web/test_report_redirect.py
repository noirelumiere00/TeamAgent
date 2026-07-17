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
_REPORT_SECRET = "report-test-secret-" + "r" * 32
_REPORT_NEXT_SECRET = "report-next-secret-" + "n" * 32
_FAKE_PRESIGNED = (
    "https://s3.ap-northeast-1.amazonaws.com/teamagent-dev-raw-files/"
    "vseo-reports/abc123.html?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", raising=False)
    monkeypatch.delenv("REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", raising=False)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_SECRET)
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
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


def test_previous_key_redirects_only_when_explicitly_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.adapters.report_link_token import encode_report_token

    token = encode_report_token(_BUCKET, _KEY)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_NEXT_SECRET)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _REPORT_SECRET)
    monkeypatch.setenv(
        "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(int(time.time()) + 60 * 60 * 24 * 7)
    )
    _patch_presign(monkeypatch, _FAKE_PRESIGNED)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == _FAKE_PRESIGNED


def test_shortlink_presign_is_short_lived(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/r が再発行する presigned は短命（token TTL＋短命＝実効窓を旧 presigned と同等に抑える）。"""
    import teamagent.adapters.report_publish as rp
    from teamagent.adapters.report_link_token import encode_report_token

    captured: dict[str, object] = {}

    def _fake(bucket: str, key: str, **kw: object) -> str:
        captured.update(kw)
        return "https://example.test/presigned"

    monkeypatch.setattr(rp, "presign_get", _fake)
    client.get(f"/r/{encode_report_token(_BUCKET, _KEY)}", follow_redirects=False)
    assert captured.get("expires_s") == 900  # 短命（7日 presigned を毎回配らない）


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


def test_access_log_redacts_shortlink_token() -> None:
    """アクセスログの /r/<token> がトークンごと伏せられ、他ルートは伏せない（CloudWatch 流出防止）。"""
    import logging

    from teamagent.connect_web.app import (
        _RedactShortLinkAccessLog,
        build_uvicorn_log_config,
    )

    fmt = '%s - "%s %s HTTP/%s" %d'
    flt = _RedactShortLinkAccessLog()

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        fmt,
        ("1.2.3.4:5", "GET", "/r/SECRET.TOKEN", "1.1", 302),
        None,
    )
    assert flt.filter(rec) is True
    assert rec.args[2] == "/r/<redacted>"  # トークンは伏せる
    assert "SECRET" not in (rec.getMessage())

    rec2 = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        fmt,
        ("1.2.3.4:5", "GET", "/app", "1.1", 200),
        None,
    )
    flt.filter(rec2)
    assert rec2.args[2] == "/app"  # 他ルートはそのまま（観測性維持）

    cfg = build_uvicorn_log_config()  # log_config に filter が登録される
    assert "redact_shortlink" in cfg.get("filters", {})
    assert "redact_shortlink" in cfg["loggers"]["uvicorn.access"].get("filters", [])
