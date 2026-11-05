"""カルテの直接投稿は MCP だけへ配線し、既定 OFF・空の許可リストを保つ。"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "infra/terraform"


@pytest.mark.parametrize(
    "env,variable,default",
    [
        ("USE_DIRECT_KARTE_POST", "use_direct_karte_post", "0"),
        ("DIRECT_KARTE_POST_ALLOWED_EMAILS", "direct_karte_post_allowed_emails", ""),
    ],
)
def test_direct_karte_env_is_wired_and_denied_by_default(
    env: str, variable: str, default: str
) -> None:
    fargate = (ROOT / "fargate.tf").read_text()
    mcp = fargate.split('resource "aws_ecs_task_definition" "mcp" {', 1)[1].split("\nresource ", 1)[
        0
    ]
    line = f'{{ name = "{env}", value = var.{variable} }}'
    assert mcp.count(line) == 1 and fargate.count(line) == 1
    variables = (ROOT / "variables_fargate.tf").read_text()
    block = variables.split(f'variable "{variable}" {{', 1)[1].split("\n}", 1)[0]
    assert "type        = string" in block
    assert f'default     = "{default}"' in block
    example = (ROOT / "terraform.tfvars.example").read_text()
    assert any(line.split() == [variable, "=", f'"{default}"'] for line in example.splitlines())
