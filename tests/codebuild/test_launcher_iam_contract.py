from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
README = ROOT / "infra" / "terraform" / "README.md"


def _launcher_section() -> str:
    body = TERRAFORM.read_text(encoding="utf-8")
    return body.split("# Human launcher boundary", maxsplit=1)[1].split(
        "# TikTok worker", maxsplit=1
    )[0]


def test_launcher_role_trust_and_resources_are_exact() -> None:
    body = _launcher_section()

    assert 'user_name = "AIIAdev"' in body
    assert "identifiers = [data.aws_iam_user.aiia_dev.arn]" in body
    assert "name                 = local.launcher_role_name" in body
    assert re.search(
        r'^\s*launcher_role_name\s+= "teamagent-dev-codebuild-launcher"$',
        TERRAFORM.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert "max_session_duration = 10800" in body
    for resource in (
        "arn:aws:s3:::teamagent-dev-raw-files",
        "arn:aws:s3:::teamagent-dev-raw-files/codebuild/source.zip",
        "arn:aws:s3:::teamagent-dev-raw-files/codebuild/connect-web-app.html",
        "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp",
        "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp-quarantine",
    ):
        assert resource in body
    assert "arn:aws:s3:::teamagent-dev-raw-files/codebuild/*" not in body


def test_start_build_allows_only_exact_provenance_environment_names() -> None:
    whole = TERRAFORM.read_text(encoding="utf-8")
    body = _launcher_section()
    base_names = {
        "GIT_COMMIT",
        "GIT_BRANCH",
        "IMAGE_TAG",
        "WITH_SCRAPE_TOOLS",
        "APP_HTML_VERSION_ID",
        "APP_HTML_SHA256",
        "RUNTIME_CONTRACT_SHA256",
    }

    assert 'test     = "ForAllValues:StringEquals"' in body
    assert 'variable = "codebuild:environment.environmentVariables.name"' in body
    for name in base_names:
        assert whole.count(f'"{name}"') >= 1
    assert re.search(r'^\s*GIT_BRANCH\s+= "dev"$', whole, re.MULTILINE)
    assert re.search(r'^\s*WITH_SCRAPE_TOOLS\s+= "true"$', whole, re.MULTILINE)
    assert 'variable = "codebuild:environment.environmentVariables/${condition.key}.value"' in body


def test_dangerous_overrides_and_alternate_entry_points_are_explicitly_denied() -> None:
    whole = TERRAFORM.read_text(encoding="utf-8")
    body = _launcher_section()
    keys = {
        "codebuild:source",
        "codebuild:source.buildspec",
        "codebuild:source.buildStatusConfig.context",
        "codebuild:source.buildStatusConfig.targetUrl",
        "codebuild:source.location",
        "codebuild:secondarySources",
        "codebuild:artifacts",
        "codebuild:secondaryArtifacts",
        "codebuild:environment.image",
        "codebuild:environment.type",
        "codebuild:environment.computeType",
        "codebuild:environment.privilegedMode",
        "codebuild:environment.registryCredential",
        "codebuild:environment.imagePullCredentialsType",
        "codebuild:logsConfig",
        "codebuild:cache",
        "codebuild:serviceRole",
        "codebuild:encryptionKey",
        "codebuild:autoRetryLimit",
    }

    for key in keys:
        assert f'"{key}"' in whole
    assert '"codebuild:timeoutInMinutes"' not in whole
    assert '"codebuild:queuedTimeoutInMinutes"' not in whole
    assert 'test     = "Null"' in body
    assert 'actions   = ["codebuild:StartBuild"]' in body
    for action in (
        "codebuild:RetryBuild",
        "codebuild:RetryBuildBatch",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
        "codebuild:StartSandboxConnection",
        "ssm:StartSession",
        "ssmmessages:*",
    ):
        assert f'"{action}"' in body


def test_launcher_ecr_access_is_read_only_and_direct_user_start_is_denied() -> None:
    body = _launcher_section()
    quarantine_read = body.split('sid = "ReadQuarantinedMcpDigest"', maxsplit=1)[1].split(
        "\n  }", maxsplit=1
    )[0]
    release_read = body.split('sid = "ReadPromotedMcpDigestAndConfig"', maxsplit=1)[1].split(
        "\n  }", maxsplit=1
    )[0]

    assert '"ecr:BatchGetImage"' in quarantine_read
    assert '"ecr:DescribeImages"' in quarantine_read
    assert '"ecr:GetDownloadUrlForLayer"' not in quarantine_read
    assert '"ecr:BatchGetImage"' in release_read
    assert '"ecr:DescribeImages"' in release_read
    assert '"ecr:GetDownloadUrlForLayer"' in release_read
    for write_action in (
        "ecr:PutImage",
        "ecr:BatchDeleteImage",
        "ecr:InitiateLayerUpload",
        "ecs:UpdateService",
        "events:PutTargets",
    ):
        assert write_action not in body
    assert re.search(r'^\s*sid\s+= "RequireDedicatedLauncherRole"$', body, re.MULTILINE)
    direct_deny = body.split('sid    = "RequireDedicatedLauncherRole"', maxsplit=1)[1].split(
        "\n  }", maxsplit=1
    )[0]
    for action in (
        "codebuild:StartBuild",
        "codebuild:RetryBuild",
        "codebuild:RetryBuildBatch",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
        "codebuild:StartSandboxConnection",
    ):
        assert f'"{action}"' in direct_deny
    assert 'resource "aws_iam_user_policy" "aiia_dev_no_direct_start_build"' in body


def test_root_key_rotation_is_documented_as_external_blocker() -> None:
    body = README.read_text(encoding="utf-8").lower()

    assert "root access keys" in body
    assert "external account-level blocker" in body
    assert "rotate/delete root keys" in body
