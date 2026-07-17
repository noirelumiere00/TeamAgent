from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDSPEC = ROOT / "infra" / "codebuild" / "buildspec.yml"
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
PROVENANCE_PATH = ROOT / "infra" / "codebuild" / "source_provenance.py"
ACTIVE_CONTRACT = ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json"
READY_CONTRACT = ROOT / "tests" / "codebuild" / "fixtures" / "teamagent_runtime_contract.ready.json"


def _load_provenance() -> object:
    spec = importlib.util.spec_from_file_location("buildspec_contract_provenance", PROVENANCE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROVENANCE = _load_provenance()


def test_source_and_runtime_contract_gates_run_before_docker_build() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    hash_position = body.index("__SOURCE_PROVENANCE_SHA256__")
    verify_source_position = body.index("source_provenance.py verify-source")
    ready_position = body.index("source_provenance.py assert-release-ready")
    docker_contract_position = body.index("source_provenance.py verify-dockerfile-contract")
    build_position = body.index("docker build")
    assert (
        hash_position
        < verify_source_position
        < ready_position
        < docker_contract_position
        < build_position
    )
    for required in (
        '--expected-commit "$GIT_COMMIT"',
        '--expected-branch "$GIT_BRANCH"',
        '--expected-with-scrape-tools "$WITH_SCRAPE_TOOLS"',
        '--expected-app-html-version-id "$APP_HTML_VERSION_ID"',
        '--expected-app-html-sha256 "$APP_HTML_SHA256"',
        '--expected-runtime-contract-sha256 "$RUNTIME_CONTRACT_SHA256"',
        ".teamagent-source-manifest.json",
    ):
        assert required in body


def test_generic_contract_passes_every_arg_including_node_image_digest() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    contract = PROVENANCE.load_runtime_contract(READY_CONTRACT)
    arguments = PROVENANCE.runtime_build_arguments(
        contract,
        PROVENANCE.runtime_contract_sha256(READY_CONTRACT),
    )

    assert "NODE_IMAGE_DIGEST" in arguments
    assert "docker-build-arguments" in body
    assert '[[ "$RUNTIME_BUILD_ARGUMENT" == NODE_IMAGE_DIGEST=* ]]' in body
    assert '--build-arg "NODE_IMAGE_DIGEST=$NODE_IMAGE_DIGEST"' in body
    assert 'DOCKER_RUNTIME_BUILD_ARGS+=(--build-arg "$RUNTIME_BUILD_ARGUMENT")' in body
    assert '"${DOCKER_RUNTIME_BUILD_ARGS[@]}"' in body
    assert "expected-runtime-labels" in body
    assert "RUNTIME_FIELDS" not in body
    assert "PLAYWRIGHT_" not in body
    assert "PYTHON_VERSION" not in body


def test_active_debian_candidate_is_explicitly_blocked_without_mutable_placeholders() -> None:
    contract = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))

    assert contract["release"]["ready"] is False
    assert "CRITICAL=4/HIGH=49" in contract["release"]["blocked_reason"]
    assert "Boyle" in contract["release"]["blocked_reason"]
    serialized = json.dumps(contract)
    assert "latest-dev" not in serialized
    assert "playwright" not in serialized.lower()
    assert "archive_sha256" not in serialized
    with pytest.raises(PROVENANCE.ProvenanceError, match="release is blocked"):
        PROVENANCE.require_release_ready(PROVENANCE.load_runtime_contract(ACTIVE_CONTRACT))


def test_scrape_tools_has_no_implicit_buildspec_default() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "WITH_SCRAPE_TOOLS must be explicitly provided" in body
    assert "WITH_SCRAPE_TOOLS must be explicitly true for production candidates" in body
    assert "WITH_SCRAPE_TOOLS:-" not in body
    assert '--build-arg "WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS"' in body
    assert "io.teamagent.build.with-scrape-tools" in body


def test_app_html_uses_only_pinned_version_and_verified_bytes() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "--bucket teamagent-dev-raw-files" in body
    assert "--key codebuild/connect-web-app.html" in body
    assert '--version-id "$APP_HTML_VERSION_ID"' in body
    assert '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"' in body
    assert '[ "$ACTUAL_APP_HTML_SHA256" = "$APP_HTML_SHA256" ]' in body
    assert '--build-arg "APP_HTML_SHA256=$APP_HTML_SHA256"' in body
    assert '--build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID"' in body
    assert "aws s3 cp" not in body


