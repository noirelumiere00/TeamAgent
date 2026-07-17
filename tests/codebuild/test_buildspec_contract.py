from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra" / "codebuild" / "buildspec.yml"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"


def test_source_provenance_runs_before_docker_build() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    hash_position = body.index("__SOURCE_PROVENANCE_SHA256__")
    verify_position = body.index("source_provenance.py verify-source")
    build_position = body.index("docker build")
    assert hash_position < verify_position < build_position
    for required in (
        '--expected-commit "$GIT_COMMIT"',
        '--expected-branch "$GIT_BRANCH"',
        '--expected-with-scrape-tools "$WITH_SCRAPE_TOOLS"',
        '--expected-app-html-version-id "$APP_HTML_VERSION_ID"',
        '--expected-app-html-sha256 "$APP_HTML_SHA256"',
        '--expected-e5-model-revision "$E5_MODEL_REVISION"',
        '--expected-node-image-digest "$NODE_IMAGE_DIGEST"',
        '--expected-playwright-chromium-sha256 "$PLAYWRIGHT_CHROMIUM_SHA256"',
        ".teamagent-source-manifest.json",
    ):
        assert required in body


def test_scrape_tools_has_no_implicit_buildspec_default() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "WITH_SCRAPE_TOOLS must be explicitly provided" in body
    assert "WITH_SCRAPE_TOOLS must be explicitly true for production candidates" in body
    assert "WITH_SCRAPE_TOOLS:-" not in body
    assert '--build-arg "WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS"' in body
    assert "io.teamagent.build.with-scrape-tools" in body


def test_app_html_uses_only_the_pinned_version_and_verified_bytes() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    get_object = body.split("aws s3api get-object", maxsplit=1)[1].split(')"', maxsplit=1)[0]
    assert "--bucket teamagent-dev-raw-files" in get_object
    assert "--key codebuild/connect-web-app.html" in get_object
    assert '--version-id "$APP_HTML_VERSION_ID"' in get_object
    assert "sha256sum" in body
    assert '[ "$ACTUAL_APP_HTML_SHA256" = "$APP_HTML_SHA256" ]' in body
    assert '--build-arg "APP_HTML_SHA256=$APP_HTML_SHA256"' in body
    assert '--build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID"' in body
    assert "io.teamagent.build.app-html-sha256" in body
    assert "io.teamagent.build.app-html-version-id" in body
    assert "src/teamagent/connect_web/static/app.html" in body
    assert "aws s3 cp" not in body
    assert "source archive app.html" not in body
    assert "s3://teamagent-dev-raw-files/codebuild/connect-web-app.html" not in body


def test_runtime_contract_is_passed_to_docker_and_checked_as_oci_labels() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    build_args = (
        "E5_MODEL_REVISION",
        "NODE_VERSION",
        "NODE_BINARY_SHA256",
        "PLAYWRIGHT_VERSION",
        "PLAYWRIGHT_CHROMIUM_REVISION",
        "PLAYWRIGHT_CHROMIUM_VERSION",
        "PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256",
        "PLAYWRIGHT_CHROMIUM_SHA256",
    )

    assert "DOCKER_BUILDKIT=1 docker build" in body
    for variable in build_args:
        assert f'--build-arg "{variable}=${variable}"' in body
    for label in (
        "io.teamagent.build.e5-model-revision",
        "io.teamagent.build.node-image-digest",
        "io.teamagent.build.node-version",
        "io.teamagent.build.chromium-sha256",
    ):
        assert label in body


def test_push_is_followed_by_complete_scan_and_strict_gate() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    push_position = body.index("docker push")
    resolve_position = body.index("resolve_ecr_image.py resolve-platform")
    remote_verify_position = body.index("source_provenance.py verify-oci-revision")
    wait_position = body.index("aws ecr wait image-scan-complete")
    findings_position = body.index("aws ecr describe-image-scan-findings")
    gate_position = body.index("python3 infra/codebuild/verify_ecr_scan.py")
    assert (
        push_position
        < resolve_position
        < remote_verify_position
        < wait_position
        < findings_position
        < gate_position
    )
    assert body.count('--image-id "imageDigest=$ARM64_DIGEST"') == 2
    assert '--expected-image-digest "$ARM64_DIGEST"' in body
    assert "ecr_scan_exceptions.json" in body


def test_terraform_uses_single_git_managed_buildspec() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")

    assert 'file("${path.module}/../codebuild/buildspec.yml")' in body
    assert "buildspec = <<" not in body
    for variable in (
        "IMAGE_TAG",
        "WITH_SCRAPE_TOOLS",
        "GIT_COMMIT",
        "GIT_BRANCH",
        "APP_HTML_VERSION_ID",
        "APP_HTML_SHA256",
        "E5_MODEL_REVISION",
        "NODE_IMAGE_DIGEST",
        "PLAYWRIGHT_CHROMIUM_SHA256",
    ):
        assert f'name  = "{variable}"' not in body


def test_trusted_codebuild_contract_files_are_hash_pinned_by_terraform() -> None:
    buildspec = BUILDSPEC.read_text(encoding="utf-8")
    terraform = TERRAFORM.read_text(encoding="utf-8")
    contracts = {
        "__SOURCE_PROVENANCE_SHA256__": "source_provenance.py",
        "__ECR_IMAGE_RESOLVER_SHA256__": "resolve_ecr_image.py",
        "__ECR_SCAN_GATE_SHA256__": "verify_ecr_scan.py",
        "__ECR_SCAN_EXCEPTIONS_SHA256__": "ecr_scan_exceptions.json",
    }

    for placeholder, filename in contracts.items():
        assert buildspec.count(placeholder) == 1
        assert terraform.count(placeholder) == 1
        assert f'filesha256("${{path.module}}/../codebuild/{filename}")' in terraform


def test_scan_iam_is_read_only_and_scoped_to_mcp_repository() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    statement = body.split('sid       = "EcrMcpScanGate"', maxsplit=1)[1].split("}", maxsplit=1)[0]

    assert 'actions   = ["ecr:DescribeImageScanFindings"]' in statement
    assert "resources = [aws_ecr_repository.mcp.arn]" in statement
    assert "StartImageScan" not in statement
    assert "openclaw" not in statement


def test_ecr_push_and_project_are_teamagent_mcp_only() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    push_statement = body.split('sid = "EcrPush"', maxsplit=1)[1].split("}", maxsplit=1)[0]

    assert "resources = [aws_ecr_repository.mcp.arn]" in push_statement
    assert "BatchGetImage" not in push_statement
    assert "GetDownloadUrlForLayer" not in push_statement
    assert 'name  = "OC_REPO"' not in body
    assert "teamagent-mcp/openclaw" not in body
    assert "Build and vulnerability-gate TeamAgent MCP candidate images" in body


def test_remote_ecr_reads_are_separate_and_mcp_scoped() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    statement = body.split('sid = "EcrMcpRemoteVerification"', maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    assert '"ecr:BatchGetImage"' in statement
    assert '"ecr:GetDownloadUrlForLayer"' in statement
    assert "resources = [aws_ecr_repository.mcp.arn]" in statement
    assert "tiktok_acquire" not in statement
