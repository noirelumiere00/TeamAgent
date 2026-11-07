"""資料配信のフラグは MCP にだけ配線し、既定では公開しない。"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "infra/terraform"


@pytest.mark.parametrize(
    "env,variable,default",
    [
        ("USE_SEARCH_FILE_DELIVERY", "use_search_file_delivery", "0"),
        ("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", "search_file_delivery_allowed_emails", ""),
    ],
)
def test_search_file_delivery_env_flag_and_tfvars_deny_by_default(env, variable, default):
    fargate = (ROOT / "fargate.tf").read_text()
    mcp = fargate.split('resource "aws_ecs_task_definition" "mcp" {', 1)[1].split("\nresource ", 1)[
        0
    ]
    line = f'{{ name = "{env}", value = var.{variable} }}'
    assert mcp.count(line) == fargate.count(line) == 1
    variables = (ROOT / "variables_fargate.tf").read_text()
    block = variables.split(f'variable "{variable}" {{', 1)[1].split("\n}", 1)[0]
    assert "type        = string" in block and f'default     = "{default}"' in block
    example = (ROOT / "terraform.tfvars.example").read_text()
    assert any(line.split() == [variable, "=", f'"{default}"'] for line in example.splitlines())
