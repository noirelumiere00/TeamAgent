#!/usr/bin/env python3
"""PR2-A0.4: Terraform state の同一アドレス rebind（fail-closed helper）。

本番の ECS task definition は承認済みリリースにより live が正しく、Terraform state の
binding だけが旧 revision を指して取り残されている。live を再デプロイして state へ
合わせるのは順序が逆なので、state の binding だけを live の exact revision へ付け替える。

正式契約:
    AWS managed application resources mutation = 0 / Terraform remote state mutation only

同一アドレスの rebind は removed/import ブロックでは表現できない（removed は config 不在を、
import は config 存在を要求し矛盾する）ため、`state rm → 即 import` を guard 監督下の
唯一の経路として儀式化する。素の state 操作は引き続き禁止。

本 helper は判定ロジックだけを持つ:
  - mapping（infra/deploy/state_rebind_targets.json）の厳密検証
  - precheck/apply 間の binding（mapping/state/principal/commit の exact 束縛と承認トークン）
  - rebind 後の state 属性 vs live DescribeTaskDefinition の機械比較
実行順序・lock・per-address の atomic 進行は guard（terraform_runtime_guard.sh）が持つ。
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

SCHEMA_VERSION = 1
BINDING_FILENAME = "rebind-binding.json"
APPROVE_TOKEN = "I-HAVE-REVIEWED-THE-STATE-REBIND"

ADDRESS_RE = re.compile(r"^aws_ecs_task_definition\.[a-z0-9_]+(\[0\])?$")
TASKDEF_ARN_RE = re.compile(
    r"^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
    r"(?P<family>[A-Za-z0-9_-]+):(?P<revision>[1-9][0-9]*)$"
)
CONSUMER_KINDS = ("ecs-service", "events-rule", "lambda-env")

# apply 前に exact match を要求する全項目。
BOUND_FIELDS = (
    "mapping_sha256",
    "git_head",
    "git_tree_clean",
    "state_lineage",
    "state_serial",
    "state_sha256",
    "aws_account",
    "aws_principal_arn",
    "targets_count",
)

_ROOT_PRINCIPAL_RE = re.compile(r"^arn:aws[a-z0-9-]*:iam::\d+:root$")


class RebindError(Exception):
    """rebind の不変条件が満たされない。呼び出し側は必ず fail-closed で扱う。"""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_canonical_sha256(state_path: Path) -> str:
    """state pull の canonical hash。

    `terraform state pull` は check_results（plan 時の check メタデータ）の並びが
    呼び出しごとに揺れて **バイト非決定**（2026-08-20 に連続 2 pull の sha 不一致で実測）。
    raw バイトを束縛すると apply 時の再照合が必ず誤爆するため、check_results を除外し
    キーを正規順序化した canonical JSON を hash する。resource 実体・serial・lineage の
    改変は引き続き検出される（serial は BOUND_FIELDS でも独立に束縛している）。
    """
    doc = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise RebindError("state is not an object")
    doc.pop("check_results", None)
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_targets(path: Path, *, require_targets: bool = False) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RebindError("mapping is not an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RebindError(f"unsupported mapping schema_version: {raw.get('schema_version')!r}")
    targets = raw.get("targets")
    if not isinstance(targets, list):
        raise RebindError("mapping.targets must be an array")
    if require_targets and not targets:
        raise RebindError(
            "mapping.targets is empty — production mapping は deployment freeze 開始後に "
            "fresh な live 再解決から確定すること（調査時点の ARN を焼かない）"
        )
    seen_addr: set[str] = set()
    seen_arn: set[str] = set()
    for index, entry in enumerate(targets):
        if not isinstance(entry, dict):
            raise RebindError(f"targets[{index}] is not an object")
        missing = [
            field for field in ("address", "family", "target_arn", "consumer") if field not in entry
        ]
        if missing:
            raise RebindError(f"targets[{index}] is missing fields: {missing}")
        address = entry["address"]
        if not ADDRESS_RE.match(address):
            raise RebindError(f"targets[{index}] address is malformed: {address!r}")
        match = TASKDEF_ARN_RE.match(entry["target_arn"])
        if not match:
            raise RebindError(f"targets[{index}] target_arn is malformed")
        if match.group("family") != entry["family"]:
            raise RebindError(
                f"targets[{index}] family {entry['family']!r} != ARN family "
                f"{match.group('family')!r}"
            )
        consumer = entry["consumer"]
        if not isinstance(consumer, dict) or consumer.get("kind") not in CONSUMER_KINDS:
            raise RebindError(f"targets[{index}] consumer.kind must be one of {CONSUMER_KINDS}")
        if not consumer.get("name"):
            raise RebindError(f"targets[{index}] consumer.name is required")
        if address in seen_addr:
            raise RebindError(f"duplicate address: {address}")
        if entry["target_arn"] in seen_arn:
            raise RebindError(f"duplicate target_arn: {entry['target_arn']}")
        seen_addr.add(address)
        seen_arn.add(entry["target_arn"])
    return targets


def _normalized_container_defs(container_defs: Any) -> list[dict[str, Any]]:
    """state / Describe の containerDefinitions を比較可能な正準形へ落とす。

    比較対象は integrity の核心（image / 環境変数 / secrets 参照名）に限定する。
    provider と API の表記差（キー命名・順序・default 補完）で誤検知しないため、
    全属性比較ではなく「実行内容を決める属性」の exact 比較とする。
    """
    if isinstance(container_defs, str):
        container_defs = json.loads(container_defs)
    if not isinstance(container_defs, list):
        raise RebindError("containerDefinitions is not a list")
    normalized = []
    for container in sorted(container_defs, key=lambda c: c.get("name") or ""):
        environment = {e["name"]: e.get("value") for e in (container.get("environment") or [])}
        secrets = sorted(s["name"] for s in (container.get("secrets") or []))
        normalized.append(
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "environment": environment,
                "secrets": secrets,
            }
        )
    return normalized


def compare_state_to_live(
    state_doc: dict[str, Any], address: str, describe_doc: dict[str, Any]
) -> None:
    """rebind 後の state 属性が live DescribeTaskDefinition と一致することを機械証明する。"""
    base_address = address.split("[", 1)[0]
    wants_index = address.endswith("[0]")
    instance = None
    for resource in state_doc.get("resources", []):
        if (
            resource.get("mode") == "managed"
            and f"{resource.get('type')}.{resource.get('name')}" == base_address
        ):
            instances = resource.get("instances") or []
            if len(instances) != 1:
                raise RebindError(f"{address}: expected exactly one instance in state")
            index_key = instances[0].get("index_key")
            if wants_index and index_key != 0:
                raise RebindError(f"{address}: state instance index_key is {index_key!r}")
            if not wants_index and index_key is not None:
                raise RebindError(f"{address}: state instance is indexed ({index_key!r})")
            instance = instances[0].get("attributes") or {}
            break
    if instance is None:
        raise RebindError(f"{address}: not found in state after import")

    live = describe_doc.get("taskDefinition") or {}
    live_arn = live.get("taskDefinitionArn")
    if not live_arn or instance.get("arn") != live_arn:
        raise RebindError(f"{address}: state arn {instance.get('arn')!r} != live arn {live_arn!r}")
    if str(instance.get("revision")) != str(live.get("revision")):
        raise RebindError(
            f"{address}: state revision {instance.get('revision')!r} != "
            f"live revision {live.get('revision')!r}"
        )
    if instance.get("family") != live.get("family"):
        raise RebindError(
            f"{address}: state family {instance.get('family')!r} != "
            f"live family {live.get('family')!r}"
        )
    state_defs = _normalized_container_defs(instance.get("container_definitions"))
    live_defs = _normalized_container_defs(live.get("containerDefinitions"))
    if state_defs != live_defs:
        raise RebindError(
            f"{address}: normalized container definitions differ between state and live "
            "(image / environment / secrets)"
        )


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RebindError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _tree_is_clean(repo_root: Path) -> bool:
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--quiet", "HEAD"],
        capture_output=True,
        check=False,
    )
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard")
    return tracked.returncode == 0 and untracked == ""


def assert_usable_principal(principal_arn: Any, *, source: str) -> str:
    if not isinstance(principal_arn, str) or not principal_arn.strip():
        raise RebindError(f"{source}: aws_principal_arn が空です")
    arn = principal_arn.strip()
    if not arn.startswith("arn:"):
        raise RebindError(f"{source}: aws_principal_arn が ARN 形式ではありません: {arn!r}")
    if _ROOT_PRINCIPAL_RE.match(arn):
        raise RebindError(f"{source}: root principal では rebind を実行できません（{arn}）")
    return arn


def _state_identity(state_path: Path) -> tuple[str, int]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    lineage = state.get("lineage")
    serial = state.get("serial")
    if not isinstance(lineage, str) or not isinstance(serial, int):
        raise RebindError("state から lineage / serial を取得できません")
    return lineage, serial


def expected_approval(binding_sha256: str) -> str:
    """承認トークンを precheck の binding へ束縛する（別 precheck の承認を流用不可）。"""
    return f"{APPROVE_TOKEN}:{binding_sha256[:16]}"


def collect(
    *,
    repo_root: Path,
    mapping: Path,
    state_path: Path,
    account: str,
    principal_arn: str,
) -> dict[str, Any]:
    targets = load_targets(mapping, require_targets=True)
    lineage, serial = _state_identity(state_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "mapping_sha256": _sha256_file(mapping),
        "git_head": _git(repo_root, "rev-parse", "HEAD"),
        "git_tree_clean": _tree_is_clean(repo_root),
        "state_lineage": lineage,
        "state_serial": serial,
        "state_sha256": _state_canonical_sha256(state_path),
        "aws_account": account,
        "aws_principal_arn": assert_usable_principal(principal_arn, source="collect"),
        "targets_count": len(targets),
    }


def compare_binding(recorded: dict[str, Any], observed: dict[str, Any]) -> None:
    if recorded.get("schema_version") != SCHEMA_VERSION:
        raise RebindError(f"unsupported binding schema_version: {recorded.get('schema_version')!r}")
    assert_usable_principal(recorded.get("aws_principal_arn"), source="recorded")
    assert_usable_principal(observed.get("aws_principal_arn"), source="observed")
    drifted = [
        f"{field}: recorded={recorded.get(field)!r} observed={observed.get(field)!r}"
        for field in BOUND_FIELDS
        if recorded.get(field) != observed.get(field)
    ]
    if drifted:
        raise RebindError(
            "precheck した世界と apply する世界が一致しません:\n  " + "\n  ".join(drifted)
        )
    if observed.get("git_tree_clean") is not True:
        raise RebindError("working tree が clean ではありません")


def check_approval(binding_path: Path, approve: str) -> None:
    expected = expected_approval(_sha256_file(binding_path))
    if approve != expected:
        raise RebindError(
            "承認トークンがこの precheck binding に束縛されていません"
            "（別 precheck の承認や無指定では apply できません）"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--mapping", required=True, type=Path)
    validate.add_argument("--require-targets", action="store_true")

    for name in ("record", "verify"):
        item = sub.add_parser(name)
        item.add_argument("--repo-root", required=True, type=Path)
        item.add_argument("--out-dir", required=True, type=Path)
        item.add_argument("--mapping", required=True, type=Path)
        item.add_argument("--state", required=True, type=Path)
        item.add_argument("--account", required=True)
        item.add_argument("--principal-arn", required=True)
        if name == "verify":
            item.add_argument("--approve", default="")

    compare = sub.add_parser("compare")
    compare.add_argument("--state-json", required=True, type=Path)
    compare.add_argument("--address", required=True)
    compare.add_argument("--describe-json", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            targets = load_targets(args.mapping, require_targets=args.require_targets)
            print(f"rebind mapping validated: {len(targets)} target(s)")
        elif args.command == "compare":
            compare_state_to_live(
                json.loads(args.state_json.read_text(encoding="utf-8")),
                args.address,
                json.loads(args.describe_json.read_text(encoding="utf-8")),
            )
            print(f"state == live verified: {args.address}")
        else:
            binding_path = args.out_dir / BINDING_FILENAME
            binding = collect(
                repo_root=args.repo_root,
                mapping=args.mapping,
                state_path=args.state,
                account=args.account,
                principal_arn=args.principal_arn,
            )
            if args.command == "record":
                if not binding["git_tree_clean"]:
                    raise RebindError("clean tree でない状態では precheck を束縛できません")
                binding_path.write_text(
                    json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                binding_path.chmod(0o600)
                print(f"rebind binding recorded: {binding_path}")
                print(f"approval token: {expected_approval(_sha256_file(binding_path))}")
            else:
                check_approval(binding_path, args.approve)
                recorded = json.loads(binding_path.read_text(encoding="utf-8"))
                compare_binding(recorded, binding)
                print("rebind binding verified: precheck した世界と apply する世界が一致")
    except (RebindError, json.JSONDecodeError, OSError, KeyError) as error:
        print(f"state rebind check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
