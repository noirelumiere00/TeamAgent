from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra" / "codebuild" / "openclaw-provenance-buildspec.yml"
CONTRACT = ROOT / "infra" / "codebuild" / "openclaw_bundle_contract.json"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
ECR = ROOT / "infra" / "terraform" / "ecr.tf"
LAUNCHER = ROOT / "infra" / "deploy" / "build_openclaw_image.sh"


def _openclaw_terraform() -> str:
    return TERRAFORM.read_text(encoding="utf-8").split(
        "# OpenClaw core/media: isolated source publisher + build boundary",
        maxsplit=1,
    )[1]


def test_project_is_dedicated_fixed_github_full_sha_source_with_embedded_buildspec() -> None:
    body = _openclaw_terraform()

    assert 'resource "aws_codebuild_project" "openclaw_provenance"' in body
    assert '"${var.project_name}-${var.environment}-openclaw-provenance-builder"' in (
        TERRAFORM.read_text(encoding="utf-8")
    )
    assert "service_role = aws_iam_role.openclaw_codebuild.arn" in body
    assert "source_version = (" in body
    assert '"0000000000000000000000000000000000000000"' in body
    assert 'location            = "https://github.com/noirelumiere00/TeamAgent.git"' in body
    assert "git_clone_depth     = 0" in body
    assert 'type     = "CODECONNECTIONS"' in body
    assert "openclaw-provenance-buildspec.yml" in body
    for placeholder in (
        "__OPENCLAW_PROVENANCE_SHA256__",
        "__OPENCLAW_BUNDLE_CONTRACT_SHA256__",
        "__OPENCLAW_SCAN_GATE_SHA256__",
        "__OPENCLAW_SIGNING_KMS_KEY_ARN__",
        "__OPENCLAW_EVIDENCE_KMS_KEY_ARN__",
    ):
        assert placeholder in body
    assert 'type            = "ARM_CONTAINER"' in body
    assert "environment_variable" not in body
    assert "source.zip" not in body


def test_build_role_has_only_openclaw_repositories_and_cannot_write_or_sign_s3_evidence() -> None:
    body = _openclaw_terraform()
    policy = body.split('data "aws_iam_policy_document" "openclaw_codebuild"', maxsplit=1)[1].split(
        'resource "aws_iam_role_policy" "openclaw_codebuild"', maxsplit=1
    )[0]

    for repository in (
        "aws_ecr_repository.openclaw_quarantine.arn",
        "aws_ecr_repository.openclaw_media_quarantine.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
    ):
        assert repository in policy
    assert re.search(r'^\s*sid\s+= "DenyMcpRepositories"$', policy, re.MULTILINE)
    assert "aws_ecr_repository.mcp.arn" in policy
    assert "aws_ecr_repository.mcp_quarantine.arn" in policy
    assert re.search(r'^\s*sid\s+= "DenyS3WritesAndDeletes"$', policy, re.MULTILINE)
    assert '"s3:PutObject"' in policy
    assert '"s3:DeleteObjectVersion"' in policy
    assert re.search(r'^\s*sid\s+= "DenySigning"$', policy, re.MULTILINE)
    assert '"kms:Sign"' in policy
    assert re.search(r'^\s*sid\s+= "VerifyTrustedPublisherSignature"$', policy, re.MULTILINE)
    assert '"kms:Verify"' in policy
    assert "/source-manifests/*" in policy
    assert "/release-evidence/*" not in policy


