#!/usr/bin/env python3
"""Freeze v2: root による production mutation を継続監視する（fail-closed）。

2026-08-24 ユーザー裁定で Freeze v2 は次のように定義された:

    enumerated non-root deployment principals へ mechanical deny
    + root は explicit break-glass exception
    + freeze 期間中は root credential / session の使用を禁止
    + CloudTrail で root mutation = 0 を継続監視

root は identity-based policy と permissions boundary をバイパスするため、
activation_freeze_policy.tf の explicit Deny では止まらない。SCP 導入と root key の
無効化はこの activation のスコープ外なので、**監視で残存リスクを可視化する**。

fail-closed 規約: CloudTrail API が失敗したら「0 件」ではなく **検査不能=違反扱い**
として非ゼロ終了する（2026-08 に ExpiredToken を空結果と誤読して偽 green を 2 回出した
実害があるため）。

使い方:
    python3 infra/deploy/root_mutation_monitor.py --since 2026-08-24T07:00:00Z
    # 0 件なら exit 0、1 件でもあれば exit 1（明細を stderr へ）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

FREEZE_DECLARATION = Path(__file__).resolve().parent / "activation_freeze.json"
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ROOT_ARN_RE = re.compile(r"^arn:aws[a-z0-9-]*:iam::\d+:root$")


class MonitorError(Exception):
    """監視の前提が満たされない。呼び出し側は必ず fail-closed で扱う。"""


def monitored_events(declaration: Path = FREEZE_DECLARATION) -> list[str]:
    """監視対象 event は freeze 宣言を単一の真実源として読む（手書き二重管理を避ける）。"""
    doc = json.loads(declaration.read_text(encoding="utf-8"))
    scope = doc["generation_publisher_freeze"]["v2"].get("scope_definition")
    if not isinstance(scope, dict):
        raise MonitorError("freeze 宣言に v2.scope_definition がありません")
    events = scope.get("monitored_events")
    if not isinstance(events, list) or not events:
        raise MonitorError("v2.scope_definition.monitored_events が空です")
    return list(events)


def _lookup(event_name: str, since: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "aws",
            "cloudtrail",
            "lookup-events",
            "--lookup-attributes",
            f"AttributeKey=EventName,AttributeValue={event_name}",
            "--start-time",
            since,
            "--max-items",
            "50",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # API 失敗を「0 件」と読まない。検査不能は違反として扱う。
        raise MonitorError(
            f"CloudTrail lookup が失敗しました（検査不能 = 違反扱い）: "
            f"{event_name}: {result.stderr.strip()[:200]}"
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise MonitorError(f"CloudTrail 応答を解釈できません: {event_name}: {error}") from error
    return payload.get("Events", [])


def root_mutations(since: str, events: list[str] | None = None) -> list[dict[str, str]]:
    if not _ISO_UTC_RE.match(since):
        raise MonitorError(f"--since は YYYY-MM-DDTHH:MM:SSZ 形式で指定してください: {since!r}")
    found: list[dict[str, str]] = []
    for event_name in events if events is not None else monitored_events():
        for entry in _lookup(event_name, since):
            raw = entry.get("CloudTrailEvent")
            if not raw:
                raise MonitorError(f"CloudTrailEvent が空です: {event_name}")
            identity = json.loads(raw).get("userIdentity", {})
            arn = identity.get("arn", "")
            if _ROOT_ARN_RE.match(arn):
                found.append(
                    {
                        "event": event_name,
                        "time": str(entry.get("EventTime")),
                        "arn": arn,
                    }
                )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="UTC ISO8601（例 2026-08-24T07:00:00Z）")
    parser.add_argument("--declaration", type=Path, default=FREEZE_DECLARATION)
    args = parser.parse_args(argv)
    try:
        events = monitored_events(args.declaration)
        found = root_mutations(args.since, events)
    except (MonitorError, OSError, KeyError) as error:
        print(f"root mutation monitor failed: {error}", file=sys.stderr)
        return 1
    print(f"監視対象 event: {len(events)} 種 / 起点: {args.since}")
    if found:
        print(
            f"★ root による mutation を {len(found)} 件検出（Freeze v2 の運用規律違反）",
            file=sys.stderr,
        )
        for entry in found:
            print(f"    {entry['time']}  {entry['event']}  {entry['arn']}", file=sys.stderr)
        return 1
    print("root mutation: 0 件 ✓（break-glass は未使用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
