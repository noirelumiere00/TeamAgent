from __future__ import annotations

import base64
import concurrent.futures
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "bootstrap" / "provenance_iam_bootstrap.py"
WRAPPER_PROVENANCE_PATH = ROOT / "infra" / "bootstrap" / "wrapper_provenance.py"
CONTRACT_PATH = ROOT / "infra" / "bootstrap" / "bootstrap_contract.json"
SEED_PATH = ROOT / "infra" / "bootstrap" / "seed-stack.yaml"
CODEBUILD_TF = ROOT / "infra" / "terraform" / "codebuild.tf"
ECR_TF = ROOT / "infra" / "terraform" / "ecr.tf"
RUNTIME_EVIDENCE_TF = ROOT / "infra" / "terraform" / "runtime_evidence.tf"
MEDIA_CUTOVER_ATTESTOR_TF = ROOT / "infra" / "terraform" / "media_cutover_attestor.tf"
BUILD_TEAMAGENT = ROOT / "infra" / "deploy" / "build_teamagent_image.sh"
BUILD_OPENCLAW = ROOT / "infra" / "deploy" / "build_openclaw_image.sh"
BUILD_TIKTOK = ROOT / "infra" / "deploy" / "build_tiktok_image.sh"
AUTHORIZE = ROOT / "infra" / "deploy" / "authorize_image_release.sh"
BOOTSTRAP_ENTRY = ROOT / "infra" / "deploy" / "bootstrap_provenance_iam.sh"
RUNTIME_ENTRY = ROOT / "infra" / "deploy" / "bootstrap_runtime_session.sh"
PROVENANCE_ENTRY = ROOT / "infra" / "deploy" / "bootstrap_provenance_session.sh"

SPEC = importlib.util.spec_from_file_location("provenance_iam_bootstrap", MODULE_PATH)
assert SPEC and SPEC.loader
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BOOTSTRAP
SPEC.loader.exec_module(BOOTSTRAP)

WRAPPER_SPEC = importlib.util.spec_from_file_location(
    "wrapper_provenance",
    WRAPPER_PROVENANCE_PATH,
)
assert WRAPPER_SPEC and WRAPPER_SPEC.loader
WRAPPER_PROVENANCE = importlib.util.module_from_spec(WRAPPER_SPEC)
sys.modules[WRAPPER_SPEC.name] = WRAPPER_PROVENANCE
WRAPPER_SPEC.loader.exec_module(WRAPPER_PROVENANCE)

PLAN_SHA = "a" * 64
LINEAGE = str(uuid.UUID("11111111-2222-4333-8444-555555555555"))
PREEXISTING_ALLOWED_OUTPUT_NAMES = frozenset(
    {
        "alarm_recipient_ack_key_arn",
        "alarm_recipient_ack_signer_role_arn",
        "runtime_automation_role_arn",
        "alarm_recipient_ack_signer_assume_policy_contract",
        "alarm_recipient_ack_signer_policy_contract",
        "media_cutover_attestor_key_arn",
        "media_cutover_attestor_role_arn",
        "media_cutover_attestor_assume_policy_contract",
        "media_cutover_attestor_policy_contract",
        "runtime_evidence_automation_policy_contract",
        "runtime_automation_boundary_arn",
        "codebuild_launcher_role_arn",
        "release_caller_arn",
        "release_launcher_role_arn",
        "release_control_update_caller_arn",
        "release_control_updater_role_arn",
        "tiktok_codebuild_project",
        "tiktok_codebuild_connection_arn",
        "tiktok_build_caller_arn",
        "tiktok_build_launcher_role_arn",
        "image_deployment_gate_role_arn",
        "image_deployment_intent_table",
        "mcp_source_publisher_project",
        "image_attestor_project",
        "image_promoter_project",
        "openclaw_codebuild_project",
        "openclaw_codebuild_connection_arn",
        "openclaw_publisher_role_arn",
        "openclaw_evidence_bucket",
    }
)
TARGET_DERIVED_OUTPUT_NAMES = frozenset(
    {
        "ecr_openclaw_url",
        "ecr_openclaw_quarantine_url",
        "ecr_openclaw_verified_candidates_url",
        "ecr_openclaw_media_url",
        "ecr_openclaw_media_quarantine_url",
        "ecr_openclaw_media_verified_candidates_url",
        "ecr_mcp_url",
        "ecr_mcp_quarantine_url",
        "ecr_mcp_verified_candidates_url",
        "ecr_mcp_media_url",
        "ecr_mcp_media_quarantine_url",
        "ecr_mcp_media_verified_candidates_url",
        "ecr_tiktok_acquire_quarantine_url",
        "ecr_tiktok_acquire_verified_candidates_url",
        "tiktok_acquire_ecr_url",
        "media_worker_ecr_url",
        "s3_raw_bucket",
        "openclaw_rollout_signing_key_arn",
    }
)


def _contract() -> Any:
    return BOOTSTRAP.load_contract(CONTRACT_PATH)


def _split_address(address: str) -> tuple[str, str, int | str | None]:
    base = BOOTSTRAP.normalize_address(address)
    resource_type, name = base.split(".", 1)
    index: int | str | None = None
    suffix = address[len(base) :]
    if suffix:
        index = json.loads(suffix[1:-1])
    return resource_type, name, index


def _state(
    addresses: list[str],
    *,
    serial: int = 10,
    lineage: str = LINEAGE,
    seed_leak: bool = False,
) -> dict[str, Any]:
    resources = []
    for address in addresses:
        resource_type, name, index = _split_address(address)
        attributes: dict[str, Any] = {"id": f"id-{resource_type}-{name}"}
        if seed_leak and not resources:
            attributes["name"] = "teamagent-production-provenance-bootstrap-v1"
        instance: dict[str, Any] = {
            "schema_version": 0,
            "attributes": attributes,
            "sensitive_attributes": [],
        }
        if index is not None:
            instance["index_key"] = index
        resources.append(
            {
                "mode": "managed",
                "type": resource_type,
                "name": name,
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [instance],
            }
        )
    return {
        "version": 4,
        "terraform_version": "1.12.2",
        "serial": serial,
        "lineage": lineage,
        "outputs": {},
        "resources": resources,
    }


def _change(address: str, actions: list[str], *, importing: bool = False) -> dict[str, Any]:
    resource_type, name, _ = _split_address(address)
    change: dict[str, Any] = {
        "actions": actions,
        "before": None if actions == ["create"] else {"id": "existing"},
        "after": {"id": "planned"},
        "after_unknown": {},
        "before_sensitive": False,
        "after_sensitive": {},
    }
    if importing:
        change["importing"] = {"id": "external"}
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": change,
    }


def _output_change(actions: list[str]) -> dict[str, Any]:
    if actions == ["create"]:
        before, after = None, "created"
    elif actions == ["update"]:
        before, after = "before", "after"
    else:
        before = after = "unchanged"
    return {
        "actions": actions,
        "before": before,
        "after": after,
        "after_unknown": False,
        "before_sensitive": False,
        "after_sensitive": False,
    }


def _state_output(value: Any) -> dict[str, Any]:
    return {"value": value, "type": "string"}


# NOTE: This is a synthetic plan derived from the contract; the real Terraform 1.12.2
# golden fixture awaits the AWS-mutation-free saved-plan rehearsal (design §4.5).
def _valid_plan(
    *,
    created: list[str] | None = None,
    no_op: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    contract = _contract()
    created = created or sorted(contract.required_main_state)
    no_op = no_op or []
    before = _state(no_op)
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.12.2",
        "errored": False,
        "complete": False,
        "applyable": True,
        "resource_drift": [],
        "deferred_changes": [],
        "output_changes": {},
        "resource_changes": [
            *[_change(address, ["create"]) for address in created],
            *[_change(address, ["no-op"]) for address in no_op],
        ],
        "planned_values": {"root_module": {"resources": []}},
        "configuration": {"root_module": {}},
    }
    return plan, before, created


def _write_release_contracts(root: Path, *, ready_index: int | None = None) -> None:
    contract = _contract()
    for index, relative in enumerate(contract.release_contracts):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        ready = index == ready_index
        path.write_text(
            json.dumps(
                {
                    "release": {
                        "ready": ready,
                        "blocked_reason": "" if ready else "reviewed NO-GO",
                    }
                }
            ),
            encoding="utf-8",
        )


def test_contract_is_strict_and_current_release_contracts_are_blocked() -> None:
    contract = _contract()
    hashes = BOOTSTRAP.validate_release_contracts(ROOT, contract)
    assert set(hashes) == set(contract.release_contracts)
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())
    assert (
        contract.bootstrap_principal_arn
        == "arn:aws:iam::718959508629:user/AIIAdev"
    )
    assert re.fullmatch(
        r"arn:aws:iam::718959508629:user/[\w+=,.@-]+",
        contract.bootstrap_principal_arn,
    )
    assert contract.backend["key"] == "teamagent/terraform.tfstate"
    assert contract.seed["max_session_seconds"] == 3600
    assert (
        contract.seed["inline_policy_name"] == "teamagent-production-provenance-bootstrap-boundary"
    )
    assert contract.required_main_state <= contract.create_allowed


def test_contract_requires_bootstrap_principal_arn(tmp_path: Path) -> None:
    raw = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    del raw["bootstrap_principal_arn"]
    path = tmp_path / "bootstrap_contract.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BOOTSTRAP.BootstrapError, match="bootstrap_principal_arn"):
        BOOTSTRAP.load_contract(path)


@pytest.mark.parametrize(
    "bootstrap_principal_arn",
    [
        None,
        "",
        "arn:aws:iam::718959508629:root",
        "arn:aws:iam::718959508629:role/AIIAdev",
        "arn:aws:iam::111122223333:user/AIIAdev",
        "arn:aws:iam::718959508629:user/",
        "arn:aws:iam::718959508629:user/*",
        "arn:aws:iam::718959508629:user/AIIA dev",
        "arn:aws:iam::718959508629:user/div/Bob",
        "arn:aws:iam::718959508629:user/AIIAdev#garbage",
    ],
)
def test_contract_rejects_invalid_bootstrap_principal_arn(
    tmp_path: Path,
    bootstrap_principal_arn: Any,
) -> None:
    raw = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["bootstrap_principal_arn"] = bootstrap_principal_arn
    path = tmp_path / "bootstrap_contract.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BOOTSTRAP.BootstrapError, match="bootstrap_principal_arn"):
        BOOTSTRAP.load_contract(path)


