from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
README = ROOT / "infra" / "terraform" / "README.md"

CODEBUILD_LAUNCHER_POLICY_DOCUMENTS = (
    "codebuild_launcher_core",
    "codebuild_launcher_manage_a",
    "codebuild_launcher_manage_b",
    "codebuild_launcher_guardrails",
)
RELEASE_LAUNCHER_POLICY_DOCUMENTS = (
    "release_launcher_a",
    "release_launcher_b",
)


def _body() -> str:
    return TERRAFORM.read_text(encoding="utf-8")


def _balanced_block_after(body: str, marker: str) -> str:
    marker_offset = body.index(marker)
    opening = body.index("{", marker_offset)
    depth = 0
    for offset in range(opening, len(body)):
        if body[offset] == "{":
            depth += 1
        elif body[offset] == "}":
            depth -= 1
            if depth == 0:
                return body[opening + 1 : offset]
    raise AssertionError(f"unterminated Terraform block after {marker!r}")


def _document(name: str) -> str:
    marker = f'data "aws_iam_policy_document" "{name}"'
    return _balanced_block_after(_body(), marker)


def _managed_policy(role_name: str, document_names: tuple[str, ...]) -> str:
    body = _body()
    documents = []
    for name in document_names:
        documents.append(_document(name))

        managed_policy = _balanced_block_after(body, f'resource "aws_iam_policy" "{name}"')
        assert f"policy = data.aws_iam_policy_document.{name}.json" in managed_policy

        attachment = _balanced_block_after(
            body,
            f'resource "aws_iam_role_policy_attachment" "{name}"',
        )
        assert f"role       = aws_iam_role.{role_name}.name" in attachment
        assert f"policy_arn = aws_iam_policy.{name}.arn" in attachment

    return "\n".join(documents)


def _statement(document: str, sid: str) -> str:
    for match in re.finditer(r"\bstatement\s*\{", document):
        statement = _balanced_block_after(document[match.start() :], match.group(0))
        if re.search(rf'\bsid\s*=\s*"{re.escape(sid)}"', statement):
            return statement
    raise AssertionError(f"missing IAM statement {sid!r}")


def _actions(statement: str) -> set[str]:
    match = re.search(r"\bactions\s*=\s*\[(.*?)\]", statement, re.DOTALL)
    if match is None:
        raise AssertionError("IAM statement has no actions list")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _effect(statement: str) -> str:
    match = re.search(r'\beffect\s*=\s*"([^"]+)"', statement)
    return match.group(1) if match else "Allow"


def test_main_launcher_is_exact_assume_once_boundary_and_direct_start_is_denied() -> None:
    body = _body()
    policy = _managed_policy("codebuild_launcher", CODEBUILD_LAUNCHER_POLICY_DOCUMENTS)

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
    direct_deny = _statement(direct, "RequireDedicatedLauncherRole")
    assert _effect(direct_deny) == "Deny"
    assert _actions(direct_deny) == {
        "codebuild:StartBuild",
        "codebuild:RetryBuild",
        "codebuild:RetryBuildBatch",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
        "codebuild:StartSandboxConnection",
    }


def test_start_build_environment_is_allowlisted_and_fixed_values_are_pinned() -> None:
    body = _body()
    policy = _managed_policy("codebuild_launcher", CODEBUILD_LAUNCHER_POLICY_DOCUMENTS)

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


def test_source_publisher_can_read_both_app_inputs_but_only_write_source_zip() -> None:
    policy = _document("mcp_source_publisher")

    read = policy.split('sid = "ReadPinnedAppInputs"', maxsplit=1)[1].split(
        "\n  statement {", maxsplit=1
    )[0]
    assert '"s3:GetObject"' in read
    assert '"s3:GetObjectVersion"' in read
    assert '"s3:PutObject"' not in read
    assert "/codebuild/connect-web-app.html" in read
    assert "/codebuild/baked-fallback/connect-web-app.html" in read

    publish = policy.split('sid     = "PublishExactSource"', maxsplit=1)[1].split(
        "\n  statement {", maxsplit=1
    )[0]
    assert 'actions = ["s3:PutObject"]' in publish
    assert "/codebuild/source.zip" in publish
    assert "connect-web-app.html" not in publish


