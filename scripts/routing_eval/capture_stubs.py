"""OpenClaw が Bedrock へ送る要求を取り出すための代役サーバ 2 つ（同じポートで待ち受ける）。

- MCP の代役（``/mcp``）: dump_tools.py が書き出したツール定義をそのまま tools/list で返す。
  tools/call は**何も実行しない**。履歴の台本にある呼び出しなら台本の結果文、それ以外は固定文を返す
  （社内データ・外部 API・Slack・Gmail には一切触れない）。
- Bedrock の代役（``/model/<id>/converse-stream``）:
  - 最後の user 発話に評価用の目印（SENTINEL）があれば、要求本文を保存して 500 を返す。
    OpenClaw は推論に失敗するが、欲しいのは「送ろうとした要求」なのでそれでよい。
  - 目印が無ければ「履歴をつくるための前置きのターン」とみなし、台本（--script）どおりの
    応答（テキスト、またはツール呼び出し→結果を受けてテキスト）を ConverseStream の形で返す。
    これで OpenClaw の transcript に本物と同じ形の履歴が残る。

    uv run --extra mcp python scripts/routing_eval/capture_stubs.py \\
        --tools runs/tools_b66e8d6e.json --out-dir runs/capture_b66e8d6e --port 18790 \\
        [--script scripts/routing_eval/history_scenarios.json --scenario prior_search]
"""

from __future__ import annotations

import argparse
import binascii
import contextlib
import json
import struct
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from mcp import types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

SENTINEL = "__ROUTING_EVAL_UTTERANCE__"
STUB_TOOL_RESULT = "（評価モード: ツールは実行していません）"


# ── ConverseStream 応答（AWS event stream 形式）の組み立て ─────────────────────


def _event_frame(event_type: str, payload: dict[str, Any]) -> bytes:
    headers = b""
    for name, value in (
        (":event-type", event_type),
        (":content-type", "application/json"),
        (":message-type", "event"),
    ):
        n = name.encode()
        v = value.encode()
        headers += struct.pack(">B", len(n)) + n + struct.pack(">BH", 7, len(v)) + v
    body = json.dumps(payload, ensure_ascii=False).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    msg = prelude + headers + body
    return msg + struct.pack(">I", binascii.crc32(msg) & 0xFFFFFFFF)


def converse_stream_text(text: str) -> bytes:
    return b"".join(
        [
            _event_frame("messageStart", {"role": "assistant"}),
            _event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}),
            _event_frame("contentBlockStop", {"contentBlockIndex": 0}),
            _event_frame("messageStop", {"stopReason": "end_turn"}),
            _event_frame(
                "metadata",
                {
                    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                    "metrics": {"latencyMs": 1},
                },
            ),
        ]
    )


