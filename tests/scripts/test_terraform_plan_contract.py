from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[2] / "infra" / "deploy" / "terraform_plan_contract.py"
_GUARD = Path(__file__).parents[2] / "infra" / "deploy" / "terraform_runtime_guard.sh"
_SPEC = importlib.util.spec_from_file_location("_terraform_plan_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module
_SPEC.loader.exec_module(module)


def _change(
    address: str,
    *,
    actions: list[str] | None = None,
    before: Any = None,
    after: Any = None,
) -> dict[str, Any]:
    resource_type, name = address.split(".", 1)
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {
            "actions": actions or ["update"],
            "before": {"value": "before"} if before is None else before,
            "after": {"value": "after"} if after is None else after,
            "after_unknown": {},
            "before_sensitive": {},
            "after_sensitive": {},
            "replace_paths": [],
        },
    }


def _plan() -> dict[str, Any]:
    return {
        "format_version": "1.2",
        "terraform_version": "1.14.4",
        "applyable": True,
        "complete": True,
        "errored": False,
        "resource_changes": [
            _change("aws_lambda_function.dispatch"),
            _change(
                "aws_s3_bucket.noop",
                actions=["no-op"],
                before={"same": True},
                after={"same": True},
            ),
        ],
        "resource_drift": [],
        "output_changes": {},
        "deferred_changes": [],
        "action_invocations": [],
    }