def test_openclaw_start_build_denies_buildspec_environment_and_dangerous_overrides() -> None:
    whole = TERRAFORM.read_text(encoding="utf-8")
    body = _openclaw_terraform()
    policy = body.split('data "aws_iam_policy_document" "openclaw_publisher"', maxsplit=1)[1].split(
        'resource "aws_iam_role_policy" "openclaw_publisher"', maxsplit=1
    )[0]

    assert re.search(r'^\s*sid\s+= "StartExactOpenClawBuild"$', policy, re.MULTILINE)
    assert re.search(
        r'^\s*sid\s+= "DenyAnyStartBuildEnvironmentOverride"$',
        policy,
        re.MULTILINE,
    )
    assert 'variable = "codebuild:environment.environmentVariables.name"' in policy
    for key in (
        "codebuild:source.buildspec",
        "codebuild:source.location",
        "codebuild:secondarySources",
        "codebuild:artifacts",
        "codebuild:secondaryArtifacts",
        "codebuild:environment.image",
        "codebuild:environment.privilegedMode",
        "codebuild:environment.registryCredential",
        "codebuild:logsConfig",
        "codebuild:cache",
        "codebuild:serviceRole",
    ):
        assert f'"{key}"' in whole
    assert '"codebuild:timeoutInMinutes"' not in whole
    assert '"codebuild:queuedTimeoutInMinutes"' not in whole
    for action in (
        "codebuild:RetryBuild",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
        "ssm:StartSession",
        "ssmmessages:*",
    ):
        assert f'"{action}"' in policy


def test_buildspec_verifies_signed_versioned_object_lock_source_before_build() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    ready = body.index("assert-release-ready")
    manifest = body.index("create-source-manifest")
    head = body.index("aws s3api head-object")
    exact_get = body.index('--version-id "$SOURCE_MANIFEST_VERSION"')
    kms = body.index("aws kms verify")
    verify = body.index("verify-source-manifest")
    build = body.index("bash infra/openclaw/build-image.sh")
    assert ready < manifest < head < exact_get < kms < verify < build
    for required in (
        'CODEBUILD_SOURCE_REPO_URL" = "$EXPECTED_SOURCE_REPOSITORY',
        "source-version must be a full Git SHA",
        'refs/remotes/origin/dev^{commit})" = "$SOURCE_COMMIT',
        'value.get("ObjectLockMode") != "COMPLIANCE"',
        'value.get("ServerSideEncryption") != "aws:kms"',
        'value.get("SSEKMSKeyId") != expected_kms_key',
        "ObjectLockRetainUntilDate",
        "VersionId",
        '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"',
        "RSASSA_PKCS1_V1_5_SHA_256",
    ):
        assert required in body
    assert "aws s3api put-object" not in body
    assert "aws kms sign" not in body


def test_buildspec_hardcodes_registry_and_non_overrideable_trivy_repositories() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 3
    assert body.count("unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY") == 3
    assert body.count('export TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"') == 3
    assert (
        body.count('export TRIVY_JAVA_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-java-db:1"')
        == 3
    )
    assert (
        "docker login --username AWS --password-stdin "
        + ('"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"')
        in body
    )
    assert "teamagent-mcp-quarantine" not in body
    assert 'teamagent-mcp"' not in body


def test_core_media_scan_signed_referrer_gates_precede_recursive_promotion() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    first_guard = body.index("CODEBUILD_BUILD_SUCCEEDING")
    tag_equality = body.index("quarantine tag does not select", first_guard)
    manifest = body.index("aws ecr batch-get-image", tag_equality)
    config_digest = body.index("arm64-config-digest", manifest)
    config_verify = body.index("verify-arm64-config", config_digest)
    scan = body.index("python3 infra/codebuild/verify_ecr_scan.py", config_verify)
    deny_all = body.index("--deny-all", scan)
    subject_referrers = body.index("list-image-referrers", deny_all)
    signature_referrers = body.index("verify-signature-referrers", subject_referrers)
    cryptographic_verify = body.index("verify-bundle-evidence.sh", signature_referrers)
    second_guard = body.index("CODEBUILD_BUILD_SUCCEEDING", first_guard + 1)
    promote = body.index("promote-bundle.sh", second_guard)
    release_equality = body.index("release digest differs", promote)
    release_referrers = body.index("list-image-referrers", release_equality)
    final_guard = body.rindex("CODEBUILD_BUILD_SUCCEEDING")
    assert (
        first_guard
        < tag_equality
        < manifest
        < config_digest
        < config_verify
        < scan
        < deny_all
        < subject_referrers
        < signature_referrers
        < cryptographic_verify
        < second_guard
        < promote
        < release_equality
        < release_referrers
        < final_guard
    )
    assert "--recursive-referrers" in body
    assert "BatchDeleteImage" not in body
    assert body.count("verify-bundle-evidence.sh") >= 3


