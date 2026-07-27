from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
CONTRACT_PATH = ROOT / "infra" / "bootstrap" / "bootstrap_contract.json"
CODEBUILD_TF = TF_ROOT / "codebuild.tf"

EXPECTED_POST_CUT_MANAGED = frozenset(
    """
    aws_cloudwatch_log_group.codebuild_approval_publisher
    aws_cloudwatch_log_group.codebuild_image_attestor
    aws_cloudwatch_log_group.codebuild_image_promoter
    aws_cloudwatch_log_group.codebuild_mcp_source_publisher
    aws_cloudwatch_log_group.codebuild_openclaw_provenance
    aws_cloudwatch_log_group.codebuild_tiktok_image
    aws_codebuild_project.approval_publisher
    aws_codebuild_project.image_attestor
    aws_codebuild_project.image_promoter
    aws_codebuild_project.mcp_source_publisher
    aws_codebuild_project.openclaw_provenance
    aws_codebuild_project.tiktok_image
    aws_codestarconnections_connection.openclaw_codebuild
    aws_codestarconnections_connection.tiktok_codebuild
    aws_dynamodb_table.image_deployment_intents
    aws_ecr_lifecycle_policy.mcp_media_quarantine
    aws_ecr_lifecycle_policy.mcp_media_verified_candidates
    aws_ecr_lifecycle_policy.mcp_quarantine
    aws_ecr_lifecycle_policy.mcp_verified_candidates
    aws_ecr_lifecycle_policy.openclaw_media_quarantine
    aws_ecr_lifecycle_policy.openclaw_media_verified_candidates
    aws_ecr_lifecycle_policy.openclaw_quarantine
    aws_ecr_lifecycle_policy.openclaw_verified_candidates
    aws_ecr_lifecycle_policy.tiktok_acquire_quarantine
    aws_ecr_lifecycle_policy.tiktok_acquire_verified_candidates
    aws_ecr_repository.mcp
    aws_ecr_repository.mcp_media
    aws_ecr_repository.mcp_media_quarantine
    aws_ecr_repository.mcp_media_verified_candidates
    aws_ecr_repository.mcp_quarantine
    aws_ecr_repository.mcp_verified_candidates
    aws_ecr_repository.openclaw
    aws_ecr_repository.openclaw_media
    aws_ecr_repository.openclaw_media_quarantine
    aws_ecr_repository.openclaw_media_verified_candidates
    aws_ecr_repository.openclaw_quarantine
    aws_ecr_repository.openclaw_verified_candidates
    aws_ecr_repository.tiktok_acquire
    aws_ecr_repository.tiktok_acquire_quarantine
    aws_ecr_repository.tiktok_acquire_verified_candidates
    aws_iam_policy.approval_caller_override_a
    aws_iam_policy.approval_caller_override_b
    aws_iam_policy.approval_caller_override_c
    aws_iam_policy.approval_reader
    aws_iam_policy.runtime_automation_boundary
    aws_iam_role.alarm_recipient_ack_signer
    aws_iam_role.approval_caller
    aws_iam_role.approval_publisher
    aws_iam_role.codebuild
    aws_iam_role.codebuild_launcher
    aws_iam_role.image_attestor
    aws_iam_role.image_deployment_gate
    aws_iam_role.image_promoter
    aws_iam_role.mcp_source_publisher
    aws_iam_role.media_cutover_attestor
    aws_iam_role.openclaw_codebuild
    aws_iam_role.openclaw_publisher
    aws_iam_role.release_control_updater
    aws_iam_role.release_launcher
    aws_iam_role.runtime_automation
    aws_iam_role.tiktok_build_launcher
    aws_iam_role.tiktok_codebuild
    aws_iam_role_policy.alarm_recipient_ack_signer
    aws_iam_role_policy.approval_caller
    aws_iam_role_policy.approval_publisher
    aws_iam_role_policy.image_attestor
    aws_iam_role_policy.image_deployment_gate
    aws_iam_role_policy.image_promoter
    aws_iam_role_policy.mcp_source_publisher
    aws_iam_role_policy.media_cutover_attestor
    aws_iam_role_policy.openclaw_codebuild
    aws_iam_role_policy.release_control_updater
    aws_iam_role_policy.runtime_evidence_automation
    aws_iam_role_policy.tiktok_codebuild
    aws_iam_policy.runtime_automation_control_plane_manage_a
    aws_iam_policy.runtime_automation_control_plane_manage_b
    aws_iam_policy.runtime_automation_control_plane_core
    aws_iam_role_policy_attachment.approval_caller_override_a
    aws_iam_role_policy_attachment.approval_caller_override_b
    aws_iam_role_policy_attachment.approval_caller_override_c
    aws_iam_role_policy_attachment.approval_reader_attestor
    aws_iam_role_policy_attachment.approval_reader_build_launcher
    aws_iam_role_policy_attachment.approval_reader_deployment_gate
    aws_iam_role_policy_attachment.approval_reader_main_builder
    aws_iam_role_policy_attachment.approval_reader_promoter
    aws_iam_role_policy_attachment.approval_reader_release_launcher
    aws_iam_role_policy_attachment.approval_reader_runtime_automation
    aws_iam_role_policy_attachment.approval_reader_source_publisher
    aws_iam_role_policy_attachment.runtime_automation_control_plane_manage_a
    aws_iam_role_policy_attachment.runtime_automation_control_plane_manage_b
    aws_iam_role_policy_attachment.runtime_automation_control_plane_core
    aws_iam_user.release_caller
    aws_iam_user.release_control_update_caller
    aws_iam_user.tiktok_build_caller
    aws_iam_user_policy.aiia_dev_no_direct_start_build
    aws_iam_user_policy.release_caller
    aws_iam_user_policy.release_control_update_caller
    aws_iam_user_policy.tiktok_build_caller
    aws_kms_alias.alarm_recipient_ack
    aws_kms_alias.approval_signing
    aws_kms_alias.image_attestor_signing
    aws_kms_alias.image_release_evidence
    aws_kms_alias.mcp_source_publisher_signing
    aws_kms_alias.media_cutover_attestor
    aws_kms_alias.openclaw_evidence
    aws_kms_alias.openclaw_publisher_signing
    aws_kms_alias.tiktok_source_publisher_signing
    aws_kms_key.alarm_recipient_ack
    aws_kms_key.approval_signing
    aws_kms_key.image_attestor_signing
    aws_kms_key.image_release_evidence
    aws_kms_key.mcp_source_publisher_signing
    aws_kms_key.media_cutover_attestor
    aws_kms_key.openclaw_evidence
    aws_kms_key.openclaw_publisher_signing
    aws_kms_key.openclaw_rollout_signing
    aws_kms_key.tiktok_source_publisher_signing
    aws_s3_bucket.image_release_evidence
    aws_s3_bucket.openclaw_build_evidence
    aws_s3_bucket.raw_files
    aws_s3_bucket_lifecycle_configuration.image_release_evidence
    aws_s3_bucket_lifecycle_configuration.openclaw_build_evidence
    aws_s3_bucket_object_lock_configuration.image_release_evidence
    aws_s3_bucket_object_lock_configuration.openclaw_build_evidence
    aws_s3_bucket_policy.image_release_evidence
    aws_s3_bucket_policy.openclaw_build_evidence
    aws_s3_bucket_public_access_block.image_release_evidence
    aws_s3_bucket_public_access_block.openclaw_build_evidence
    aws_s3_bucket_server_side_encryption_configuration.image_release_evidence
    aws_s3_bucket_server_side_encryption_configuration.openclaw_build_evidence
    aws_s3_bucket_versioning.image_release_evidence
    aws_s3_bucket_versioning.openclaw_build_evidence
    aws_s3_object.approval_publisher_buildspec
    aws_s3_object.image_attestor_buildspec
    aws_s3_object.image_promoter_buildspec
    aws_s3_object.mcp_source_publisher_buildspec
    aws_s3_object.tiktok_image_buildspec
    """.split()
)

