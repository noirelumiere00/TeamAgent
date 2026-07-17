from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
BUILDSPEC = ROOT / "infra" / "codebuild" / "tiktok-buildspec.yml"


def _terraform_sections() -> tuple[str, str]:
    body = TERRAFORM.read_text(encoding="utf-8")
    marker = "# TikTok worker: separate repository, project, role, and ECR boundary"
    mcp, tiktok = body.split(marker, maxsplit=1)
    return mcp, tiktok


def test_mcp_and_tiktok_repository_permissions_are_separate() -> None:
    mcp, tiktok = _terraform_sections()

    assert "aws_ecr_repository.tiktok_acquire" not in mcp
    assert "aws_ecr_repository.mcp" not in tiktok
    assert "aws_ecr_repository.openclaw" not in tiktok
    assert "resources = [aws_ecr_repository.tiktok_acquire[0].arn]" in tiktok
    assert 'sid       = "TiktokEcrScanGate"' in tiktok
    assert 'actions   = ["ecr:DescribeImageScanFindings"]' in tiktok
    assert 'sid = "TiktokEcrRemoteVerification"' in tiktok
    assert '"ecr:BatchGetImage"' in tiktok
    assert '"ecr:GetDownloadUrlForLayer"' in tiktok
    assert "ecr:StartImageScan" not in tiktok


def test_tiktok_project_has_dedicated_git_source_and_no_latest_default() -> None:
    _mcp, tiktok = _terraform_sections()

    assert 'resource "aws_codebuild_project" "tiktok_image"' in tiktok
    assert 'resource "aws_iam_role" "tiktok_codebuild"' in tiktok
    assert 'resource "aws_codestarconnections_connection" "tiktok_codebuild"' in tiktok
    assert (
        'location            = "https://github.com/noirelumiere00/tiktok-data-service.git"'
        in tiktok
    )
    assert 'type                = "GITHUB"' in tiktok
    assert 'type     = "CODECONNECTIONS"' in tiktok
    assert '"0000000000000000000000000000000000000000"' in tiktok
    assert 'name  = "GIT_COMMIT"' not in tiktok
    assert 'name  = "GIT_BRANCH"' not in tiktok
    assert 'filebase64("${path.module}/../codebuild/verify_ecr_scan.py")' in tiktok
    assert 'filebase64("${path.module}/../codebuild/resolve_ecr_image.py")' in tiktok


def test_tiktok_buildspec_binds_commit_and_calls_only_safe_builder() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert '[ "$CODEBUILD_RESOLVED_SOURCE_VERSION" = "$GIT_COMMIT" ]' in body
    assert '[ "$(git rev-parse --verify HEAD^{commit})" = "$GIT_COMMIT" ]' in body
    assert (
        '[ "$(git rev-parse --verify refs/remotes/origin/main^{commit})" = "$GIT_COMMIT" ]' in body
    )
    assert "git status --porcelain=v1 --untracked-files=all" in body
    assert "https://github.com/noirelumiere00/tiktok-data-service.git" in body
    assert 'EXPECTED_SOURCE_REVISION="$GIT_COMMIT"' in body
    assert "scripts/build_acquire_image.sh" in body
    assert '--repository "$TIKTOK_REPO"' in body
    assert "aws s3" not in body
    assert "source.zip" not in body
    assert "buildspec-override" not in body


def test_tiktok_buildspec_has_pinned_trivy_and_zero_exception_ecr_gate() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert 'TRIVY_VERSION="0.70.0"' in body
    assert "2f6bb988b553a1bbac6bdd1ce890f5e412439564e17522b88a4541b4f364fc8d" in body
    assert "sha256sum -c -" in body
    assert body.count("__ECR_SCAN_GATE_BASE64__") == 1
    assert body.count("__ECR_IMAGE_RESOLVER_BASE64__") == 1
    assert "resolve-platform" in body
    assert "verify-config-platform" in body
    assert body.count('--image-id "imageDigest=$ARM64_DIGEST"') == 2
    assert '--expected-image-digest "$ARM64_DIGEST"' in body
    assert "aws ecr wait image-scan-complete" in body
    assert "aws ecr describe-image-scan-findings" in body
    assert "--deny-all" in body
    assert "--exceptions" not in body
    assert not any(token in body for token in ("aws ecs", "update-service", "register-task"))
