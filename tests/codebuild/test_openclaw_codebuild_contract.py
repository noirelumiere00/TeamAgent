from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra" / "codebuild" / "openclaw-provenance-buildspec.yml"
ATTESTOR = ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml"
PROMOTER = ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml"
CONTRACT = ROOT / "infra" / "codebuild" / "openclaw_bundle_contract.json"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
ECR = ROOT / "infra" / "terraform" / "ecr.tf"
LAUNCHER = ROOT / "infra" / "deploy" / "build_openclaw_image.sh"


def _terraform() -> str:
    return TERRAFORM.read_text(encoding="utf-8")


def _policy(name: str) -> str:
    body = _terraform()
    return body.split(f'data "aws_iam_policy_document" "{name}"', maxsplit=1)[1].split(
        '\nresource "aws_iam_role_policy"', maxsplit=1
    )[0]


def test_project_is_dedicated_fixed_full_sha_source_with_embedded_buildspec() -> None:
    body = _terraform()
    project = body.split('resource "aws_codebuild_project" "openclaw_provenance"', maxsplit=1)[
        1
    ].split('data "aws_iam_policy_document" "openclaw_publisher_assume"', maxsplit=1)[0]

    assert "service_role = aws_iam_role.openclaw_codebuild.arn" in project
    assert '"0000000000000000000000000000000000000000"' in project
    assert 'location            = "https://github.com/noirelumiere00/TeamAgent.git"' in project
    assert "git_clone_depth     = 0" in project
    assert 'type     = "CODECONNECTIONS"' in project
    assert "openclaw-provenance-buildspec.yml" in project
    assert 'type            = "ARM_CONTAINER"' in project
    assert "environment_variable" not in project
    assert "source.zip" not in project


def test_build_role_can_write_only_openclaw_quarantine_and_not_mcp_candidate_or_release() -> None:
    policy = _policy("openclaw_codebuild")

    allow = policy.split('sid = "OpenClawQuarantineOnlyBuildAndVerify"', maxsplit=1)[1].split(
        "\n  }", maxsplit=1
    )[0]
    assert "aws_ecr_repository.openclaw_quarantine.arn" in allow
    assert "aws_ecr_repository.openclaw_media_quarantine.arn" in allow
    assert "verified_candidates" not in allow
    assert "aws_ecr_repository.openclaw.arn" not in allow
    deny = policy.split('sid    = "DenyOpenClawCandidateAndReleaseWrite"', maxsplit=1)[1]
    assert "aws_ecr_repository.openclaw_verified_candidates.arn" in deny
    assert "aws_ecr_repository.openclaw.arn" in deny
    assert "aws_ecr_repository.openclaw_media_verified_candidates.arn" in deny
    assert "aws_ecr_repository.openclaw_media.arn" in deny
    assert 'sid     = "DenyMcpRepositories"' in policy
    assert "aws_ecr_repository.mcp_verified_candidates.arn" in policy
    assert '"s3:PutObject"' in policy
    assert '"kms:Sign"' in policy


def test_openclaw_start_build_denies_environment_buildspec_and_runtime_overrides() -> None:
    body = _terraform()
    policy = _policy("openclaw_publisher")

    assert 'sid       = "StartExactOpenClawBuild"' in policy
    assert 'sid       = "DenyAnyStartBuildEnvironmentOverride"' in policy
    assert 'variable = "codebuild:environment.environmentVariables.name"' in policy
    for key in (
        "codebuild:source.buildspec",
        "codebuild:source.location",
        "codebuild:secondarySources",
        "codebuild:artifacts",
        "codebuild:environment.image",
        "codebuild:environment.privilegedMode",
        "codebuild:environment.registryCredential",
        "codebuild:logsConfig",
        "codebuild:cache",
        "codebuild:serviceRole",
    ):
        assert f'"{key}"' in body
    assert '"codebuild:timeoutInMinutes"' not in body
    assert '"ssmmessages:*"' in policy


def test_buildspec_verifies_signed_exact_source_before_quarantine_build() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    fetch = body.index("git fetch --no-tags --force origin refs/heads/dev")
    remote = body.index("independently fetched origin/dev head")
    ready = body.index("assert-release-ready")
    manifest = body.index("create-source-manifest")
    head = body.index("aws s3api head-object")
    exact_get = body.index('--version-id "$SOURCE_MANIFEST_VERSION"')
    kms = body.index("aws kms verify")
    verify = body.index("verify-source-manifest")
    build = body.index("bash infra/openclaw/build-image.sh")
    assert fetch < remote < ready < manifest < head < exact_get < kms < verify < build
    assert "ObjectLockRetainUntilDate" in body
    assert '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"' in body
    assert "RSASSA_PKCS1_V1_5_SHA_256" in body
    assert "aws s3api put-object" not in body
    assert "aws kms sign" not in body


