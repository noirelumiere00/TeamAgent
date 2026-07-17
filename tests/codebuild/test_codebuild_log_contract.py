from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
LOG_PRODUCERS = (
    ROOT / "infra" / "codebuild" / "buildspec.yml",
    ROOT / "infra" / "codebuild" / "tiktok-buildspec.yml",
    ROOT / "infra" / "codebuild" / "openclaw-provenance-buildspec.yml",
    ROOT / "infra" / "deploy" / "build_teamagent_image.sh",
    ROOT / "infra" / "deploy" / "build_openclaw_image.sh",
)


def _resource(body: str, kind: str, name: str) -> str:
    marker = f'resource "{kind}" "{name}" {{'
    return body.split(marker, maxsplit=1)[1].split("\n}", maxsplit=1)[0]


def test_all_codebuild_log_groups_have_explicit_thirty_day_retention() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")

    assert re.search(r"^\s*codebuild_log_retention_days\s+= 30$", body, re.MULTILINE)
    assert 'var.project_name == "teamagent"' in body
    assert 'var.environment == "dev"' in body
    expected = {
        "codebuild_image": "/aws/codebuild/${local.main_codebuild_project_name}",
        "codebuild_tiktok_image": ("/aws/codebuild/${local.tiktok_codebuild_project_name}"),
        "codebuild_openclaw_provenance": (
            "/aws/codebuild/${local.openclaw_codebuild_project_name}"
        ),
        "codebuild_aiia_image_legacy": "/aws/codebuild/aiia-image-builder",
    }
    for resource_name, log_group_name in expected.items():
        resource = _resource(body, "aws_cloudwatch_log_group", resource_name)
        assert f'name              = "{log_group_name}"' in resource
        assert "retention_in_days = local.codebuild_log_retention_days" in resource

    for project, log_group in (
        ("image", "aws_cloudwatch_log_group.codebuild_image.name"),
        ("tiktok_image", "aws_cloudwatch_log_group.codebuild_tiktok_image[0].name"),
        (
            "openclaw_provenance",
            "aws_cloudwatch_log_group.codebuild_openclaw_provenance.name",
        ),
    ):
        project_body = _resource(body, "aws_codebuild_project", project)
        assert f"group_name = {log_group}" in project_body
    assert '"logs:CreateLogGroup"' not in body


def test_codebuild_paths_do_not_enable_trace_or_print_credentials() -> None:
    credential_names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_CREDENTIAL_EXPIRATION",
    )
    for path in LOG_PRODUCERS:
        body = path.read_text(encoding="utf-8")
        assert not re.search(r"(?m)^\s*set\s+(?:-[^\n ]*x\b|-o\s+xtrace\b)", body)
        assert not re.search(r"(?m)^\s*(?:printenv|env|export\s+-p|declare\s+-p)(?:\s|$)", body)
        assert "--debug" not in body
        for credential_name in credential_names:
            assert not re.search(
                rf"(?m)^\s*(?:echo|printf)\b[^\n]*"
                rf"\$(?:\{{{credential_name}\}}|{credential_name}\b)",
                body,
            )


def test_ecr_passwords_are_piped_only_to_fixed_password_stdin_logins() -> None:
    for path in LOG_PRODUCERS[:3]:
        body = path.read_text(encoding="utf-8")
        assert body.count("ecr get-login-password") == 1
        login = body.split("ecr get-login-password", maxsplit=1)[1].split("\n", maxsplit=2)
        assert "--password-stdin" in "\n".join(login)
