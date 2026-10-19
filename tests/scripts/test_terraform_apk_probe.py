"""Offline contracts for the isolated, inventory-only CodeBuild project."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra/terraform/apk_probe.tf"
README = ROOT / "infra/terraform/README.md"


def _balanced(text: str, start: int) -> str:
    opening = text.index("{", start)
    depth = 0
    quoted = escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("unterminated Terraform block")


def _block(kind: str, name: str) -> str:
    text = TF.read_text(encoding="utf-8")
    return _balanced(text, text.index(f'{kind} "{name}" {{'))


def _list(block: str, name: str) -> set[str]:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*\[(.*?)\]", block, re.DOTALL)
    assert match is not None, f"missing {name} list"
    return set(re.findall(r'"([^"\n]+)"', match.group(1)))


def _statements(name: str) -> dict[str, str]:
    policy = _block('data "aws_iam_policy_document"', name)
    statements = {}
    for match in re.finditer(r"\bstatement\s*\{", policy):
        block = _balanced(policy, match.start())
        sid = re.search(r'\bsid\s*=\s*"([^"\n]+)"', block)
        assert sid is not None
        assert sid.group(1) not in statements
        assert not re.search(r'\beffect\s*=\s*"Deny"', block)
        statements[sid.group(1)] = block
    return statements


def test_service_role_has_only_the_five_required_scoped_statements() -> None:
    statements = _statements("apk_probe")
    assert set(statements) == {
        "EcrAuthorization",
        "ChromiumMirrorPull",
        "ReadProbeInputs",
        "WriteProbeOutputs",
        "ProbeLogs",
    }
    expected = {
        "EcrAuthorization": ({"ecr:GetAuthorizationToken"}, {"*"}),
        "ChromiumMirrorPull": (
            {
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability",
            },
            {
                "arn:aws:ecr:${local.expected_build_region}:"
                "${local.expected_build_account_id}:"
                "repository/teamagent-mirror/chromium-headless"
            },
        ),
        "ReadProbeInputs": (
            {"s3:GetObject"},
            {"${aws_s3_bucket.apk_probe.arn}/inputs/*"},
        ),
        "WriteProbeOutputs": (
            {"s3:PutObject"},
            {"${aws_s3_bucket.apk_probe.arn}/outputs/*"},
        ),
        "ProbeLogs": (
            {"logs:CreateLogStream", "logs:PutLogEvents"},
            {"${aws_cloudwatch_log_group.apk_probe.arn}:*"},
        ),
    }
    for sid, (actions, resources) in expected.items():
        assert _list(statements[sid], "actions") == actions
        assert _list(statements[sid], "resources") == resources

    role = _block('resource "aws_iam_role"', "apk_probe")
    assert "data.aws_iam_policy_document.apk_probe_assume.json" in role
    policy = _block('resource "aws_iam_role_policy"', "apk_probe")
    assert "aws_iam_role.apk_probe.id" in policy
    assert "data.aws_iam_policy_document.apk_probe.json" in policy
    assert "aws_iam_role_policy_attachment" not in TF.read_text(encoding="utf-8")


def test_service_trust_is_bound_to_the_exact_account_and_project() -> None:
    trust = _block('data "aws_iam_policy_document"', "apk_probe_assume")
    assert _list(trust, "actions") == {"sts:AssumeRole"}
    assert 'identifiers = ["codebuild.amazonaws.com"]' in trust
    assert 'variable = "aws:SourceAccount"' in trust
    assert "values   = [local.expected_build_account_id]" in trust
    assert 'variable = "aws:SourceArn"' in trust
    assert "values   = [local.apk_probe_project_arn]" in trust
    text = TF.read_text(encoding="utf-8")
    assert 'apk_probe_project_name = "teamagent-dev-apk-probe"' in text
    assert (
        'apk_probe_bucket_name  = "${local.apk_probe_project_name}-'
        '${local.expected_build_account_id}"'
    ) in text
    assert (
        'apk_probe_project_arn  = "arn:aws:codebuild:'
        '${local.expected_build_region}:${local.expected_build_account_id}:'
        'project/${local.apk_probe_project_name}"'
    ) in text


def test_project_is_arm_small_privileged_without_release_sources_or_artifacts() -> None:
    project = _block('resource "aws_codebuild_project"', "apk_probe")
    for setting in (
        'compute_type    = "BUILD_GENERAL1_SMALL"',
        'image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"',
        'type            = "ARM_CONTAINER"',
        "privileged_mode = true",
        'artifacts { type = "NO_ARTIFACTS" }',
        'type      = "NO_SOURCE"',
        'buildspec = file("${path.module}/../codebuild/apk-probe-buildspec.yml")',
        'name  = "APK_PROBE_BUCKET"',
        "value = aws_s3_bucket.apk_probe.id",
        'name  = "APK_PROBE_REGISTRY"',
        'name  = "APK_PROBE_RUN_ID"',
        'value = ""',
        "group_name = aws_cloudwatch_log_group.apk_probe.name",
        "concurrent_build_limit = 1",
        'var.project_name == "teamagent"',
        'var.environment == "dev"',
        "var.aws_region == local.expected_build_region",
        "data.aws_caller_identity.cb.account_id == local.expected_build_account_id",
        "terraform_data.runtime_guard",
    ):
        assert setting in project
    assert "vpc_config" not in project
    assert "image_release_evidence" not in project
    assert "codebuild-buildspecs/" not in project
    assert len((ROOT / "infra/codebuild/apk-probe-buildspec.yml").read_bytes()) <= 25600


def test_bucket_is_private_encrypted_and_https_only_with_bounded_log_retention() -> None:
    public = _block('resource "aws_s3_bucket_public_access_block"', "apk_probe")
    for setting in (
        "block_public_acls       = true",
        "block_public_policy     = true",
        "ignore_public_acls      = true",
        "restrict_public_buckets = true",
    ):
        assert setting in public
    encryption = _block(
        'resource "aws_s3_bucket_server_side_encryption_configuration"', "apk_probe"
    )
    assert 'sse_algorithm = "AES256"' in encryption
    bucket_policy = _block('data "aws_iam_policy_document"', "apk_probe_bucket")
    assert 'effect  = "Deny"' in bucket_policy
    assert 'variable = "aws:SecureTransport"' in bucket_policy
    assert 'values   = ["false"]' in bucket_policy
    assert _list(bucket_policy, "actions") == {"s3:*"}
    logs = _block('resource "aws_cloudwatch_log_group"', "apk_probe")
    assert 'name              = "/aws/codebuild/${local.apk_probe_project_name}"' in logs
    assert "retention_in_days = local.codebuild_log_retention_days" in logs


def test_new_files_are_outside_authoritative_frozen_and_generation_surfaces() -> None:
    helper_path = ROOT / "infra/deploy/activation_freeze_check.py"
    spec = importlib.util.spec_from_file_location("apk_probe_freeze_check", helper_path)
    assert spec is not None and spec.loader is not None
    freeze_helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(freeze_helper)
    freeze = json.loads((ROOT / "infra/deploy/activation_freeze.json").read_text())
    surface = freeze_helper.frozen_paths(ROOT, freeze, "HEAD")
    for path in (
        "infra/terraform/apk_probe.tf",
        "infra/terraform/README.md",
        "infra/codebuild/apk-probe-buildspec.yml",
        "tests/scripts/test_terraform_apk_probe.py",
        "tests/codebuild/test_apk_probe_buildspec.py",
    ):
        assert path not in surface
    manifest = json.loads((ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text())
    assert "infra/terraform/apk_probe.tf" not in manifest["inputs"]
    assert "infra/codebuild/apk-probe-buildspec.yml" not in manifest["inputs"]


def test_operator_docs_preserve_canonical_tfvars_and_guard_requirements() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme[readme.index("## media の APK 実インベントリ取得") :]
    for required in (
        "~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars",
        "kind: \"config\"",
        "to.allowed_resource_changes",
        "DenyIamSelfEscalation",
        "PassOnlyExistingTeamAgentServiceRoles",
        "arn:aws:iam::718959508629:role/teamagent-dev-apk-probe",
        "iam:PassedToService=codebuild.amazonaws.com",
        "terraform_runtime_guard.sh preflight",
        "terraform_runtime_guard.sh review-plan",
        "terraform_runtime_guard.sh plan",
        "terraform_runtime_guard.sh apply",
        "inputs/<run-id>/repo.tar.gz",
        "outputs/<run-id>/apk-installed.lock",
        "logs:GetLogEvents",
        "required=false",
    ):
        assert required in section
