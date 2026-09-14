#!/usr/bin/env python3
"""公開済み buildspec 世代が repo の期待値と一致しているかを検証する（fail-closed）。

なぜ必要か（2026-09-11 mcp 便 r20 段2 の実測）:

契約 JSON（``infra/codebuild/teamagent_core_media_release_contract.json`` など）は
buildspec に base64 で焼き込まれる。したがって契約を 1 バイトでも変えると buildspec
世代の content-addressed key が動く。repo 側の
``infra/deploy/buildspec_generation_inputs.json#expected_generation_sha256`` は
その PR で更新されるが、**S3 publish と UpdateProject は admin CLI の別儀式**なので
取り残されうる。

取り残されると次が起きる:

  1. ランチャーは repo の契約ファイルから ``RELEASE_CONTRACT_SHA256`` を実測して渡す。
  2. CodeBuild は古い世代の buildspec を実行する。そこに焼かれている契約は旧版。
  3. pre_build の
     ``[ "$(sha256sum /tmp/teamagent_core_media_release_contract.json ...)"
     = "$RELEASE_CONTRACT_SHA256" ]``
     が偽になり ``FATAL: embedded release contract hash mismatch`` で段2 が落ちる。

実測: build ``teamagent-dev-mcp-source-publisher:679b024e-1208-474f-80e0-08c8cde4c0a1``
（live 世代 1ed75a2e… / repo 期待値 8e27d780…）。段1 の approval-publisher は契約を
焼いていないので世代が動かず SUCCEEDED し、**段2 だけが落ちて原因が見えにくかった**。

本スクリプトは AWS を呼ばない純粋な照合器である。live の pin は呼び出し側
（``scripts/aws/release_mcp.sh``）が ``aws codebuild batch-get-projects`` で読み、
その JSON をそのまま渡す。こうすることでオフラインで完全にテストできる。

使い方::

    aws codebuild batch-get-projects --names <projects...> \\
      --query 'projects[].{name:name,buildspec:source.buildspec}' --output json \\
      | python3 infra/deploy/assert_published_generation.py \\
          --manifest infra/deploy/buildspec_generation_inputs.json --pins-json -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_EXPECTED_BUCKET = "teamagent-dev-image-release-evidence"

# CodeBuild が返す source.buildspec の形。世代は key の basename（64 hex）に載る。
BUILDSPEC_PIN_RE = re.compile(
    r"^arn:aws:s3:::(?P<bucket>[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])"
    r"/codebuild-buildspecs/(?P<project>[A-Za-z0-9_\-]{1,255})"
    r"/(?P<generation>[0-9a-f]{64})\.yml$"
)

REMEDY = (
    "→ 対処: repo が期待する世代の buildspec を S3 へ publish し、UpdateProject で\n"
    "   各プロジェクトの source.buildspec を新 key へ差し替え、launcher IAM の\n"
    "   契約 sha ピンも合わせて更新すること（docs/runbooks/supply_chain_adopt.md）。\n"
    "   publish 前に StartBuild しても段2 が\n"
    "   'FATAL: embedded release contract hash mismatch' で必ず落ちる。"
)


class GenerationPinError(Exception):
    """公開済み buildspec の世代が repo の期待値と一致しない。発射してはならない。"""


def load_expected(manifest_path: Path) -> dict[str, str]:
    """manifest から expected_generation_sha256 を取り出す（型まで固める）。"""
    raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise GenerationPinError("manifest is not an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GenerationPinError(
            f"unsupported manifest schema_version: {raw.get('schema_version')!r}"
        )
    expected: Any = raw.get("expected_generation_sha256")
    if not isinstance(expected, dict) or not expected:
        raise GenerationPinError("manifest.expected_generation_sha256 must be a non-empty object")
    out: dict[str, str] = {}
    for project, generation in expected.items():
        if not isinstance(project, str) or not project:
            raise GenerationPinError(f"invalid project name in manifest: {project!r}")
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise GenerationPinError(
                f"{project}: expected generation は 64 hex でなければならない: {generation!r}"
            )
        out[project] = generation
    return out


def normalize_pins(raw: Any) -> dict[str, str]:
    """live pin を {project: buildspec} へ正規化する。

    受け付ける形:
      - ``[{"name": ..., "buildspec": ...}, ...]``（aws CLI の --query 出力そのまま）
      - ``{"<project>": "<buildspec>", ...}``（テスト・手書き用）
    """
    pins: dict[str, str] = {}
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                raise GenerationPinError(f"pin entry is not an object: {entry!r}")
            name = entry.get("name")
            buildspec = entry.get("buildspec")
            if not isinstance(name, str) or not name:
                raise GenerationPinError(f"pin entry has no project name: {entry!r}")
            if name in pins:
                raise GenerationPinError(f"{name}: pin が重複している")
            if not isinstance(buildspec, str) or not buildspec:
                # buildspec が null = インライン buildspec。content-addressed pin が
                # 外れている状態で、世代 freeze が成立していない。必ず落とす。
                raise GenerationPinError(
                    f"{name}: source.buildspec が空（インライン buildspec）。"
                    "content-addressed な S3 pin が外れている。"
                )
            pins[name] = buildspec
        return pins
    if isinstance(raw, dict):
        for name, buildspec in raw.items():
            if not isinstance(name, str) or not isinstance(buildspec, str) or not buildspec:
                raise GenerationPinError(f"invalid pin entry: {name!r} -> {buildspec!r}")
            pins[name] = buildspec
        return pins
    raise GenerationPinError("pins JSON must be a list or an object")


def parse_pin(project: str, buildspec: str, expected_bucket: str) -> str:
    """buildspec ARN から世代 sha を取り出す。形が違えば落とす。"""
    match = BUILDSPEC_PIN_RE.match(buildspec)
    if match is None:
        raise GenerationPinError(
            f"{project}: source.buildspec が content-addressed な S3 pin の形をしていない: "
            f"{buildspec!r}"
        )
    if match.group("bucket") != expected_bucket:
        raise GenerationPinError(
            f"{project}: buildspec の bucket が想定外: {match.group('bucket')!r} "
            f"(expected {expected_bucket!r})"
        )
    if match.group("project") != project:
        # 別プロジェクトの buildspec を指している = 取り違え publish。
        raise GenerationPinError(
            f"{project}: buildspec key のプロジェクト区画が一致しない: {match.group('project')!r}"
        )
    return match.group("generation")


def assert_pins(
    expected: Mapping[str, str],
    pins: Mapping[str, str],
    expected_bucket: str = DEFAULT_EXPECTED_BUCKET,
) -> list[str]:
    """全プロジェクトの live 世代が期待値と一致することを要求する。

    戻り値は成功時の 1 行サマリ群。1 件でも不一致・欠落があれば例外。
    """
    problems: list[str] = []
    ok: list[str] = []

    missing = sorted(set(expected) - set(pins))
    if missing:
        # 読めなかったプロジェクトを「問題なし」にはしない（fail-closed）。
        problems.append(f"live pin を取得できなかったプロジェクト: {missing}")

    unexpected = sorted(set(pins) - set(expected))
    if unexpected:
        problems.append(
            f"manifest に expected_generation_sha256 が無いプロジェクトの pin が来た: {unexpected}"
        )

    for project in sorted(set(expected) & set(pins)):
        want = expected[project]
        try:
            live = parse_pin(project, pins[project], expected_bucket)
        except GenerationPinError as error:
            problems.append(str(error))
            continue
        if live != want:
            problems.append(f"{project}: STALE 世代 — live={live[:16]}… / repo 期待値={want[:16]}…")
        else:
            ok.append(f"  {project}: 世代一致 {live[:16]}…")

    if problems:
        raise GenerationPinError(
            "公開済み buildspec の世代が repo の期待値と一致しない:\n  "
            + "\n  ".join(problems)
            + "\n"
            + REMEDY
        )
    return ok


def _read_pins_json(value: str) -> Any:
    text = sys.stdin.read() if value == "-" else value
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公開済み buildspec 世代の照合（fail-closed）")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--pins-json",
        required=True,
        help="aws codebuild batch-get-projects の JSON。'-' で標準入力から読む。",
    )
    parser.add_argument("--expected-bucket", default=DEFAULT_EXPECTED_BUCKET)
    args = parser.parse_args(argv)
    try:
        expected = load_expected(args.manifest)
        pins = normalize_pins(_read_pins_json(args.pins_json))
        ok = assert_pins(expected, pins, args.expected_bucket)
    except (GenerationPinError, json.JSONDecodeError, OSError) as error:
        print(f"published generation check failed: {error}", file=sys.stderr)
        return 1
    print(f"published buildspec generation OK: {len(ok)} projects")
    for line in ok:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