def test_builder_stops_at_quarantine_and_source_free_projects_own_promotion() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    attestor = (ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml").read_text()
    promoter = (ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml").read_text()

    first_guard = body.index("CODEBUILD_BUILD_SUCCEEDING")
    push = body.index('docker push "$MCP_QUARANTINE_REPO:$IMAGE_TAG"')
    resolve = body.index("resolve_ecr_image.py resolve-platform")
    provenance = body.index("source_provenance.py verify-oci-revision")
    wait = body.index("aws ecr wait image-scan-complete")
    scan = body.index("python3 infra/codebuild/verify_ecr_scan.py")
    second_guard = body.index("CODEBUILD_BUILD_SUCCEEDING", first_guard + 1)
    assert (
        first_guard
        < push
        < resolve
        < provenance
        < wait
        < scan
        < second_guard
    )
    assert "--deny-all" in body
    assert "ecr_scan_exceptions.json" not in body
    assert "BatchDeleteImage" not in body
    assert "teamagent-mcp-verified-candidates" not in body
    assert "teamagent-mcp\"" not in body
    assert "verify_actual_image.sh" in attestor
    assert 'PROMOTION_CHANNEL" = "verified-candidate"' in promoter
    assert 'SOURCE_REPOSITORY="$QUARANTINE_REPOSITORY"' in promoter
    assert 'DESTINATION_REPOSITORY="$CANDIDATE_REPOSITORY"' in promoter
    assert 'SOURCE_REPOSITORY="$CANDIDATE_REPOSITORY"' in promoter
    assert 'DESTINATION_REPOSITORY="$RELEASE_REPOSITORY"' in promoter
    assert "oras cp --recursive" in promoter


def test_registry_and_s3_destinations_ignore_hostile_overrides() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 3
    assert body.count('EXPECTED_ACCOUNT_ID="718959508629"') == 3
    assert body.count('EXPECTED_REGION="ap-northeast-1"') == 3
    assert 'MCP_QUARANTINE_REPO="$ECR_REGISTRY/teamagent-mcp-quarantine"' in body
    assert body.count("unset ECR_REGISTRY MCP_REPO MCP_QUARANTINE_REPO MCP_RELEASE_REPO") == 3
    assert body.count("unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY") == 3
    assert body.count(
        'TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"'
    ) == 3
    assert body.count('--registry-id "$EXPECTED_ACCOUNT_ID"') >= 5
    assert "$ECR_REGISTRY/teamagent-mcp\"" not in body
    assert "attacker" not in body


def test_independent_publisher_pins_origin_dev_versioned_source_and_current_app() -> None:
    body = (
        ROOT / "infra" / "codebuild" / "mcp-source-publisher-buildspec.yml"
    ).read_text(encoding="utf-8")

    assert "git fetch --no-tags --force origin refs/heads/dev:refs/remotes/origin/dev" in body
    assert 'refs/remotes/origin/dev^{commit})" = "$EXPECTED_COMMIT"' in body
    assert "get-bucket-versioning" in body
    assert "--expected-bucket-owner 718959508629" in body
    assert "git -C \"$CODEBUILD_SRC_DIR\" archive" in body
    assert 'SOURCE_DECLARATION_KEY="source-declarations/mcp/$EXPECTED_COMMIT/$SOURCE_SHA256/$PUBLISHED_SOURCE_VERSION_ID.json"' in body
    assert "--object-lock-mode COMPLIANCE" in body
    assert "aws kms sign" in body
    for value in (
        "I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY",
        "46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067",
        "15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2",
        "1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2",
    ):
        assert value in body


def test_terraform_embeds_all_verifier_and_contract_hashes() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")

    for path, placeholder in (
        ("source_provenance.py", "__SOURCE_PROVENANCE_SHA256__"),
        ("resolve_ecr_image.py", "__ECR_IMAGE_RESOLVER_SHA256__"),
        ("verify_ecr_scan.py", "__ECR_SCAN_GATE_SHA256__"),
    ):
        assert path in body
        assert placeholder in body
    assert "runtime_contract_sha256" in body
    assert "teamagent_runtime_contract.json" in body
