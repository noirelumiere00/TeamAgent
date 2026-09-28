"""学習係 HTTP サーバのテスト（本物の HTTP で叩く。TLS は本番起動の要件として別に検査する）。"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from aico_hermes import server as server_mod
from aico_hermes.runner import JobError, LearnResult
from aico_hermes.schema import LearnRequest

TOKEN = "t" * 48


class _Runner:
    def __init__(self) -> None:
        self.calls: list[LearnRequest] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.error: Exception | None = None

    def run(self, request: LearnRequest) -> LearnResult:
        self.calls.append(request)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return LearnResult(
            job_id=request.job_id,
            user_entries=(*request.user_entries, "資料は表形式を好む"),
            memory_entries=request.memory_entries,
            elapsed_s=0.1,
        )


@pytest.fixture
def running() -> Iterator[tuple[server_mod.LearnServer, _Runner]]:
    runner = _Runner()
    srv = server_mod.make_server(
        "127.0.0.1", 0, token=TOKEN, runner=runner, ssl_context=None, max_concurrency=1
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, runner
    finally:
        srv.shutdown()
        srv.server_close()


def _call(
    srv: server_mod.LearnServer,
    method: str,
    path: str,
    body: Any = None,
    *,
    token: str | None = TOKEN,
    raw: bytes | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = (
        raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    )
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, payload


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "job_id": "job_0123456789",
        "snapshot": {"user": ["返事は結論から3行"], "memory": []},
        "utterances": ["花王の資料は表でお願い"],
    }
    body.update(overrides)
    return body


def test_healthz(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, _ = running
    assert _call(srv, "GET", "/healthz", token=None) == (200, {"ok": True})


def test_unknown_paths_are_404(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, _ = running
    assert _call(srv, "GET", "/v1/learn")[0] == 404
    assert _call(srv, "POST", "/v1/other", _body())[0] == 404


@pytest.mark.parametrize("token", [None, "", "wrong" * 10, TOKEN + "x"])
def test_learn_requires_exact_bearer(
    running: tuple[server_mod.LearnServer, _Runner], token: str | None
) -> None:
    srv, runner = running
    status, payload = _call(srv, "POST", "/v1/learn", _body(), token=token)
    assert (status, payload) == (401, {"error": "unauthorized"})
    assert runner.calls == []


def test_learn_success(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, runner = running
    status, payload = _call(srv, "POST", "/v1/learn", _body())
    assert status == 200
    assert payload == {
        "job_id": "job_0123456789",
        "user": ["返事は結論から3行", "資料は表形式を好む"],
        "memory": [],
        "dropped": 0,
        "elapsed_s": 0.1,
    }
    assert len(runner.calls) == 1


def test_schema_error_returns_code_without_echo(
    running: tuple[server_mod.LearnServer, _Runner],
) -> None:
    srv, runner = running
    status, payload = _call(srv, "POST", "/v1/learn", _body(email="tanaka@example.com"))
    assert (status, payload) == (400, {"error": "unexpected_keys"})
    status, payload = _call(srv, "POST", "/v1/learn", raw=b"{not json")
    assert (status, payload) == (400, {"error": "bad_json"})
    assert runner.calls == []


def test_content_type_and_size_limits(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, runner = running
    assert _call(srv, "POST", "/v1/learn", _body(), content_type="text/plain")[0] == 415
    big = json.dumps(
        _body(utterances=["x" * 800] * 5, pad="y" * server_mod.MAX_BODY_BYTES)
    ).encode()
    assert _call(srv, "POST", "/v1/learn", raw=big)[0] == 413
    assert runner.calls == []


def test_busy_when_slots_exhausted(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, runner = running
    runner.gate = threading.Event()
    results: list[tuple[int, dict[str, Any]]] = []
    first = threading.Thread(
        target=lambda: results.append(_call(srv, "POST", "/v1/learn", _body()))
    )
    first.start()
    assert runner.entered.wait(5)
    assert _call(srv, "POST", "/v1/learn", _body()) == (503, {"error": "busy"})
    runner.gate.set()
    first.join(5)
    assert results and results[0][0] == 200
    # 枠が戻ったので次は通る
    runner.gate = None
    assert _call(srv, "POST", "/v1/learn", _body())[0] == 200


def test_job_error_maps_to_502_with_code(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, runner = running
    runner.error = JobError("timeout")
    assert _call(srv, "POST", "/v1/learn", _body()) == (
        502,
        {"error": "timeout", "job_id": "job_0123456789"},
    )


def test_unexpected_error_hides_detail(running: tuple[server_mod.LearnServer, _Runner]) -> None:
    srv, runner = running
    runner.error = RuntimeError("田中さんの電話 090-1234-5678")
    status, payload = _call(srv, "POST", "/v1/learn", _body())
    assert (status, payload) == (500, {"error": "internal", "job_id": "job_0123456789"})
    # 失敗しても枠は返っている
    runner.error = None
    assert _call(srv, "POST", "/v1/learn", _body())[0] == 200


def test_bearer_scheme_is_case_insensitive(
    running: tuple[server_mod.LearnServer, _Runner],
) -> None:
    srv, _ = running
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(
        "POST",
        "/v1/learn",
        body=json.dumps(_body()).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert conn.getresponse().status == 200
    conn.close()


def test_unread_body_never_leaks_into_next_response(
    running: tuple[server_mod.LearnServer, _Runner],
) -> None:
    """本文を読まずに返す経路（401 など）の後、同じ接続の次の応答に本文が載らないこと。"""
    srv, _ = running
    secret = "ZQX-MARKER 山田様 090-1234-5678"
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    body = json.dumps(_body(utterances=[secret]), ensure_ascii=False).encode("utf-8")
    conn.request(
        "POST",
        "/v1/learn",
        body=body,
        headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
    )
    first = conn.getresponse()
    first_raw = first.read()
    assert first.status == 401
    assert first.getheader("Connection") == "close"
    conn.request(
        "POST",
        "/v1/learn",
        body=json.dumps(_body()).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    second = conn.getresponse()
    second_raw = second.read()
    assert second.status == 200
    for raw in (first_raw, second_raw, (second.reason or "").encode()):
        assert b"ZQX-MARKER" not in raw
    conn.close()


def test_crash_log_has_no_exception_text(
    running: tuple[server_mod.LearnServer, _Runner], caplog: pytest.LogCaptureFixture
) -> None:
    srv, runner = running
    runner.error = RuntimeError("ZQX-MARKER 田中さんの電話 090-1234-5678")
    with caplog.at_level("INFO", logger="aico_hermes"):
        assert _call(srv, "POST", "/v1/learn", _body())[0] == 500
    assert "ZQX-MARKER" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_short_token_refused() -> None:
    with pytest.raises(ValueError):
        server_mod.make_server("127.0.0.1", 0, token="short", runner=_Runner(), ssl_context=None)


def test_main_refuses_to_start_without_tls_or_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HERMES_INGRESS_TOKEN",
        "HERMES_TLS_CERT_FILE",
        "HERMES_TLS_KEY_FILE",
        "AICO_HERMES_MODEL",
        "AWS_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_INGRESS_TOKEN", TOKEN)
    monkeypatch.setenv("AICO_HERMES_MODEL", "m")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    assert server_mod.main() == 2  # 証明書・鍵が無いので起動しない


def test_tls_roundtrip_and_plain_http_refused(tmp_path: Any) -> None:
    import shutil
    import ssl
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl が無い環境")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    ctx = server_mod.tls_context(str(cert), str(key))
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    srv = server_mod.make_server("127.0.0.1", 0, token=TOKEN, runner=_Runner(), ssl_context=ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    import socket

    # TCP でつないだまま何も送らない接続があっても、ほかの接続は受け付けられる（握手は受付ループの外）
    stalled = socket.create_connection(("127.0.0.1", srv.server_address[1]))
    try:
        client_ctx = ssl.create_default_context(cafile=str(cert))
        client_ctx.check_hostname = False
        conn = http.client.HTTPSConnection(
            "127.0.0.1", srv.server_address[1], context=client_ctx, timeout=5
        )
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
        plain = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
            plain.request("GET", "/healthz")
            plain.getresponse()
        plain.close()
    finally:
        # 止まった接続を先に閉じる（握手が受付ループの中に戻る退行があっても shutdown で固まらない）
        stalled.close()
        srv.shutdown()
        srv.server_close()
