from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "infra" / "terraform" / "image_release_gate.tf"
ECR = ROOT / "infra" / "terraform" / "ecr.tf"
EVIDENCE = ROOT / "infra" / "codebuild" / "release_evidence.py"
ATTESTOR = ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml"
PROMOTER = ROOT / "infra" / "codebuild" / "image-promoter-buildspec.yml"
BOOTSTRAP_TARGETS = (
    ROOT / "infra" / "terraform" / "codebuild_provenance_bootstrap_targets.txt"
)


def test_terraform_uses_a_hard_precondition_not_a_warning_only_check() -> None:
    body = GATE.read_text(encoding="utf-8")

    assert 'resource "terraform_data" "production_image_release_gate"' in body
    assert "lifecycle {" in body
    assert "precondition {" in body
    assert 'check "production_images_require_signed_digest_release_evidence"' not in body
    assert "local.deployment_references_are_digest_only" in body
    assert "local.deployment_contracts_are_ready" in body
    assert "local.deployment_evidence_is_complete" in body
    assert "tiktok   = var.enable_tiktok_acquire" in body
    assert "!local.deployment_pipeline_enabled[pipeline]" in body
    assert "signed_image_release_gate[0].result.verified" in body


def test_every_deployable_pipeline_accepts_only_a_fixed_release_repo_digest() -> None:
    body = GATE.read_text(encoding="utf-8")
    evidence = EVIDENCE.read_text(encoding="utf-8")

    for repository in (
        "teamagent-mcp@sha256:",
        "teamagent-openclaw@sha256:",
        "teamagent-dev-tiktok-acquire@sha256:",
    ):
        assert repository.replace(".", r"\.") in body or repository in body
    assert "verified-candidates@sha256" not in body
    assert "quarantine@sha256" not in body
    assert "image_release_evidence" in body
    assert "release.ready" in body
    assert "filesha256" in body
    assert '"terraform-gate"' in body
    assert "signing_key_arn" in body
    assert "encryption_key_arn" in body
    assert "_validate_promoted_release(validated_receipt)" in evidence
    assert '"batch-get-image"' in evidence
    assert evidence.count('"list-image-referrers"') == evidence.count('"50"')


def test_promoter_is_receipt_only_and_never_executes_repository_source() -> None:
    body = PROMOTER.read_text(encoding="utf-8")

    verify = body.index("aws kms verify")
    schema = body.index("verify-release-receipt")
    guard = body.index("promoter gate failed before release write")
    copy = body.index("oras cp --recursive")
    assert verify < schema < guard < copy
    assert "git " not in body
    assert "source.zip" not in body
    assert "aws kms sign" not in body
    assert "aws s3api put-object" not in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")
    assert "DESTINATION_DIGEST" in body
    assert '[ "$DESTINATION_DIGEST" = "$DIGEST" ]' in body
    assert body.count("signature.referrer_digest") == 3
    assert '.digest == $image_signature' in body
    assert '.digest == $signature' in body


def test_active_or_rollback_attestation_rechecks_candidate_signatures() -> None:
    body = ATTESTOR.read_text(encoding="utf-8")

    assert 'KMS_URI="awskms:///$SIGNING_KEY_ARN"' in body
    assert "verify-release-locator" in body
    assert "candidate is missing, changed, or an image index" in body
    assert body.count("cosign verify --experimental-oci11") == 3
    assert "verify_cosign_claim" in body
    assert body.count("signature.referrer_digest") == 3
    assert '.digest == $image_signature' in body
    assert '.digest == $signature' in body
    assert "authorize-release-receipt" in body
    assert "date -u -d '+30 days'" in body
    assert "date -u -d '+30 minutes'" in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")


def test_lifecycle_physically_separates_expiring_candidates_from_release_tags() -> None:
    body = ECR.read_text(encoding="utf-8")

    assert 'description  = "expire verified candidates after 30 days"' in body
    assert 'description  = "expire all quarantined candidates after 2 days"' in body
    assert 'description  = "expire only untagged release artifacts after 365 days"' in body
    assert 'tagStatus   = "untagged"' in body
    assert 'tagStatus   = "any"' in body
    for name in (
        "mcp_verified_candidates",
        "openclaw_verified_candidates",
        "openclaw_media_verified_candidates",
        "tiktok_acquire_verified_candidates",
    ):
        assert f'aws_ecr_lifecycle_policy" "{name}' in body
    assert "DenyQuarantineRuntimePull" in body


def test_ready_false_bootstrap_scope_cannot_target_runtime_or_the_deploy_gate() -> None:
    targets = {
        line
        for line in BOOTSTRAP_TARGETS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert "aws_codebuild_project.image_attestor" in targets
    assert "aws_codebuild_project.image_promoter" in targets
    assert "aws_ecr_repository.mcp_quarantine" in targets
    assert "aws_ecr_repository.mcp_verified_candidates" in targets
    assert "aws_iam_policy.deny_quarantine_runtime_pull" in targets
    assert "terraform_data.production_image_release_gate" not in targets
    assert not any(
        token in target
        for target in targets
        for token in (
            "aws_ecs_",
            "aws_cloudwatch_event_",
            "aws_scheduler_",
            "task_definition",
        )
    )
    declared = set()
    for path in (
        ROOT / "infra" / "terraform" / "codebuild.tf",
        ROOT / "infra" / "terraform" / "ecr.tf",
    ):
        declared.update(
            f"{resource_type}.{name}"
            for resource_type, name in re.findall(
                r'^resource "([^"]+)" "([^"]+)"',
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
    assert targets == declared
