#!/usr/bin/env python3
"""CodeBuild の失敗ログから「実際の出力」だけを抜き出す。

なぜ必要か（2026-09-11 mcp 便 r20 段2 の実測）:

CodeBuild は 1 つの ``commands`` エントリ（``|`` ブロック）を実行するとき、
その全文を 1 行 1 イベントでエコーする。失敗するとさらに
``Command did not exit successfully`` の後ろに**同じ全文をもう一度**エコーする。
本 buildspec の pre_build は 1 ブロック 200 行超あるため、363 イベント中
**本物の出力は 1 行だけ**（index 238 の ``FATAL: embedded release contract hash mismatch``）
で、残り 361 イベントはすべてスクリプト本文のエコーだった。

旧実装は ``get-log-events --limit 40``（＝末尾 40 件）を取って
``grep -iE "FATAL|error|fail"`` していた。末尾 40 件は 2 度目のエコーの中なので、
拾えるのは本文中の ``|| { echo "FATAL: ..."; }`` という**ソース断片**だけになる。
実測でこの 3 行が出た::

    || { echo "FATAL: approval verifier returned invalid evidence"; exit 1; }
    [ "${#PRODUCTION_APP_RECORD[@]}" -eq 4 ] || { echo "FATAL: latest production app record is incomplete"; exit 1; }
    || { echo "FATAL: baked fallback key differs from the fixed release location"; exit 1; }

本物の失敗（契約 sha の不一致）とは無関係な 3 行であり、これを見た運用者は
pre_build 末尾を疑って時間を溶かす。「stderr が 1 行も出ていない」という誤った
所見もここから生まれた。

抜き出し方:

エコーは 1 回の書き込みなので **全行が同一 timestamp** を持つ（実測: events
30..237 の 208 件がすべて 1789095923017、実出力の 238/239 だけ 1789095937653）。
``Running command`` イベントの timestamp と異なるものだけを残せばエコーは消える。
timestamp が使えない入力（--output text 由来など）向けに、2 つのエコーを
lockstep 比較する副系統も持つ。どちらも成立しない場合は ``[Container]`` 行を
除いた末尾を出す best-effort へ落ちる。診断側の失敗で本体の失敗を隠さない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

CONTAINER_PREFIX = "[Container]"
RUNNING_MARKER = "Running command "
FAILED_MARKER = "Command did not exit successfully "


class Event(NamedTuple):
    """ログイベント 1 件。timestamp は取れなければ None。"""

    timestamp: int | None
    message: str


def load_events(raw: Any) -> list[Event]:
    """``aws logs get-log-events`` の JSON からイベント列を取り出す。"""
    events: Any = raw.get("events", []) if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    out: list[Event] = []
    for event in events:
        if isinstance(event, dict):
            message = event.get("message")
            timestamp = event.get("timestamp")
            out.append(
                Event(
                    timestamp if isinstance(timestamp, int) else None,
                    message.rstrip("\n") if isinstance(message, str) else "",
                )
            )
        elif isinstance(event, str):
            out.append(Event(None, event.rstrip("\n")))
        else:
            out.append(Event(None, ""))
    return out


def _find_last(events: Sequence[Event], marker: str, stop: int | None = None) -> int:
    end = len(events) if stop is None else stop
    for index in range(end - 1, -1, -1):
        message = events[index].message
        if message.startswith(CONTAINER_PREFIX) and marker in message:
            return index
    return -1


def _echo_tail_length(events: Sequence[Event], run_at: int, fail_at: int) -> int:
    """2 つのエコーを lockstep 比較し、一致が続く長さを返す。

    2 度目のエコーはログ側で打ち切られることがある（実測: 208 行中 122 行）。
    その場合この値は過小になるので、timestamp 系統の副系統として使う。
    """
    length = 0
    while True:
        left = run_at + 1 + length
        right = fail_at + 1 + length
        if left >= fail_at or right >= len(events):
            break
        if events[left].message != events[right].message:
            break
        length += 1
    return length


def extract(events: Sequence[Event], limit: int = 20) -> list[str]:
    """実出力だけを返す（最大 limit 行、末尾側を優先）。"""
    fail_at = _find_last(events, FAILED_MARKER)
    run_at = _find_last(events, RUNNING_MARKER, stop=fail_at if fail_at >= 0 else None)

    body: list[Event]
    if fail_at >= 0 and run_at >= 0:
        region = events[run_at + 1 : fail_at]
        echo_timestamp = events[run_at].timestamp
        if echo_timestamp is not None:
            body = [e for e in region if e.timestamp != echo_timestamp]
        else:
            body = []
        if not body:
            # timestamp が無い / エコーと実出力が同一バッチだった場合の副系統。
            body = list(region[_echo_tail_length(events, run_at, fail_at) :])
    else:
        # マーカーが無い（タイムアウト等）。エコーと区別できないので
        # コンテナ行だけ落として末尾を出す。
        body = [e for e in events if not e.message.startswith(CONTAINER_PREFIX)]

    return [e.message for e in body if e.message.strip()][-limit:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodeBuild 失敗ログから実出力を抜き出す")
    parser.add_argument(
        "--events-json",
        default="-",
        help="aws logs get-log-events の JSON。'-' で標準入力（既定）。",
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        text = (
            sys.stdin.read()
            if args.events_json == "-"
            else Path(args.events_json).read_text(encoding="utf-8")
        )
        lines = extract(load_events(json.loads(text)), args.limit)
    except (ValueError, json.JSONDecodeError, OSError) as error:
        # 診断の失敗で本体の失敗を隠さない。
        print(f"(ログ抜粋に失敗: {error})", file=sys.stderr)
        return 0
    if not lines:
        print("(実出力なし — buildspec のエコー以外にログが無い)", file=sys.stderr)
        return 0
    for line in lines:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
