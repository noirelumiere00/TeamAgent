"""OpenClaw の Haiku 5.5 移行に向けた IAM の範囲と、MCP のモデル分離を固定する。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
HAIKU_45 = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_55 = "jp.anthropic.claude-haiku-5-5"


def _variable(name: str) -> str:
    body = (TF_ROOT / "variables_fargate.tf").read_text(encoding="utf-8")
    return body.split(f'variable "{name}" {{', 1)[1].split("\n}\n", 1)[0]


def _local_list(body: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {name} = (?:distinct\()?\[(.*?)^  \]",
        body,
    )
    assert match is not None, f"missing local list: {name}"
    return match.group(1)


def test_openclaw_default_stays_haiku_45_until_the_switch_pr() -> None:
    """権限だけ先に足す段階。既定モデルの切替は Bedrock の枠が通った後の別 PR で行う。"""
    variable = _variable("openclaw_model_id")
    assert re.search(rf'^  default\s*=\s*"{re.escape(HAIKU_45)}"$', variable, re.M)
    assert f'condition     = var.openclaw_model_id == "{HAIKU_45}"' in variable


def test_openclaw_iam_keeps_both_exact_jp_profiles_and_backing_models() -> None:
    body = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    profiles = _local_list(body, "openclaw_bedrock_profile_arns")
    assert "local.openclaw_bedrock_profile_arn" in profiles
    assert re.findall(r'"([^"]+)"', profiles) == [
        f"arn:aws:bedrock:${{var.aws_region}}:${{local.account_id}}:inference-profile/{model}"
        for model in (HAIKU_45, HAIKU_55)
    ]
    backing_models = _local_list(body, "openclaw_bedrock_backing_model_arns")
    assert re.findall(r'"([^"]+)"', backing_models) == [
        f"arn:aws:bedrock:{region}::foundation-model/{model.removeprefix('jp.')}"
        for model in (HAIKU_45, HAIKU_55)
        for region in ("ap-northeast-1", "ap-northeast-3")
    ]
    policy = body.split('data "aws_iam_policy_document" "openclaw_task" {', 1)[1]
    policy = policy.split('\nresource "aws_iam_role" "openclaw_task"', 1)[0]
    assert "resources = local.openclaw_bedrock_profile_arns" in policy
    assert "resources = local.openclaw_bedrock_backing_model_arns" in policy
    assert 'variable = "bedrock:InferenceProfileArn"' in policy
    assert "values   = local.openclaw_bedrock_profile_arns" in policy
    assert policy.count('["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]') == 2


def test_mcp_keeps_haiku_45_model_and_does_not_receive_55_permissions() -> None:
    variable = _variable("mcp_model_id")
    assert re.search(rf'^  default\s*=\s*"{re.escape(HAIKU_45)}"$', variable, re.M)
    assert f'condition     = var.mcp_model_id == "{HAIKU_45}"' in variable
    body = (TF_ROOT / "fargate.tf").read_text(encoding="utf-8")
    mcp_models = body.split("  haiku_inference_profile_arn = (", 1)[1]
    mcp_models = mcp_models.split("  openclaw_bedrock_profile_arn = (", 1)[0]
    assert "inference-profile/${var.mcp_model_id}" in mcp_models
    assert 'foundation-model/${trimprefix(var.mcp_model_id, "jp.")}' in mcp_models
    assert "claude-haiku-5-5" not in mcp_models
    mcp_resources = body.split("  bedrock_resources = concat(", 1)[1]
    mcp_resources = mcp_resources.split("\n  lambda_bedrock_resources = concat(", 1)[0]
    assert "openclaw" not in mcp_resources