def test_run_requires_contract_bootstrap_principal_as_initial_identity() -> None:
    source = inspect.getsource(BOOTSTRAP.run_bootstrap)
    initial_identity_check = source.split(
        "principal_identity = _assert_identity(",
        maxsplit=1,
    )[1].split("external_id =", maxsplit=1)[0]
    success_receipt = source.split(
        '"kind": "teamagent-provenance-iam-bootstrap-receipt"',
        maxsplit=1,
    )[1].split('"seed": {', maxsplit=1)[0]

    assert "principal_arn = contract.bootstrap_principal_arn" in source
    assert "expected_arn=principal_arn" in initial_identity_check
    assert 'label="initial bootstrap principal caller"' in initial_identity_check
    assert "initial root caller" not in source
    assert '"principal": {' in success_receipt
    assert '"root": {' not in success_receipt


def test_release_ready_true_blocks_before_any_aws_or_command_runner(tmp_path: Path) -> None:
    _write_release_contracts(tmp_path, ready_index=1)
    contract = _contract()
    with pytest.raises(BOOTSTRAP.BootstrapError, match=r"release\.ready=false"):
        BOOTSTRAP.validate_release_contracts(tmp_path, contract)


def test_missing_or_empty_blocked_reason_is_rejected(tmp_path: Path) -> None:
    _write_release_contracts(tmp_path)
    contract = _contract()
    path = tmp_path / contract.release_contracts[0]
    value = json.loads(path.read_text(encoding="utf-8"))
    value["release"]["blocked_reason"] = "   "
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="needs a reason"):
        BOOTSTRAP.validate_release_contracts(tmp_path, contract)


def test_create_only_plan_and_direct_main_state_handoff_are_accepted() -> None:
    contract = _contract()
    plan, before, created = _valid_plan()
    checked = BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)
    assert set(checked.created_addresses) == set(created)
    after = _state(created, serial=11)
    handoff = BOOTSTRAP.validate_handoff(before, after, checked, contract)
    assert handoff.before_serial == 10
    assert handoff.after_serial == 11
    assert handoff.lineage == LINEAGE
    assert handoff.before_addresses_sha256 != handoff.after_addresses_sha256


