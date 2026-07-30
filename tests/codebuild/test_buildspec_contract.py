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
RELEASE_CONTRACT = ROOT / "infra" / "codebuild" / "teamagent_core_media_release_contract.json"


def _load_provenance() -> object:
    spec = importlib.util.spec_from_file_location("buildspec_contract_provenance", PROVENANCE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROVENANCE = _load_provenance()


def test_source_and_core_media_contract_gates_run_before_both_final_builds() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    hash_position = body.index("__SOURCE_PROVENANCE_SHA256__")
    ready_position = body.index('"$BUNDLE_PROVENANCE" assert-contract-ready')
    verify_declaration_position = body.index("release_evidence.py verify-source-declaration")
    approval_position = body.index("release_evidence.py assert-approved-release")
    verify_source_position = body.index("source_provenance.py verify-source")
    interface_position = body.index('"$BUNDLE_PROVENANCE" verify-source-interface')
    pair_position = body.index("validate-contract-pair")
    first_build_position = body.index("docker buildx build")
    second_build_position = body.index(
        "docker buildx build",
        first_build_position + 1,
    )
    assert (
        hash_position
        < ready_position
        < verify_declaration_position
        < approval_position
        < verify_source_position
        < interface_position
        < pair_position
        < first_build_position
        < second_build_position
    )
    assert (
        "--runtime-contract \\\n"
        '            "$CONTEXT_VERIFY_DIR/infra/codebuild/teamagent_runtime_contract.json"'
    ) in body
    assert (
        "--contract \\\n"
        '            "$CONTEXT_VERIFY_DIR/infra/codebuild/'
        'teamagent_core_media_release_contract.json"'
    ) in body
    assert '--repo-root "$CONTEXT_VERIFY_DIR"' in body
    assert (
        'python3 "$BUNDLE_PROVENANCE" assert-contract-ready \\\n'
        '          --contract "$RELEASE_CONTRACT"'
    ) in body
    assert (
        'python3 "$BUNDLE_PROVENANCE" verify-source-interface \\\n'
        '          --repo-root "$CODEBUILD_SRC_DIR" \\\n'
        '          --contract "$RELEASE_CONTRACT" \\\n'
        '          --deploy-log "$DEPLOY_LOG"'
    ) in body
    assert (
        "python3 infra/codebuild/source_provenance.py assert-contract-ready \\\n"
        "          --contract infra/codebuild/teamagent_runtime_contract.json"
    ) in body
    assert ('--repo-root "$CONTEXT_VERIFY_DIR"') in body
    assert "assert-release-ready" not in body
    assert "--approval-locators-json" in body
    assert "mcp: {" in body
    for required in (
        '--expected-commit "$GIT_COMMIT"',
        '--expected-branch "$GIT_BRANCH"',
        "--expected-with-scrape-tools true",
        '--expected-app-html-version-id "$APP_HTML_VERSION_ID"',
        '--expected-app-html-sha256 "$APP_HTML_SHA256"',
        '--expected-runtime-contract-sha256 "$SOURCE_MANIFEST_CONTRACT_SHA256"',
        ".teamagent-source-manifest.json",
    ):
        assert required in body


def test_core_and_media_builds_pass_every_required_provenance_binding() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    contract = json.loads(RELEASE_CONTRACT.read_text(encoding="utf-8"))

    assert body.count("docker buildx build") == 2
    assert '--file "$CONTEXT_VERIFY_DIR/infra/docker/Dockerfile.teamagent-mcp"' in body
    assert ('--file "$CONTEXT_VERIFY_DIR/infra/docker/Dockerfile.teamagent-media-worker"') in body
    assert body.count('- <"$BUILD_CONTEXT_TAR"') == 2
    assert body.count("--target final") == 2
    assert body.count("--platform linux/arm64") == 2
    assert body.count("--provenance=mode=max") == 2
    assert body.count("--sbom=true") == 2
    assert body.count("--push") == 2
    runtime_arguments = {
        "RUNTIME_CONTRACT_SHA256",
        "RUNTIME_RECEIPT_B64",
        "RUNTIME_RECEIPT_SHA256",
    }
    for subject in contract["subjects"]:
        for argument in subject["required_build_args"]:
            if subject["name"] == "core" and argument in runtime_arguments:
                continue
            assert f'--build-arg "{argument}=' in body
    assert (
        'python3 "$CONTEXT_VERIFY_DIR/infra/codebuild/source_provenance.py" \\\n'
        "          docker-build-arguments"
    ) in body
    assert (
        '--contract "$CONTEXT_VERIFY_DIR/infra/codebuild/teamagent_runtime_contract.json"'
    ) in body
    for argument in runtime_arguments:
        assert f"\n          {argument}" in body
    core_build = body.index("docker buildx build")
    media_build = body.index("docker buildx build", core_build + 1)
    runtime_expansion = body.index('"${DOCKER_RUNTIME_BUILD_ARGS[@]}"')
    assert core_build < runtime_expansion < media_build
    assert body.count('"${DOCKER_RUNTIME_BUILD_ARGS[@]}"') == 1
    assert '--build-arg "WITH_SCRAPE_TOOLS=true"' not in body
    assert '--tag "$CORE_QUARANTINE_REPOSITORY:$CORE_TAG"' in body
    assert '--tag "$MEDIA_QUARANTINE_REPOSITORY:$MEDIA_TAG"' in body
    assert body.count('--build-arg "RELEASE_APPROVAL_SHA256=$RELEASE_APPROVAL_SHA256"') == 2
    for dockerfile in (
        ROOT / "infra" / "docker" / "Dockerfile.teamagent-mcp",
        ROOT / "infra" / "docker" / "Dockerfile.teamagent-media-worker",
    ):
        dockerfile_body = dockerfile.read_text(encoding="utf-8")
        assert "ARG RELEASE_APPROVAL_SHA256" in dockerfile_body
        assert (
            'io.teamagent.build.release-approval-sha256="$RELEASE_APPROVAL_SHA256"'
            in dockerfile_body
        )


def test_post_build_uses_bundle_context_binding_and_core_receipt_verifier() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    bundle_start = body.index("teamagent_bundle_provenance.py verify-oci-config")
    bundle_end = body.index('if [ "$subject" = "core" ]', bundle_start)
    bundle_call = body[bundle_start:bundle_end]
    assert "--runtime-contract infra/codebuild/teamagent_runtime_contract.json" in bundle_call
    assert '--expected-build-context-sha256 "$BUILD_CONTEXT_SHA256"' in bundle_call
    assert '--expected-release-approval-sha256 "$RELEASE_APPROVAL_SHA256"' in bundle_call

    receipt_start = body.index("source_provenance.py verify-oci-revision", bundle_end)
    receipt_end = body.index("\n          fi", receipt_start)
    receipt_call = body[receipt_start:receipt_end]
    assert '--expected-commit "$GIT_COMMIT"' in receipt_call
    assert "--contract infra/codebuild/teamagent_runtime_contract.json" in receipt_call
    assert '--expected-runtime-contract-sha256 "$SOURCE_MANIFEST_CONTRACT_SHA256"' in receipt_call
    for legacy_argument in (
        "--expected-with-scrape-tools",
        "--expected-app-html-version-id",
        "--expected-app-html-sha256",
    ):
        assert legacy_argument not in receipt_call


def test_active_schema_alignment_is_released_with_the_same_hygiene_intact() -> None:
    # This used to assert release.ready is False.  The flip to true is a
    # deliberate, operator-authorised decision, so the assertion is inverted
    # rather than deleted: the contract must now be *provably* ready, and every
    # hygiene check that surrounded the blocked state still has to hold.
    contract = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))

    assert contract["release"]["ready"] is True
    # The schema forbids saying anything here while ready is true
    # (teamagent_bundle_provenance.py:446 -- ready XOR blocked_reason), which is
    # why the provisional nature of this release lives in the commit message and
    # the runbook instead of in the contract.
    assert contract["release"]["blocked_reason"] == ""
    assert set(contract["release"]) == {"ready", "blocked_reason"}
    assert "approval_record" not in contract
    serialized = json.dumps(contract)
    assert all(entry["value"] != "latest-dev" for entry in contract["receipt"]["entries"])
    assert "playwright" not in serialized.lower()
    assert "archive_sha256" not in serialized
    # The six measured values are what made the flip defensible; losing any of
    # them must fail here, not silently ship.
    assert len(contract["receipt"]["entries"]) >= 6
    PROVENANCE.require_release_ready(PROVENANCE.load_runtime_contract(ACTIVE_CONTRACT))


