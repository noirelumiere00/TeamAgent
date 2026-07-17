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
