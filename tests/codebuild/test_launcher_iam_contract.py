from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
README = ROOT / "infra" / "terraform" / "README.md"


def _body() -> str:
    return TERRAFORM.read_text(encoding="utf-8")


def _document(name: str) -> str:
    body = _body()
    marker = f'data "aws_iam_policy_document" "{name}"'
    return body.split(marker, maxsplit=1)[1].split(
        '\nresource "aws_iam_role_policy"', maxsplit=1
    )[0]


def test_main_launcher_is_exact_assume_once_boundary_and_direct_start_is_denied() -> None:
    body = _body()
    policy = _document("codebuild_launcher")

    assert 'user_name = "AIIAdev"' in body
    assert "identifiers = [data.aws_iam_user.aiia_dev.arn]" in body
    assert 'launcher_role_name            = "teamagent-dev-codebuild-launcher"' in body
    assert "max_session_duration = 10800" in body
    assert "local.launcher_project_arn" in policy
    assert "local.launcher_all_project_arns" in policy
    assert "teamagent-dev-image-builder" in body
    assert "teamagent-dev-mcp-source-publisher" in body
    assert "teamagent-dev-image-attestor" in body
    assert "teamagent-dev-image-promoter" in body
    assert "teamagent-mcp-quarantine" in policy
    assert "teamagent-mcp-verified-candidates" in policy
    assert "repository/teamagent-mcp\"" not in policy
    assert "codebuild/source.zip" not in policy
    assert 'resource "aws_iam_user_policy" "aiia_dev_no_direct_start_build"' in body
    direct = _document("aiia_dev_no_direct_start_build")
    for action in (
        "codebuild:StartBuild",
        "codebuild:RetryBuild",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
    ):
        assert f'"{action}"' in direct


def test_start_build_environment_is_allowlisted_and_fixed_values_are_pinned() -> None:
    body = _body()
    policy = _document("codebuild_launcher")

    for name in (
        "GIT_COMMIT",
        "GIT_BRANCH",
        "WITH_SCRAPE_TOOLS",
        "APP_HTML_VERSION_ID",
        "APP_HTML_SHA256",
        "VAULT_MANIFEST_SHA256",
        "BUILD_INPUTS_SHA256",
        "RUNTIME_CONTRACT_SHA256",
        "SOURCE_ARCHIVE_VERSION_ID",
        "SOURCE_DECLARATION_VERSION_ID",
        "SOURCE_DECLARATION_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}"' in body
    assert '"IMAGE_TAG"' not in body
    assert 'test     = "ForAllValues:StringEquals"' in policy
    assert 'variable = "codebuild:environment.environmentVariables.name"' in policy
    assert 'GIT_BRANCH              = "dev"' in body
    assert 'WITH_SCRAPE_TOOLS       = "true"' in body
    assert "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee" in body
    assert "03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c" in body


def test_official_dangerous_override_condition_keys_are_explicit_denies() -> None:
    body = _body()
    policy = _document("codebuild_launcher")

    keys = {
        "codebuild:source",
        "codebuild:source.buildspec",
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
        assert f'"{key}"' in body
    assert '"codebuild:timeoutInMinutes"' not in body
    assert '"codebuild:queuedTimeoutInMinutes"' not in body
    assert 'for_each = local.launcher_denied_override_condition_keys' in policy
    assert 'test     = "Null"' in policy
    assert '"ssmmessages:*"' in policy


def test_all_fixed_build_roles_deny_non_plaintext_environment_resolution() -> None:
    for policy_name in (
        "codebuild",
        "tiktok_codebuild",
        "mcp_source_publisher",
        "image_attestor",
        "image_promoter",
        "openclaw_codebuild",
    ):
        policy = _document(policy_name)
        assert '"secretsmanager:GetSecretValue"' in policy
        assert '"ssm:GetParameter"' in policy
        assert '"ssm:GetParameters"' in policy


def test_only_source_free_promoter_can_write_release_repositories() -> None:
    body = _body()
    main_builder = _document("codebuild")
    tiktok_builder = _document("tiktok_codebuild")
    openclaw_builder = _document("openclaw_codebuild")
    attestor = _document("image_attestor")
    promoter = _document("image_promoter")

    for policy in (main_builder, tiktok_builder, openclaw_builder, attestor):
        assert "Deny" in policy
        assert "ecr:PutImage" in policy
    assert 'sid = "WriteOnlyAllowlistedCandidateAndReleaseRepositories"' in promoter
    for release in (
        "aws_ecr_repository.mcp.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
        "aws_ecr_repository.tiktok_acquire[0].arn",
    ):
        assert release in promoter
    assert '"kms:Sign"' in promoter
    assert '"s3:PutObject"' in promoter
    assert '"codeconnections:GetConnection"' in promoter
    source_free = body.split(
        'resource "aws_codebuild_project" "image_promoter"', maxsplit=1
    )[1].split('output "image_promoter_project"', maxsplit=1)[0]
    assert 'type      = "NO_SOURCE"' in source_free
    assert "privileged_mode = false" in source_free


def test_every_codebuild_service_trust_pins_source_account_and_project_arn() -> None:
    body = _body()
    trusts = body.split('identifiers = ["codebuild.amazonaws.com"]')[1:]

    assert len(trusts) == 6
    for trust in trusts:
        section = trust.split("\n  }\n}", maxsplit=1)[0]
        assert 'variable = "aws:SourceAccount"' in section
        assert 'variable = "aws:SourceArn"' in section
        assert "arn:aws:codebuild:" in section


def test_release_launcher_accepts_candidate_locator_fields_only_for_active_or_rollback() -> None:
    body = _body()
    policy = _document("release_launcher")

    for name in (
        "CANDIDATE_RECEIPT_KEY",
        "CANDIDATE_RECEIPT_VERSION_ID",
        "CANDIDATE_RECEIPT_SIGNATURE_KEY",
        "CANDIDATE_RECEIPT_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}"' in body
    assert "local.release_attestor_environment_names" in policy
    assert 'values   = ["active", "rollback"]' in policy
    assert "ReadCandidateAndReleaseDigest" in policy
    assert re.search(r'sid\s+= "DenyMutationAndAlternateEntryPoints"', policy)
    assert '"ecs:*"' in policy and '"events:*"' in policy


def test_root_key_rotation_is_documented_as_external_blocker() -> None:
    body = README.read_text(encoding="utf-8").lower()

    assert "root access keys" in body
    assert "external account-level blocker" in body
    assert "rotate/delete root keys" in body