def _run_verify(
    tmp_path: Path,
    plan: dict[str, Any],
    reviewed: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    plan_path = tmp_path / "plan.json"
    reviewed_path = tmp_path / "reviewed.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "verify",
            "--plan",
            str(plan_path),
            "--reviewed",
            str(reviewed_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_shell_guard(
    tmp_path: Path,
    plan: dict[str, Any],
    reviewed: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    plan_path = tmp_path / "guard-plan.json"
    migration_path = tmp_path / "migration.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    migration_path.write_text(
        json.dumps({"kind": "runtime", "reviewed_plan": reviewed}),
        encoding="utf-8",
    )
    function = re.search(
        r"validate_manifest_change_allowlist\(\) \{.*?"
        r"(?=\nvalidate_runtime_task_contracts\(\))",
        _GUARD.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert function is not None
    shell = "\n".join(
        (
            "set -euo pipefail",
            f"TMP_ROOT={str(tmp_path)!r}",
            f"PLAN_CONTRACT_HELPER={str(_SCRIPT)!r}",
            'die() { echo "FATAL: $*" >&2; return 1; }',
            function.group(0),
            'validate_manifest_change_allowlist "$1" "$2"',
        )
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            shell,
            "validator",
            str(plan_path),
            str(migration_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_shell_extract(
    tmp_path: Path,
    plan: dict[str, Any],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    plan_path = tmp_path / "candidate-plan.json"
    migration_path = tmp_path / "candidate-migration.json"
    output_path = tmp_path / "reviewed-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    migration_path.write_text(
        json.dumps({"kind": "runtime", "reviewed_plan": None}),
        encoding="utf-8",
    )
    function = re.search(
        r"validate_manifest_change_allowlist\(\) \{.*?"
        r"(?=\nvalidate_runtime_task_contracts\(\))",
        _GUARD.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert function is not None
    shell = "\n".join(
        (
            "set -euo pipefail",
            f"TMP_ROOT={str(tmp_path)!r}",
            f"PLAN_CONTRACT_HELPER={str(_SCRIPT)!r}",
            'die() { echo "FATAL: $*" >&2; return 1; }',
            function.group(0),
            'validate_manifest_change_allowlist "$1" "$2" extract "$3"',
        )
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            shell,
            "validator",
            str(plan_path),
            str(migration_path),
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, output_path


def test_exact_reviewed_contract_accepts_only_identical_mutations(
    tmp_path: Path,
) -> None:
    plan = _plan()
    reviewed = module.extract_contract(plan)

    result = _run_verify(tmp_path, plan, reviewed)

    assert result.returncode == 0, result.stderr
    assert [row["address"] for row in reviewed["resource_changes"]] == [
        "aws_lambda_function.dispatch"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "new_address",
        "actions",
        "before",
        "after",
        "unknown",
        "replace_paths",
        "hidden_change_field",
        "drift",
        "output",
    ],
)
def test_exact_reviewed_contract_rejects_any_unreviewed_semantic_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    plan = _plan()
    reviewed = module.extract_contract(plan)
    changed = copy.deepcopy(plan)
    row = changed["resource_changes"][0]
    if mutation == "new_address":
        changed["resource_changes"].append(_change("aws_iam_role.unreviewed"))
    elif mutation == "actions":
        row["change"]["actions"] = ["create", "delete"]
    elif mutation == "before":
        row["change"]["before"] = {"value": "other"}
    elif mutation == "after":
        row["change"]["after"] = {"value": "other"}
    elif mutation == "unknown":
        row["change"]["after_unknown"] = {"arn": True}
    elif mutation == "replace_paths":
        row["change"]["replace_paths"] = [["name"]]
    elif mutation == "hidden_change_field":
        row["change"]["future_terraform_field"] = {"unexpected": True}
    elif mutation == "drift":
        changed["resource_drift"] = [_change("aws_sqs_queue.drift")]
    elif mutation == "output":
        changed["output_changes"] = {"secret": {"actions": ["update"]}}

    result = _run_verify(tmp_path, changed, reviewed)

    assert result.returncode == 1
    assert "differs from the exact reviewed contract" in result.stderr


def test_reviewed_contract_rejects_unknown_schema_even_when_values_match(
    tmp_path: Path,
) -> None:
    plan = _plan()
    reviewed = module.extract_contract(plan)
    reviewed["implicit_addresses_allowed"] = True

    result = _run_verify(tmp_path, plan, reviewed)

    assert result.returncode == 1
    assert "schema is invalid" in result.stderr


def test_plan_contract_rejects_duplicate_resource_identity() -> None:
    plan = _plan()
    plan["resource_changes"].append(copy.deepcopy(plan["resource_changes"][0]))

    with pytest.raises(module.ContractError, match="duplicate address"):
        module.extract_contract(plan)


def test_shell_migration_guard_uses_exact_contract_not_address_allowlist(
    tmp_path: Path,
) -> None:
    plan = _plan()
    reviewed = module.extract_contract(plan)
    accepted = _run_shell_guard(tmp_path, plan, reviewed)
    assert accepted.returncode == 0, accepted.stderr

    plan["resource_changes"].append(_change("aws_iam_role.implicit"))
    rejected = _run_shell_guard(tmp_path, plan, reviewed)
    assert rejected.returncode == 1
    assert "exact reviewed_plan" in rejected.stderr


def test_shell_migration_guard_still_rejects_pure_destroy(
    tmp_path: Path,
) -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
    plan["resource_changes"][0]["change"]["after"] = None
    reviewed = module.extract_contract(plan)

    result = _run_shell_guard(tmp_path, plan, reviewed)

    assert result.returncode == 1
    assert "pure destroy" in result.stderr


def test_candidate_extract_then_final_verify_is_exact_and_reproducible(
    tmp_path: Path,
) -> None:
    plan = _plan()

    extracted, reviewed_path = _run_shell_extract(tmp_path, plan)
    assert extracted.returncode == 0, extracted.stderr
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    assert reviewed == module.extract_contract(plan)

    final = _run_shell_guard(tmp_path, plan, reviewed)
    assert final.returncode == 0, final.stderr


def test_runtime_guard_review_plan_is_side_effect_free_until_final_plan() -> None:
    guard = _GUARD.read_text(encoding="utf-8")
    branch = guard[guard.index("review-plan|plan)") : guard.index("\n  verify)")]

    assert 'migration_to_file "$MIGRATION_ID" "$MIGRATION_JSON" candidate' in branch
    assert 'PLAN_CONTRACT_MODE="extract"' in branch
    assert ".reviewed_inputs.image_deployment_intent_id" in branch
    assert ".created_at_epoch" in branch
    review_exit = branch.index('echo "✅ exact reviewed plan candidate')
    intent_write = branch.index("prepare_image_deployment_intent")
    assert review_exit < intent_write


def _run_prepare_validation(
    tmp_path: Path, plan: dict[str, Any], contract_mode: str
) -> subprocess.CompletedProcess[str]:
    plan_path = tmp_path / f"{contract_mode}-plan.json"
    core_path = tmp_path / "core.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    core_path.write_text(
        json.dumps({"mode": "migration", "aws_region": "ap-northeast-1"}), encoding="utf-8"
    )
    function = re.search(
        r"^prepare_plan_for_validation\(\) \{\n.*?\n\}\n",
        _GUARD.read_text(encoding="utf-8"),
        flags=re.DOTALL | re.MULTILINE,
    )
    assert function is not None
    shell = "\n".join(
        (
            "set -euo pipefail",
            f"TMP_ROOT={str(tmp_path)!r}",
            'die() { echo "FATAL: $*" >&2; return 1; }',
            function.group(0),
            'prepare_plan_for_validation "$1" "$2" "$3"',
            'cat "$VALIDATION_PLAN_JSON"',
        )
    )
    result = subprocess.run(
        ["bash", "-c", shell, "validator", str(plan_path), str(core_path), contract_mode],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan_path.read_text(encoding="utf-8") == json.dumps(plan)
    return result


def _review_plan_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    final = _plan()
    final.pop("resource_drift")
    final["variables"] = {"runtime_guard_review_only": {"value": False}}
    final["checks"] = [
        {
            "address": {
                "kind": "resource",
                "mode": "managed",
                "type": "terraform_data",
                "name": "runtime_guard_review_blocker",
                "to_display": "terraform_data.runtime_guard_review_blocker",
            },
            "status": "pass",
            "instances": [],
        }
    ]
    review = copy.deepcopy(final)
    review["variables"]["runtime_guard_review_only"]["value"] = True
    blocker = _change(
        "terraform_data.runtime_guard_review_blocker[0]",
        actions=["create"],
        after={"input": {"aws_region": "ap-northeast-1", "billing_region": "us-east-1"}},
    )
    blocker.update(
        name="runtime_guard_review_blocker",
        index=0,
        provider_name="terraform.io/builtin/terraform",
    )
    blocker["change"].update(before=None, after_unknown={"id": True, "input": {}, "output": True})
    review["resource_changes"].append(blocker)
    review["checks"][0].update(
        status="unknown",
        instances=[
            {
                "address": {
                    "to_display": "terraform_data.runtime_guard_review_blocker[0]",
                    "instance_key": 0,
                },
                "status": "unknown",
            }
        ],
    )
    return review, final


def test_review_blocker_is_excluded_from_exact_final_contract(tmp_path: Path) -> None:
    review, final = _review_plan_pair()
    # Similar names still bind their complete change hash; only the exact blocker is omitted.
    lookalike = _change("terraform_data.runtime_guard_review_blocker_extra")
    review["resource_changes"].append(lookalike)
    final["resource_changes"].append(copy.deepcopy(lookalike))

    normalized_review = _run_prepare_validation(tmp_path, review, "extract")
    assert normalized_review.returncode == 0, normalized_review.stderr
    extracted, reviewed_path = _run_shell_extract(tmp_path, json.loads(normalized_review.stdout))
    assert extracted.returncode == 0, extracted.stderr
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    assert reviewed == module.extract_contract(final)
    assert "terraform_data.runtime_guard_review_blocker_extra" in {
        row["address"] for row in reviewed["resource_changes"]
    }

    normalized_final = _run_prepare_validation(tmp_path, final, "verify")
    assert normalized_final.returncode == 0, normalized_final.stderr
    verified = _run_shell_guard(tmp_path, json.loads(normalized_final.stdout), reviewed)
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize("mutation", ["before", "unknown", "actions", "drift", "output"])
def test_review_normalization_preserves_nonblocker_contract_mutations(
    tmp_path: Path, mutation: str
) -> None:
    review, final = _review_plan_pair()
    normalized = _run_prepare_validation(tmp_path, review, "extract")
    assert normalized.returncode == 0, normalized.stderr
    reviewed = module.extract_contract(json.loads(normalized.stdout))
    row = final["resource_changes"][0]["change"]
    if mutation == "before":
        row["before"] = {"unexpected": True}
    elif mutation == "unknown":
        row["after_unknown"] = {"unexpected": True}
    elif mutation == "actions":
        row["actions"] = ["create", "delete"]
    elif mutation == "drift":
        final["resource_drift"] = [_change("aws_sqs_queue.drift")]
    elif mutation == "output":
        final["output_changes"] = {"unexpected": {"actions": ["update"]}}

    normalized_final = _run_prepare_validation(tmp_path, final, "verify")
    assert normalized_final.returncode == 0, normalized_final.stderr
    result = _run_verify(tmp_path, json.loads(normalized_final.stdout), reviewed)
    assert result.returncode == 1
    assert "differs from the exact reviewed contract" in result.stderr
