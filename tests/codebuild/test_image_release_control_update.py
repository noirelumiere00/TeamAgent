from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "image_release_control_update.py"
LAUNCHER = ROOT / "infra" / "terraform" / "update_image_release_controls.sh"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
COMMIT = "1" * 40


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "image_release_control_update_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTROL = _load_module()


def _project(buildspec: str, *, image_project: bool = False) -> dict[str, Any]:
    variables: list[dict[str, str]] = []
    if image_project:
        markers = CONTROL.contract_markers(ROOT)
        variables = [
            {
                "name": "SOURCE_MANIFEST_CONTRACT_SHA256",
                "type": "PLAINTEXT",
                "value": markers["mcp_runtime"]["sha256"],
            },
            {
                "name": "RELEASE_CONTRACT_SHA256",
                "type": "PLAINTEXT",
                "value": markers["mcp_release"]["sha256"],
            },
        ]
    return {
        "name": "fixture",
        "service_role": "fixture-role",
        "source": [{"buildspec": buildspec, "type": "NO_SOURCE"}],
        "environment": [{"environment_variable": variables}],
    }


def _final_projects() -> dict[str, dict[str, Any]]:
    markers = CONTROL.contract_markers(ROOT)
    project_tokens: dict[str, list[str]] = {
        address: [] for address in CONTROL.PROJECT_ADDRESSES.values()
    }
    for contract, consumers in CONTROL.CONTRACT_CONSUMERS.items():
        for address, marker_kind in consumers.items():
            project_tokens[address].append(markers[contract][marker_kind])
    return {
        address: _project(
            "\n".join(tokens),
            image_project=address == CONTROL.PROJECT_ADDRESSES["image"],
        )
        for address, tokens in project_tokens.items()
    }


def _mcp_release_update_plan() -> dict[str, Any]:
    markers = CONTROL.contract_markers(ROOT)
    final = _final_projects()
    before = copy.deepcopy(final)
    release_marker = markers["mcp_release"]
    changed_consumers = set(CONTROL.CONTRACT_CONSUMERS["mcp_release"])
    for address, marker_kind in CONTROL.CONTRACT_CONSUMERS["mcp_release"].items():
        before[address]["source"][0]["buildspec"] = before[address]["source"][0][
            "buildspec"
        ].replace(release_marker[marker_kind], f"old-{marker_kind}-contract")
    for variable in before[CONTROL.PROJECT_ADDRESSES["image"]]["environment"][0][
        "environment_variable"
    ]:
        if variable["name"] == "RELEASE_CONTRACT_SHA256":
            variable["value"] = "0" * 64

    resources = [
        {
            "address": address,
            "mode": "managed",
            "type": "aws_codebuild_project",
            "name": address.rsplit(".", maxsplit=1)[-1],
            "values": values,
        }
        for address, values in final.items()
    ]
    changes = []
    for address in CONTROL.PROJECT_ADDRESSES.values():
        changed = address in changed_consumers
        changes.append(
            {
                "address": address,
                "mode": "managed",
                "type": "aws_codebuild_project",
                "name": address.rsplit(".", maxsplit=1)[-1],
                "change": {
                    "actions": ["update"] if changed else ["no-op"],
                    "before": before[address],
                    "after": final[address],
                    "after_unknown": {},
                },
            }
        )
    return {
        "complete": True,
        "errored": False,
        "applyable": True,
        "planned_values": {"root_module": {"resources": resources}},
        "resource_changes": changes,
    }