@pytest.mark.parametrize(
    "actions",
    [
        ["update"],
        ["delete"],
        ["delete", "create"],
        ["create", "delete"],
    ],
)
def test_plan_rejects_update_delete_and_replacement(actions: list[str]) -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(BOOTSTRAP.BootstrapError, match="create/no-op only"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_openclaw_rollout_signing_key_create_and_no_op_are_accepted() -> None:
    contract = _contract()
    address = "aws_kms_key.openclaw_rollout_signing"

    create_plan, create_before, _ = _valid_plan()
    create_validation = BOOTSTRAP.validate_plan(
        create_plan,
        create_before,
        contract,
        plan_sha256=PLAN_SHA,
    )
    assert address in create_validation.created_addresses

    no_op_plan, no_op_before, _ = _valid_plan(
        created=sorted(contract.required_main_state - {address}),
        no_op=[address],
    )
    no_op_validation = BOOTSTRAP.validate_plan(
        no_op_plan,
        no_op_before,
        contract,
        plan_sha256=PLAN_SHA,
    )
    assert no_op_validation.no_op_addresses == (address,)


@pytest.mark.parametrize("actions", [["update"], ["delete"]])
def test_openclaw_rollout_signing_key_update_and_delete_are_rejected(
    actions: list[str],
) -> None:
    contract = _contract()
    address = "aws_kms_key.openclaw_rollout_signing"
    plan, before, _ = _valid_plan()
    key_change = next(change for change in plan["resource_changes"] if change["address"] == address)
    key_change["change"]["actions"] = actions
    with pytest.raises(BOOTSTRAP.BootstrapError, match="create/no-op only"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_import_move_and_drift() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["resource_changes"][0]["change"]["importing"] = {"id": "existing"}
    with pytest.raises(BOOTSTRAP.BootstrapError, match="import is forbidden"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    plan, before, _ = _valid_plan()
    plan["resource_changes"][0]["previous_address"] = "aws_iam_role.old"
    with pytest.raises(BOOTSTRAP.BootstrapError, match="moved resource"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    plan, before, _ = _valid_plan()
    plan["resource_drift"] = [_change("aws_iam_role.codebuild_launcher", ["update"])]
    with pytest.raises(BOOTSTRAP.BootstrapError, match="resource drift"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_failed_checks_duplicate_addresses_and_other_providers() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["checks"] = [{"address": {"kind": "check", "name": "gate"}, "status": "fail"}]
    with pytest.raises(BOOTSTRAP.BootstrapError, match="failed check"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    plan, before, _ = _valid_plan()
    plan["resource_changes"].append(dict(plan["resource_changes"][0]))
    with pytest.raises(BOOTSTRAP.BootstrapError, match="duplicate Terraform plan address"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    plan, before, _ = _valid_plan()
    plan["resource_changes"][0]["provider_name"] = "example.invalid/unreviewed/aws"
    with pytest.raises(BOOTSTRAP.BootstrapError, match="provider is outside"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_builtin_terraform_data_provider_is_rejected_at_provider_boundary() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    runtime_guard = _change("terraform_data.runtime_guard", ["no-op"])
    runtime_guard["provider_name"] = "terraform.io/builtin/terraform"
    plan["resource_changes"].append(runtime_guard)
    with pytest.raises(BOOTSTRAP.BootstrapError, match="provider is outside"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_terraform_data_type_is_rejected_at_forbidden_type_boundary() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["resource_changes"].append(_change("terraform_data.runtime_guard", ["create"]))
    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="runtime/guard resource reached bootstrap plan",
    ):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


@pytest.mark.parametrize("complete", [True, None], ids=["complete", "missing"])
def test_plan_requires_complete_false_for_fixed_targets(complete: bool | None) -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    if complete is None:
        plan.pop("complete")
    else:
        plan["complete"] = complete
    with pytest.raises(BOOTSTRAP.BootstrapError, match="fixed-target"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_deferred_changes() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["deferred_changes"] = [{"reason": "deferred prerequisite"}]
    with pytest.raises(BOOTSTRAP.BootstrapError, match="deferred changes"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_target_derived_output_contract_is_exact() -> None:
    contract = _contract()
    assert len(TARGET_DERIVED_OUTPUT_NAMES) == 18
    assert contract.allowed_outputs == (
        PREEXISTING_ALLOWED_OUTPUT_NAMES | TARGET_DERIVED_OUTPUT_NAMES
    )


@pytest.mark.parametrize(
    "actions",
    [["create"], ["no-op"], ["update"]],
    ids=["create", "no-op", "update"],
)
def test_target_derived_output_changes_and_handoff_are_accepted(
    actions: list[str],
) -> None:
    contract = _contract()
    plan, before, created = _valid_plan()
    plan["output_changes"] = {name: _output_change(actions) for name in TARGET_DERIVED_OUTPUT_NAMES}
    if actions != ["create"]:
        before["outputs"] = {
            name: _state_output(change["before"]) for name, change in plan["output_changes"].items()
        }

    checked = BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)
    after = _state(created, serial=11)
    after["outputs"] = {
        name: _state_output(change["after"]) for name, change in plan["output_changes"].items()
    }
    BOOTSTRAP.validate_handoff(before, after, checked, contract)


@pytest.mark.parametrize("actions", [["create"], ["update"]], ids=["create", "update"])
def test_plan_rejects_unreviewed_output_changes(actions: list[str]) -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["output_changes"] = {"database_password": _output_change(actions)}
    with pytest.raises(BOOTSTRAP.BootstrapError, match="output change"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_missing_output_changes() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan.pop("output_changes")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="output_changes"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


@pytest.mark.parametrize("change", ["create", "update"])
def test_handoff_rejects_unreviewed_output_changes(change: str) -> None:
    contract = _contract()
    plan, before, created = _valid_plan()
    checked = BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)
    if change == "update":
        before["outputs"]["database_password"] = _state_output("before")
    after = _state(created, serial=11)
    after["outputs"]["database_password"] = _state_output("after")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="output allowlist"):
        BOOTSTRAP.validate_handoff(before, after, checked, contract)


def test_plan_rejects_runtime_or_unknown_create() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    plan["resource_changes"].append(_change("aws_ecs_service.openclaw", ["create"]))
    with pytest.raises(BOOTSTRAP.BootstrapError, match="runtime/guard resource"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    plan, before, _ = _valid_plan()
    plan["resource_changes"].append(_change("aws_s3_bucket.unreviewed", ["create"]))
    with pytest.raises(BOOTSTRAP.BootstrapError, match="outside bootstrap allowlist"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_external_or_unknown_data_source_execution() -> None:
    contract = _contract()
    plan, before, _ = _valid_plan()
    external = _change("external.signed_image_release_gate", ["read"])
    external["mode"] = "data"
    plan["resource_changes"].append(external)
    with pytest.raises(BOOTSTRAP.BootstrapError, match="data source"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_plan_rejects_duplicate_object_state_ownership() -> None:
    contract = _contract()
    plan, _, created = _valid_plan()
    before = _state([created[0]])
    with pytest.raises(BOOTSTRAP.BootstrapError, match="already owned"):
        BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)


def test_handoff_rejects_removal_extra_addition_lineage_and_seed_leak() -> None:
    contract = _contract()
    existing = "aws_s3_bucket.raw_files"
    plan, before, created = _valid_plan(no_op=[existing])
    checked = BOOTSTRAP.validate_plan(plan, before, contract, plan_sha256=PLAN_SHA)

    with pytest.raises(BOOTSTRAP.BootstrapError, match="removed main-state ownership"):
        BOOTSTRAP.validate_handoff(
            before,
            _state(created, serial=11),
            checked,
            contract,
        )

    with pytest.raises(BOOTSTRAP.BootstrapError, match="extra="):
        BOOTSTRAP.validate_handoff(
            before,
            _state([existing, *created, "aws_iam_role.unreviewed"], serial=11),
            checked,
            contract,
        )

    with pytest.raises(BOOTSTRAP.BootstrapError, match="lineage changed"):
        BOOTSTRAP.validate_handoff(
            before,
            _state(
                [existing, *created],
                serial=11,
                lineage=str(uuid.uuid4()),
            ),
            checked,
            contract,
        )

    with pytest.raises(BOOTSTRAP.BootstrapError, match="temporary bootstrap object"):
        BOOTSTRAP.validate_handoff(
            before,
            _state([existing, *created], serial=11, seed_leak=True),
            checked,
            contract,
        )


def test_seed_is_temporary_mfa_sts_only_and_denies_dangerous_paths() -> None:
    body = SEED_PATH.read_text(encoding="utf-8")
    bootstrap_principal_arn = _contract().bootstrap_principal_arn
    trust_sid = "ExactIamAdminPrincipalMfaBootstrapSession"
    assert f"Sid: {trust_sid}" in body
    trust = body.split(
        f"- Sid: {trust_sid}",
        maxsplit=1,
    )[1].split("ManagedPolicyArns:", maxsplit=1)[0]
    assert body.count("Type: AWS::IAM::Role") == 1
    assert body.count("Type: AWS::IAM::ManagedPolicy") == 1
    assert "teamagent-production-provenance-bootstrap-deny-v1" in body
    assert "PolicyName: teamagent-production-provenance-bootstrap-boundary" in body
    assert "AWS::IAM::AccessKey" not in body
    assert "AWS::IAM::User" not in body
    assert "AWS::CodeBuild::Project" not in body
    assert "AWS::DynamoDB::Table" not in body
    assert f"AWS: {bootstrap_principal_arn}" in trust
    assert f"aws:PrincipalArn: {bootstrap_principal_arn}" in trust
    assert trust.count(bootstrap_principal_arn) == 2
    assert "arn:aws:iam::718959508629:root" not in trust
    assert "- sts:AssumeRole" in trust
    assert "- sts:SetSourceIdentity" in trust
    assert 'aws:MultiFactorAuthPresent: "true"' in trust
    assert "sts:ExternalId: !Ref BootstrapExternalId" in trust
    assert "sts:RoleSessionName: teamagent-provenance-bootstrap" in trust
    assert "sts:SourceIdentity: teamagent-production-provenance-bootstrap" in trust
    assert "arn:aws:iam::aws:policy/ReadOnlyAccess" not in body
    assert "arn:aws:iam::aws:policy/PowerUserAccess" not in body
    assert "Sid: ExactBootstrapInventory" in body
    inventory = body.split("- Sid: ExactBootstrapInventory", maxsplit=1)[1].split(
        "- Sid: ReadExactTerraformBuckets",
        maxsplit=1,
    )[0]
    inventory_actions = set(
        re.findall(
            r"^\s+- ([a-z0-9-]+:[A-Za-z0-9]+)\s*$",
            inventory,
            flags=re.MULTILINE,
        )
    )
    assert inventory_actions == {
        "codebuild:BatchGetProjects",
        "codeconnections:GetConnection",
        "codeconnections:ListConnections",
        "codeconnections:ListTagsForResource",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:GetItem",
        "dynamodb:ListTagsOfResource",
        "ecr:DescribeRepositories",
        "ecr:GetLifecyclePolicy",
        "ecr:ListTagsForResource",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:GetUser",
        "iam:GetUserPolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListPolicyTags",
        "iam:ListPolicyVersions",
        "iam:ListRolePolicies",
        "iam:ListRoleTags",
        "iam:ListUserPolicies",
        "iam:ListUserTags",
        "kms:DescribeKey",
        "kms:GetKeyPolicy",
        "kms:GetKeyRotationStatus",
        "kms:ListAliases",
        "kms:ListResourceTags",
        "logs:DescribeLogGroups",
        "logs:ListTagsForResource",
        "s3:GetBucketAcl",
        "s3:GetBucketLocation",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketPolicy",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "s3:GetObject",
        "s3:ListBucket",
        "sts:GetCallerIdentity",
    }
    assert "s3:GetBucketLifecycleConfiguration" not in body
    bucket_reads = body.split(
        "- Sid: ReadExactTerraformBuckets",
        maxsplit=1,
    )[1].split("- Sid: S", maxsplit=1)[0]
    assert "Effect: Allow" in bucket_reads
    assert set(
        re.findall(
            r"^\s+- (s3:[A-Za-z0-9]+)\s*$",
            bucket_reads,
            flags=re.MULTILINE,
        )
    ) == {
        "s3:GetAccelerateConfiguration",
        "s3:GetBucketCORS",
        "s3:GetBucketLogging",
        "s3:GetBucketRequestPayment",
        "s3:GetBucketWebsite",
        "s3:GetLifecycleConfiguration",
        "s3:GetReplicationConfiguration",
    }
    assert set(
        re.findall(
            r"^\s+- (arn:aws:s3:::[^\s]+)\s*$",
            bucket_reads,
            flags=re.MULTILINE,
        )
    ) == {
        "arn:aws:s3:::teamagent-dev-raw-files",
        "arn:aws:s3:::teamagent-dev-image-release-evidence",
        "arn:aws:s3:::teamagent-dev-openclaw-build-evidence",
    }
    assert "iam:CreateAccessKey" in body
    assert "codebuild:StartBuild" in body
    assert "ecr:PutImage" in body
    assert "kms:Sign" in body
    assert "ecs:UpdateService" in body
    assert "DenyAllRoleChaining" in body
    assert re.search(
        r"Sid: DenyAllRoleChaining\s+Effect: Deny\s+"
        r'Action: sts:AssumeRole\s+Resource: "\*"',
        body,
    )
    assert "Sid: PassOnlyReviewedRolesToCodeBuild" in body
    assert "iam:PassedToService: codebuild.amazonaws.com" in body
    assert "CreateOnlyTaggedGitHubCodeConnections" in body
    assert "ReadConnectionInventory" in body
    assert "codeconnections:ProviderType: GitHub" in body
    assert "kms:EnableKeyRotation" in body
    assert "Sid: CreateImmutableRuntimeBoundary" in body
    assert "teamagent-dev-terraform-runtime-automation-boundary" in body
    assert "BootstrapNonce" in body


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_bootstrap_ca_bundle_is_optional(raw: str | None) -> None:
    env = {} if raw is None else {BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: raw}
    assert BOOTSTRAP._bootstrap_ca_bundle(env) is None


def test_bootstrap_ca_bundle_accepts_absolute_regular_file(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca_bundle.pem"
    ca_bundle.write_text("test CA bundle\n", encoding="utf-8")

    assert BOOTSTRAP._bootstrap_ca_bundle(
        {BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: str(ca_bundle)}
    ) == str(ca_bundle)


def test_bootstrap_ca_bundle_rejects_relative_path() -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="absolute"):
        BOOTSTRAP._bootstrap_ca_bundle(
            {BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: "ca_bundle.pem"}
        )


def test_bootstrap_ca_bundle_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="regular file"):
        BOOTSTRAP._bootstrap_ca_bundle(
            {BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: str(tmp_path / "missing.pem")}
        )


def test_bootstrap_ca_bundle_rejects_control_characters(tmp_path: Path) -> None:
    invalid_path = f"{tmp_path}/ca\n_bundle.pem"

    with pytest.raises(BOOTSTRAP.BootstrapError, match="control characters"):
        BOOTSTRAP._bootstrap_ca_bundle(
            {BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: invalid_path}
        )


def test_temporary_principal_environment_uses_only_explicit_ca_bundle(
    tmp_path: Path,
) -> None:
    ambient_bundle = tmp_path / "ambient.pem"
    ambient_bundle.write_text("ambient CA bundle\n", encoding="utf-8")
    explicit_bundle = tmp_path / "explicit.pem"
    explicit_bundle.write_text("explicit CA bundle\n", encoding="utf-8")
    source = {
        "AWS_ACCESS_KEY_ID": "temporary-access",
        "AWS_SECRET_ACCESS_KEY": "temporary-secret",
        "AWS_SESSION_TOKEN": "temporary-session",
        "AWS_CA_BUNDLE": str(ambient_bundle),
        "SSL_CERT_FILE": str(ambient_bundle),
        BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: str(explicit_bundle),
    }

    checked = BOOTSTRAP._temporary_principal_environment(
        source,
        region="ap-northeast-1",
    )
    assert checked["AWS_CA_BUNDLE"] == str(explicit_bundle)
    assert checked["SSL_CERT_FILE"] == str(explicit_bundle)

    source.pop(BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV)
    checked_without_explicit_bundle = BOOTSTRAP._temporary_principal_environment(
        source,
        region="ap-northeast-1",
    )
    assert "AWS_CA_BUNDLE" not in checked_without_explicit_bundle
    assert "SSL_CERT_FILE" not in checked_without_explicit_bundle


def test_session_environment_uses_only_explicit_ca_bundle(tmp_path: Path) -> None:
    ambient_bundle = tmp_path / "ambient.pem"
    ambient_bundle.write_text("ambient CA bundle\n", encoding="utf-8")
    explicit_bundle = tmp_path / "explicit.pem"
    explicit_bundle.write_text("explicit CA bundle\n", encoding="utf-8")
    base = {
        "AWS_CA_BUNDLE": str(ambient_bundle),
        "SSL_CERT_FILE": str(ambient_bundle),
        BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV: str(explicit_bundle),
    }
    credentials = {
        "AccessKeyId": "seed-access",
        "SecretAccessKey": "seed-secret",
        "SessionToken": "seed-session",
    }

    checked = BOOTSTRAP._session_environment(
        base,
        credentials,
        region="ap-northeast-1",
    )
    assert checked["AWS_CA_BUNDLE"] == str(explicit_bundle)
    assert checked["SSL_CERT_FILE"] == str(explicit_bundle)

    base.pop(BOOTSTRAP.BOOTSTRAP_AWS_CA_BUNDLE_ENV)
    checked_without_explicit_bundle = BOOTSTRAP._session_environment(
        base,
        credentials,
        region="ap-northeast-1",
    )
    assert "AWS_CA_BUNDLE" not in checked_without_explicit_bundle
    assert "SSL_CERT_FILE" not in checked_without_explicit_bundle


def test_principal_credentials_must_be_an_explicit_temporary_session() -> None:
    source = {
        "PATH": os.environ["PATH"],
        "AWS_ACCESS_KEY_ID": "temporary-access",
        "AWS_SECRET_ACCESS_KEY": "temporary-secret",
        "AWS_SESSION_TOKEN": "temporary-session",
        "AWS_PROFILE": "must-not-be-used",
        "AWS_DEFAULT_PROFILE": "must-not-be-used",
    }
    checked = BOOTSTRAP._temporary_principal_environment(
        source,
        region="ap-northeast-1",
    )
    assert checked["AWS_SESSION_TOKEN"] == "temporary-session"
    assert checked["AWS_CONFIG_FILE"] == "/dev/null"
    assert checked["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert "AWS_PROFILE" not in checked
    assert "AWS_DEFAULT_PROFILE" not in checked

    for missing in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        invalid = dict(source)
        invalid.pop(missing)
        with pytest.raises(BOOTSTRAP.BootstrapError, match="temporary STS"):
            BOOTSTRAP._temporary_principal_environment(
                invalid,
                region="ap-northeast-1",
            )


@pytest.mark.parametrize(
    "name",
    [
        "TF_CLI_ARGS_plan",
        "TF_WORKSPACE",
        "TF_DATA_DIR",
        "TF_VAR_environment",
        "TF_REATTACH_PROVIDERS",
        "GIT_CONFIG_COUNT",
        "GIT_CURL_VERBOSE",
        "GIT_DIR",
        "GIT_SSH_COMMAND",
        "GIT_SSL_NO_VERIFY",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "PYTHONPATH",
    ],
)
def test_inherited_git_or_terraform_control_environment_is_rejected(name: str) -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="control variables"):
        BOOTSTRAP._reject_influential_environment({name: "untrusted"})


def test_git_environment_discards_global_and_system_transport_configuration() -> None:
    checked = BOOTSTRAP._git_environment(
        {
            "PATH": os.environ["PATH"],
            "AWS_ACCESS_KEY_ID": "must-not-reach-git",
            "AWS_SECRET_ACCESS_KEY": "must-not-reach-git",
            "AWS_SESSION_TOKEN": "must-not-reach-git",
        }
    )
    assert checked["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert checked["GIT_CONFIG_NOSYSTEM"] == "1"
    assert checked["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert checked["GIT_TERMINAL_PROMPT"] == "0"
    assert "AWS_ACCESS_KEY_ID" not in checked


@pytest.mark.parametrize("token", [None, ""])
def test_bootstrap_http_auth_args_are_empty_without_token(token: str | None) -> None:
    env = {} if token is None else {BOOTSTRAP.BOOTSTRAP_GIT_TOKEN_ENV: token}
    assert BOOTSTRAP._http_auth_args(env) == []


def test_bootstrap_http_auth_args_use_host_limited_basic_auth() -> None:
    token = "read-only-token"
    basic = base64.b64encode(b"x-access-token:" + token.encode()).decode()

    assert BOOTSTRAP._http_auth_args({BOOTSTRAP.BOOTSTRAP_GIT_TOKEN_ENV: token}) == [
        "-c",
        "http.https://github.com/.extraHeader=Authorization: Basic " + basic,
    ]


def test_bootstrap_http_auth_args_strip_surrounding_whitespace() -> None:
    token = "read-only-token"
    basic = base64.b64encode(b"x-access-token:" + token.encode()).decode()

    assert BOOTSTRAP._http_auth_args({BOOTSTRAP.BOOTSTRAP_GIT_TOKEN_ENV: f"  {token}  "}) == [
        "-c",
        "http.https://github.com/.extraHeader=Authorization: Basic " + basic,
    ]


def test_bootstrap_http_auth_args_reject_control_characters() -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="control characters"):
        BOOTSTRAP._http_auth_args({BOOTSTRAP.BOOTSTRAP_GIT_TOKEN_ENV: "read-only\ntoken"})


def test_command_runner_pins_executable_bytes(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)
    fake_aws = fake_bin / "aws"
    fake_aws.write_text("#!/bin/sh\nprintf '{}\\n'\n", encoding="utf-8")
    fake_aws.chmod(0o755)
    runner = BOOTSTRAP.CommandRunner()
    environment = {"PATH": str(fake_bin)}

    first = runner.run(["aws", "--version"], cwd=tmp_path, env=environment)
    assert first.returncode == 0
    assert runner.tool_evidence()["aws"]["sha256"] == BOOTSTRAP.sha256_file(fake_aws)

    fake_aws.write_text("#!/bin/sh\nprintf 'changed\\n'\n", encoding="utf-8")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="changed during bootstrap"):
        runner.run(["aws", "--version"], cwd=tmp_path, env=environment)


def test_ignored_terraform_override_and_hidden_index_state_are_rejected(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    terraform_dir = repo / "infra" / "terraform"
    terraform_dir.mkdir(parents=True)
    (repo / ".gitignore").write_text(
        "infra/terraform/ignored.tf\n",
        encoding="utf-8",
    )
    source = terraform_dir / "main.tf"
    source.write_text('terraform { required_version = ">= 1.12" }\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".gitignore", "infra/terraform/main.tf"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Bootstrap Test",
            "-c",
            "user.email=bootstrap@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    runner = BOOTSTRAP.CommandRunner()
    environment = {"PATH": os.environ["PATH"]}
    BOOTSTRAP._assert_safe_local_git_transport(repo, runner, environment)
    BOOTSTRAP._assert_tracked_terraform_source(repo, runner, environment)
    assert len(BOOTSTRAP._materialized_head_tree_sha256(repo, runner, environment)) == 64

    ignored = terraform_dir / "ignored.tf"
    ignored.write_text('resource "null_resource" "override" {}\n', encoding="utf-8")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="untracked/ignored override"):
        BOOTSTRAP._assert_tracked_terraform_source(repo, runner, environment)
    ignored.unlink()

    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "infra/terraform/main.tf"],
        cwd=repo,
        check=True,
    )
    with pytest.raises(BOOTSTRAP.BootstrapError, match="assume-unchanged"):
        BOOTSTRAP._assert_tracked_terraform_source(repo, runner, environment)
    source.write_text('terraform { required_version = ">= 9.99" }\n', encoding="utf-8")
    with pytest.raises(BOOTSTRAP.BootstrapError, match="bytes differ from HEAD"):
        BOOTSTRAP._materialized_head_tree_sha256(repo, runner, environment)

    subprocess.run(
        ["git", "config", "url.file:///private/tmp/.insteadOf", "https://github.com/"],
        cwd=repo,
        check=True,
    )
    with pytest.raises(BOOTSTRAP.BootstrapError, match="redirect provenance lookup"):
        BOOTSTRAP._assert_safe_local_git_transport(repo, runner, environment)


class _RetirementRunner:
    nonce = "c" * 64
    commit = "b" * 40
    stack_id = (
        "arn:aws:cloudformation:ap-northeast-1:718959508629:"
        "stack/teamagent-production-provenance-bootstrap-v1/"
        "11111111-2222-4333-8444-555555555555"
    )

    def __init__(self, *, hostile_nonce: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.deleted = False
        self.hostile_nonce = hostile_nonce

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
    ) -> Any:
        del cwd, env, check
        call = list(arguments)
        self.calls.append(call)
        if "describe-stacks" in call:
            payload = {
                "Stacks": [
                    {
                        "StackId": self.stack_id,
                        "StackName": "teamagent-production-provenance-bootstrap-v1",
                        "Parameters": [
                            {
                                "ParameterKey": "BootstrapExternalId",
                                "ParameterValue": "****",
                            },
                            {
                                "ParameterKey": "BootstrapNonce",
                                "ParameterValue": self.hostile_nonce or self.nonce,
                            },
                            {
                                "ParameterKey": "BootstrapCommit",
                                "ParameterValue": self.commit,
                            },
                        ],
                        "Tags": [
                            {
                                "Key": "BootstrapId",
                                "Value": "teamagent-production-provenance-iam-v1",
                            },
                            {
                                "Key": "BootstrapNonce",
                                "Value": self.hostile_nonce or self.nonce,
                            },
                            {"Key": "ControlCommit", "Value": self.commit},
                            {"Key": "ManagedBy", "Value": "TeamAgentBootstrap"},
                        ],
                    }
                ]
            }
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps(payload).encode(),
                stderr=b"",
                returncode=0,
            )
        if "describe-stack-resources" in call:
            payload = {
                "StackResources": [
                    {
                        "LogicalResourceId": "BootstrapDenyPolicy",
                        "ResourceType": "AWS::IAM::ManagedPolicy",
                        "PhysicalResourceId": (
                            "arn:aws:iam::718959508629:policy/"
                            "teamagent-production-provenance-bootstrap-deny-v1"
                        ),
                    },
                    {
                        "LogicalResourceId": "BootstrapExecutorRole",
                        "ResourceType": "AWS::IAM::Role",
                        "PhysicalResourceId": ("teamagent-production-provenance-bootstrap-v1"),
                    },
                ]
            }
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps(payload).encode(),
                stderr=b"",
                returncode=0,
            )
        if "delete-stack" in call:
            self.deleted = True
            return BOOTSTRAP.CommandResult(stdout=b"{}", stderr=b"", returncode=0)
        if "list-connections" in call:
            return BOOTSTRAP.CommandResult(
                stdout=b"",
                stderr=b"AccessDenied: explicit deny",
                returncode=254,
            )
        if "get-role" in call and not self.deleted:
            payload = {
                "Role": {
                    "Arn": (
                        "arn:aws:iam::718959508629:role/"
                        "teamagent-production-provenance-bootstrap-v1"
                    ),
                    "Tags": [
                        {"Key": "Project", "Value": "TeamAgent"},
                        {
                            "Key": "Purpose",
                            "Value": "OneTimeProvenanceIamBootstrap",
                        },
                        {"Key": "BootstrapCommit", "Value": self.commit},
                        {
                            "Key": "BootstrapNonce",
                            "Value": self.hostile_nonce or self.nonce,
                        },
                        {
                            "Key": "BootstrapId",
                            "Value": "teamagent-production-provenance-iam-v1",
                        },
                        {
                            "Key": "ManagedBy",
                            "Value": "CloudFormationTemporarySeed",
                        },
                    ],
                }
            }
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps(payload).encode(),
                stderr=b"",
                returncode=0,
            )
        if "list-attached-role-policies" in call:
            payload = {
                "AttachedPolicies": [
                    {
                        "PolicyName": ("teamagent-production-provenance-bootstrap-deny-v1"),
                        "PolicyArn": (
                            "arn:aws:iam::718959508629:policy/"
                            "teamagent-production-provenance-bootstrap-deny-v1"
                        ),
                    }
                ]
            }
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps(payload).encode(),
                stderr=b"",
                returncode=0,
            )
        if ("get-role" in call and self.deleted) or "get-policy" in call:
            return BOOTSTRAP.CommandResult(
                stdout=b"",
                stderr=b"NoSuchEntity",
                returncode=254,
            )
        return BOOTSTRAP.CommandResult(stdout=b"{}", stderr=b"", returncode=0)


def test_seed_creation_records_the_exact_cloudformation_stack_id() -> None:
    contract = _contract()
    stack_id = (
        "arn:aws:cloudformation:ap-northeast-1:718959508629:"
        "stack/teamagent-production-provenance-bootstrap-v1/"
        "11111111-2222-4333-8444-555555555555"
    )

    class Runner:
        def run(
            self,
            arguments: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            check: bool = True,
        ) -> Any:
            del cwd, env, check
            assert "cloudformation" in arguments
            assert "create-stack" in arguments
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps({"StackId": stack_id}).encode(),
                stderr=b"",
                returncode=0,
            )

    assert (
        BOOTSTRAP._create_seed_stack(
            Runner(),
            repo_root=ROOT,
            principal_env={"PATH": os.environ["PATH"]},
            contract=contract,
            external_id="a" * 64,
            nonce="c" * 64,
            commit="b" * 40,
        )
        == stack_id
    )


def test_seed_retirement_closes_trust_before_revoking_sessions_and_deleting() -> None:
    runner = _RetirementRunner()
    result = BOOTSTRAP._revoke_and_delete_seed(
        runner,
        repo_root=ROOT,
        principal_env={"PATH": os.environ["PATH"]},
        session_env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        nonce=runner.nonce,
        commit=runner.commit,
        expected_stack_id=runner.stack_id,
    )
    flattened = [" ".join(call) for call in runner.calls]
    trust_index = next(
        index for index, call in enumerate(flattened) if "iam update-assume-role-policy" in call
    )
    revoke_index = next(
        index for index, call in enumerate(flattened) if "iam put-role-policy" in call
    )
    delete_index = next(
        index for index, call in enumerate(flattened) if "cloudformation delete-stack" in call
    )
    assert trust_index < revoke_index < delete_index
    assert '"Sid":"RetiredBootstrapRole"' in flattened[trust_index]
    revoke_call = runner.calls[revoke_index]
    assert revoke_call[revoke_call.index("--policy-name") + 1] == (
        "teamagent-production-provenance-bootstrap-boundary"
    )
    revoke_document = json.loads(revoke_call[revoke_call.index("--policy-document") + 1])
    denied_through = revoke_document["Statement"][0]["Condition"]["DateLessThanEquals"][
        "aws:TokenIssueTime"
    ]
    assert denied_through == result["sessions_issued_through_denied"]
    assert denied_through > result["trust_closed_at"]
    for call in runner.calls:
        if "iam" in call:
            assert call[call.index("--region") + 1] == "us-east-1"
    assert result["trust_closed_before_revocation"] is True
    assert result["session_probe_denied"] is True
    assert result["role_deleted"] is True
    assert result["deny_policy_deleted"] is True


def test_hostile_seed_ownership_aborts_before_every_retirement_write() -> None:
    runner = _RetirementRunner(hostile_nonce="d" * 64)
    with pytest.raises(BOOTSTRAP.BootstrapError, match="ownership"):
        BOOTSTRAP._revoke_and_delete_seed(
            runner,
            repo_root=ROOT,
            principal_env={"PATH": os.environ["PATH"]},
            session_env=None,
            contract=_contract(),
            nonce=runner.nonce,
            commit=runner.commit,
            expected_stack_id=runner.stack_id,
        )
    flattened = [" ".join(call) for call in runner.calls]
    assert not any(
        mutation in call
        for call in flattened
        for mutation in (
            "iam update-assume-role-policy",
            "iam put-role-policy",
            "cloudformation delete-stack",
        )
    )


class _UpsertProbeRunner:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
    ) -> Any:
        del cwd, env, check
        self.calls.append(list(arguments))
        if self.exists:
            return BOOTSTRAP.CommandResult(
                stdout=b'{"PolicyDocument":{"Version":"2012-10-17"}}',
                stderr=b"",
                returncode=0,
            )
        return BOOTSTRAP.CommandResult(
            stdout=b"",
            stderr=b"An error occurred (NoSuchEntity) when calling GetUserPolicy",
            returncode=254,
        )


def test_aiiadev_inline_policy_create_requires_exact_aws_absence() -> None:
    address = "aws_iam_user_policy.aiia_dev_no_direct_start_build"
    plan = {"resource_changes": [_change(address, ["create"])]}
    runner = _UpsertProbeRunner(exists=False)
    BOOTSTRAP._assert_upsert_create_ownership(
        runner,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        plan_value=plan,
        before_addresses=set(),
    )
    call = runner.calls[0]
    assert call[:6] == [
        "aws",
        "--region",
        "us-east-1",
        "--endpoint-url",
        BOOTSTRAP.AWS_ENDPOINTS["iam"],
        "--no-cli-pager",
    ]
    assert call[6:12] == [
        "iam",
        "get-user-policy",
        "--user-name",
        "AIIAdev",
        "--policy-name",
        "require-teamagent-codebuild-launcher-role",
    ]

    hostile = _UpsertProbeRunner(exists=True)
    with pytest.raises(BOOTSTRAP.BootstrapError, match="already exists"):
        BOOTSTRAP._assert_upsert_create_ownership(
            hostile,
            cwd=ROOT,
            env={"PATH": os.environ["PATH"]},
            contract=_contract(),
            plan_value=plan,
            before_addresses=set(),
        )
    assert len(hostile.calls) == 1


def test_every_create_allowlisted_upsert_has_an_exact_aws_owner_probe() -> None:
    contract = _contract()
    mapped = (
        set(BOOTSTRAP.INLINE_POLICY_OWNERSHIP)
        | set(BOOTSTRAP.ECR_LIFECYCLE_OWNERSHIP)
        | set(BOOTSTRAP.S3_UPSERT_OWNERSHIP)
    )
    upserts = {
        BOOTSTRAP.normalize_address(address)
        for address in contract.create_allowed
        if BOOTSTRAP.normalize_address(address).split(".", 1)[0] in BOOTSTRAP.UPSERT_RESOURCE_TYPES
    }
    assert upserts
    assert upserts == mapped


def test_handoff_artifacts_are_full_fsynced_and_conflict_safe(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "handoff"
    artifact_dir.mkdir(mode=0o700)
    contract = _contract()
    plan, before, created = _valid_plan()
    validation = BOOTSTRAP.validate_plan(
        plan,
        before,
        contract,
        plan_sha256=PLAN_SHA,
    )
    after = _state(created, serial=11)
    handoff = BOOTSTRAP.validate_handoff(before, after, validation, contract)
    claims, ownership = BOOTSTRAP._handoff_documents(
        contract=contract,
        nonce="c" * 64,
        commit="b" * 40,
        source_tree_sha256="d" * 64,
        contract_sha256="e" * 64,
        seed_template_sha256="f" * 64,
        tfvars_sha256="1" * 64,
        release_hashes={"release.json": "2" * 64},
        tool_versions={"terraform": "1.12.2"},
        tool_evidence={"terraform": {"sha256": "3" * 64}},
        plan_sha256=PLAN_SHA,
        plan_validation=validation,
        handoff=handoff,
        before_state=before,
        after_state=after,
        connections=[],
        seed_stack_id=_RetirementRunner.stack_id,
    )
    claims_sha, ownership_sha = BOOTSTRAP._persist_handoff_artifacts(
        artifact_dir,
        claims=claims,
        ownership=ownership,
    )
    assert claims["owned_main_state_addresses"] == sorted(created)
    assert ownership["addresses_after"] == sorted(created)
    assert ownership["required_main_state_addresses"] == sorted(contract.required_main_state)
    durable = BOOTSTRAP.load_json(
        artifact_dir / "bootstrap-handoff-durable.json",
        label="durable handoff",
    )
    assert durable["claims_sha256"] == claims_sha
    assert durable["ownership_sha256"] == ownership_sha
    for name in (
        "bootstrap-handoff-claims.json",
        "bootstrap-handoff-ownership.json",
        "bootstrap-handoff-durable.json",
    ):
        assert (artifact_dir / name).stat().st_mode & 0o777 == 0o600

    assert BOOTSTRAP._persist_handoff_artifacts(
        artifact_dir,
        claims=claims,
        ownership=ownership,
    ) == (claims_sha, ownership_sha)
    with pytest.raises(BOOTSTRAP.BootstrapError, match="conflicts"):
        BOOTSTRAP._persist_handoff_artifacts(
            artifact_dir,
            claims={**claims, "control_commit": "a" * 40},
            ownership=ownership,
        )

    run_source = inspect.getsource(BOOTSTRAP.run_bootstrap)
    persist_offset = run_source.index("_persist_handoff_artifacts(")
    consumed_offset = run_source.index('next_state="CONSUMED"', persist_offset)
    assert persist_offset < consumed_offset


def test_crash_after_atomic_publish_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = tmp_path / "crash"
    artifact_dir.mkdir(mode=0o700)
    target = artifact_dir / "claim.json"
    value = {"claim": "fully durable before terminal transition"}
    real_fsync_directory = BOOTSTRAP._fsync_directory
    calls = 0

    def fail_after_publish(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash after atomic publish")
        real_fsync_directory(path)

    monkeypatch.setattr(BOOTSTRAP, "_fsync_directory", fail_after_publish)
    with pytest.raises(OSError, match="simulated crash"):
        BOOTSTRAP._persist_or_verify_private_json(target, value)
    assert target.read_bytes() == BOOTSTRAP.canonical_bytes(value)

    monkeypatch.setattr(BOOTSTRAP, "_fsync_directory", real_fsync_directory)
    assert BOOTSTRAP._persist_or_verify_private_json(target, value) == (
        BOOTSTRAP.sha256_bytes(BOOTSTRAP.canonical_bytes(value))
    )
    assert not list(artifact_dir.glob(".*.tmp"))


def test_concurrent_same_handoff_publish_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = tmp_path / "concurrent"
    artifact_dir.mkdir(mode=0o700)
    target = artifact_dir / "ownership.json"
    value = {"addresses_after": ["aws_iam_role.codebuild_launcher"]}
    barrier = threading.Barrier(2)
    real_link = os.link

    def racing_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        barrier.wait(timeout=5)
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(BOOTSTRAP.os, "link", racing_link)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: BOOTSTRAP._persist_or_verify_private_json(target, value),
                range(2),
            )
        )
    assert results[0] == results[1]
    assert target.read_bytes() == BOOTSTRAP.canonical_bytes(value)
    assert not list(artifact_dir.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param(b"", {}, id="empty"),
        pytest.param(
            b'{"Item":{"LockID":{"S":"bootstrap#example"}}}',
            {"Item": {"LockID": {"S": "bootstrap#example"}}},
            id="item",
        ),
        pytest.param(b"{}", {}, id="empty-object"),
    ],
)
def test_decode_optional_item_result_accepts_empty_or_json_object(
    stdout: bytes,
    expected: dict[str, Any],
) -> None:
    result = BOOTSTRAP.CommandResult(stdout=stdout, stderr=b"", returncode=0)
    assert BOOTSTRAP._decode_optional_item_result(result, label="get-item") == expected


def test_decode_optional_item_result_rejects_non_json_output() -> None:
    result = BOOTSTRAP.CommandResult(stdout=b"not-json", stderr=b"", returncode=0)
    with pytest.raises(
        BOOTSTRAP.BootstrapError,
        match="get-item command returned invalid JSON",
    ):
        BOOTSTRAP._decode_optional_item_result(result, label="get-item")


class _StaticGetItemRunner:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
    ) -> Any:
        del cwd, env, check
        assert "get-item" in arguments
        self.calls.append(list(arguments))
        return BOOTSTRAP.CommandResult(
            stdout=self.stdout,
            stderr=b"",
            returncode=0,
        )