def test_openclaw_subjects_are_single_linux_arm64_manifests_not_indexes() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert ('"arm64_subject_media_type": "application/vnd.oci.image.manifest.v1+json"') in contract
    assert body.count("--accepted-media-types application/vnd.oci.image.manifest.v1+json") == 2
    assert "application/vnd.oci.image.index.v1+json" not in body
    assert body.count("arm64-config-digest") == 2
    assert "verify-arm64-config" in body

    quarantine_manifest = body.index("aws ecr batch-get-image")
    quarantine_config = body.index("arm64-config-digest", quarantine_manifest)
    quarantine_scan = body.index("aws ecr wait image-scan-complete", quarantine_config)
    promote = body.index("promote-bundle.sh", quarantine_scan)
    release_manifest = body.index("aws ecr batch-get-image", promote)
    release_config = body.index("arm64-config-digest", release_manifest)
    release_referrers = body.index("list-image-referrers", release_config)
    assert (
        quarantine_manifest
        < quarantine_config
        < quarantine_scan
        < promote
        < release_manifest
        < release_config
        < release_referrers
    )


def test_immutable_evidence_bucket_is_kms_encrypted_versioned_and_compliance_locked() -> None:
    body = _openclaw_terraform()

    assert "bucket              = local.openclaw_evidence_bucket" in body
    assert "object_lock_enabled = true" in body
    assert 'status = "Enabled"' in body
    assert 'sse_algorithm     = "aws:kms"' in body
    assert "kms_master_key_id = aws_kms_key.openclaw_evidence.arn" in body
    assert 'mode = "COMPLIANCE"' in body
    assert "days = 30" in body
    assert re.search(r'^\s*sid\s+= "DenyEvidenceWithoutComplianceLock"$', body, re.MULTILINE)
    assert re.search(r'^\s*sid\s+= "DenyEvidenceDeletion"$', body, re.MULTILINE)
    assert "prevent_destroy = true" in body


def test_safe_publisher_launcher_assumes_once_pins_dev_and_never_deploys() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    assert body.count("aws sts assume-role") == 1
    assert 'EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"' in body
    assert "teamagent-dev-openclaw-build-publisher" in body
    assert "local dev HEAD must exactly equal origin/dev" in body
    assert body.index("assert-release-ready") < body.index("aws sts get-caller-identity")
    start = body.split("aws codebuild start-build", maxsplit=1)[1].split(')"', maxsplit=1)[0]
    assert '--project-name "$CODEBUILD_PROJECT"' in start
    assert '--source-version "$COMMIT"' in start
    assert "environment-variables-override" not in start
    assert "buildspec-override" not in start
    assert "source-type-override" not in start
    assert "--if-none-match '*'" in body
    assert "--object-lock-mode COMPLIANCE" in body
    assert "create-release-evidence" in body
    assert "verify-release-evidence" in body
    assert body.count("--accepted-media-types application/vnd.oci.image.manifest.v1+json") == 1
    assert "arm64-config-digest" in body
    assert "verify-arm64-config" in body
    assert "application/vnd.oci.image.index.v1+json" not in body
    assert not any(command in body for command in ("aws ecs ", "aws events ", "put-targets"))


def test_openclaw_quarantine_repositories_are_never_runtime_pull_paths() -> None:
    body = ECR.read_text(encoding="utf-8")

    assert "aws_ecr_repository.openclaw_quarantine.arn" in body
    assert "aws_ecr_repository.openclaw_media_quarantine.arn" in body
    assert 'sid    = "DenyQuarantineRuntimePull"' in body