EXPECTED_POST_CUT_DATA = frozenset(
    """
    data.aws_iam_policy_document.aiia_dev_no_direct_start_build
    data.aws_iam_policy_document.alarm_recipient_ack_signer
    data.aws_iam_policy_document.alarm_recipient_ack_signer_assume
    data.aws_iam_policy_document.approval_caller
    data.aws_iam_policy_document.approval_caller_assume
    data.aws_iam_policy_document.approval_caller_override_a
    data.aws_iam_policy_document.approval_caller_override_b
    data.aws_iam_policy_document.approval_caller_override_c
    data.aws_iam_policy_document.approval_publisher
    data.aws_iam_policy_document.approval_publisher_assume
    data.aws_iam_policy_document.approval_reader
    data.aws_iam_policy_document.codebuild_launcher_assume
    data.aws_iam_policy_document.image_attestor
    data.aws_iam_policy_document.image_attestor_assume
    data.aws_iam_policy_document.image_deployment_gate
    data.aws_iam_policy_document.image_deployment_gate_assume
    data.aws_iam_policy_document.image_promoter
    data.aws_iam_policy_document.image_promoter_assume
    data.aws_iam_policy_document.image_release_evidence_bucket
    data.aws_iam_policy_document.main_codebuild_assume
    data.aws_iam_policy_document.mcp_source_publisher
    data.aws_iam_policy_document.mcp_source_publisher_assume
    data.aws_iam_policy_document.media_cutover_attestor
    data.aws_iam_policy_document.media_cutover_attestor_assume
    data.aws_iam_policy_document.openclaw_build_evidence_bucket
    data.aws_iam_policy_document.openclaw_codebuild
    data.aws_iam_policy_document.openclaw_codebuild_assume
    data.aws_iam_policy_document.openclaw_publisher_assume
    data.aws_iam_policy_document.release_caller
    data.aws_iam_policy_document.release_control_update_caller
    data.aws_iam_policy_document.release_control_updater
    data.aws_iam_policy_document.release_control_updater_assume
    data.aws_iam_policy_document.release_launcher_assume
    data.aws_iam_policy_document.runtime_automation_assume
    data.aws_iam_policy_document.runtime_automation_boundary
    data.aws_iam_policy_document.runtime_automation_control_plane_manage_a
    data.aws_iam_policy_document.runtime_automation_control_plane_manage_b
    data.aws_iam_policy_document.runtime_automation_control_plane_core
    data.aws_iam_policy_document.runtime_evidence_automation
    data.aws_iam_policy_document.tiktok_build_caller
    data.aws_iam_policy_document.tiktok_build_launcher_assume
    data.aws_iam_policy_document.tiktok_codebuild
    data.aws_iam_policy_document.tiktok_codebuild_assume
    data.aws_iam_user.aiia_dev
    """.split()
)

