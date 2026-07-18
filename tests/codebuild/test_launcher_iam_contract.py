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
    return body.split(marker, maxsplit=1)[1].split('\nresource "aws_iam_role_policy"', maxsplit=1)[
        0
    ]


def test_main_launcher_is_exact_assume_once_boundary_and_direct_start_is_denied() -> None:
    body = _body()
    policy = _document("codebuild_launcher")

    assert 'user_name = "AIIAdev"' in body
    assert "identifiers = [data.aws_iam_user.aiia_dev.arn]" in body
    assert re.search(
        r'^\s*launcher_role_name\s*=\s*"teamagent-dev-codebuild-launcher"$',
        body,
        re.MULTILINE,
    )
    assert "max_session_duration = 10800" in body
    assert "local.launcher_project_arn" in policy
    assert "local.launcher_all_project_arns" in policy
    assert "teamagent-dev-image-builder" in body
    assert "teamagent-dev-mcp-source-publisher" in body
    assert "teamagent-dev-image-attestor" in body
    assert "teamagent-dev-image-promoter" in body
    assert "teamagent-mcp-verified-candidates" in policy
    assert "teamagent-media-worker-verified-candidates" in policy
    assert "teamagent-mcp-quarantine" not in policy
    assert "teamagent-media-worker-quarantine" not in policy
    assert 'repository/teamagent-mcp"' not in policy
    assert 'repository/teamagent-media-worker"' not in policy
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
        "APP_HTML_VERSION_ID",
        "APP_HTML_SHA256",
        "VAULT_MANIFEST_SHA256",
        "BUILD_INPUTS_SHA256",
        "BAKED_APP_HTML_VERSION_ID",
        "BAKED_APP_HTML_SHA256",
        "APP_PROVENANCE_SHA256",
        "SOURCE_MANIFEST_CONTRACT_SHA256",
        "RELEASE_CONTRACT_SHA256",
        "SOURCE_ARCHIVE_VERSION_ID",
        "SOURCE_DECLARATION_VERSION_ID",
        "SOURCE_DECLARATION_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}"' in body
    assert '"IMAGE_TAG"' not in body
    assert 'test     = "ForAllValues:StringEquals"' in policy
    assert 'variable = "codebuild:environment.environmentVariables.name"' in policy
    assert re.search(r'^\s*GIT_BRANCH\s*=\s*"dev"$', body, re.MULTILINE)
    assert ("APP_HTML_VERSION_ID             = local.canonical_app_html_version_id") in body
    assert ("APP_HTML_SHA256                 = local.canonical_app_html_sha256") in body
    assert ("APP_PROVENANCE_SHA256           = local.canonical_app_provenance_sha256") in body


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
    assert "for_each = local.launcher_denied_override_condition_keys" in policy
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
        "aws_ecr_repository.mcp_media.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
        "aws_ecr_repository.tiktok_acquire[0].arn",
    ):
        assert release in promoter
    assert '"kms:Sign"' in promoter
    assert '"s3:PutObject"' in promoter
    assert '"codeconnections:GetConnection"' in promoter
    source_free = body.split('resource "aws_codebuild_project" "image_promoter"', maxsplit=1)[
        1
    ].split('output "image_promoter_project"', maxsplit=1)[0]
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


def test_image_deployment_gate_role_is_read_verify_and_ledger_only() -> None:
    body = _body()
    policy = _document("image_deployment_gate")

    assert "ReadExactImmutableReleaseReceipts" in policy
    assert "release-receipts/*" in policy
    assert '"kms:Verify"' in policy
    assert '"kms:Sign"' in policy
    assert 'sid       = "VerifyAttestorReceiptSignature"' in policy
    assert '"ReadReleaseSubjectAndReferrerGraph"' in policy
    assert '"ecr:BatchGetImage"' in policy
    assert '"ecr:GetLifecyclePolicy"' in policy
    assert '"ecr:ListImageReferrers"' not in policy
    for repository in (
        "aws_ecr_repository.mcp.arn",
        "aws_ecr_repository.mcp_media.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
        "aws_ecr_repository.tiktok_acquire[0].arn",
    ):
        assert repository in policy
    for action in (
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    ):
        assert f'"{action}"' in policy
    assert "aws_dynamodb_table.image_deployment_intents.arn" in policy
    assert "PrepareUniqueDeploymentIntent" in policy
    assert "AtomicallyStartAndConsumeDeployment" in policy
    assert "ReleaseOnlySharedDeploymentLock" in policy
    assert policy.count('variable = "dynamodb:LeadingKeys"') == 5
    delete_statement = policy.split(
        '"ReleaseOnlySharedDeploymentLock"',
        maxsplit=1,
    )[1].split('"DenyRuntimeEvidenceAndImageMutation"', maxsplit=1)[0]
    assert '"lock#teamagent/terraform.tfstate"' in delete_statement
    assert '"intent#*"' not in delete_statement
    assert '"receipt#*"' not in delete_statement
    for denied in (
        '"ecs:*"',
        '"events:*"',
        '"ecr:PutImage"',
        '"ecr:BatchDeleteImage"',
        '"kms:Sign"',
        '"s3:PutObject"',
    ):
        assert denied in policy
    assert 'aws_iam_user_policy" "aiia_dev_image_deployment_gate"' not in body
    trust = _document("image_deployment_gate_assume")
    assert '"arn:aws:iam::718959508629:root"' in trust
    assert 'variable = "aws:PrincipalArn"' in trust
    assert "local.terraform_automation_role_arn" in trust
    assert "data.aws_iam_user.aiia_dev.arn" not in trust


def test_existing_admin_authority_is_documented_as_an_accepted_bypass_risk() -> None:
    body = README.read_text(encoding="utf-8").lower()

    assert "do not rotate, delete, or reduce" in body
    assert "administrators can technically bypass" in body
    assert "administrator iam" in body
    assert "intentionally unchanged" in body