def test_buildspec_is_quarantine_only_and_attestor_owns_actual_image_signatures() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    attestor = ATTESTOR.read_text(encoding="utf-8")
    promoter = PROMOTER.read_text(encoding="utf-8")

    assert "teamagent-openclaw-quarantine:candidate-${SOURCE_COMMIT}-core" in body
    assert "teamagent-openclaw-media-quarantine:candidate-${SOURCE_COMMIT}-media" in body
    assert "aws ecr wait image-scan-complete" in body
    assert "--deny-all" in body
    assert "teamagent-openclaw-verified-candidates" not in body
    assert "oras cp" not in body
    assert "list-image-referrers" not in body
    assert "/tmp/verify_actual_image.sh" in attestor
    assert "cosign verify --experimental-oci11" in attestor
    assert "application/spdx+json" in attestor
    assert "application/vnd.in-toto+json" in attestor
    assert "--max-results 50" in attestor
    assert "oras cp --recursive" in promoter


def test_openclaw_registry_and_trivy_sources_ignore_hostile_overrides() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 3
    for variable in ("ECR_REGISTRY", "OC_REPO", "OPENCLAW_REPO", "OPENCLAW_MEDIA_REPO", "MCP_REPO"):
        assert (
            sum(variable in line for line in body.splitlines() if line.strip().startswith("unset "))
            == 3
        )
    assert body.count("unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY") == 3
    assert body.count('TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"') == 3
    assert 'REGISTRY="718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"' in body
    assert (
        "docker login --username AWS --password-stdin "
        '"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"'
    ) in body
    assert "teamagent-mcp-quarantine" not in body


def test_openclaw_final_subjects_are_single_arm64_manifests_and_full_sha_tagged() -> None:
    buildspec = BUILDSPEC.read_text(encoding="utf-8")
    launcher = LAUNCHER.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert '"arm64_subject_media_type": "application/vnd.oci.image.manifest.v1+json"' in contract
    assert "--accepted-media-types application/vnd.oci.image.manifest.v1+json" in buildspec
    assert "arm64-config-digest" in buildspec
    assert "verify-arm64-config" in buildspec
    assert 'TAG="candidate-$COMMIT-$SUBJECT"' in launcher
    assert "candidate-${SOURCE_COMMIT}-core" in buildspec
    assert "candidate-${SOURCE_COMMIT}-media" in buildspec
    assert "[:12]" not in launcher
    assert "git-" not in buildspec
    assert "resolve-platform" in launcher


def test_immutable_openclaw_evidence_bucket_and_runtime_pull_denies_are_explicit() -> None:
    terraform = _terraform()
    ecr = ECR.read_text(encoding="utf-8")

    assert "bucket              = local.openclaw_evidence_bucket" in terraform
    assert "object_lock_enabled = true" in terraform
    assert 'status = "Enabled"' in terraform
    assert 'sse_algorithm     = "aws:kms"' in terraform
    assert 'mode = "COMPLIANCE"' in terraform
    assert "days = 3650" in terraform
    assert "prevent_destroy = true" in terraform
    for repository in (
        "aws_ecr_repository.openclaw_quarantine.arn",
        "aws_ecr_repository.openclaw_verified_candidates.arn",
        "aws_ecr_repository.openclaw_media_quarantine.arn",
        "aws_ecr_repository.openclaw_media_verified_candidates.arn",
    ):
        assert repository in ecr


def test_safe_launcher_assumes_once_pins_dev_and_never_deploys() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    assert body.count("aws sts assume-role") == 1
    assert 'EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"' in body
    assert "local dev HEAD must exactly equal origin/dev" in body
    assert body.index("assert-release-ready") < body.index("aws sts get-caller-identity")
    first_start = body.split("aws codebuild start-build", maxsplit=1)[1].split(')"', maxsplit=1)[0]
    assert '--project-name "$CODEBUILD_PROJECT"' in first_start
    assert '--source-version "$COMMIT"' in first_start
    assert "environment-variables-override" not in first_start
    assert "--if-none-match '*'" in body
    assert "--object-lock-mode COMPLIANCE" in body
    assert "source-manifests/$COMMIT/$SOURCE_MANIFEST_SHA256.json" in body
    assert not any(command in body for command in ("aws ecs ", "aws events ", "put-targets"))
