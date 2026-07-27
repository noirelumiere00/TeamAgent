from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
BUILDSPEC = ROOT / "infra" / "codebuild" / "tiktok-buildspec.yml"
LAUNCHER = ROOT / "infra" / "deploy" / "build_tiktok_image.sh"
CONTRACT = ROOT / "infra" / "codebuild" / "tiktok_release_contract.json"


def _body() -> str:
    return TERRAFORM.read_text(encoding="utf-8")


def _policy(name: str) -> str:
    body = _body()
    return body.split(f'data "aws_iam_policy_document" "{name}"', maxsplit=1)[1].split(
        '\nresource "aws_iam_role_policy"', maxsplit=1
    )[0]


def test_tiktok_has_dedicated_caller_launcher_and_no_aiiadev_dependency() -> None:
    body = _body()
    start = body.index('resource "aws_iam_user" "tiktok_build_caller"')
    end = body.index("# Immutable cross-pipeline evidence", start)
    section = body[start:end]

    assert 'name  = "teamagent-tiktok-build-caller"' in section
    assert "aws_iam_user.tiktok_build_caller[0].arn" in section
    assert "AIIAdev" not in section
    assert 'resource "aws_iam_role" "tiktok_build_launcher"' in section
    assert 'resource "aws_iam_user_policy" "tiktok_build_caller"' in section
    assert 'sid    = "DenyDirectBuildEntryPoints"' in section
    assert '"codebuild:StartBuild"' in section
    assert 'values   = ["tiktok"]' in section
    assert 'values   = ["verified-candidate"]' in section


def test_tiktok_builder_can_write_only_quarantine_not_candidate_or_release() -> None:
    policy = _policy("tiktok_codebuild")
    allow = policy.split('sid = "TiktokEcrQuarantineWrite"', maxsplit=1)[1].split(
        "\n  }", maxsplit=1
    )[0]
    deny = policy.split('sid    = "DenyTiktokCandidateAndReleaseWrite"', maxsplit=1)[1]

    assert "aws_ecr_repository.tiktok_acquire_quarantine[0].arn" in allow
    assert "verified_candidates" not in allow
    assert "aws_ecr_repository.tiktok_acquire[0].arn" not in allow
    assert "aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn" in deny
    assert "aws_ecr_repository.tiktok_acquire[0].arn" in deny
    assert '"kms:Sign"' in policy
    assert '"s3:PutObject"' in policy
    assert "aws_ecr_repository.mcp" not in policy
    assert "aws_ecr_repository.openclaw" not in policy


def test_tiktok_project_has_fixed_git_source_and_no_latest_default() -> None:
    body = _body()
    project = body.split('resource "aws_codebuild_project" "tiktok_image"', maxsplit=1)[1].split(
        'output "tiktok_codebuild_project"', maxsplit=1
    )[0]

    assert '"0000000000000000000000000000000000000000"' in project
    assert (
        'location            = "https://github.com/noirelumiere00/tiktok-data-service.git"'
        in project
    )
    assert 'type                = "GITHUB"' in project
    assert 'type     = "CODECONNECTIONS"' in project
    assert "git_clone_depth     = 0" in project
    assert 'type            = "ARM_CONTAINER"' in project
    assert "environment_variable" not in project


def test_tiktok_buildspec_verifies_full_main_commit_and_signed_immutable_source() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    fetch = body.index("git fetch --no-tags --force origin refs/heads/main")
    remote = body.index("independently fetched main head")
    head = body.index("aws s3api head-object")
    exact = body.index('--version-id "$SOURCE_VERSION"')
    kms = body.index("aws kms verify")
    checkout = body.index("verify-checkout")
    build = body.index("scripts/build_acquire_image.sh")
    assert fetch < remote < head < exact < kms < checkout < build
    assert "source-version must" not in body or "GIT_COMMIT" in body
    assert '[[ "$GIT_COMMIT" =~ ^[0-9a-f]{40}$ ]]' in body
    assert "ObjectLockRetainUntilDate" in body
    assert '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"' in body
    assert "aws s3api put-object" not in body
    assert "aws kms sign" not in body
    assert "source.zip" not in body


def test_tiktok_buildspec_stops_at_quarantine_after_single_arm64_scan() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    guard = body.index("CODEBUILD_BUILD_SUCCEEDING")
    push = body.index("--push")
    resolve = body.index("resolve-platform")
    platform = body.index("verify-config-platform")
    scan = body.index("python3 /tmp/verify-ecr-scan.py")
    second_guard = body.index("CODEBUILD_BUILD_SUCCEEDING", guard + 1)
    assert push < guard < resolve < platform < scan < second_guard
    assert "--deny-all" in body
    assert '--expected-image-digest "$ARM64_DIGEST"' in body
    assert "teamagent-dev-tiktok-acquire-verified-candidates" not in body
    assert '$ECR_REGISTRY/teamagent-dev-tiktok-acquire"' not in body
    assert "BatchDeleteImage" not in body


def test_tiktok_registry_and_trivy_endpoints_ignore_hostile_overrides() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 3
    assert body.count('EXPECTED_ACCOUNT_ID="718959508629"') == 3
    assert body.count('EXPECTED_REGION="ap-northeast-1"') == 3
    assert (
        body.count("unset ECR_REGISTRY TIKTOK_REPO TIKTOK_QUARANTINE_REPO TIKTOK_RELEASE_REPO") == 3
    )
    assert 'TIKTOK_QUARANTINE_REPO="$ECR_REGISTRY/teamagent-dev-tiktok-acquire-quarantine"' in body
    assert (
        "docker login --username AWS --password-stdin "
        '"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"'
    ) in body
    assert body.count("unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY") >= 3
    assert body.count('export TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"') >= 3


def test_tiktok_launcher_signs_source_assumes_once_and_never_deploys() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert body.count("aws sts assume-role") == 1
    assert "teamagent-tiktok-build-caller" in body
    assert "AIIAdev" not in body
    assert "verify_clean_remote_head" in body
    assert 'verify_clean_remote_head "$CONTROL_ROOT" "$CONTROL_BRANCH"' in body
    assert 'verify_clean_remote_head "$SOURCE_ROOT" "$SOURCE_BRANCH"' in body
    assert 'die "$label HEAD must exactly equal origin/$branch"' in body
    assert "get-bucket-versioning" in body
    assert "get-object-lock-configuration" in body
    assert "aws kms sign" in body and "aws kms verify" in body
    assert "--if-none-match '*'" in body
    assert "--object-lock-mode GOVERNANCE" in body
    assert "date -u -d" not in body
    assert "base64 --decode" not in body
    assert "teamagent-dev-tiktok-acquire-verified-candidates" in body
    assert '"candidate_repository": "teamagent-dev-tiktok-acquire-verified-candidates"' in contract
    assert not any(token in body for token in ("aws ecs ", "aws events ", "terraform apply"))