INTENTIONALLY_UNDECLARED_INLINE_POLICY_TARGETS = frozenset(
    {
        "aws_iam_role_policy.codebuild_launcher",
        "aws_iam_role_policy.openclaw_publisher",
        "aws_iam_role_policy.release_launcher",
        "aws_iam_role_policy.tiktok_build_launcher",
    }
)

ECR_LIFECYCLE_TARGETS = (
    "aws_ecr_lifecycle_policy.openclaw_quarantine",
    "aws_ecr_lifecycle_policy.openclaw_verified_candidates",
    "aws_ecr_lifecycle_policy.openclaw_media_quarantine",
    "aws_ecr_lifecycle_policy.openclaw_media_verified_candidates",
    "aws_ecr_lifecycle_policy.mcp_quarantine",
    "aws_ecr_lifecycle_policy.mcp_verified_candidates",
    "aws_ecr_lifecycle_policy.mcp_media_quarantine",
    "aws_ecr_lifecycle_policy.mcp_media_verified_candidates",
    "aws_ecr_lifecycle_policy.tiktok_acquire_quarantine",
    "aws_ecr_lifecycle_policy.tiktok_acquire_verified_candidates",
)

_TOP_LEVEL_HEADER = re.compile(
    r"(?m)^(?:resource|data|locals|output|variable|terraform|provider|moved|import|check)"
    r"\b[^\n]*\{"
)
_RESOURCE_DECLARATION = re.compile(r'^(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{')
_LOCAL_DECLARATION = re.compile(r"(?m)^  ([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _contract() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(CONTRACT_PATH.read_text(encoding="utf-8")),
    )


