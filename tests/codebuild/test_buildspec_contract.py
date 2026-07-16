from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra" / "codebuild" / "buildspec.yml"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"


def test_source_provenance_runs_before_docker_build() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    verify_position = body.index("source_provenance.py verify-source")
    build_position = body.index("docker build")
    assert verify_position < build_position
    for required in (
        '--expected-commit "$GIT_COMMIT"',
        '--expected-branch "$GIT_BRANCH"',
        '--expected-with-scrape-tools "$WITH_SCRAPE_TOOLS"',
        ".teamagent-source-manifest.json",
    ):
        assert required in body


def test_scrape_tools_has_no_implicit_buildspec_default() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "WITH_SCRAPE_TOOLS must be explicitly provided" in body
    assert "WITH_SCRAPE_TOOLS:-" not in body
    assert '--build-arg "WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS"' in body
    assert "io.teamagent.build.with-scrape-tools" in body


def test_app_html_s3_hot_swap_build_contract_is_preserved() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "s3://teamagent-dev-raw-files/codebuild/connect-web-app.html" in body
    assert "src/teamagent/connect_web/static/app.html" in body
    assert "source archive app.html" in body
    assert "app.html is absent from both S3 and the source archive" in body


def test_push_is_followed_by_complete_scan_and_strict_gate() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    push_position = body.index("docker push")
    wait_position = body.index("aws ecr wait image-scan-complete")
    findings_position = body.index("aws ecr describe-image-scan-findings")
    gate_position = body.index("verify_ecr_scan.py")
    assert push_position < wait_position < findings_position < gate_position
    assert "ecr_scan_exceptions.json" in body


def test_terraform_uses_single_git_managed_buildspec() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")

    assert 'buildspec = file("${path.module}/../codebuild/buildspec.yml")' in body
    assert "buildspec = <<" not in body
    for variable in ("IMAGE_TAG", "WITH_SCRAPE_TOOLS", "GIT_COMMIT", "GIT_BRANCH"):
        assert f'name  = "{variable}"' not in body


def test_scan_iam_is_read_only_and_scoped_to_mcp_repository() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")
    statement = body.split('sid       = "EcrMcpScanGate"', maxsplit=1)[1].split("}", maxsplit=1)[0]

    assert 'actions   = ["ecr:DescribeImageScanFindings"]' in statement
    assert "resources = [aws_ecr_repository.mcp.arn]" in statement
    assert "StartImageScan" not in statement
    assert "openclaw" not in statement
