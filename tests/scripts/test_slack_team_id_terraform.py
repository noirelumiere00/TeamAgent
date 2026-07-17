"""Terraform must fail closed to the configured Slack workspace."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
TEAM_ID_ENV = '{ name = "SLACK_TEAM_ID", value = var.slack_team_id },'


def test_all_slack_identity_consumers_receive_the_workspace_id() -> None:
    for filename in ("fargate.tf", "connect_web.tf", "canary_schedule.tf"):
        body = (TF_ROOT / filename).read_text(encoding="utf-8")
        assert body.count(TEAM_ID_ENV) == 1, filename
