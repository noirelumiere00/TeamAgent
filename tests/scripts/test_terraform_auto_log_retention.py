"""既存の通常運用log groupを安全にin-place adoptionする契約。"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TF_ROOT = PROJECT_ROOT / "infra/terraform"
GUARD = PROJECT_ROOT / "infra/deploy/terraform_runtime_guard.sh"
MIGRATIONS = PROJECT_ROOT / "infra/deploy/terraform_runtime_migrations.json"
MANAGED_TAGS = {
    "Environment": "dev",
    "ManagedBy": "Terraform",
    "Project": "TeamAgent",
    "Version": "v3.0",
}
LOG_GROUPS = {
    "aws_cloudwatch_log_group.codebuild_aiia_image_builder": (
        "codebuild.tf",
        "codebuild_aiia_image_builder",
        "/aws/codebuild/${var.project_name}-${var.environment}-aiia-image-builder",
        "/aws/codebuild/teamagent-dev-aiia-image-builder",
        0,
    ),
    "aws_cloudwatch_log_group.codebuild_image": (
        "codebuild.tf",
        "codebuild_image",
        "/aws/codebuild/${local.main_codebuild_project_name}",
        "/aws/codebuild/teamagent-dev-image-builder",
        0,
    ),
    "aws_cloudwatch_log_group.ecs_containerinsights_teamagent": (
        "container_insights_retention.tf",
        "ecs_containerinsights_teamagent",
        "/aws/ecs/containerinsights/teamagent-dev/performance",
        "/aws/ecs/containerinsights/teamagent-dev/performance",
        1,
    ),
    "aws_cloudwatch_log_group.ecs_containerinsights_tiktok": (
        "container_insights_retention.tf",
        "ecs_containerinsights_tiktok",
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
        1,
    ),
    "aws_cloudwatch_log_group.reminder_notify": (
        "reminders.tf",
        "reminder_notify",
        "/aws/lambda/${local.rem_name}-notify",
        "/aws/lambda/teamagent-dev-reminders-notify",
        0,
    ),
    "aws_cloudwatch_log_group.tiktok_dispatch": (
        "tiktok_acquire.tf",
        "tiktok_dispatch",
        "/aws/lambda/${local.tk_name}-dispatch",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
        0,
    ),
    "aws_cloudwatch_log_group.x_dispatch": (
        "x_research.tf",
        "x_dispatch",
        "/aws/lambda/${local.xr_name}-dispatch",
        "/aws/lambda/teamagent-dev-x-buzz-dispatch",
        0,
    ),
}


def _hcl_block(path: Path, kind: str, name: str) -> str:
    body = path.read_text(encoding="utf-8")
    match = re.search(
        rf'{re.escape(kind)} "{re.escape(name)}" \{{',
        body,
    )
    assert match is not None, f"{kind}.{name}が見つかりません"
    start = match.start()
    opening = body.index("{", match.start())
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(body)):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[start : index + 1]
    raise AssertionError(f"{kind}.{name}の閉じ括弧がありません")


@pytest.mark.parametrize(
    ("address", "spec"),
    LOG_GROUPS.items(),
)
def test_existing_log_groups_are_adopted_without_recreation(
    address: str,
    spec: tuple[str, str, str, str, int],
) -> None:
    filename, resource, configured_name, import_id, _ = spec
    path = TF_ROOT / filename
    block = _hcl_block(path, 'resource "aws_cloudwatch_log_group"', resource)
    body = path.read_text(encoding="utf-8")

    assert f'name              = "{configured_name}"' in block
    if filename == "codebuild.tf":
        assert "retention_in_days = local.codebuild_log_retention_days" in block
        assert "codebuild_log_retention_days  = 30" in body
    else:
        assert "retention_in_days = 30" in block
    assert "depends_on = [terraform_data.runtime_guard]" in block
    assert "prevent_destroy = true" in block
    assert re.search(r"ignore_changes\s*=\s*\[kms_key_id\]", block)
    assert re.search(r"(?m)^\s*(count|for_each)\s*=", block) is None
    assert re.search(
        rf"import \{{\s*to = {re.escape(address)}\s*id = {re.escape(json.dumps(import_id))}\s*\}}",
        body,
        flags=re.DOTALL,
    )


def test_lambda_creation_waits_for_adopted_log_groups() -> None:
    reminders = _hcl_block(
        TF_ROOT / "reminders.tf",
        'resource "aws_lambda_function"',
        "reminder_notify",
    )
    tiktok = _hcl_block(
        TF_ROOT / "tiktok_acquire.tf",
        'resource "aws_lambda_function"',
        "tiktok_dispatch",
    )
    assert "aws_cloudwatch_log_group.reminder_notify" in reminders
    assert "aws_cloudwatch_log_group.tiktok_dispatch" in tiktok


def test_migration_allowlist_guard_and_runbook_cover_exact_adoption_addresses() -> None:
    migrations = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"]
    migration = migrations["2026-07-wolfi-runtime-v1"]
    assert "allowed_changes" not in migration
    if migration["enabled"]:
        reviewed_addresses = {
            row["address"] for row in migration["reviewed_plan"]["resource_changes"]
        }
    else:
        assert migration["reviewed_plan"] is None
        reviewed_addresses = set()
    guard = GUARD.read_text(encoding="utf-8")
    readme = (TF_ROOT / "README.md").read_text(encoding="utf-8")

    for address, (_, _, _, import_id, _) in LOG_GROUPS.items():
        if migration["enabled"]:
            assert address in reviewed_addresses
        assert address in guard
        assert address in readme
        assert import_id in guard
        assert import_id in readme
    assert "validate_auto_created_log_retention_plan" in guard
    assert guard.count('validate_auto_created_log_retention_plan "$plan_json"') == 1
    assert "KMS不変" in readme
    assert "state所有者" in readme
    assert "30日より古い既存event" in readme
    assert "最大72時間" in readme
    assert "checksum付きで保全" in readme


def _validator_filter() -> str:
    body = GUARD.read_text(encoding="utf-8")
    match = re.search(
        r"validate_auto_created_log_retention_plan\(\) \{.*?"
        r'\n  jq -e --slurpfile ownership "\$state_contract" '
        r"'(?P<filter>.*?)'\s+\"\$plan_json\" >/dev/null \|\|"
        r".*?\n\}",
        body,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group("filter")


def _exact_plan() -> dict[str, object]:
    changes: list[dict[str, object]] = []
    for address, (_, resource, _, import_id, initial_retention) in LOG_GROUPS.items():
        before = {
            "name": import_id,
            "retention_in_days": initial_retention,
            "kms_key_id": None,
            "log_group_class": "STANDARD",
            "skip_destroy": False,
            "tags": {},
            "tags_all": {},
        }
        after = copy.deepcopy(before)
        after["retention_in_days"] = 30
        after["tags"] = None
        after["tags_all"] = copy.deepcopy(MANAGED_TAGS)
        changes.append(
            {
                "address": address,
                "mode": "managed",
                "type": "aws_cloudwatch_log_group",
                "name": resource,
                "change": {
                    "actions": ["update"],
                    "before": before,
                    "after": after,
                    "after_unknown": {},
                    "importing": {"id": import_id},
                },
            }
        )
    return {"resource_changes": changes}


def _run_validator(
    tmp_path: Path,
    plan: dict[str, object],
    *,
    present: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is unavailable")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    present = present or set()
    ownership = {
        "imports": {
            address: {
                "expected_id": spec[3],
                "present": address in present,
            }
            for address, spec in LOG_GROUPS.items()
        }
    }
    ownership_path = tmp_path / "ownership.json"
    ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
    return subprocess.run(
        [
            jq,
            "-e",
            "--slurpfile",
            "ownership",
            str(ownership_path),
            _validator_filter(),
            str(plan_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_exact_in_place_retention_plan_is_accepted(tmp_path: Path) -> None:
    result = _run_validator(tmp_path, _exact_plan())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("state_present", "current_retention"), [(False, 30), (True, 0), (True, 30)]
)
def test_partial_import_migration_resumes_idempotently(
    tmp_path: Path,
    state_present: bool,
    current_retention: int,
) -> None:
    plan = _exact_plan()
    address = next(iter(LOG_GROUPS))
    change = plan["resource_changes"][0]["change"]  # type: ignore[index]
    change["before"]["retention_in_days"] = current_retention  # type: ignore[index]
    if current_retention == 30:
        change["after"] = copy.deepcopy(change["before"])  # type: ignore[index]
        change["after"]["tags"] = None  # type: ignore[index]
        change["after"]["tags_all"] = copy.deepcopy(MANAGED_TAGS)  # type: ignore[index]
        change["actions"] = ["no-op"]  # type: ignore[index]
    if state_present:
        change.pop("importing", None)  # type: ignore[union-attr]
    result = _run_validator(
        tmp_path,
        plan,
        present={address} if state_present else set(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "create",
        "replace",
        "wrong_import",
        "wrong_before_retention",
        "wrong_container_initial_retention",
        "wrong_after_retention",
        "kms_change",
        "name_change",
        "other_attribute_change",
        "missing",
        "duplicate",
    ],
)
def test_retention_validator_rejects_non_exact_or_destructive_plan(
    tmp_path: Path,
    mutation: str,
) -> None:
    plan = _exact_plan()
    changes = plan["resource_changes"]
    assert isinstance(changes, list)
    target = changes[0]
    assert isinstance(target, dict)
    change = target["change"]
    assert isinstance(change, dict)

    if mutation == "create":
        change["actions"] = ["create"]
        change["before"] = None
    elif mutation == "replace":
        change["actions"] = ["delete", "create"]
    elif mutation == "wrong_import":
        change["importing"] = {"id": "/aws/codebuild/wrong"}
    elif mutation == "wrong_before_retention":
        change["before"]["retention_in_days"] = 30
    elif mutation == "wrong_container_initial_retention":
        target = next(
            item
            for item in changes
            if item["address"] == "aws_cloudwatch_log_group.ecs_containerinsights_teamagent"
        )
        target["change"]["before"]["retention_in_days"] = 0
    elif mutation == "wrong_after_retention":
        change["after"]["retention_in_days"] = 7
    elif mutation == "kms_change":
        change["after"]["kms_key_id"] = "arn:aws:kms:ap-northeast-1:718959508629:key/wrong"
    elif mutation == "name_change":
        change["after"]["name"] = "/aws/codebuild/wrong"
    elif mutation == "other_attribute_change":
        change["after"]["log_group_class"] = "INFREQUENT_ACCESS"
    elif mutation == "missing":
        changes.pop(0)
    elif mutation == "duplicate":
        changes.append(copy.deepcopy(target))
    else:
        raise AssertionError(f"unknown mutation: {mutation}")

    result = _run_validator(tmp_path, plan)
    assert result.returncode != 0