class _LedgerRunner:
    def __init__(self, state: str) -> None:
        self.state = state

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
    ) -> Any:
        del cwd, env
        if "update-item" in arguments:
            raw_values = arguments[arguments.index("--expression-attribute-values") + 1]
            values = json.loads(raw_values)
            expected = values[":expected"]["S"]
            if self.state != expected:
                if check:
                    raise BOOTSTRAP.BootstrapError("ConditionalCheckFailed")
                return BOOTSTRAP.CommandResult(
                    stdout=b"",
                    stderr=b"ConditionalCheckFailed",
                    returncode=254,
                )
            self.state = values[":next"]["S"]
            payload = {
                "Attributes": {
                    "LockID": {"S": "bootstrap#teamagent-production-provenance-iam-v1"},
                    "RecordType": {"S": "teamagent-production-provenance-iam-v1"},
                    "BootstrapNonce": {"S": "b" * 64},
                    "State": {"S": self.state},
                }
            }
            return BOOTSTRAP.CommandResult(
                stdout=json.dumps(payload).encode(),
                stderr=b"",
                returncode=0,
            )
        payload = {
            "Item": {
                "LockID": {"S": "bootstrap#teamagent-production-provenance-iam-v1"},
                "RecordType": {"S": "teamagent-production-provenance-iam-v1"},
                "BootstrapNonce": {"S": "b" * 64},
                "State": {"S": self.state},
            }
        }
        return BOOTSTRAP.CommandResult(
            stdout=json.dumps(payload).encode(),
            stderr=b"",
            returncode=0,
        )


