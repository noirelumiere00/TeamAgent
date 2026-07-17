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

    get_object = body.split("aws s3api get-object", maxsplit=1)[1].split(')"', maxsplit=1)[0]
    assert "--bucket teamagent-dev-raw-files" in get_object
    assert "--key codebuild/connect-web-app.html" in get_object
    assert '--version-id "$APP_HTML_VERSION_ID"' in get_object
    assert '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"' in get_object
    assert '[ "$ACTUAL_APP_HTML_SHA256" = "$APP_HTML_SHA256" ]' in body
    assert '--build-arg "APP_HTML_SHA256=$APP_HTML_SHA256"' in body
    assert '--build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID"' in body
    assert "aws s3 cp" not in body


def test_quarantine_gates_precede_digest_preserving_release_promotion() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    first_guard = body.index("CODEBUILD_BUILD_SUCCEEDING")
    push = body.index('docker push "$MCP_QUARANTINE_REPO:$IMAGE_TAG"')
    resolve = body.index("resolve_ecr_image.py resolve-platform")
    provenance = body.index("source_provenance.py verify-oci-revision")
    wait = body.index("aws ecr wait image-scan-complete")
    scan = body.index("python3 infra/codebuild/verify_ecr_scan.py")
    second_guard = body.index("CODEBUILD_BUILD_SUCCEEDING", first_guard + 1)
    pull = body.index('docker pull "$MCP_QUARANTINE_REPO@$VERIFIED_QUARANTINE_DIGEST"')
    release_push = body.index('docker push "$MCP_RELEASE_REPO:$IMAGE_TAG"')
    equality = body.index('[ "$RELEASE_DIGEST" = "$VERIFIED_QUARANTINE_DIGEST" ]')
    assert (
        first_guard
        < push
        < resolve
        < provenance
        < wait
        < scan
        < second_guard
        < pull
        < release_push
        < equality
    )
    assert "--deny-all" in body
    assert "ecr_scan_exceptions.json" not in body
    assert "BatchDeleteImage" not in body


def test_registry_and_s3_destinations_ignore_hostile_overrides() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 3
    assert body.count('EXPECTED_ACCOUNT_ID="718959508629"') == 3
    assert body.count('EXPECTED_REGION="ap-northeast-1"') == 3
    assert 'MCP_QUARANTINE_REPO="$ECR_REGISTRY/teamagent-mcp-quarantine"' in body
    assert 'MCP_RELEASE_REPO="$ECR_REGISTRY/teamagent-mcp"' in body
    assert body.count("unset ECR_REGISTRY MCP_REPO MCP_QUARANTINE_REPO MCP_RELEASE_REPO") == 3
    assert body.count('--registry-id "$EXPECTED_ACCOUNT_ID"') >= 7
    assert "attacker" not in body


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
