from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "infra" / "deploy" / "build_teamagent_image.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )


def test_launcher_is_executable_build_only_and_accepts_no_mutable_input() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    assert os.access(LAUNCHER, os.X_OK)
    help_result = _run("--help")
    assert help_result.returncode == 0
    assert "Builds, attests, and publishes" in help_result.stdout
    assert "never updates ECS" in help_result.stdout
    for option in (
        "--image-tag",
        "--source-bucket",
        "--project-name",
        "--repository-name",
        "--region",
        "--buildspec-override",
    ):
        result = _run(option, "attacker")
        assert result.returncode != 0
        assert f"unknown argument: {option}" in result.stderr
    assert "source.zip" not in body
    assert "aws s3api put-object" not in body
    assert not any(token in body for token in ("aws ecs ", "aws events ", "terraform apply"))


def test_launcher_requires_clean_local_dev_equal_to_exact_remote_head() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    dirty = body.index("status --porcelain=v1 --untracked-files=all")
    branch = body.index('BRANCH="$(git')
    origin = body.index("config --get remote.origin.url")
    fetch = body.index('git -C "$REPO_ROOT" fetch')
    equal = body.index("local dev HEAD must exactly equal remote origin/dev")
    first_aws = body.index("aws sts get-caller-identity")
    assert dirty < branch < origin < fetch < equal < first_aws
    assert 'EXPECTED_BRANCH="dev"' in body
    assert 'EXPECTED_ORIGIN_URL="git@github.com:noirelumiere00/TeamAgent.git"' in body
    assert "assert-release-ready" in body


def test_launcher_assumes_exact_role_once_and_pins_temporary_session() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    assert body.count("aws sts assume-role") == 1
    assert 'EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"' in body
    assert (
        'LAUNCHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-codebuild-launcher"'
        in body
    )
    assert (
        'EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/'
        'teamagent-dev-codebuild-launcher/teamagent-build-launcher"'
    ) in body
    assert "AWS_CONFIG_FILE=/dev/null" in body
    assert "AWS_SHARED_CREDENTIALS_FILE=/dev/null" in body
    assert "unset AWS_PROFILE AWS_DEFAULT_PROFILE" in body


def test_launcher_ignores_endpoint_overrides_and_uses_only_fixed_resources() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    assert "export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true" in body
    assert "unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_S3" in body
    for fixed in (
        'REGION="ap-northeast-1"',
        'ACCOUNT_ID="718959508629"',
        'APP_BUCKET="teamagent-dev-raw-files"',
        'APP_KEY="codebuild/connect-web-app.html"',
        'SOURCE_PUBLISHER_PROJECT="teamagent-dev-mcp-source-publisher"',
        'IMAGE_PROJECT="teamagent-dev-image-builder"',
        'ATTESTOR_PROJECT="teamagent-dev-image-attestor"',
        'PROMOTER_PROJECT="teamagent-dev-image-promoter"',
        'VERIFIED_CANDIDATE_REPOSITORY="teamagent-mcp-verified-candidates"',
        ('MEDIA_VERIFIED_CANDIDATE_REPOSITORY="teamagent-media-worker-verified-candidates"'),
    ):
        assert fixed in body
    assert 'RELEASE_REPOSITORY="teamagent-mcp"' not in body


def test_launcher_binds_current_canonical_app_and_signed_source_versions() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    for marker in (
        "teamagent_core_media_release_contract.json",
        "teamagent_bundle_provenance.py",
        "production-record",
        "app-provenance-sha256",
        "APP_VERSION_ID",
        "APP_SHA256",
        "VAULT_MANIFEST_SHA256",
        "BUILD_INPUTS_SHA256",
        "BAKED_APP_HTML_VERSION_ID",
        "BAKED_APP_HTML_SHA256",
        "APP_PROVENANCE_SHA256",
    ):
        assert marker in body
    assert '--version-id "$APP_VERSION_ID"' in body
    assert '--version-id "$BAKED_APP_HTML_VERSION_ID"' in body
    assert '--expected-bucket-owner "$ACCOUNT_ID"' in body
    for name in (
        "SOURCE_ARCHIVE_VERSION_ID",
        "SOURCE_DECLARATION_KEY",
        "SOURCE_DECLARATION_VERSION_ID",
        "SOURCE_DECLARATION_SHA256",
        "SOURCE_DECLARATION_SIGNATURE_KEY",
        "SOURCE_DECLARATION_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}=' in body


def test_launcher_orders_publisher_builder_attestor_then_candidate_promoter() -> None:
    body = LAUNCHER.read_text(encoding="utf-8")

    publisher = body.index('start_build "$SOURCE_PUBLISHER_PROJECT"')
    image = body.index('start_build "$IMAGE_PROJECT"')
    core_digest = body.index("MCP_CORE_ARM64_DIGEST")
    media_digest = body.index("MCP_MEDIA_ARM64_DIGEST")
    attestor = body.index('start_build "$ATTESTOR_PROJECT"')
    promoter = body.index('start_build "$PROMOTER_PROJECT"')
    candidate = body.index('repository-name "$candidate_repository"')
    assert publisher < image < core_digest < media_digest < attestor < promoter < candidate
    assert '"PROMOTION_CHANNEL=verified-candidate"' in body
    assert 'candidate_repository: "teamagent-mcp-verified-candidates"' in body
    assert 'candidate_repository: "teamagent-media-worker-verified-candidates"' in body
    assert body.count("for subject in core media") == 1


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_poll_and_timeout_values_fail_before_any_aws_call(value: str) -> None:
    result = _run("--poll-seconds", value)

    assert result.returncode != 0
    assert "poll interval must be positive" in result.stderr