def test_ambiguous_ledger_state_is_conditionally_reconciled_by_nonce() -> None:
    runner = _LedgerRunner("APPLYING")
    state, response = BOOTSTRAP._reconcile_ledger_after_failure(
        runner,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        nonce="b" * 64,
        failure_sha256="c" * 64,
    )
    assert state == "RECONCILE_REQUIRED"
    assert response is not None
    assert runner.state == "RECONCILE_REQUIRED"


def test_consumed_ledger_is_observed_without_rewriting_terminal_state() -> None:
    runner = _LedgerRunner("CONSUMED")
    state, response = BOOTSTRAP._reconcile_ledger_after_failure(
        runner,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        nonce="b" * 64,
        failure_sha256="c" * 64,
    )
    assert state == "CONSUMED"
    assert response is None
    assert runner.state == "CONSUMED"


def test_consumed_reconcile_is_idempotent_and_never_touches_terraform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    artifact_dir = tmp_path / "consumed"
    artifact_dir.mkdir(mode=0o700)
    nonce = "c" * 64
    commit = "b" * 40
    source_tree = "d" * 64
    invocation = {
        "kind": "teamagent-provenance-bootstrap-invocation",
        "schema_version": 1,
        "bootstrap_id": contract.bootstrap_id,
        "bootstrap_nonce": nonce,
        "control_commit": commit,
        "source_tree_sha256": source_tree,
        "contract_sha256": BOOTSTRAP.sha256_file(contract.path),
        "seed_template_sha256": BOOTSTRAP.sha256_file(SEED_PATH),
        "release_contract_sha256": {},
        "tfvars_sha256": "e" * 64,
        "toolchain_versions": {},
    }
    BOOTSTRAP._write_private_json(
        artifact_dir / "bootstrap-invocation.json",
        invocation,
    )
    claims_sha, ownership_sha = BOOTSTRAP._persist_handoff_artifacts(
        artifact_dir,
        claims={"kind": "claims", "bootstrap_nonce": nonce},
        ownership={"kind": "ownership", "bootstrap_nonce": nonce},
    )
    ledger_item = {
        "LockID": {"S": str(contract.backend["ledger_key"])},
        "RecordType": {"S": contract.bootstrap_id},
        "BootstrapNonce": {"S": nonce},
        "State": {"S": "CONSUMED"},
        "HandoffClaimsSha256": {"S": claims_sha},
        "HandoffOwnershipSha256": {"S": ownership_sha},
    }
    terraform_calls: list[list[str]] = []
    retirements: list[str] = []
    identity_checks: list[dict[str, Any]] = []

    monkeypatch.setattr(BOOTSTRAP, "validate_release_contracts", lambda *args: {})
    monkeypatch.setattr(
        BOOTSTRAP,
        "_validate_repository",
        lambda *args, **kwargs: (commit, source_tree),
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_validate_local_toolchain",
        lambda *args, **kwargs: {},
    )
    def assert_identity(*args: Any, **kwargs: Any) -> None:
        del args
        identity_checks.append(kwargs)

    monkeypatch.setattr(BOOTSTRAP, "_assert_identity", assert_identity)
    monkeypatch.setattr(
        BOOTSTRAP,
        "_read_bootstrap_ledger_item",
        lambda *args, **kwargs: ledger_item,
    )

    def forbid_terraform(
        runner: Any,
        arguments: list[str],
        *,
        terraform_dir: Path,
        env: dict[str, str],
    ) -> Any:
        del runner, terraform_dir, env
        terraform_calls.append(arguments)
        raise AssertionError("consumed reconciliation must not invoke Terraform")

    monkeypatch.setattr(BOOTSTRAP, "_terraform", forbid_terraform)

    def retire(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        retirements.append("retired")
        return {
            "already_absent": True,
            "ownership_proved": True,
            "stack_deleted": True,
        }

    monkeypatch.setattr(BOOTSTRAP, "_revoke_and_delete_seed", retire)
    process_env = {
        "PATH": os.environ["PATH"],
        "AWS_ACCESS_KEY_ID": "temporary-access",
        "AWS_SECRET_ACCESS_KEY": "temporary-secret",
        "AWS_SESSION_TOKEN": "temporary-session",
    }
    for _ in range(2):
        receipt_path = BOOTSTRAP.reconcile_and_retire(
            repo_root=ROOT,
            artifact_dir=artifact_dir,
            contract_path=CONTRACT_PATH,
            runner=object(),
            process_env=process_env,
        )
        receipt = BOOTSTRAP.load_json(receipt_path, label="reconcile receipt")
        assert receipt["status"] == "CONSUMED_RETIRED"
        assert receipt["plan_reapplied"] is False
    assert terraform_calls == []
    assert retirements == ["retired", "retired"]
    assert [check["expected_arn"] for check in identity_checks] == [
        contract.bootstrap_principal_arn,
        contract.bootstrap_principal_arn,
    ]
    assert [check["label"] for check in identity_checks] == [
        "reconcile bootstrap principal caller",
        "reconcile bootstrap principal caller",
    ]


def test_reconcile_rejects_unknown_ledger_state_before_terraform_or_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    artifact_dir = tmp_path / "unknown-ledger-state"
    artifact_dir.mkdir(mode=0o700)
    nonce = "c" * 64
    commit = "b" * 40
    source_tree = "d" * 64
    BOOTSTRAP._write_private_json(
        artifact_dir / "bootstrap-invocation.json",
        {
            "kind": "teamagent-provenance-bootstrap-invocation",
            "schema_version": 1,
            "bootstrap_id": contract.bootstrap_id,
            "bootstrap_nonce": nonce,
            "control_commit": commit,
            "source_tree_sha256": source_tree,
            "contract_sha256": BOOTSTRAP.sha256_file(contract.path),
            "seed_template_sha256": BOOTSTRAP.sha256_file(SEED_PATH),
            "release_contract_sha256": {},
            "tfvars_sha256": "e" * 64,
            "toolchain_versions": {},
        },
    )
    ledger_item = {
        "LockID": {"S": str(contract.backend["ledger_key"])},
        "RecordType": {"S": contract.bootstrap_id},
        "BootstrapNonce": {"S": nonce},
        "State": {"S": "HOSTILE"},
    }
    monkeypatch.setattr(BOOTSTRAP, "validate_release_contracts", lambda *args: {})
    monkeypatch.setattr(
        BOOTSTRAP,
        "_validate_repository",
        lambda *args, **kwargs: (commit, source_tree),
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_validate_local_toolchain",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_assert_identity",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_read_bootstrap_ledger_item",
        lambda *args, **kwargs: ledger_item,
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_terraform",
        lambda *args, **kwargs: pytest.fail("unexpected Terraform invocation"),
    )
    monkeypatch.setattr(
        BOOTSTRAP,
        "_revoke_and_delete_seed",
        lambda *args, **kwargs: pytest.fail("unexpected seed retirement"),
    )

    with pytest.raises(BOOTSTRAP.BootstrapError, match="unsupported state: HOSTILE"):
        BOOTSTRAP.reconcile_and_retire(
            repo_root=ROOT,
            artifact_dir=artifact_dir,
            contract_path=CONTRACT_PATH,
            runner=object(),
            process_env={
                "PATH": os.environ["PATH"],
                "AWS_ACCESS_KEY_ID": "temporary-access",
                "AWS_SECRET_ACCESS_KEY": "temporary-secret",
                "AWS_SESSION_TOKEN": "temporary-session",
            },
        )


def test_empty_get_item_response_means_bootstrap_ledger_is_absent() -> None:
    runner = _StaticGetItemRunner(b"")
    assert (
        BOOTSTRAP._read_bootstrap_ledger_item(
            runner,
            cwd=ROOT,
            env={"PATH": os.environ["PATH"]},
            contract=_contract(),
            nonce="b" * 64,
        )
        is None
    )
    assert len(runner.calls) == 1


def test_empty_get_item_response_passes_one_use_ledger_preflight() -> None:
    runner = _StaticGetItemRunner(b"")
    BOOTSTRAP._assert_bootstrap_ledger_absent(
        runner,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
    )
    assert len(runner.calls) == 1


def test_existing_one_use_ledger_blocks_before_seed_creation() -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="ledger already exists"):
        BOOTSTRAP._assert_bootstrap_ledger_absent(
            _LedgerRunner("CONSUMED"),
            cwd=ROOT,
            env={"PATH": os.environ["PATH"]},
            contract=_contract(),
        )


