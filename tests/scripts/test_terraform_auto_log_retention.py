"""Auto-created CodeBuild/Lambda log groupの安全なin-place adoption契約。"""

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
    ),
    "aws_cloudwatch_log_group.codebuild_image_builder": (
        "codebuild.tf",
        "codebuild_image_builder",
        "/aws/codebuild/${local.retired_codebuild_project_name}",
        "/aws/codebuild/teamagent-dev-image-builder",
    ),
    "aws_cloudwatch_log_group.reminder_notify": (
        "reminders.tf",
        "reminder_notify",
        "/aws/lambda/${local.rem_name}-notify",
        "/aws/lambda/teamagent-dev-reminders-notify",
    ),
    "aws_cloudwatch_log_group.tiktok_dispatch": (
        "tiktok_acquire.tf",
        "tiktok_dispatch",
        "/aws/lambda/${local.tk_name}-dispatch",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
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
    spec: tuple[str, str, str, str],
) -> None:
    filename, resource, configured_name, import_id = spec
    path = TF_ROOT / filename
    block = _hcl_block(path, 'resource "aws_cloudwatch_log_group"', resource)
    body = path.read_text(encoding="utf-8")

    assert f'name              = "{configured_name}"' in block
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
    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    allowed = migration["allowed_changes"]
    guard = GUARD.read_text(encoding="utf-8")
    readme = (TF_ROOT / "README.md").read_text(encoding="utf-8")

    for address, (_, _, _, import_id) in LOG_GROUPS.items():
        assert allowed.count(address) == 1
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
        r"\n  jq -e '(?P<filter>.*?)'\s+\"\$plan_json\" >/dev/null \|\|"
        r".*?\n\}",
        body,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group("filter")


def _exact_plan() -> dict[str, object]:
    changes: list[dict[str, object]] = []
    for address, (_, resource, _, import_id) in LOG_GROUPS.items():
        before = {
            "name": import_id,
            "retention_in_days": 0,
            "kms_key_id": None,
            "log_group_class": "STANDARD",
            "skip_destroy": False,
            "tags": {},
            "tags_all": {},
        }
        after = copy.deepcopy(before)
        after["retention_in_days"] = 30
        after["tags"] = copy.deepcopy(MANAGED_TAGS)
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
) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is unavailable")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return subprocess.run(
        [jq, "-e", _validator_filter(), str(plan_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_exact_in_place_retention_plan_is_accepted(tmp_path: Path) -> None:
    result = _run_validator(tmp_path, _exact_plan())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "create",
        "replace",
        "wrong_import",
        "wrong_before_retention",
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