def test_outer_contract_pin_tracks_the_inner_contract_bytes() -> None:
    # Flipping the inner contract changes its bytes, so the outer pin has to be
    # re-cut in the same commit.  Nothing else in the suite catches a stale pin
    # at rest, and a stale pin only surfaces mid-release as
    # "outer source runtime contract SHA-256 does not match inner raw bytes".
    import hashlib

    outer = json.loads(RELEASE_CONTRACT.read_text(encoding="utf-8"))
    inner_sha256 = hashlib.sha256(ACTIVE_CONTRACT.read_bytes()).hexdigest()
    assert outer["source_runtime_contract"]["sha256"] == inner_sha256
    assert outer["release"]["ready"] is True
    assert outer["release"]["blocked_reason"] == ""


def test_provenance_inputs_have_no_unknown_or_implicit_defaults() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

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
    ):
        assert f"${{{name}:?{name} must be explicitly provided}}" in body
        assert f"${{{name}:-" not in body
    assert "unknown" not in body.lower()


def test_app_html_uses_only_pinned_version_and_verified_bytes() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert "--bucket teamagent-dev-raw-files" in body
    assert "--key codebuild/connect-web-app.html" in body
    assert "BAKED_APP_HTML_KEY=\"$(jq -er '.app_html.baked_fallback.key'" in body
    assert '--key "$BAKED_APP_HTML_KEY"' in body
    assert '--version-id "$APP_HTML_VERSION_ID"' in body
    assert '--version-id "$BAKED_APP_HTML_VERSION_ID"' in body
    assert '--expected-bucket-owner "$EXPECTED_ACCOUNT_ID"' in body
    assert (
        "[ \"$(sha256sum /tmp/production-connect-web-app.html | awk '{print $1}')\" "
        '= "$APP_HTML_SHA256" ]'
    ) in body
    assert (
        'sha256sum "$CONTEXT_VERIFY_DIR/src/teamagent/connect_web/static/app.html"'
        " | awk '{print $1}'"
    ) in body
    assert '"$BAKED_APP_HTML_SHA256" ]' in body
    assert "install -m 0644 /tmp/baked-connect-web-app.html" not in body
    assert '[ "$APP_HTML_SHA256" != "$BAKED_APP_HTML_SHA256" ]' in body
    assert '--build-arg "APP_HTML_SHA256=$APP_HTML_SHA256"' in body
    assert '--build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID"' in body
    assert '--build-arg "APP_HTML_MANIFEST_SHA256=$VAULT_MANIFEST_SHA256"' in body
    assert '--build-arg "APP_HTML_BUILD_INPUTS_SHA256=$BUILD_INPUTS_SHA256"' in body
    assert '--build-arg "APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256"' in body
    assert '--build-arg "BAKED_APP_HTML_VERSION_ID=$BAKED_APP_HTML_VERSION_ID"' in body
    assert "aws s3 cp" not in body
    assert "aws s3api put-object" not in body


