#!/usr/bin/env python3
"""PR2-A0 Supply-Chain Adopt の plan binding（stale-plan 封じ・fail-closed）。

保存済み plan を後から apply できてしまうと、「plan した世界」と「apply する世界」が
ずれる。具体的には次が通ってしまう。

    adopt-plan @ commit A → コードを commit B へ更新 → 古い plan を adopt-apply

本モジュールは plan 時点の世界を manifest へ固定し、apply 時に全項目を exact match で
再照合する。1 項目でも違えば例外にして apply させない。

固定する項目:
    plan_sha256 / plan_json_sha256 / git_head / git_tree_clean /
    mapping_sha256 / state_lineage / state_serial /
    aws_account / terraform_workspace / terraform_version

承認トークンも plan_sha256 に束縛する（`<TOKEN>:<plan_sha256 の先頭 16 桁>`）。
これにより「A という plan を承認したのに B を apply」ができなくなる。

使い方:
    supply_chain_adopt_binding.py record --repo-root R --tf-dir T --out-dir O \\
        --mapping M --state S --account ACC --workspace WS
    supply_chain_adopt_binding.py verify --repo-root R --tf-dir T --out-dir O \\
        --mapping M --account ACC --workspace WS --approve TOKEN
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

BINDING_FILENAME = "adopt-binding.json"
APPROVE_TOKEN = "I-HAVE-REVIEWED-THE-ADOPT-PLAN"
# 2: aws_principal_arn を追加。principal を束縛しない v1 manifest は受け付けない。
SCHEMA_VERSION = 2

# apply 前に exact match を要求する全項目。
BOUND_FIELDS = (
    "plan_sha256",
    "plan_json_sha256",
    "git_head",
    "git_tree_clean",
    "mapping_sha256",
    "state_lineage",
    "state_serial",
    "aws_account",
    "aws_principal_arn",
    "terraform_workspace",
    "terraform_version",
)

# account root user の ARN。root は全リソースへの実質無制限権限を持ち、
# 一時 credential でもないため activation の実行主体にしない。
_ROOT_PRINCIPAL_RE = re.compile(r"^arn:aws[a-z0-9-]*:iam::\d+:root$")


class BindingError(Exception):
    """plan した世界と apply する世界が一致しない。呼び出し側は必ず fail-closed で扱う。"""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BindingError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _tree_is_clean(repo_root: Path) -> bool:
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--quiet", "HEAD"],
        capture_output=True,
        check=False,
    )
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard")
    return tracked.returncode == 0 and untracked == ""


def _terraform_version(tf_dir: Path) -> str:
    result = subprocess.run(
        ["terraform", f"-chdir={tf_dir}", "version", "-json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BindingError(f"terraform version failed: {result.stderr.strip()}")
    return str(json.loads(result.stdout)["terraform_version"])


def _state_identity(state_path: Path) -> tuple[str, int]:
    """state の lineage と serial を取り出す（同じコードでも state が動いたら弾くため）。"""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    lineage = state.get("lineage")
    serial = state.get("serial")
    if not isinstance(lineage, str) or not isinstance(serial, int):
        raise BindingError("state から lineage / serial を取得できません")
    return lineage, serial


def assert_usable_principal(principal_arn: Any, *, source: str) -> str:
    """activation を実行する principal が「実在する非 root の ARN」であることを要求する。

    account ID の一致だけでは principal の差し替えを検出できないため、caller identity の
    ARN 自体を束縛する。root は AWS 自身が日常運用に使わないよう推奨している主体であり、
    ここでは plan / apply の両方で明示的に拒否する。
    """
    if not isinstance(principal_arn, str) or not principal_arn.strip():
        raise BindingError(
            f"{source}: aws_principal_arn が空です（caller identity を束縛できません）"
        )
    arn = principal_arn.strip()
    if not arn.startswith("arn:"):
        raise BindingError(f"{source}: aws_principal_arn が ARN 形式ではありません: {arn!r}")
    if _ROOT_PRINCIPAL_RE.match(arn):
        raise BindingError(
            f"{source}: root principal では adopt を実行できません（{arn}）。"
            "非 root の一時 credential で実行してください。"
        )
    return arn


def expected_approval(plan_sha256: str) -> str:
    """承認トークンを plan へ束縛した形にする（別 plan の承認を流用できないように）。"""
    return f"{APPROVE_TOKEN}:{plan_sha256[:16]}"


def collect(
    *,
    repo_root: Path,
    tf_dir: Path,
    out_dir: Path,
    mapping: Path,
    state_path: Path,
    account: str,
    principal_arn: str,
    workspace: str,
) -> dict[str, Any]:
    lineage, serial = _state_identity(state_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_sha256": _sha256_file(out_dir / "adopt.tfplan"),
        "plan_json_sha256": _sha256_file(out_dir / "adopt-plan.json"),
        "git_head": _git(repo_root, "rev-parse", "HEAD"),
        "git_tree_clean": _tree_is_clean(repo_root),
        "mapping_sha256": _sha256_file(mapping),
        "state_lineage": lineage,
        "state_serial": serial,
        "aws_account": account,
        "aws_principal_arn": assert_usable_principal(principal_arn, source="collect"),
        "terraform_workspace": workspace,
        "terraform_version": _terraform_version(tf_dir),
    }


def compare_binding(recorded: dict[str, Any], observed: dict[str, Any]) -> None:
    """記録値と現在値を全項目 exact match で照合する。差があれば BindingError。"""
    if recorded.get("schema_version") != SCHEMA_VERSION:
        raise BindingError(
            f"unsupported binding schema_version: {recorded.get('schema_version')!r}"
        )
    # 改竄された manifest が root を名乗る／principal を欠く場合、両者が一致していても通さない。
    assert_usable_principal(recorded.get("aws_principal_arn"), source="recorded")
    assert_usable_principal(observed.get("aws_principal_arn"), source="observed")
    drifted = [
        f"{field}: recorded={recorded.get(field)!r} observed={observed.get(field)!r}"
        for field in BOUND_FIELDS
        if recorded.get(field) != observed.get(field)
    ]
    if drifted:
        raise BindingError(
            "plan した世界と apply する世界が一致しません:\n  " + "\n  ".join(drifted)
        )
    if observed.get("git_tree_clean") is not True:
        raise BindingError("working tree が clean ではありません")


def check_approval(recorded: dict[str, Any], approve: str) -> None:
    """承認トークンがこの plan の SHA256 に束縛されていることを要求する。"""
    expected = expected_approval(str(recorded["plan_sha256"]))
    if approve != expected:
        raise BindingError(
            "承認トークンがこの plan に束縛されていません"
            "（別 plan の承認や無指定では apply できません）"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "verify"):
        item = sub.add_parser(name)
        item.add_argument("--repo-root", required=True, type=Path)
        item.add_argument("--tf-dir", required=True, type=Path)
        item.add_argument("--out-dir", required=True, type=Path)
        item.add_argument("--mapping", required=True, type=Path)
        item.add_argument("--account", required=True)
        item.add_argument("--principal-arn", required=True)
        item.add_argument("--workspace", required=True)
        if name == "record":
            item.add_argument("--state", required=True, type=Path)
        else:
            item.add_argument("--approve", default="")

    args = parser.parse_args(argv)
    binding_path = args.out_dir / BINDING_FILENAME
    try:
        if args.command == "record":
            binding = collect(
                repo_root=args.repo_root,
                tf_dir=args.tf_dir,
                out_dir=args.out_dir,
                mapping=args.mapping,
                state_path=args.state,
                account=args.account,
                principal_arn=args.principal_arn,
                workspace=args.workspace,
            )
            if not binding["git_tree_clean"]:
                raise BindingError("clean tree でない状態では plan を束縛できません")
            binding_path.write_text(
                json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            binding_path.chmod(0o600)
            print(f"adopt plan binding recorded: {binding_path}")
        else:
            recorded = json.loads(binding_path.read_text(encoding="utf-8"))
            check_approval(recorded, args.approve)
            # state は「今の」実体を見る必要があるので plan 時の backup ではなく現在値を引く。
            current_state = args.out_dir / "adopt-state-now.json"
            pulled = subprocess.run(
                ["terraform", f"-chdir={args.tf_dir}", "state", "pull"],
                capture_output=True,
                text=True,
                check=False,
            )
            if pulled.returncode != 0:
                raise BindingError(f"state pull failed: {pulled.stderr.strip()}")
            current_state.write_text(pulled.stdout, encoding="utf-8")
            current_state.chmod(0o600)
            observed = collect(
                repo_root=args.repo_root,
                tf_dir=args.tf_dir,
                out_dir=args.out_dir,
                mapping=args.mapping,
                state_path=current_state,
                account=args.account,
                principal_arn=args.principal_arn,
                workspace=args.workspace,
            )
            compare_binding(recorded, observed)
            print("adopt plan binding verified: plan した世界と apply する世界が一致")
    except (BindingError, json.JSONDecodeError, OSError, KeyError) as error:
        print(f"adopt plan binding check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
