"""Inert two-origin fixture for a CUA browser check of form -> 303 CSP behavior.

No OAuth, accounts, credentials, or external network calls are involved. Open
http://127.0.0.1:17941/self-only and /allowed-callback in the browser, submit once,
and compare whether the inert callback on port17942 is reached. The 303 response
keeps the application's unmodified baseline CSP in both cases.
"""

from __future__ import annotations

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FORM_PORT = 17941
CALLBACK_PORT = 17942
CALLBACK = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


def baseline_policy() -> str:
    source = Path(__file__).resolve().parents[2] / "src/teamagent/connect_web/app.py"
    module = ast.parse(source.read_text())
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_CONTENT_SECURITY_POLICY"
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Call)
            return "; ".join(ast.literal_eval(node.value.args[0]))
    raise RuntimeError("Application baseline CSP was not found")


POLICY = baseline_policy()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def respond(self, status: int, body: str = "", **headers: str) -> None:
        raw = body.encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.server.server_port == CALLBACK_PORT and self.path == "/callback":
            print(json.dumps({"event": "inert_callback_reached"}), flush=True)
            self.respond(
                200,
                "<!doctype html><title>Callback reached</title>"
                "<h1>Inert callback reached</h1><p>No OAuth was performed.</p>",
            )
            return
        if self.path not in {"/self-only", "/allowed-callback"}:
            self.respond(404)
            return
        policy = POLICY
        if self.path == "/allowed-callback":
            policy = policy.replace("form-action 'self'", f"form-action 'self' {CALLBACK}")
        self.respond(
            200,
            "<!doctype html><title>Consent CSP fixture</title>"
            f"<h1>{self.path[1:]}</h1><p>Inert local form. Submit once.</p>"
            '<form method="post"><button>Send inert form</button></form>',
            Content_Security_Policy=policy,
        )

    def do_POST(self) -> None:
        if self.server.server_port != FORM_PORT or self.path not in {
            "/self-only",
            "/allowed-callback",
        }:
            self.respond(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024:
            self.respond(413)
            return
        self.rfile.read(length)
        print(json.dumps({"event": "inert_form_submitted", "variant": self.path[1:]}), flush=True)
        self.respond(303, Location=CALLBACK, Content_Security_Policy=POLICY)


def main() -> None:
    callback = ThreadingHTTPServer(("127.0.0.1", CALLBACK_PORT), Fixture)
    form = ThreadingHTTPServer(("127.0.0.1", FORM_PORT), Fixture)
    thread = threading.Thread(target=callback.serve_forever, daemon=True)
    thread.start()
    print(
        json.dumps(
            {
                "event": "fixture_ready",
                "self_only": f"http://127.0.0.1:{FORM_PORT}/self-only",
                "allowed": f"http://127.0.0.1:{FORM_PORT}/allowed-callback",
            }
        ),
        flush=True,
    )
    try:
        form.serve_forever()
    finally:
        form.server_close()
        callback.shutdown()
        callback.server_close()


if __name__ == "__main__":
    main()