def converse_stream_tool_use(tool_use_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    return b"".join(
        [
            _event_frame("messageStart", {"role": "assistant"}),
            _event_frame(
                "contentBlockStart",
                {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
                },
            ),
            _event_frame(
                "contentBlockDelta",
                {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": json.dumps(arguments, ensure_ascii=False)}},
                },
            ),
            _event_frame("contentBlockStop", {"contentBlockIndex": 0}),
            _event_frame("messageStop", {"stopReason": "tool_use"}),
            _event_frame(
                "metadata",
                {
                    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                    "metrics": {"latencyMs": 1},
                },
            ),
        ]
    )


# ── 台本 ─────────────────────────────────────────────────────────────────────


def _texts(content: list[dict[str, Any]]) -> list[str]:
    return [c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]


def scripted_reply(body: dict[str, Any], turns: list[dict[str, Any]]) -> bytes:
    """前置きのターンへの応答を台本から選ぶ。

    最後のメッセージが toolResult ならツール呼び出しの後の締めの文、そうでなければ
    最後の user 発話に一致する台本のターンを返す（一致しなければ例外＝台本と取り違えない）。
    """
    messages = body["messages"]
    # 最後の assistant より後ろの user メッセージ（発話本体と、OpenClaw が直後に足す
    # 「runtime context」ブロックの 2 通に分かれて届く）をまとめて見る。
    tail: list[dict[str, Any]] = []
    for m in reversed(messages):
        if m["role"] == "assistant":
            break
        tail.insert(0, m)
    if any("toolResult" in c for m in tail for c in m["content"] if isinstance(c, dict)):
        # ツール結果を受けた直後: 直前のツール呼び出しの名前から台本を引く
        for prev in reversed(messages):
            if prev["role"] == "assistant":
                for c in prev["content"]:
                    if "toolUse" in c:
                        name = c["toolUse"]["name"]
                        for turn in turns:
                            if (
                                turn.get("tool_call")
                                and f"teamagent__{turn['tool_call']['name']}" == name
                            ):
                                return converse_stream_text(turn["assistant"])
        raise ValueError("toolResult に対応する台本のターンが無い")
    joined = "\n".join(t for m in tail for t in _texts(m["content"]))
    for i, turn in enumerate(turns):
        if turn["user"] in joined:
            call = turn.get("tool_call")
            if call:
                return converse_stream_tool_use(
                    f"tooluse_routingeval_{i}", f"teamagent__{call['name']}", call["arguments"]
                )
            return converse_stream_text(turn["assistant"])
    raise ValueError(f"台本に無い発話: {joined[:120]!r}")


# ── サーバ ───────────────────────────────────────────────────────────────────


def build_mcp_server(
    tools: list[dict[str, Any]], turns: list[dict[str, Any]], calls_log: Path
) -> Server:
    server: Server = Server("teamagent")
    canned = {t["tool_call"]["name"]: t["tool_result"] for t in turns if t.get("tool_call")}

    @server.list_tools()  # type: ignore[no-untyped-call, misc]
    async def _list_tools() -> list[mcp_types.Tool]:
        return [mcp_types.Tool.model_validate(t) for t in tools]

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        with calls_log.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"at": time.time(), "name": name, "arguments": arguments}, ensure_ascii=False
                )
                + "\n"
            )
        return [mcp_types.TextContent(type="text", text=canned.get(name, STUB_TOOL_RESULT))]

    return server


def build_app(tools: list[dict[str, Any]], out_dir: Path, turns: list[dict[str, Any]]) -> Starlette:
    out_dir.mkdir(parents=True, exist_ok=True)
    manager = StreamableHTTPSessionManager(
        app=build_mcp_server(tools, turns, out_dir / "mcp_calls.jsonl"),
        json_response=False,
        stateless=True,
    )
    counter = {"n": 0}

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    async def handle_bedrock(request: Request) -> Response:
        counter["n"] += 1
        raw = await request.body()
        body = json.loads(raw) if raw else {}
        is_target = SENTINEL in json.dumps(body.get("messages", [])[-2:], ensure_ascii=False)
        record = {
            "path": request.url.path,
            "target": is_target,
            "headers": {
                k: v
                for k, v in request.headers.items()
                # 署名は使わないので残さない（ダミー資格情報でも記録しない）。
                if k.lower() not in ("authorization", "x-amz-security-token")
            },
            "body": body,
        }
        name = f"bedrock_request_{counter['n']:03d}{'' if is_target else '_prelude'}.json"
        (out_dir / name).write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        if is_target:
            return JSONResponse(
                {"message": "routing-eval capture stub: request recorded"}, status_code=500
            )
        try:
            stream = scripted_reply(body, turns)
        except ValueError as exc:
            return JSONResponse({"message": f"routing-eval capture stub: {exc}"}, status_code=500)
        return Response(stream, media_type="application/vnd.amazon.eventstream")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Mount("/mcp", app=handle_mcp),
            Route("/model/{model_id:path}", handle_bedrock, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def load_turns(script: Path | None, scenario: str | None) -> list[dict[str, Any]]:
    if script is None or scenario is None:
        return []
    scenarios = json.loads(script.read_text(encoding="utf-8"))["scenarios"]
    if scenario not in scenarios:
        raise SystemExit(f"台本に {scenario} が無い（{', '.join(scenarios)}）")
    return list(scenarios[scenario]["turns"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tools", type=Path, required=True, help="dump_tools.py の出力")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--script", type=Path, help="履歴の台本（history_scenarios.json）")
    ap.add_argument("--scenario", help="台本の中の場面名")
    ap.add_argument("--host", default="0.0.0.0")  # Docker から host.docker.internal で届かせる
    ap.add_argument("--port", type=int, default=18790)
    args = ap.parse_args()
    tools = json.loads(args.tools.read_text(encoding="utf-8"))["tools"]
    turns = load_turns(args.script, args.scenario)
    uvicorn.run(
        build_app(tools, args.out_dir, turns), host=args.host, port=args.port, log_level="warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
