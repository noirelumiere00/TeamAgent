#!/usr/bin/env python3
"""PR2-A0.3: generation input manifest の freshness 検証（fail-closed）。

buildspec 世代の content-addressed key は、契約 JSON / helper スクリプト / buildspec 本体 /
Terraform の合成式（replace チェーン・heredoc）/ KMS key ARN から決定論で導出される。
adoption mapping はその導出値に束縛された短命 manifest であり、入力が 1 つでも動けば
陳腐化する（2026-08-19 に実測: 契約 JSON の CVE 対応 1 コミットで 3 世代が入れ替わった）。

本スクリプトは manifest（infra/deploy/buildspec_generation_inputs.json）が固定した入力
git blob と現在の worktree を突き合わせ、1 件でも不一致なら STALE MANIFEST として
非ゼロ終了する。freeze の repo 側を機械化するのが目的で、手書きリストの記憶に依存しない。
（publisher の freeze はリポジトリ外の admin 操作なので human gate — 二層構造）

使い方:
    verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json \
        --repo-root .

ゲート: Gate 3（A0.3 merge 判断直前）と adopt-plan 開始前に必ず実行する。
KMS ARN / Terraform 評価 context の live 照合は fresh credential を要するため
runbook 側の手順で行う（本スクリプトは repo 内で完結する部分だけを検証する）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1

REQUIRED_FIELDS = (
    "schema_version",
    "generation_baseline",
    "inputs",
    "kms_keys",
    "terraform_evaluation_context",
    "expected_generation_sha256",
)


class StaleManifestError(Exception):
    """generation inputs が manifest から動いた。adopt へ進んではならない。"""


def load_manifest(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StaleManifestError("manifest is not an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise StaleManifestError(
            f"unsupported manifest schema_version: {raw.get('schema_version')!r}"
        )
    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise StaleManifestError(f"manifest is missing fields: {missing}")
    inputs = raw["inputs"]
    if not isinstance(inputs, dict) or not inputs:
        raise StaleManifestError("manifest.inputs must be a non-empty object")
    return raw


def current_blob(repo_root: Path, relative: str) -> str:
    """現在の worktree のファイル実体を git blob として hash する（HEAD ではなく実体）。

    HEAD 参照ではなく hash-object なのは、commit 前の変更（未コミットの入力改変）も
    STALE として検出するため。symlink は実体を hash しないので明示的に拒否する。
    """
    path = repo_root / relative
    if path.is_symlink():
        raise StaleManifestError(f"{relative}: symlink は入力として認めない")
    if not path.is_file():
        raise StaleManifestError(f"{relative}: 入力ファイルが存在しない")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "hash-object", "--no-filters", "--", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise StaleManifestError(f"{relative}: git hash-object failed: {result.stderr.strip()}")
    return result.stdout.strip()


def verify(manifest: dict, repo_root: Path) -> None:
    drifted: list[str] = []
    for relative, expected in sorted(manifest["inputs"].items()):
        try:
            actual = current_blob(repo_root, relative)
        except StaleManifestError as error:
            drifted.append(str(error))
            continue
        if actual != expected:
            drifted.append(f"{relative}: blob {expected} -> {actual}")
    if drifted:
        raise StaleManifestError(
            "STALE MANIFEST — generation inputs が Generation Baseline から変化した:\n  "
            + "\n  ".join(drifted)
            + "\n  adopt へ進まず、manifest（PR2-A0.3 相当）を再生成すること。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        verify(manifest, args.repo_root)
    except (StaleManifestError, json.JSONDecodeError, OSError) as error:
        print(f"generation input freshness check failed: {error}", file=sys.stderr)
        return 1
    count = len(manifest["inputs"])
    print(
        f"generation inputs fresh: {count} inputs unchanged since "
        f"{manifest['generation_baseline'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