class _ConnectionRunner:
    def __init__(self, connections: list[dict[str, str]]) -> None:
        self.connections = connections
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool = True,
    ) -> Any:
        del cwd, env, check
        self.calls.append(list(arguments))
        return BOOTSTRAP.CommandResult(
            stdout=json.dumps(
                {
                    "Connections": self.connections,
                }
            ).encode(),
            stderr=b"",
            returncode=0,
        )


def _connection(name: str, *, status: str = "PENDING") -> dict[str, str]:
    return {
        "ConnectionName": name,
        "ConnectionArn": (
            "arn:aws:codeconnections:ap-northeast-1:718959508629:"
            f"connection/{'1' * 8}-{'2' * 4}-{'3' * 4}-{'4' * 4}-{'5' * 12}"
        ),
        "ProviderType": "GitHub",
        "OwnerAccountId": "718959508629",
        "ConnectionStatus": status,
    }


def test_pending_connection_is_a_safe_no_go_and_disabled_tiktok_may_be_absent() -> None:
    runner = _ConnectionRunner([_connection("teamagent-dev-openclaw-codebuild")])
    inventory = BOOTSTRAP._connection_inventory(
        runner,
        repo_root=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        after_addresses={"aws_codestarconnections_connection.openclaw_codebuild"},
    )
    assert inventory == [
        {
            "name": "teamagent-dev-openclaw-codebuild",
            "arn": runner.connections[0]["ConnectionArn"],
            "status": "PENDING",
        }
    ]
    assert "list-connections" in runner.calls[0]
    assert BOOTSTRAP.AWS_ENDPOINTS["codeconnections"] in runner.calls[0]


