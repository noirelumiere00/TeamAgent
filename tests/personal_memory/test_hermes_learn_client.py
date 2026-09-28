"""HermesLearnClient の試験。本物の hermes_runtime（aico_hermes.server）に TLS で当てる。

hermes_runtime は TeamAgent の wheel に入らない別パッケージなので、path を足して読む（tests/hermes_runtime と同じ）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from teamagent.adapters import hermes_learn_client as hlc
from teamagent.adapters.hermes_learn_client import HermesLearnClient, HermesLearnError

_RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "hermes_runtime"
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from aico_hermes import schema as runtime_schema  # noqa: E402
from aico_hermes import server as runtime_server  # noqa: E402
from aico_hermes.runner import JobError, LearnResult  # noqa: E402

TOKEN = "b" * 48


def _cert(tmp: Path, name: str, san: str) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl が無い環境")
    cert, key = tmp / f"{name}.pem", tmp / f"{name}.key"
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
            "/CN=hermes-test",
            "-addext",
            f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class _Runner:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.error: Exception | None = None

    def run(self, request: Any) -> LearnResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return LearnResult(
            job_id=request.job_id,
            user_entries=(*request.user_entries, "資料は表形式を好む"),
            memory_entries=request.memory_entries,
            elapsed_s=0.1,
            dropped=0,
        )


@pytest.fixture(scope="module")
def tls(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("tls")
    cert, key = _cert(tmp, "server", "DNS:localhost,IP:127.0.0.1")
    other, _ = _cert(tmp, "other", "DNS:localhost,IP:127.0.0.1")
    wrong_name, wrong_key = _cert(tmp, "wrongname", "DNS:not-hermes.example")
    return {
        "cert": cert,
        "key": key,
        "other": other,
        "wrong_name": wrong_name,
        "wrong_key": wrong_key,
    }


@pytest.fixture
def served(tls: dict[str, Path]) -> Iterator[tuple[Any, _Runner]]:
    runner = _Runner()
    ctx = runtime_server.tls_context(str(tls["cert"]), str(tls["key"]))
    srv = runtime_server.make_server("127.0.0.1", 0, token=TOKEN, runner=runner, ssl_context=ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, runner
    finally:
        srv.shutdown()
        srv.server_close()


def _client(
    srv: Any, ca: Path, *, token: str = TOKEN, host: str = "127.0.0.1"
) -> HermesLearnClient:
    port = srv.server_address[1]
    return HermesLearnClient(
        service_url=f"https://{host}:{port}",
        bearer=token,
        ca_pem=ca.read_text(),
        expected_port=port,
    )


def _learn(client: HermesLearnClient, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "job_id": "job_0123456789",
        "user_entries": ["返事は結論から"],
        "memory_entries": [],
        "utterances": ["花王の資料は表で"],
    }
    kwargs.update(overrides)
    return client.learn(**kwargs)


def test_learn_roundtrip_over_tls(served: tuple[Any, _Runner], tls: dict[str, Path]) -> None:
    srv, runner = served
    result = _learn(_client(srv, tls["cert"]))
    assert result.job_id == "job_0123456789"
    assert result.user_entries == ("返事は結論から", "資料は表形式を好む")
    assert result.dropped == 0
    assert runner.calls[0].utterances == ("花王の資料は表で",)


def test_wrong_bearer_is_unauthorized(served: tuple[Any, _Runner], tls: dict[str, Path]) -> None:
    srv, runner = served
    with pytest.raises(HermesLearnError) as exc:
        _learn(_client(srv, tls["cert"], token="w" * 48))
    assert exc.value.code == "unauthorized"
    assert runner.calls == []


def test_untrusted_certificate_is_refused(
    served: tuple[Any, _Runner], tls: dict[str, Path]
) -> None:
    srv, runner = served
    with pytest.raises(HermesLearnError) as exc:
        _learn(_client(srv, tls["other"]))  # 別の自己署名をトラストアンカーにした
    assert exc.value.code == "connect_failed"
    assert runner.calls == []


def test_hostname_mismatch_is_refused(tls: dict[str, Path]) -> None:
    runner = _Runner()
    ctx = runtime_server.tls_context(str(tls["wrong_name"]), str(tls["wrong_key"]))
    srv = runtime_server.make_server("127.0.0.1", 0, token=TOKEN, runner=runner, ssl_context=ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HermesLearnError) as exc:
            _learn(_client(srv, tls["wrong_name"]))  # 信頼はしているがホスト名が違う
        assert exc.value.code == "connect_failed"
        assert runner.calls == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_busy_and_failures_map_to_codes(served: tuple[Any, _Runner], tls: dict[str, Path]) -> None:
    srv, runner = served
    runner.error = JobError("timeout")
    with pytest.raises(HermesLearnError) as exc:
        _learn(_client(srv, tls["cert"]))
    assert exc.value.code == "hermes_failed"
    srv.slots = threading.BoundedSemaphore(1)
    srv.slots.acquire()  # 枠を埋めて 503 を起こす
    with pytest.raises(HermesLearnError) as exc:
        _learn(_client(srv, tls["cert"]))
    assert exc.value.code == "busy"


@pytest.mark.parametrize(
    "overrides",
    [
        {"utterances": []},
        {"utterances": ["a"] * 6},
        {"utterances": ["x" * 801]},
        {"user_entries": ["区切り§入り"]},
        {"user_entries": ["あ" * 200] * 7},
    ],
)
def test_bad_requests_are_rejected_before_sending(overrides: dict[str, Any]) -> None:
    sent: list[Any] = []
    transport = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200))
    client = HermesLearnClient(
        service_url="https://hermes.local:8790",
        bearer=TOKEN,
        ca_pem="",
        http=httpx.Client(transport=transport),
    )
    with pytest.raises(HermesLearnError) as exc:
        _learn(client, **overrides)
    assert exc.value.code == "bad_request"
    assert sent == []


def _mock_client(payload: Any, status: int = 200) -> HermesLearnClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(status, content=body)

    return HermesLearnClient(
        service_url="https://hermes.local:8790",
        bearer=TOKEN,
        ca_pem="",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


_OK = {"job_id": "job_0123456789", "user": ["a"], "memory": [], "dropped": 0, "elapsed_s": 1.0}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        ["job_0123456789"],
        {**_OK, "extra": 1},
        {k: v for k, v in _OK.items() if k != "dropped"},
        {**_OK, "job_id": "job_other000000"},
        {**_OK, "user": ["区切り§入り"]},
        {**_OK, "user": [" 空白 "]},
        {**_OK, "user": ["あ" * 201]},
        {**_OK, "user": ["あ" * 200] * 7},
        {**_OK, "memory": "not a list"},
        {**_OK, "dropped": True},
        {**_OK, "dropped": -1},
    ],
)
def test_malformed_responses_are_rejected(payload: Any) -> None:
    with pytest.raises(HermesLearnError) as exc:
        _learn(_mock_client(payload))
    assert exc.value.code == "bad_response"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "rejected"),
        (401, "unauthorized"),
        (413, "rejected"),
        (500, "hermes_failed"),
        (502, "hermes_failed"),
        (503, "busy"),
        (302, "unexpected_status"),
    ],
)
def test_status_codes(status: int, code: str) -> None:
    with pytest.raises(HermesLearnError) as exc:
        _learn(_mock_client({"error": "x"}, status=status))
    assert exc.value.code == code


def test_connect_error_retried_once_but_timeout_not() -> None:
    calls: list[str] = []

    def connect_fail(request: httpx.Request) -> httpx.Response:
        calls.append("c")
        raise httpx.ConnectError("refused", request=request)

    client = HermesLearnClient(
        service_url="https://hermes.local:8790",
        bearer=TOKEN,
        ca_pem="",
        http=httpx.Client(transport=httpx.MockTransport(connect_fail)),
    )
    with pytest.raises(HermesLearnError) as exc:
        _learn(client)
    assert exc.value.code == "connect_failed" and calls == ["c", "c"]

    calls.clear()

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append("t")
        raise httpx.ReadTimeout("slow", request=request)

    client = HermesLearnClient(
        service_url="https://hermes.local:8790",
        bearer=TOKEN,
        ca_pem="",
        http=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(HermesLearnError) as exc:
        _learn(client)
    assert exc.value.code == "timeout" and calls == [
        "t"
    ]  # 送った後は再送しない（二重学習を避ける）


@pytest.mark.parametrize(
    "url",
    [
        "http://hermes.local:8790",
        "https://hermes.local:443",
        "https://hermes.local",
        "https://user:pw@hermes.local:8790",
        "https://hermes.local:8790/v1?x=1",
        "https://hermes.local:8790/#frag",
        "https://hermes.local:8790/other",
    ],
)
def test_service_url_validation(url: str) -> None:
    with pytest.raises(HermesLearnError) as exc:
        hlc.validate_service_url(url)
    assert exc.value.code == "bad_service_url"


def test_from_env_disabled_when_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HERMES_SERVICE_URL", "TEAMAGENT_HERMES_INGRESS_BEARER", "HERMES_TLS_CA_PEM"):
        monkeypatch.delenv(name, raising=False)
    assert HermesLearnClient.from_env() is None
    monkeypatch.setenv("HERMES_SERVICE_URL", "https://hermes.local:8790")
    monkeypatch.setenv("TEAMAGENT_HERMES_INGRESS_BEARER", "short")
    monkeypatch.setenv("HERMES_TLS_CA_PEM", "-----BEGIN CERTIFICATE-----\nx\n")
    assert HermesLearnClient.from_env() is None


def test_error_and_repr_never_contain_secrets_or_text() -> None:
    client = _mock_client(b"not json")
    assert TOKEN not in repr(client)
    with pytest.raises(HermesLearnError) as exc:
        _learn(client, utterances=["ZQX-MARKER 山田様 090-1234-5678"])
    assert "ZQX" not in str(exc.value) and TOKEN not in str(exc.value)


def test_limits_match_hermes_runtime_contract() -> None:
    assert hlc.MAX_UTTERANCES == runtime_schema.MAX_UTTERANCES
    assert hlc.MAX_UTTERANCE_CHARS == runtime_schema.MAX_UTTERANCE_CHARS
    assert hlc.MAX_ENTRY_CHARS == runtime_schema.MAX_ENTRY_CHARS
    assert hlc.MAX_SNAPSHOT_ENTRIES == runtime_schema.MAX_SNAPSHOT_ENTRIES
    assert hlc.USER_CHAR_LIMIT == runtime_schema.USER_CHAR_LIMIT
    assert hlc.MEMORY_CHAR_LIMIT == runtime_schema.MEMORY_CHAR_LIMIT
    assert hlc.ENTRY_DELIMITER == runtime_schema.ENTRY_DELIMITER
    assert hlc.HERMES_PORT == runtime_server.DEFAULT_PORT
    assert runtime_schema.MAX_ENTRY_CHARS == 200


def test_store_limits_match_client() -> None:
    from teamagent.adapters import personal_memory_store as store

    assert store.TARGET_CHAR_LIMIT == {"user": hlc.USER_CHAR_LIMIT, "memory": hlc.MEMORY_CHAR_LIMIT}
    assert store.MAX_ENTRY_CHARS == hlc.MAX_ENTRY_CHARS
    assert store.ENTRY_DELIMITER == hlc.ENTRY_DELIMITER
    assert store.MAX_ENTRIES_PER_TARGET == hlc.MAX_SNAPSHOT_ENTRIES
