"""学習係の HTTP サーバ（TLS 必須）。

- GET  /healthz   … 生存確認
- POST /v1/learn  … ingress bearer 必須。同時 2 件まで（超えたら 503 で断り、待たせない）

応答にも記録にも、発話・メモの中身は出さない（理由コードと件数だけ）。
- 応答のたびに接続を閉じる（読まずに残した本文が次のリクエスト行として解釈され、
  エラー応答に本文が載るのを防ぐ）
- TLS の握手は受付ループではなく各接続のスレッドで、時間切れ付きで行う
  （何も送らない接続 1 本で受付全体が止まるのを防ぐ）。同時接続数にも上限を設ける
本番の起動（main）は TLS の証明書・鍵と ingress bearer が揃わなければ起動しない。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final, Protocol

from .runner import JobError, JobRunner, LearnResult, sweep_stale_workdirs
from .schema import LearnRequest, RequestError, parse_learn_request

logger = logging.getLogger("aico_hermes")

MAX_BODY_BYTES: Final = 128 * 1024
MIN_TOKEN_CHARS: Final = 32
DEFAULT_PORT: Final = 8790
DEFAULT_MAX_CONCURRENCY: Final = 2
DEFAULT_MAX_CONNECTIONS: Final = 16
SOCKET_TIMEOUT_S: Final = 15.0
_KNOWN_PATHS: Final = frozenset({"/healthz", "/v1/learn"})


class Runner(Protocol):
    def run(self, request: LearnRequest) -> LearnResult: ...


class LearnServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        runner: Runner,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        if len(token) < MIN_TOKEN_CHARS:
            raise ValueError("ingress token が短すぎる")
        super().__init__(address, LearnHandler)
        self.token = token.encode("utf-8")
        self.runner = runner
        self.slots = threading.BoundedSemaphore(max_concurrency)
        self.connection_slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        # 同時接続の上限を超えたら、スレッドを作らずに閉じる
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # 既定の実装は traceback を stderr に出す。握手の失敗や切断は型名だけを記録する
        exc_type = sys.exc_info()[0]
        logger.warning("connection error type=%s", exc_type.__name__ if exc_type else "unknown")


class LearnHandler(BaseHTTPRequestHandler):
    server: LearnServer
    protocol_version = "HTTP/1.1"
    server_version = "aico-hermes"
    sys_version = ""
    # 読み書きの時間切れ（握手・ヘッダ・本文が途中で止まった接続を切る）
    timeout = SOCKET_TIMEOUT_S

    def setup(self) -> None:
        super().setup()  # ここで self.timeout がソケットに設定される
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    def log_message(self, format: str, *args: Any) -> None:
        # 既定の実装はリクエスト行（本文が紛れ込みうる）を stderr に出す。記録は _send の 1 行だけ
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        path = self.path if self.path in _KNOWN_PATHS else "other"
        logger.info("request method=%s path=%s status=%d", self.command, path, status)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not_found"})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        return hmac.compare_digest(value.strip().encode("utf-8"), self.server.token)

    def do_POST(self) -> None:
        if self.path != "/v1/learn":
            self._send(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, {"error": "length_required"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "too_large"})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._send(415, {"error": "unsupported_media_type"})
            return
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            return
        if len(raw) != length:
            self._send(400, {"error": "short_body"})
            return
        try:
            request = parse_learn_request(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            code = exc.code if isinstance(exc, RequestError) else "bad_json"
            self._send(400, {"error": code})
            return
        if not self.server.slots.acquire(blocking=False):
            self._send(503, {"error": "busy"})
            return
        try:
            result = self.server.runner.run(request)
        except JobError as exc:
            self._send(502, {"error": exc.code, "job_id": request.job_id})
            return
        except Exception as exc:
            # 例外文に発話が含まれうるので、型名だけを記録する
            logger.error("learn job crashed job_id=%s type=%s", request.job_id, type(exc).__name__)
            self._send(500, {"error": "internal", "job_id": request.job_id})
            return
        finally:
            self.server.slots.release()
        self._send(
            200,
            {
                "job_id": result.job_id,
                "user": list(result.user_entries),
                "memory": list(result.memory_entries),
                "dropped": result.dropped,
                "elapsed_s": result.elapsed_s,
            },
        )


def tls_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def make_server(
    host: str,
    port: int,
    *,
    token: str,
    runner: Runner,
    ssl_context: ssl.SSLContext | None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
) -> LearnServer:
    server = LearnServer(
        (host, port),
        token=token,
        runner=runner,
        max_concurrency=max_concurrency,
        max_connections=max_connections,
    )
    if ssl_context is not None:
        # 握手は LearnHandler.setup で（接続ごとのスレッドの中で、時間切れ付きで）行う
        server.socket = ssl_context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = os.environ
    required = (
        "HERMES_INGRESS_TOKEN",
        "HERMES_TLS_CERT_FILE",
        "HERMES_TLS_KEY_FILE",
        "AICO_HERMES_MODEL",
        "AWS_REGION",
    )
    missing = [name for name in required if not env.get(name)]
    if missing:
        logger.error("起動に必要な設定がない: %s", ",".join(missing))
        return 2
    swept = sweep_stale_workdirs()
    if swept:
        logger.warning("前回の作業場を %d 件消した", swept)
    runner = JobRunner(
        child_cmd=[sys.executable, "-P", "-m", "aico_hermes.child"],
        model=env["AICO_HERMES_MODEL"],
        region=env["AWS_REGION"],
    )
    server = make_server(
        env.get("HERMES_BIND", "0.0.0.0"),  # nosec B104 - SG で MCP からだけ届く
        int(env.get("HERMES_PORT", str(DEFAULT_PORT))),
        token=env["HERMES_INGRESS_TOKEN"],
        runner=runner,
        ssl_context=tls_context(env["HERMES_TLS_CERT_FILE"], env["HERMES_TLS_KEY_FILE"]),
    )

    def _stop(signum: int, frame: Any) -> None:
        # PID 1 で動くので SIGTERM を自分で受けて止める（既定では無視され、停止に 30 秒かかる）
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    logger.info("aico-hermes listening port=%s", server.server_address[1])
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
