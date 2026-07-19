from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ECR = ROOT / "infra" / "terraform" / "ecr.tf"
TIKTOK = ROOT / "infra" / "terraform" / "tiktok_acquire.tf"
TERRAFORM_DIR = ROOT / "infra" / "terraform"


def _resource(body: str, resource_type: str, name: str) -> str:
    marker = f'resource "{resource_type}" "{name}"'
    return body.split(marker, maxsplit=1)[1].split('\nresource "', maxsplit=1)[0]


def _assert_repository_controls(body: str, name: str) -> None:
    repository = _resource(body, "aws_ecr_repository", name)
    assert 'image_tag_mutability = "IMMUTABLE"' in repository
    assert "scan_on_push = true" in repository
    assert 'encryption_type = "AES256"' in repository


def test_release_quarantine_and_verified_candidate_repositories_have_full_controls() -> None:
    body = ECR.read_text(encoding="utf-8")

    for repository in (
        "openclaw",
        "openclaw_quarantine",
        "openclaw_verified_candidates",
        "openclaw_media",
        "openclaw_media_quarantine",
        "openclaw_media_verified_candidates",
        "mcp",
        "mcp_quarantine",
        "mcp_verified_candidates",
        "mcp_media",
        "mcp_media_quarantine",
        "mcp_media_verified_candidates",
    ):
        _assert_repository_controls(body, repository)
    for repository in (
        "openclaw_quarantine",
        "openclaw_media_quarantine",
        "mcp_quarantine",
        "mcp_media_quarantine",
    ):
        lifecycle = _resource(body, "aws_ecr_lifecycle_policy", repository)
        assert "local.ecr_quarantine_lifecycle_policy" in lifecycle
    for repository in (
        "openclaw_verified_candidates",
        "openclaw_media_verified_candidates",
        "mcp_verified_candidates",
        "mcp_media_verified_candidates",
    ):
        lifecycle = _resource(body, "aws_ecr_lifecycle_policy", repository)
        assert "local.ecr_verified_candidate_lifecycle_policy" in lifecycle
    assert 'description  = "expire all quarantined candidates after 2 days"' in body
    assert 'tagStatus   = "any"' in body
    assert "countNumber = 2" in body
    assert "ecr_release_lifecycle_policy" not in body
    for repository in (
        "openclaw",
        "openclaw_media",
        "mcp",
        "mcp_media",
    ):
        assert f'resource "aws_ecr_lifecycle_policy" "{repository}"' not in body


def test_tiktok_release_and_quarantine_repositories_have_full_controls() -> None:
    release_body = TIKTOK.read_text(encoding="utf-8")
    ecr_body = ECR.read_text(encoding="utf-8")

    _assert_repository_controls(release_body, "tiktok_acquire")
    _assert_repository_controls(ecr_body, "tiktok_acquire_quarantine")
    _assert_repository_controls(ecr_body, "tiktok_acquire_verified_candidates")
    assert 'name                 = "${local.tk_name}-quarantine"' in ecr_body
    assert 'resource "aws_ecr_lifecycle_policy" "tiktok_acquire"' not in ecr_body
    assert 'resource "aws_ecr_lifecycle_policy" "tiktok_acquire_quarantine"' in ecr_body
    lifecycle = _resource(
        ecr_body,
        "aws_ecr_lifecycle_policy",
        "tiktok_acquire_verified_candidates",
    )
    assert "local.ecr_verified_candidate_lifecycle_policy" in lifecycle


def test_only_rejected_quarantine_graphs_are_subject_to_candidate_cleanup() -> None:
    body = ECR.read_text(encoding="utf-8")

    assert "can match only an explicitly" in body
    candidate_policy = body.split(
        "ecr_verified_candidate_lifecycle_policy = jsonencode(",
        maxsplit=1,
    )[1].split("ecr_quarantine_lifecycle_policy = jsonencode(", maxsplit=1)[0]
    quarantine_policy = body.split("ecr_quarantine_lifecycle_policy = jsonencode(", maxsplit=1)[
        1
    ].split('resource "aws_ecr_lifecycle_policy"', maxsplit=1)[0]
    assert '"tagged"' in candidate_policy
    assert 'tagPrefixList = ["rejected-"]' in candidate_policy
    assert 'tagPrefixList = ["verified-"]' not in candidate_policy
    assert '"any"' not in candidate_policy
    assert "countNumber = 2" in " ".join(candidate_policy.split())
    assert 'tagStatus   = "any"' in quarantine_policy
    assert "countNumber = 2" in quarantine_policy
    assert "ecr_release_lifecycle_policy" not in body


def test_production_execution_roles_cannot_pull_any_quarantine_image() -> None:
    body = ECR.read_text(encoding="utf-8")

    assert 'resource "aws_iam_policy" "deny_quarantine_runtime_pull"' in body
    assert 'sid    = "DenyQuarantineRuntimePull"' in body
    assert '"ecr:BatchGetImage"' in body
    assert '"ecr:GetDownloadUrlForLayer"' in body
    for repository in (
        "aws_ecr_repository.mcp_quarantine.arn",
        "aws_ecr_repository.mcp_verified_candidates.arn",
        "aws_ecr_repository.openclaw_quarantine.arn",
        "aws_ecr_repository.openclaw_verified_candidates.arn",
        "aws_ecr_repository.openclaw_media_quarantine.arn",
        "aws_ecr_repository.openclaw_media_verified_candidates.arn",
        "teamagent-dev-tiktok-acquire-quarantine",
        "teamagent-dev-tiktok-acquire-verified-candidates",
    ):
        assert repository in body
    execution_roles = {
        "openclaw",
        "mcp",
        "connect_web",
        "ingest",
        "canary",
        "morning_digest",
        "x_buzz",
        "tiktok",
    }
    for role in execution_roles:
        marker = f'resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_{role}"'
        assert marker in body
    assert body.count("policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn") == len(
        execution_roles
    )


def test_production_task_definitions_keep_release_inputs_and_never_use_quarantine() -> None:
    task_files = (
        "canary_schedule.tf",
        "connect_web.tf",
        "fargate.tf",
        "ingest_schedule.tf",
        "morning_digest_schedule.tf",
        "tiktok_acquire.tf",
        "x_research.tf",
    )
    bodies = {name: (TERRAFORM_DIR / name).read_text(encoding="utf-8") for name in task_files}

    for name, body in bodies.items():
        task_sections = "\n".join(
            section for section in body.split('resource "aws_ecs_task_definition"')[1:]
        )
        assert "quarantine" not in task_sections.lower(), name
    assert re.search(r"image\s+=\s+var\.mcp_image", bodies["connect_web.tf"])
    assert re.search(r"image\s+=\s+var\.mcp_image", bodies["fargate.tf"])
    assert re.search(r"image\s+=\s+local\.media_worker_image", bodies["tiktok_acquire.tf"])
    assert re.search(r"image\s+=\s+var\.openclaw_image", bodies["fargate.tf"])