def test_empty_or_ambiguous_required_connection_inventory_fails_closed() -> None:
    for connections in (
        [],
        [
            _connection("teamagent-dev-openclaw-codebuild"),
            _connection("teamagent-dev-openclaw-codebuild"),
        ],
    ):
        with pytest.raises(BOOTSTRAP.BootstrapError, match="missing or ambiguous"):
            BOOTSTRAP._connection_inventory(
                _ConnectionRunner(connections),
                repo_root=ROOT,
                env={"PATH": os.environ["PATH"]},
                contract=_contract(),
                after_addresses={"aws_codestarconnections_connection.openclaw_codebuild"},
            )


def test_empty_connection_inventory_is_safe_before_create_only_apply() -> None:
    BOOTSTRAP._assert_connection_ownership_before_apply(
        _ConnectionRunner([]),
        repo_root=ROOT,
        env={"PATH": os.environ["PATH"]},
        contract=_contract(),
        before_addresses=set(),
        created_addresses=("aws_codestarconnections_connection.openclaw_codebuild",),
    )


def test_unowned_connection_name_cannot_be_duplicated_by_bootstrap() -> None:
    with pytest.raises(BOOTSTRAP.BootstrapError, match="outside main state"):
        BOOTSTRAP._assert_connection_ownership_before_apply(
            _ConnectionRunner([_connection("teamagent-dev-openclaw-codebuild")]),
            repo_root=ROOT,
            env={"PATH": os.environ["PATH"]},
            contract=_contract(),
            before_addresses=set(),
            created_addresses=("aws_codestarconnections_connection.openclaw_codebuild",),
        )


def test_wrong_provider_connection_name_is_not_hidden_by_inventory_filter() -> None:
    wrong_provider = _connection("teamagent-dev-openclaw-codebuild")
    wrong_provider["ProviderType"] = "GitLab"
    with pytest.raises(BOOTSTRAP.BootstrapError, match="outside main state"):
        BOOTSTRAP._assert_connection_ownership_before_apply(
            _ConnectionRunner([wrong_provider]),
            repo_root=ROOT,
            env={"PATH": os.environ["PATH"]},
            contract=_contract(),
            before_addresses=set(),
            created_addresses=("aws_codestarconnections_connection.openclaw_codebuild",),
        )