def _terraform_nodes_and_locals() -> tuple[
    dict[str, tuple[str, str]],
    dict[str, str],
]:
    nodes: dict[str, tuple[str, str]] = {}
    local_expressions: dict[str, str] = {}
    for path in sorted(TF_ROOT.glob("*.tf")):
        source = path.read_text(encoding="utf-8")
        starts = [match.start() for match in _TOP_LEVEL_HEADER.finditer(source)]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(source)
            body = source[start:end]
            declaration = _RESOURCE_DECLARATION.match(body)
            if declaration:
                mode, resource_type, name = declaration.groups()
                address = f"{'data.' if mode == 'data' else ''}{resource_type}.{name}"
                assert address not in nodes, f"duplicate Terraform declaration: {address}"
                nodes[address] = (mode, body)
                continue
            if not body.startswith("locals {"):
                continue
            declarations = list(_LOCAL_DECLARATION.finditer(body))
            for local_index, local_declaration in enumerate(declarations):
                local_end = (
                    declarations[local_index + 1].start()
                    if local_index + 1 < len(declarations)
                    else len(body)
                )
                name = f"local.{local_declaration.group(1)}"
                assert name not in local_expressions, f"duplicate Terraform local: {name}"
                local_expressions[name] = body[local_declaration.start() : local_end]
    return nodes, local_expressions


def _reference_pattern(addresses: set[str]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(address) for address in sorted(addresses, key=len, reverse=True)
    )
    return re.compile(rf"(?<![A-Za-z0-9_])({alternatives})(?![A-Za-z0-9_])")


def _post_cut_closure() -> tuple[set[str], set[str], dict[str, tuple[str, str]]]:
    # This is intentionally a fail-closed, repository-scoped textual graph, not
    # a general HCL parser. It retains dependencies in every conditional branch,
    # nested lifecycle/depends_on block, template, and transitive local.
    nodes, local_expressions = _terraform_nodes_and_locals()
    node_pattern = _reference_pattern(set(nodes))
    local_pattern = _reference_pattern(set(local_expressions))

    def references(body: str, *, own_address: str | None = None) -> tuple[set[str], set[str]]:
        direct = set(node_pattern.findall(body))
        if own_address is not None:
            direct.discard(own_address)
        return direct, set(local_pattern.findall(body))

    expanded_locals: dict[str, set[str]] = {}

    def expand_local(name: str, stack: tuple[str, ...] = ()) -> set[str]:
        if name in expanded_locals:
            return expanded_locals[name]
        assert name not in stack, f"cyclic Terraform local dependency: {(*stack, name)}"
        direct, nested = references(local_expressions[name])
        expanded = set(direct)
        for dependency in nested:
            expanded.update(expand_local(dependency, (*stack, name)))
        expanded_locals[name] = expanded
        return expanded

    edges: dict[str, set[str]] = {}
    for address, (_, body) in nodes.items():
        direct, local_dependencies = references(body, own_address=address)
        edges[address] = set(direct)
        for local_dependency in local_dependencies:
            edges[address].update(expand_local(local_dependency))

    contract = _contract()
    targets = set(cast(list[str], contract["terraform_targets"]))
    undeclared_targets = targets - set(edges)
    assert undeclared_targets == INTENTIONALLY_UNDECLARED_INLINE_POLICY_TARGETS
    closure = targets - undeclared_targets
    frontier = list(closure)
    while frontier:
        current = frontier.pop()
        assert current in edges, f"Terraform closure reached an undeclared node: {current}"
        for dependency in edges[current]:
            if dependency not in closure:
                closure.add(dependency)
                frontier.append(dependency)

    managed = {address for address in closure if nodes[address][0] == "resource"}
    data = {address for address in closure if nodes[address][0] == "data"}
    return managed, data, nodes