def test_both_builds_stop_at_quarantine_and_source_free_projects_own_promotion() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")
    attestor = (ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml").read_text()
    promoter = (ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml").read_text()

    first_guard = body.index("CODEBUILD_BUILD_SUCCEEDING")
    core_push = body.index('--tag "$CORE_QUARANTINE_REPOSITORY:$CORE_TAG"')
    media_push = body.index('--tag "$MEDIA_QUARANTINE_REPOSITORY:$MEDIA_TAG"')
    resolve = body.index("resolve_ecr_image.py resolve-platform")
    provenance = body.index("teamagent_bundle_provenance.py verify-oci-config")
    wait = body.index("aws ecr wait image-scan-complete")
    scan = body.index("python3 infra/codebuild/verify_ecr_scan.py")
    second_guard = body.index("CODEBUILD_BUILD_SUCCEEDING", first_guard + 1)
    assert core_push < media_push < first_guard < resolve < provenance < wait < scan < second_guard
    assert "--deny-all" in body
    assert '--expected-config-digest "$config_digest"' in body
    assert "ecr_scan_exceptions.json" not in body
    assert "BatchDeleteImage" not in body
    assert "teamagent-mcp-verified-candidates" not in body
    assert "teamagent-media-worker-verified-candidates" not in body
    assert "verify_actual_image.sh" in attestor
    assert 'PROMOTION_CHANNEL" = "verified-candidate"' in promoter
    assert 'SOURCE_REPOSITORY="$QUARANTINE_REPOSITORY"' in promoter
    assert 'DESTINATION_REPOSITORY="$CANDIDATE_REPOSITORY"' in promoter
    assert 'SOURCE_REPOSITORY="$CANDIDATE_REPOSITORY"' in promoter
    assert 'DESTINATION_REPOSITORY="$RELEASE_REPOSITORY"' in promoter
    assert "oras cp --recursive" in promoter


@pytest.mark.parametrize(
    ("path", "write_marker"),
    (
        ("mcp-source-publisher-buildspec.yml", "aws s3api put-object"),
        ("buildspec.yml", "docker buildx build"),
        ("image-attestor-buildspec.yml", "/tmp/verify_actual_image.sh"),
        ("image-promoter-buildspec.yml", "aws ecr get-login-password"),
    ),
)
def test_mcp_buildspecs_separate_static_ready_from_external_approval(
    path: str,
    write_marker: str,
) -> None:
    body = (ROOT / "infra" / "codebuild" / path).read_text(encoding="utf-8")

    static_ready = body.index("assert-contract-ready")
    approved = body.index("assert-approved-release")
    write = body.index(write_marker, approved)
    assert static_ready < approved < write
    assert "--approval-locators-json" in body
    assert "mcp: {" in body
    for name in (
        "APPROVAL_PAYLOAD_BUCKET",
        "APPROVAL_PAYLOAD_KEY",
        "APPROVAL_PAYLOAD_VERSION_ID",
        "APPROVAL_PAYLOAD_SHA256",
        "APPROVAL_SIGNATURE_BUCKET",
        "APPROVAL_SIGNATURE_KEY",
        "APPROVAL_SIGNATURE_VERSION_ID",
        "APPROVAL_SIGNATURE_SHA256",
        "APPROVAL_SIGNING_KEY_ARN",
    ):
        assert name in body


def test_promoter_reverifies_signed_source_before_external_approval_and_ecr() -> None:
    body = (ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml").read_text(
        encoding="utf-8"
    )

    receipt = body.index("verify-release-receipt")
    source_head = body.index("source-$source_object-head.json", receipt)
    source_signature = body.index("source-declaration-kms-verify.json", source_head)
    source_binding = body.index("verify-source-approval-binding", source_signature)
    approval = body.index("assert-approved-release", source_binding)
    login = body.index("aws ecr get-login-password", approval)
    assert receipt < source_head < source_signature < source_binding < approval < login
    assert '--version-id "$source_version"' in body
    assert '.ObjectLockMode == "COMPLIANCE"' in body
    assert '.ObjectLockMode == "GOVERNANCE"' in body
    assert ".SSEKMSKeyId == $kms" in body


def test_registry_and_s3_destinations_ignore_hostile_overrides() -> None:
    body = BUILDSPEC.read_text(encoding="utf-8")

    assert body.count("export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true") == 1
    assert body.count('EXPECTED_ACCOUNT_ID="718959508629"') == 1
    assert body.count('EXPECTED_REGION="ap-northeast-1"') == 1
    assert 'CORE_QUARANTINE_REPOSITORY="$ECR_REGISTRY/teamagent-mcp-quarantine"' in body
    assert 'MEDIA_QUARANTINE_REPOSITORY="$ECR_REGISTRY/teamagent-media-worker-quarantine"' in body
    assert "unset ECR_REGISTRY CORE_QUARANTINE_REPOSITORY" in body
    assert body.count("unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY") == 1
    assert 'TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"' in body
    assert body.count('--registry-id "$EXPECTED_ACCOUNT_ID"') >= 5
    assert '$ECR_REGISTRY/teamagent-mcp"' not in body
    assert "attacker" not in body


def test_independent_publisher_pins_origin_dev_versioned_source_and_current_app() -> None:
    body = (ROOT / "infra" / "codebuild" / "mcp-source-publisher-buildspec.yml").read_text(
        encoding="utf-8"
    )

    assert 'git ls-remote --exit-code --heads "$EXPECTED_REPOSITORY"' in body
    assert '"$EXPECTED_HEAD_REF" "$EXPECTED_BASE_REF"' in body
    assert "worktree add --quiet --detach" in body
    assert 'merge-base "$EXPECTED_BASE_OID" "$EXPECTED_COMMIT"' in body
    assert "get-bucket-versioning" in body
    assert "--expected-bucket-owner 718959508629" in body
    assert 'git -C "$PUBLISHER_CHECKOUT" archive' in body
    assert "canonical_build_context.py" in body
    assert "--build-context-sha256" in body
    assert (
        'SOURCE_DECLARATION_KEY="source-declarations/mcp/$EXPECTED_COMMIT/$SOURCE_SHA256/$PUBLISHED_SOURCE_VERSION_ID.json"'
        in body
    )
    assert "--object-lock-mode GOVERNANCE" in body
    assert "aws kms sign" in body
    assert "teamagent_bundle_provenance.py production-record" in body
    embedded_contract_hash = body.index("embedded release contract hash mismatch")
    checkout_contract_hash = body.index(
        "source commit release contract differs from trusted contract"
    )
    checkout_verifier_hash = body.index("source commit bundle verifier differs from trusted bytes")
    pair = body.index("teamagent_bundle_provenance.py validate-contract-pair")
    ready = body.index("teamagent_bundle_provenance.py assert-contract-ready")
    approval = body.index("release_evidence.py assert-approved-release")
    assert (
        embedded_contract_hash
        < checkout_contract_hash
        < checkout_verifier_hash
        < pair
        < ready
        < approval
    )
    assert (
        "--runtime-contract \\\n"
        '            "$PUBLISHER_CHECKOUT/infra/codebuild/'
        'teamagent_runtime_contract.json"'
    ) in body
    assert "--contract /tmp/teamagent_core_media_release_contract.json" in body
    assert ('--repo-root "$PUBLISHER_CHECKOUT"') in body
    assert (
        "teamagent_bundle_provenance.py assert-contract-ready \\\n"
        "          --contract /tmp/teamagent_core_media_release_contract.json"
    ) in body
    assert (
        "teamagent_bundle_provenance.py verify-source-interface \\\n"
        '          --repo-root "$EXTRACTED" \\\n'
        '          --contract "$EXTRACTED/infra/codebuild/'
        'teamagent_core_media_release_contract.json" \\\n'
        '          --deploy-log "$EXTRACTED/infra/deploy_log.md"'
    ) in body
    assert "assert-release-ready" not in body
    assert "--approval-evidence-json" in body
    assert '--version-id "$APP_HTML_VERSION_ID"' in body
    assert '--version-id "$BAKED_APP_HTML_VERSION_ID"' in body
    assert '--baked-fallback "$BAKED_APP_HTML"' in body
    assert '"src/teamagent/connect_web/static/app.html"' not in body
    assert "archive, manifest = sys.argv[1:]" in body
    assert '".teamagent-source-manifest.json": manifest' in body
    assert "aws s3 cp" not in body
    assert "aws s3api copy-object" not in body
    for get_object in body.split("aws s3api get-object")[1:]:
        call = get_object.split(")", maxsplit=1)[0]
        if "MCP_SOURCE_PUBLISHER_BUILDSPEC_KEY" in call:
            # CodeBuild cannot reference a buildspec by VersionId: a
            # "?versionId=" suffix is read as part of the key and the fetch
            # fails with NoSuchKey (measured 2026-07-27). This one fetch is
            # pinned by a content-addressed key plus a SHA-256 comparison,
            # which binds the exact bytes rather than the exact version.
            assert "$MCP_SOURCE_PUBLISHER_BUILDSPEC_SHA256" in body
            assert 'case "$MCP_SOURCE_PUBLISHER_BUILDSPEC_KEY" in' in body
            continue
        assert "--version-id" in call

    contract = json.loads(RELEASE_CONTRACT.read_text(encoding="utf-8"))
    assert contract["app_html"]["production"] == {
        "app_html_s3_version_id": "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee",
        "app_html_sha256": ("03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"),
        "vault_manifest_sha256": (
            "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
        ),
        "build_inputs_sha256": ("6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"),
    }


def test_terraform_embeds_all_verifier_and_contract_hashes() -> None:
    body = TERRAFORM.read_text(encoding="utf-8")

    for path, placeholder in (
        ("source_provenance.py", "__SOURCE_PROVENANCE_SHA256__"),
        (
            "teamagent_schema_versions.py",
            "__TEAMAGENT_SCHEMA_VERSIONS_SHA256__",
        ),
        (
            "teamagent_release_approval.py",
            "__TEAMAGENT_RELEASE_APPROVAL_SHA256__",
        ),
        (
            "teamagent_bundle_provenance.py",
            "__TEAMAGENT_BUNDLE_PROVENANCE_SHA256__",
        ),
        ("resolve_ecr_image.py", "__ECR_IMAGE_RESOLVER_SHA256__"),
        ("verify_ecr_scan.py", "__ECR_SCAN_GATE_SHA256__"),
    ):
        assert path in body
        assert placeholder in body
    assert "mcp_release_contract_sha256" in body
    assert "teamagent_runtime_contract.json" in body
    assert "teamagent_core_media_release_contract.json" in body
    assert "teamagent_core_media_release_contract.json" in body


# Every production caller of assert-approved-release picks its operation from a
# literal in one of these files.  The "drill" operation accepts a provisional
# gate with no byte pin and no sunset, so flipping any of these literals to
# "drill" would open a permanent, unaudited equivalent of the initial-release
# exemption.  Nothing pinned these literals before this test.
_APPROVAL_OPERATION_CALLERS = {
    "infra/codebuild/buildspec.yml": {"build"},
    "infra/codebuild/mcp-source-publisher-buildspec.yml": {"build"},
    "infra/codebuild/image-attestor-buildspec.yml": {"build", "authorize"},
    "infra/codebuild/image-promoter-buildspec.yml": {"build", "authorize"},
    "infra/deploy/build_teamagent_image.sh": {"build"},
    "infra/deploy/authorize_image_release.sh": {"authorize"},
}


@pytest.mark.parametrize(
    ("relative_path", "expected_operations"),
    sorted(_APPROVAL_OPERATION_CALLERS.items()),
)
def test_release_approval_operation_literals_are_pinned(
    relative_path: str,
    expected_operations: set[str],
) -> None:
    import re

    text = (ROOT / relative_path).read_text(encoding="utf-8")
    # Both spellings are in use: a direct --operation flag, and an
    # APPROVAL_OPERATION shell variable that the flag then expands.
    found = set(re.findall(r"--operation\s+([a-z][a-z-]*)", text))
    found |= set(re.findall(r"APPROVAL_OPERATION=\"?([a-z][a-z-]*)\"?", text))
    assert found == expected_operations, (
        f"{relative_path} approval operations changed: {sorted(found)} "
        f"!= {sorted(expected_operations)}"
    )


def test_no_production_caller_overrides_the_validation_clock() -> None:
    # --now exists on the CLI, so a caller could otherwise wind the clock back
    # past the exemption sunset.
    for relative_path in _APPROVAL_OPERATION_CALLERS:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "--now" not in text, f"{relative_path} must not pass --now"