def test_bootstrap_targets_resolve_to_main_terraform_resources() -> None:
    contract = _contract()
    declarations: set[str] = set()
    for path in (
        CODEBUILD_TF,
        ECR_TF,
        RUNTIME_EVIDENCE_TF,
        MEDIA_CUTOVER_ATTESTOR_TF,
    ):
        declarations.update(
            f"{resource_type}.{name}"
            for resource_type, name in re.findall(
                r'^resource "([^"]+)" "([^"]+)" \{',
                path.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
    assert set(contract.targets) <= declarations
    assert "aws_codebuild_project.image" not in contract.targets
    assert "terraform_data.runtime_guard" not in contract.targets


def test_runtime_prerequisites_are_main_owned_and_root_must_assume_sts() -> None:
    body = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    assert 'resource "aws_kms_key" "alarm_recipient_ack"' in body
    assert 'resource "aws_iam_role" "alarm_recipient_ack_signer"' in body
    assert 'resource "aws_iam_role" "runtime_automation"' in body
    assert 'resource "aws_iam_role_policy" "runtime_evidence_automation"' in body
    assert 'resource "aws_iam_role_policy" "runtime_automation_control_plane"' in body
    assert 'resource "aws_iam_policy" "runtime_automation_boundary"' in body
    assert 'resource "aws_iam_role_policy_attachment" "runtime_automation_power_user"' not in body
    assert "arn:aws:iam::aws:policy/PowerUserAccess" not in body
    assert "permissions_boundary = aws_iam_policy.runtime_automation_boundary.arn" in body
    assert 'data "aws_iam_role" "runtime_automation"' not in body
    assert 'data "aws_kms_alias" "alarm_recipient_ack"' not in body
    assert 'variable = "aws:MultiFactorAuthPresent"' in body
    assert 'variable = "sts:RoleSessionName"' in body
    assert 'variable = "sts:SourceIdentity"' in body
    assert "teamagent-alarm-recipient-ack" in body
    assert "teamagent-production-alarm-recipient" in body
    assert (
        '"arn:aws:sns:${var.aws_region}:718959508629:'
        '${var.project_name}-${var.environment}-openclaw-alarms"'
    ) in body
    assert (
        '"arn:aws:sns:${var.aws_region}:718959508629:${var.project_name}-${var.environment}-alarms"'
    ) not in body
    assert '"iam:CreateAccessKey"' in body
    assert "DenyIamSelfEscalation" in body
    assert "DenyRoleChaining" in body
    boundary = body.split(
        'data "aws_iam_policy_document" "runtime_automation_boundary"',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_policy" "runtime_automation_boundary"',
        maxsplit=1,
    )[0]
    assert boundary.count('actions   = ["*"]') == 1
    assert 'sid    = "DenyIamSelfEscalation"' in boundary
    assert 'sid    = "DenyRoleChaining"' in boundary
    for action in (
        "iam:AttachRolePolicy",
        "iam:CreateAccessKey",
        "iam:CreatePolicyVersion",
        "iam:CreateRole",
        "iam:DeleteRolePermissionsBoundary",
        "iam:PutRolePermissionsBoundary",
        "iam:PutRolePolicy",
        "iam:SetDefaultPolicyVersion",
        "iam:UpdateAssumeRolePolicy",
        "sts:AssumeRole",
        "sts:AssumeRoleWithSAML",
        "sts:AssumeRoleWithWebIdentity",
    ):
        assert f'"{action}"' in boundary
    assert "PassOnlyExistingTeamAgentServiceRoles" in body
    assert "ManageOnlyTeamAgentIam" not in body
    assert "AssumeOnlyImageDeploymentGate" not in body
    assert "DenyBootstrapAuditMutation" in body
    assert "DenyBootstrapAuditTableDeletion" in body
    assert "DenyBootstrapSeedIamMutation" in body
    assert "bootstrap#teamagent-production-provenance-iam-v1" in body
    assert "TransitionExactDeploymentIntentLedger" in body
    assert '"codebuild:StartBuild"' in body
    assert '"ecr:PutImage"' in body
    assert '"kms:Sign"' in body
    evidence_policy = body.split(
        'data "aws_iam_policy_document" "runtime_evidence_automation"',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "runtime_evidence_automation"',
        maxsplit=1,
    )[0]
    assert not re.search(r'actions\s*=\s*\[[^\]]*"[^"]+:\*"', evidence_policy, re.DOTALL)
    assert '"aws-portal:ViewBilling"' in evidence_policy
    assert '"budgets:ViewBudget"' in evidence_policy
    assert "budgets:Describe*" not in evidence_policy
    assert "ec2:Describe*" not in evidence_policy

    control_policy = body.split(
        'data "aws_iam_policy_document" "runtime_automation_control_plane"',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "runtime_automation_control_plane"',
        maxsplit=1,
    )[0]
    control_allows = control_policy.split(
        'sid    = "DenyBootstrapAuditMutation"',
        maxsplit=1,
    )[0]
    assert not re.search(r'"(?:iam|sts):[^"]*\*"', control_allows)
    assert '"iam:PassRole"' in control_allows
    assert '"sts:GetCallerIdentity"' in control_allows
    for forbidden in (
        "iam:AttachRolePolicy",
        "iam:CreatePolicyVersion",
        "iam:CreateRole",
        "iam:PutRolePermissionsBoundary",
        "iam:PutRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "sts:AssumeRole",
    ):
        assert f'"{forbidden}"' not in control_allows


def test_entrypoints_never_dispatch_build_or_release_from_bootstrap_session() -> None:
    bootstrap = BOOTSTRAP_ENTRY.read_text(encoding="utf-8")
    runtime = RUNTIME_ENTRY.read_text(encoding="utf-8")
    helper = MODULE_PATH.read_text(encoding="utf-8")
    assert "build_teamagent_image.sh" not in bootstrap
    assert "build_openclaw_image.sh" not in bootstrap
    assert "authorize_image_release.sh" not in bootstrap
    assert 'git -C "$SCRIPT_DIR" rev-parse' not in bootstrap
    assert 'python3 -I "$PROVENANCE_HELPER"' in bootstrap
    assert "--profile bootstrap-iam" in bootstrap
    assert "build_teamagent_image.sh" not in runtime
    assert "authorize_image_release.sh" not in runtime
    assert 'EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/' in runtime
    assert 'ROOT_ARN="arn:aws:iam::718959508629:root"' in runtime
    assert "sign-alarm-ack)" in runtime
    assert "teamagent-dev-alarm-recipient-ack-signer" in runtime
    assert "teamagent-dev-media-cutover-attestor" in runtime
    assert "teamagent-production-media-cutover-attestor" in runtime
    assert "--profile runtime-session" in runtime
    assert runtime.index("unset AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE") < runtime.index(
        'bash "$GUARD" "$@"'
    )
    for command in (
        "snapshot",
        "attest-log-versioning",
        "issue-alarm-challenge",
        "attest-alarm-delivery",
        "advance-alarm-migration",
        "prepare-media-cutover",
        "attest-media-cutover",
        "authorize-media-apply",
        "attest-log-readiness",
        "preflight",
        "review-plan",
        "plan",
        "verify",
        "apply",
    ):
        assert command in runtime
    assert 'bash "$GUARD" "$@"' in runtime
    assert "terraform state rm" not in helper
    assert "terraform import" not in helper
    assert '"build_or_release_invoked": False' in helper


def test_wrappers_verify_fresh_detached_transitive_checkout_before_sts() -> None:
    helper = WRAPPER_PROVENANCE_PATH.read_text(encoding="utf-8")
    assert "refs/remotes/origin/dev" in helper
    assert "refs/heads/dev" in helper
    assert "git@github.com:noirelumiere00/TeamAgent.git" in helper
    assert "https://github.com/noirelumiere00/TeamAgent.git" in helper
    assert '"symbolic-ref", "--quiet", "HEAD"' in helper
    assert '"ls-remote"' in helper
    assert '"worktree"' in helper and '"--detach"' in helper
    assert "transitive_child_sha256" in helper
    assert "_chmod_immutable(checkout_dir)" in helper
    assert "_chmod_immutable(bare)" in helper

    for entrypoint in (BOOTSTRAP_ENTRY, PROVENANCE_ENTRY, RUNTIME_ENTRY):
        body = entrypoint.read_text(encoding="utf-8")
        provenance = body.index("wrapper_provenance.py")
        materialize = body.index("--checkout-dir")
        sts = body.find("aws sts ")
        if sts >= 0:
            assert provenance < materialize < sts
        assert "HEAD^{commit}" in body
        assert "hash-object --no-filters" in body
        assert "unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN" in body
        assert re.search(r"^unset [^\n]*\bGIT_DIR\b", body, re.MULTILINE)
        assert "export GIT_CONFIG_GLOBAL=/dev/null" in body

    assert {
        "infra/bootstrap/provenance_iam_bootstrap.py",
        "infra/bootstrap/seed-stack.yaml",
        "infra/deploy/bootstrap_provenance_iam.sh",
    } <= set(WRAPPER_PROVENANCE.PROFILE_CHILDREN["bootstrap-iam"])
    assert {
        "infra/deploy/bootstrap_provenance_session.sh",
        "infra/deploy/authorize_image_release.sh",
        "infra/deploy/build_teamagent_image.sh",
        "infra/deploy/build_openclaw_image.sh",
        "infra/deploy/build_tiktok_image.sh",
    } <= set(WRAPPER_PROVENANCE.PROFILE_CHILDREN["provenance-session"])
    assert {
        "infra/deploy/bootstrap_runtime_session.sh",
        "infra/deploy/deployment_apply_finalizer.py",
        "infra/deploy/media_cutover_apply_authorizer.py",
        "infra/deploy/terraform_runtime_guard.sh",
        "infra/deploy/terraform_plan_contract.py",
        "infra/deploy/run_image_deployment_gate.sh",
        "infra/codebuild/release_evidence.py",
        "infra/terraform/ecs_service_apply_saga.py",
        "infra/terraform/eventbridge_apply_saga.py",
    } <= set(WRAPPER_PROVENANCE.PROFILE_CHILDREN["runtime-session"])


@pytest.mark.parametrize(
    "environment",
    [
        {"AWS_PROFILE": "hostile"},
        {"AWS_ACCESS_KEY_ID": "hostile"},
        {"GIT_DIR": "/tmp/hostile.git"},
        {"GIT_CONFIG_COUNT": "1"},
        {"GIT_CONFIG_KEY_0": "url.file:///tmp/.insteadOf"},
        {"GIT_CURL_VERBOSE": "1"},
        {"GIT_SSL_NO_VERIFY": "1"},
        {"GIT_TRACE": "1"},
        {"GIT_TRACE_CURL": "1"},
        {"GIT_TRACE_CURL_NO_DATA": "1"},
        {"GIT_TRACE_PACKET": "1"},
        {"GIT_TRACE_REDACT": "1"},
        {"SSH_AUTH_SOCK": "/tmp/agent.sock"},
    ],
)
def test_wrapper_provenance_rejects_credentials_and_git_redirects(
    environment: dict[str, str],
) -> None:
    with pytest.raises(WRAPPER_PROVENANCE.ProvenanceError):
        WRAPPER_PROVENANCE._git_env(
            {
                "PATH": os.environ["PATH"],
                **environment,
            }
        )


@pytest.mark.parametrize("token", [None, ""])
def test_wrapper_http_auth_args_are_empty_without_token(token: str | None) -> None:
    env = {} if token is None else {WRAPPER_PROVENANCE.BOOTSTRAP_GIT_TOKEN_ENV: token}
    assert WRAPPER_PROVENANCE._http_auth_args(env) == []


def test_wrapper_http_auth_args_use_host_limited_basic_auth() -> None:
    token = "read-only-token"
    basic = base64.b64encode(b"x-access-token:" + token.encode()).decode()

    assert WRAPPER_PROVENANCE._http_auth_args(
        {WRAPPER_PROVENANCE.BOOTSTRAP_GIT_TOKEN_ENV: token}
    ) == [
        "-c",
        "http.https://github.com/.extraHeader=Authorization: Basic " + basic,
    ]


def test_wrapper_http_auth_args_strip_surrounding_whitespace() -> None:
    token = "read-only-token"
    basic = base64.b64encode(b"x-access-token:" + token.encode()).decode()

    assert WRAPPER_PROVENANCE._http_auth_args(
        {WRAPPER_PROVENANCE.BOOTSTRAP_GIT_TOKEN_ENV: f"  {token}  "}
    ) == [
        "-c",
        "http.https://github.com/.extraHeader=Authorization: Basic " + basic,
    ]


def test_wrapper_http_auth_args_reject_control_characters() -> None:
    with pytest.raises(WRAPPER_PROVENANCE.ProvenanceError, match="control characters"):
        WRAPPER_PROVENANCE._http_auth_args(
            {WRAPPER_PROVENANCE.BOOTSTRAP_GIT_TOKEN_ENV: "read-only\ntoken"}
        )


def test_blocked_contract_cannot_mint_root_provenance_session(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    aws_log = tmp_path / "aws.log"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$AWS_CALL_LOG"\nexit 99\n',
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(PROVENANCE_ENTRY), "teamagent"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AWS_CALL_LOG": str(aws_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert not aws_log.exists()
    assert any(
        marker in completed.stderr
        for marker in (
            "not ready",
            "release is blocked",
            "provenance",
            "detached",
        )
    )


def test_root_uses_exact_preassumed_launcher_sessions_not_direct_build_calls() -> None:
    terraform = CODEBUILD_TF.read_text(encoding="utf-8")
    wrapper = PROVENANCE_ENTRY.read_text(encoding="utf-8")
    assert terraform.count('identifiers = ["arn:aws:iam::718959508629:root"]') >= 4
    assert terraform.count('variable = "aws:MultiFactorAuthPresent"') >= 4
    assert terraform.count('variable = "sts:RoleSessionName"') >= 4
    assert terraform.count('variable = "sts:SourceIdentity"') >= 4
    assert 'ROOT_ARN="arn:aws:iam::718959508629:root"' in wrapper
    assert 'git -C "$SCRIPT_DIR" rev-parse' not in wrapper
    assert 'python3 -I "$CONTRACT_HELPER"' in wrapper
    assert "--source-identity" in wrapper
    assert "--duration-seconds 10800" in wrapper
    assert "release requires exactly one --pipeline" in wrapper
    assert "codebuild start-build" not in wrapper
    assert "ecr put-image" not in wrapper
    for launcher in (BUILD_TEAMAGENT, BUILD_OPENCLAW, BUILD_TIKTOK, AUTHORIZE):
        body = launcher.read_text(encoding="utf-8")
        assert "PREASSUMED_" in body
        assert "exact pinned STS" in body
        assert "arn:aws:iam::718959508629:root" not in body


@pytest.mark.parametrize("launcher", [BUILD_TEAMAGENT, BUILD_OPENCLAW])
def test_builds_gate_contract_before_aws_and_connection_before_writes(
    launcher: Path,
) -> None:
    body = launcher.read_text(encoding="utf-8")
    ready = body.index("assert-release-ready")
    identity = body.index("aws sts get-caller-identity")
    connection = body.index("assert_source_connection_available")
    first_evidence_write_candidates = [
        index
        for token in ("s3api put-object", "codebuild start-build")
        if (index := body.find(token)) >= 0
    ]
    assert ready < identity < connection
    assert first_evidence_write_candidates
    assert connection < min(first_evidence_write_candidates)
    assert "codeconnections list-connections" in body
    assert "--provider-type-filter" not in body
    assert 'ConnectionStatus:"AVAILABLE"' in body
    assert '(.NextToken // "") == ""' in body


def test_launcher_policies_allow_read_only_connection_preflight() -> None:
    body = CODEBUILD_TF.read_text(encoding="utf-8")
    assert body.count('"codeconnections:ListConnections"') >= 3
    assert "RequireAvailableTeamAgentCodeConnection" in body
    assert "RequireAvailableOpenClawCodeConnection" in body
    assert "RequireAvailableTikTokCodeConnection" in body
    assert "codeconnections:CreateConnection" not in "\n".join(
        line
        for line in body.splitlines()
        if "RequireAvailable" in line or "codeconnections:" in line
    )


def test_authorize_remains_blocked_before_aws_when_release_is_not_ready() -> None:
    body = AUTHORIZE.read_text(encoding="utf-8")
    assert body.index("release.ready is false") < body.index("aws sts get-caller-identity")
    assert 'EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/teamagent-release-caller"' in body
    assert "arn:aws:iam::718959508629:root" not in body