def test_post_cut_bootstrap_closure_is_the_exact_reviewed_graph() -> None:
    managed, data, _ = _post_cut_closure()
    assert len(EXPECTED_POST_CUT_MANAGED) == 137
    assert len(EXPECTED_POST_CUT_DATA) == 44
    assert managed == EXPECTED_POST_CUT_MANAGED
    assert data == EXPECTED_POST_CUT_DATA

    contract = _contract()
    targets = set(cast(list[str], contract["terraform_targets"]))
    dependencies = set(cast(list[str], contract["create_allowed_dependency_addresses"]))
    existing = set(cast(list[str], contract["existing_dependency_addresses"]))
    forbidden_prefixes = set(cast(list[str], contract["forbidden_change_type_prefixes"]))
    assert len(targets) == 119
    assert len(dependencies) == 20
    assert existing == {
        "aws_iam_role.codebuild",
        "aws_s3_bucket.raw_files",
    }
    assert targets.isdisjoint(dependencies)
    assert targets.isdisjoint(existing)
    assert dependencies.isdisjoint(existing)
    assert INTENTIONALLY_UNDECLARED_INLINE_POLICY_TARGETS <= targets
    assert (
        targets - INTENTIONALLY_UNDECLARED_INLINE_POLICY_TARGETS
    ) | dependencies | existing == EXPECTED_POST_CUT_MANAGED
    assert "terraform_data" in forbidden_prefixes


def test_lifecycle_and_rollout_kms_contract_membership_is_exact() -> None:
    contract = _contract()
    targets = tuple(cast(list[str], contract["terraform_targets"]))
    dependencies = set(cast(list[str], contract["create_allowed_dependency_addresses"]))
    required = set(cast(list[str], contract["required_main_state_addresses"]))
    existing = set(cast(list[str], contract["existing_dependency_addresses"]))
    allowed_outputs = set(cast(list[str], contract["allowed_output_names"]))

    assert targets[-len(ECR_LIFECYCLE_TARGETS) :] == ECR_LIFECYCLE_TARGETS
    assert (
        tuple(target for target in targets if target.startswith("aws_ecr_lifecycle_policy."))
        == ECR_LIFECYCLE_TARGETS
    )
    assert not dependencies.intersection(ECR_LIFECYCLE_TARGETS)

    rollout_key = "aws_kms_key.openclaw_rollout_signing"
    assert rollout_key in dependencies
    assert rollout_key in required
    assert rollout_key not in targets
    assert rollout_key not in existing
    assert "openclaw_rollout_signing_key_arn" in allowed_outputs
    assert "aws_kms_alias.openclaw_rollout_signing" not in (set(targets) | dependencies | existing)


def test_launcher_arn_edge_cut_preserves_the_runtime_guard_dependencies() -> None:
    managed, _, nodes = _post_cut_closure()
    for policy_address in (
        "data.aws_iam_policy_document.release_control_updater",
        "data.aws_iam_policy_document.image_attestor",
    ):
        policy = nodes[policy_address][1]
        assert policy.count("local.launcher_project_arn") == 1
        assert "aws_codebuild_project.image.arn" not in policy

    codebuild = CODEBUILD_TF.read_text(encoding="utf-8")
    assert re.search(
        r'^\s*launcher_project_arn\s*=\s*"arn:aws:codebuild:ap-northeast-1:'
        r'718959508629:project/teamagent-dev-image-builder"$',
        codebuild,
        flags=re.MULTILINE,
    )

    image_project = nodes["aws_codebuild_project.image"][1]
    image_log_group = nodes["aws_cloudwatch_log_group.codebuild_image"][1]
    assert "terraform_data.runtime_guard" in image_project
    assert "aws_iam_role_policy.codebuild" in image_project
    assert re.search(
        r"depends_on\s*=\s*\[\s*terraform_data\.runtime_guard\s*\]",
        image_log_group,
    )
    assert "aws_codebuild_project.image" not in managed
    assert "terraform_data.runtime_guard" not in managed