def test_contract_control_plan_binds_every_consumer_and_only_buildspec_hash_fields(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "control.tfplan"
    plan.write_bytes(b"opaque contract-control plan")

    authorization = CONTROL.validate_plan(
        plan,
        repo_root=ROOT,
        control_commit=COMMIT,
        plan_json=_mcp_release_update_plan(),
    )

    assert authorization["changed_contracts"] == ["mcp_release"]
    assert authorization["changed_addresses"] == sorted(CONTROL.CONTRACT_CONSUMERS["mcp_release"])
    assert authorization["control_commit"] == COMMIT
    assert len(authorization["plan_sha256"]) == 64


def test_contract_control_plan_rejects_a_runtime_resource_change(
    tmp_path: Path,
) -> None:
    plan_json = _mcp_release_update_plan()
    plan_json["resource_changes"].append(
        {
            "address": "aws_ecs_task_definition.mcp",
            "change": {
                "actions": ["update"],
                "before": {"family": "old"},
                "after": {"family": "new"},
                "after_unknown": {},
            },
        }
    )
    plan = tmp_path / "control.tfplan"
    plan.write_bytes(b"opaque contract-control plan")

    with pytest.raises(
        CONTROL.ControlUpdateError,
        match="only update embedded-contract CodeBuild projects",
    ):
        CONTROL.validate_plan(
            plan,
            repo_root=ROOT,
            control_commit=COMMIT,
            plan_json=plan_json,
        )


def test_contract_control_plan_rejects_non_contract_project_fields(
    tmp_path: Path,
) -> None:
    plan_json = _mcp_release_update_plan()
    change = next(
        item
        for item in plan_json["resource_changes"]
        if item["address"] == CONTROL.PROJECT_ADDRESSES["attestor"]
    )
    change["change"]["after"]["service_role"] = "expanded-role"
    planned = next(
        item
        for item in plan_json["planned_values"]["root_module"]["resources"]
        if item["address"] == CONTROL.PROJECT_ADDRESSES["attestor"]
    )
    planned["values"]["service_role"] = "expanded-role"
    plan = tmp_path / "control.tfplan"
    plan.write_bytes(b"opaque contract-control plan")

    with pytest.raises(CONTROL.ControlUpdateError, match="unauthorized project field"):
        CONTROL.validate_plan(
            plan,
            repo_root=ROOT,
            control_commit=COMMIT,
            plan_json=plan_json,
        )


def test_contract_control_plan_rejects_a_consumer_missing_the_current_contract(
    tmp_path: Path,
) -> None:
    plan_json = _mcp_release_update_plan()
    marker = CONTROL.contract_markers(ROOT)["mcp_release"]["base64"]
    planned = next(
        item
        for item in plan_json["planned_values"]["root_module"]["resources"]
        if item["address"] == CONTROL.PROJECT_ADDRESSES["attestor"]
    )
    planned["values"]["source"][0]["buildspec"] = planned["values"]["source"][0][
        "buildspec"
    ].replace(marker, "missing-current-contract")
    change = next(
        item
        for item in plan_json["resource_changes"]
        if item["address"] == CONTROL.PROJECT_ADDRESSES["attestor"]
    )
    change["change"]["after"] = copy.deepcopy(planned["values"])
    plan = tmp_path / "control.tfplan"
    plan.write_bytes(b"opaque contract-control plan")

    with pytest.raises(CONTROL.ControlUpdateError, match="does not embed the current"):
        CONTROL.validate_plan(
            plan,
            repo_root=ROOT,
            control_commit=COMMIT,
            plan_json=plan_json,
        )


def test_control_update_stage_has_an_independent_fail_closed_iam_boundary() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")
    terraform = TERRAFORM.read_text(encoding="utf-8")

    assert "teamagent-release-control-update-caller" in launcher
    assert "teamagent-dev-release-control-updater" in launcher
    assert "image_release_control_update.py" in launcher
    assert launcher.count("-target=aws_codebuild_project.") == 5
    assert "aws_ecs_" not in launcher
    assert "terraform_data.production_image_release_gate" not in launcher
    assert "SAVED_PLAN.control-update.json" in launcher

    policy = terraform.split(
        'data "aws_iam_policy_document" "release_control_updater"',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "release_control_updater"',
        maxsplit=1,
    )[0]
    assert '"codebuild:BatchGetProjects", "codebuild:UpdateProject"' in policy
    assert "teamagent/terraform.tfstate" in policy
    assert "teamagent-tflock" in policy
    for denied in (
        '"codebuild:StartBuild"',
        '"ecr:*"',
        '"ecs:*"',
        '"events:*"',
        '"scheduler:*"',
    ):
        assert denied in policy
    assert '"iam:GetRole"' in policy
    assert not any(
        action in policy
        for action in (
            '"iam:AttachRolePolicy"',
            '"iam:CreateRole"',
            '"iam:PutRolePolicy"',
            '"iam:UpdateAssumeRolePolicy"',
        )
    )