def test_official_dangerous_override_condition_keys_are_explicit_denies() -> None:
    body = _body()
    policy = _managed_policy("codebuild_launcher", CODEBUILD_LAUNCHER_POLICY_DOCUMENTS)

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
    for key_set in (
        "launcher_denied_override_condition_keys_manage_a",
        "launcher_denied_override_condition_keys_manage_b",
        "launcher_denied_override_condition_keys_guardrails",
    ):
        deny = _balanced_block_after(policy, f"for_each = local.{key_set}")
        assert 'effect    = "Deny"' in deny
        assert 'actions   = ["codebuild:StartBuild"]' in deny
        assert "resources = local.launcher_all_project_arns" in deny
        assert 'test     = "Null"' in deny
        assert "variable = statement.value" in deny
        assert 'values   = ["false"]' in deny
    assert '"ssmmessages:*"' in policy


def test_all_fixed_build_roles_deny_non_plaintext_environment_resolution() -> None:
    dynamic_environment_denies = {
        "codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
        "tiktok_codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
        "mcp_source_publisher": (
            "DenyAllEcrAndEvidenceDeletion",
            {
                "ecr:*",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "image_attestor": (
            "DenySourceAndEvidenceDeletion",
            {
                "codeconnections:GetConnection",
                "codeconnections:GetConnectionToken",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "image_promoter": (
            "DenySourceSigningAndEvidenceWrites",
            {
                "codeconnections:GetConnection",
                "codeconnections:GetConnectionToken",
                "kms:Sign",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "s3:PutObject",
                "s3:PutObjectRetention",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "openclaw_codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
    }
    for policy_name, (sid, expected_actions) in dynamic_environment_denies.items():
        policy = _document(policy_name)
        denial = _statement(policy, sid)
        assert _effect(denial) == "Deny"
        assert _actions(denial) == expected_actions


def test_only_source_free_promoter_can_write_release_repositories() -> None:
    body = _body()
    main_builder = _document("codebuild")
    tiktok_builder = _document("tiktok_codebuild")
    openclaw_builder = _document("openclaw_codebuild")
    attestor = _document("image_attestor")
    promoter = _document("image_promoter")

    quarantine_write_actions = {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeImageScanFindings",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    release_write_denies = {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchDeleteImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    assert _actions(_statement(main_builder, "EcrMcpQuarantineWrite")) == (quarantine_write_actions)
    for policy, sid in (
        (main_builder, "DenyMcpCandidateAndReleaseWrite"),
        (tiktok_builder, "DenyTiktokCandidateAndReleaseWrite"),
        (openclaw_builder, "DenyOpenClawCandidateAndReleaseWrite"),
        (attestor, "DenyEveryCandidateAndReleaseRepositoryWrite"),
    ):
        denial = _statement(policy, sid)
        assert _effect(denial) == "Deny"
        assert _actions(denial) == release_write_denies

    promoter_write = _statement(promoter, "WriteOnlyAllowlistedCandidateAndReleaseRepositories")
    assert _effect(promoter_write) == "Allow"
    assert _actions(promoter_write) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
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
    policy = _managed_policy("release_launcher", RELEASE_LAUNCHER_POLICY_DOCUMENTS)

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
    verify_signature = _statement(policy, "VerifyAttestorReceiptSignature")
    assert _effect(verify_signature) == "Allow"
    assert _actions(verify_signature) == {
        "kms:DescribeKey",
        "kms:GetPublicKey",
        "kms:Verify",
    }
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
    assert _actions(_statement(policy, "ReadDeploymentLedger")) == {"dynamodb:GetItem"}
    assert _actions(_statement(policy, "PrepareUniqueDeploymentIntent")) == {"dynamodb:PutItem"}
    assert _actions(_statement(policy, "TransitionDeploymentIntentOrHeartbeatLock")) == {
        "dynamodb:UpdateItem"
    }
    assert _actions(_statement(policy, "AtomicallyStartAndConsumeDeployment")) == {
        "dynamodb:TransactWriteItems"
    }
    assert _actions(_statement(policy, "ReleaseOnlySharedDeploymentLock")) == {
        "dynamodb:DeleteItem"
    }
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
    runtime_mutation_deny = _statement(policy, "DenyRuntimeEvidenceAndImageMutation")
    assert _effect(runtime_mutation_deny) == "Deny"
    assert _actions(runtime_mutation_deny) == {
        "codebuild:StartBuild",
        "codebuild:StartBuildBatch",
        "ecr:BatchDeleteImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecs:*",
        "events:*",
        "kms:Sign",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutObject",
        "s3:PutObjectRetention",
    }
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
