"""Terraform must fail closed to the configured Slack workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
TEAM_ID_ENV = '{ name = "SLACK_TEAM_ID", value = var.slack_team_id },'


def _task_definition_block(filename: str, resource_name: str) -> str:
    body = (TF_ROOT / filename).read_text(encoding="utf-8")
    marker = f'resource "aws_ecs_task_definition" "{resource_name}"'
    start = body.index(marker)
    opening = body.index("{", start)
    depth = 0
    for index in range(opening, len(body)):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
            if depth == 0:
                return body[start : index + 1]
    raise AssertionError(f"unterminated Terraform block: {filename}:{resource_name}")


@pytest.mark.parametrize(
    ("filename", "resource_name"),
    [
        ("fargate.tf", "mcp"),
        ("fargate.tf", "openclaw"),
        ("connect_web.tf", "connect_web"),
        ("canary_schedule.tf", "canary"),
    ],
)
def test_known_slack_identity_tasks_receive_the_workspace_id(
    filename: str,
    resource_name: str,
) -> None:
    block = _task_definition_block(filename, resource_name)
    assert block.count(TEAM_ID_ENV) == 1


def test_slack_team_id_is_a_required_canonical_production_value() -> None:
    variables = (TF_ROOT / "variables_fargate.tf").read_text(encoding="utf-8")
    assert 'variable "slack_team_id"' in variables
    assert 'can(regex("^T[A-Z0-9]{8,}$", var.slack_team_id))' in variables
    assert "本番必須" in variables
