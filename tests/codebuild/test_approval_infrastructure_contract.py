from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import defaultdict
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DIR = ROOT / "infra" / "terraform"
APPROVAL_TERRAFORM = ROOT / "infra" / "terraform" / "mcp_approval.tf"
RUNTIME_TERRAFORM = ROOT / "infra" / "terraform" / "runtime_evidence.tf"
RUNTIME_GUARD = ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"

ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"

APPROVAL_READER_ROLE_ARNS = [
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-image",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-mcp-source-publisher",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-image-attestor",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-image-promoter",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-launcher",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-release-launcher",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-image-deployment-gate",
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-terraform-runtime-automation",
]

# jsonencode deterministically sorts object keys while preserving list order.
# Keep this independent copy in Python so a Terraform-policy edit cannot silently
# update both the policy and its bootstrap byte contract.
EXPECTED_APPROVAL_KEY_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowApprovalKeyAdministration",
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_ID}:root"},
            "Action": [
                "kms:CancelKeyDeletion",
                "kms:CreateAlias",
                "kms:CreateGrant",
                "kms:DeleteAlias",
                "kms:DescribeKey",
                "kms:DisableKey",
                "kms:EnableKey",
                "kms:GetKeyPolicy",
                "kms:GetPublicKey",
                "kms:ListGrants",
                "kms:ListKeyPolicies",
                "kms:ListResourceTags",
                "kms:ListRetirableGrants",
                "kms:PutKeyPolicy",
                "kms:RevokeGrant",
                "kms:ScheduleKeyDeletion",
                "kms:TagResource",
                "kms:UntagResource",
                "kms:UpdateAlias",
                "kms:UpdateKeyDescription",
                "kms:UpdatePrimaryRegion",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AllowOnlyApprovalPublisherSigning",
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_ID}:root"},
            "Condition": {
                "ArnEquals": {
                    "aws:PrincipalArn": (
                        f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-approval-publisher"
                    )
                }
            },
            "Action": ["kms:Sign"],
            "Resource": "*",
        },
        {
            "Sid": "AllowOnlyApprovalReadersVerification",
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_ID}:root"},
            "Condition": {
                "ArnEquals": {
                    "aws:PrincipalArn": APPROVAL_READER_ROLE_ARNS,
                }
            },
            "Action": ["kms:Verify"],
            "Resource": "*",
        },
    ],
}
EXPECTED_APPROVAL_KEY_POLICY_JSON = json.dumps(
    EXPECTED_APPROVAL_KEY_POLICY,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)
EXPECTED_APPROVAL_KEY_POLICY_SHA256 = hashlib.sha256(
    EXPECTED_APPROVAL_KEY_POLICY_JSON.encode("utf-8")
).hexdigest()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _balanced_block_after(body: str, marker: str) -> str:
    marker_offset = body.index(marker)
    opening = body.index("{", marker_offset)
    depth = 0
    in_string = False
    escaped = False
    for offset in range(opening, len(body)):
        char = body[offset]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[opening + 1 : offset]
    raise AssertionError(f"unterminated Terraform block after {marker!r}")


def _resource(body: str, resource_type: str, name: str) -> str:
    marker = f'resource "{resource_type}" "{name}"'
    return _balanced_block_after(body, marker)


def _document(body: str, name: str) -> str:
    marker = f'data "aws_iam_policy_document" "{name}"'
    return _balanced_block_after(body, marker)


def _named_blocks(
    body: str,
    block_kind: str,
    block_type: str,
) -> dict[str, str]:
    pattern = re.compile(rf'{re.escape(block_kind)} "{re.escape(block_type)}" "([^"]+)"')
    return {
        match.group(1): _balanced_block_after(body[match.start() :], match.group(0))
        for match in pattern.finditer(body)
    }


def _statements(document: str) -> list[str]:
    statements: list[str] = []
    offset = 0
    marker = "statement {"
    while True:
        match = re.search(r"\bstatement\s*\{", document[offset:])
        if match is None:
            return statements
        start = offset + match.start()
        statements.append(_balanced_block_after(document[start:], marker))
        offset = start + len(match.group(0))


def _statement(document: str, sid: str) -> str:
    for statement in _statements(document):
        if re.search(rf'\bsid\s*=\s*"{re.escape(sid)}"', statement):
            return statement
    raise AssertionError(f"missing IAM statement {sid!r}")


def _attribute_list(block: str, attribute: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(attribute)}\s*=\s*\[", block)
    if match is None:
        raise AssertionError(f"missing list attribute {attribute!r}")
    opening = block.index("[", match.start())
    depth = 0
    in_string = False
    escaped = False
    for offset in range(opening, len(block)):
        char = block[offset]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return block[opening + 1 : offset]
    raise AssertionError(f"unterminated list attribute {attribute!r}")


def _quoted_list(block: str, attribute: str) -> set[str]:
    return set(re.findall(r'"([^"]+)"', _attribute_list(block, attribute)))


def _ordered_quoted_list(block: str, attribute: str) -> list[str]:
    return re.findall(r'"([^"]+)"', _attribute_list(block, attribute))


def _list_expressions(block: str, attribute: str) -> tuple[str, ...]:
    raw = _attribute_list(block, attribute)
    items: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False
    for offset, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            item = re.sub(r"\s+", "", raw[start:offset])
            if item:
                items.append(item)
            start = offset + 1
    item = re.sub(r"\s+", "", raw[start:])
    if item:
        items.append(item)
    return tuple(items)


def _actions(statement: str) -> set[str]:
    scalar = re.search(r'(?m)^\s*actions\s*=\s*\[\s*"([^"]+)"\s*\]', statement)
    if scalar is not None:
        return {scalar.group(1)}
    return _quoted_list(statement, "actions")


def _action_matches(statement: str, action: str) -> bool:
    return any(fnmatchcase(action, pattern) for pattern in _actions(statement))


def _effect(statement: str) -> str:
    match = re.search(r'(?m)^\s*effect\s*=\s*"([^"]+)"', statement)
    return match.group(1) if match else "Allow"


def _allow_statements(document: str) -> list[str]:
    return [statement for statement in _statements(document) if _effect(statement) == "Allow"]


def _local_literal(body: str, name: str) -> str:
    match = re.search(
        rf'(?ms)^\s*{re.escape(name)}\s*=\s*(?:\(\s*)?"([^"]+)"'
        r"(?:\s*\))?\s*$",
        body,
    )
    if match is None:
        raise AssertionError(f"missing fixed string local {name!r}")
    return match.group(1)


def _conditions(statement: str) -> list[str]:
    conditions: list[str] = []
    offset = 0
    while True:
        match = re.search(r"\bcondition\s*\{", statement[offset:])
        if match is None:
            return conditions
        start = offset + match.start()
        conditions.append(_balanced_block_after(statement[start:], "condition"))
        offset = start + len(match.group(0))


def _condition(statement: str, variable: str) -> str:
    for condition in _conditions(statement):
        if re.search(
            rf'(?m)^\s*variable\s*=\s*"{re.escape(variable)}"\s*$',
            condition,
        ):
            return condition
    raise AssertionError(f"missing IAM condition for {variable!r}")


def _terraform_policy_hash_literal(body: str) -> str:
    match = re.search(
        r"(?m)^\s*approval_signing_key_policy_expected_sha256\s*=\s*"
        r'"([0-9a-f]{64})"\s*$',
        body,
    )
    if match is None:
        raise AssertionError(
            "approval_signing_key_policy_expected_sha256 must be a reviewed literal, "
            "not a hash derived from the policy under test"
        )
    return match.group(1)


def test_approval_signing_key_has_dedicated_asymmetric_contract() -> None:
    body = _read(APPROVAL_TERRAFORM)
    key = _resource(body, "aws_kms_key", "approval_signing")
    alias = _resource(body, "aws_kms_alias", "approval_signing")

    assert re.search(
        r'(?m)^\s*customer_master_key_spec\s*=\s*"RSA_3072"\s*$',
        key,
    )
    assert re.search(r'(?m)^\s*key_usage\s*=\s*"SIGN_VERIFY"\s*$', key)
    assert re.search(r"(?m)^\s*prevent_destroy\s*=\s*true\s*$", key)
    assert re.search(
        r"(?m)^\s*policy\s*=\s*local\.approval_signing_key_policy_json\s*$",
        key,
    )
    assert "depends_on" not in key
    assert re.search(
        r"(?ms)^\s*approval_signing_key_alias\s*=\s*\(\s*"
        r'"alias/\$\{var\.project_name\}-\$\{var\.environment\}-mcp-approval"\s*'
        r"\)\s*$",
        body,
    )
    assert re.search(
        r"(?m)^\s*name\s*=\s*local\.approval_signing_key_alias\s*$",
        alias,
    )
    assert re.search(
        r"(?m)^\s*target_key_id\s*=\s*aws_kms_key\.approval_signing\.key_id\s*$",
        alias,
    )

    # The approval authority must not alias or target either existing signer.
    assert "mcp_source_publisher_signing" not in key + alias
    assert "image_attestor_signing" not in key + alias


def test_approval_foundation_names_and_prefix_are_fixed() -> None:
    body = _read(APPROVAL_TERRAFORM)
    assert _local_literal(body, "approval_evidence_prefix") == "approval-records/mcp"
    check = _balanced_block_after(body, 'check "approval_foundation_preconditions"')
    for contract in (
        'local.approval_publisher_project_name ==\n      "teamagent-dev-approval-publisher"',
        'local.approval_caller_role_name ==\n      "teamagent-dev-approval-caller"',
        'local.approval_signing_key_alias ==\n      "alias/teamagent-dev-mcp-approval"',
    ):
        assert contract in check


def test_approval_key_policy_jsonencode_and_reviewed_hash_are_deterministic(
    tmp_path: Path,
) -> None:
    body = _read(APPROVAL_TERRAFORM)

    assert re.search(
        r"(?ms)^\s*approval_signing_key_policy_json\s*=\s*jsonencode\(\s*"
        r"local\.approval_signing_key_policy\s*\)\s*$",
        body,
    )
    assert _terraform_policy_hash_literal(body) == EXPECTED_APPROVAL_KEY_POLICY_SHA256
    assert (
        hashlib.sha256(EXPECTED_APPROVAL_KEY_POLICY_JSON.encode("utf-8")).hexdigest()
        == EXPECTED_APPROVAL_KEY_POLICY_SHA256
    )
    assert re.search(
        r"(?ms)^\s*approval_signing_key_policy_sha256\s*=\s*sha256\(\s*"
        r"local\.approval_signing_key_policy_json\s*\)\s*$",
        body,
    )

    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform CLI is unavailable for provider-free jsonencode verification")
    expression = (
        "sha256(jsonencode("
        + json.dumps(EXPECTED_APPROVAL_KEY_POLICY, separators=(",", ":"))
        + "))\n"
    )
    rendered = subprocess.run(
        [terraform, f"-chdir={tmp_path}", "console", "-no-color"],
        input=expression,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert json.loads(rendered.stdout.strip()) == EXPECTED_APPROVAL_KEY_POLICY_SHA256

    # This check deliberately stops at Terraform's expected bytes. Whether
    # GetKeyPolicy returns those exact bytes remains an AWS-measured gate.
    check = _balanced_block_after(body, 'check "approval_foundation_preconditions"')
    assert (
        "local.approval_signing_key_policy_sha256 ==\n"
        "      local.approval_signing_key_policy_expected_sha256"
    ) in check


def test_approval_key_policy_separates_administration_signing_and_verification() -> None:
    body = _read(APPROVAL_TERRAFORM)
    policy = _balanced_block_after(
        body,
        "approval_signing_key_policy =",
    )
    admin_marker = 'Sid    = "AllowApprovalKeyAdministration"'
    publisher_marker = 'Sid    = "AllowOnlyApprovalPublisherSigning"'
    readers_marker = 'Sid    = "AllowOnlyApprovalReadersVerification"'
    markers = [admin_marker, publisher_marker, readers_marker]
    assert policy.count("Sid    =") == len(markers)
    assert all(policy.count(marker) == 1 for marker in markers)
    assert [policy.index(marker) for marker in markers] == sorted(
        policy.index(marker) for marker in markers
    )
    assert re.search(
        r'(?m)^\s*Version\s*=\s*"2012-10-17"\s*$',
        policy,
    )

    admin = policy.split(admin_marker, maxsplit=1)[1].split(publisher_marker, maxsplit=1)[0]
    publisher = policy.split(publisher_marker, maxsplit=1)[1].split(readers_marker, maxsplit=1)[0]
    readers = policy.split(readers_marker, maxsplit=1)[1]

    expected_admin_actions = EXPECTED_APPROVAL_KEY_POLICY["Statement"][0]["Action"]
    assert _ordered_quoted_list(admin, "Action") == expected_admin_actions
    assert "kms:Sign" not in admin
    assert "kms:Verify" not in admin
    assert 'AWS = "arn:aws:iam::${local.expected_build_account_id}:root"' in admin
    assert "Condition" not in admin

    assert _ordered_quoted_list(publisher, "Action") == ["kms:Sign"]
    assert 'AWS = "arn:aws:iam::${local.expected_build_account_id}:root"' in publisher
    assert '"aws:PrincipalArn" = local.approval_publisher_role_arn' in publisher
    assert "local.approval_reader_role_arns" not in publisher

    assert _ordered_quoted_list(readers, "Action") == ["kms:Verify"]
    assert 'AWS = "arn:aws:iam::${local.expected_build_account_id}:root"' in readers
    assert '"aws:PrincipalArn" = local.approval_reader_role_arns' in readers
    assert "local.approval_publisher_role_arn" not in readers
    assert policy.count('"kms:Sign"') == 1
    assert policy.count('"kms:Verify"') == 1
    for statement in (admin, publisher, readers):
        assert len(re.findall(r'(?m)^\s*Effect\s*=\s*"Allow"\s*$', statement)) == 1
        assert len(re.findall(r'(?m)^\s*Resource\s*=\s*"\*"\s*$', statement)) == 1

    reader_roles = _list_expressions(
        body[body.index("approval_reader_role_arns") :],
        "approval_reader_role_arns",
    )
    assert reader_roles == (
        '"arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-mcp-source-publisher"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image-attestor"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${var.project_name}-${var.environment}-codebuild-image-promoter"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${local.launcher_role_name}"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${local.release_launcher_role_name}"',
        '"arn:aws:iam::${local.expected_build_account_id}:role/${local.image_deployment_gate_role_name}"',
        "local.terraform_automation_role_arn",
    )


def test_approval_caller_trust_is_exact_aiia_dev_mfa_session() -> None:
    body = _read(APPROVAL_TERRAFORM)
    trust = _document(body, "approval_caller_assume")
    statements = _statements(trust)
    assert len(statements) == 1
    statement = statements[0]

    assert _actions(statement) == {"sts:AssumeRole", "sts:SetSourceIdentity"}
    principals = _balanced_block_after(statement, "principals")
    assert re.search(r'(?m)^\s*type\s*=\s*"AWS"\s*$', principals)
    assert _list_expressions(principals, "identifiers") == ("data.aws_iam_user.aiia_dev.arn",)

    mfa = _condition(statement, "aws:MultiFactorAuthPresent")
    assert re.search(r'(?m)^\s*test\s*=\s*"Bool"\s*$', mfa)
    assert _list_expressions(mfa, "values") == ('"true"',)

    session = _condition(statement, "sts:RoleSessionName")
    assert _list_expressions(session, "values") == ("local.approval_caller_session_name",)
    assert _local_literal(body, "approval_caller_session_name")

    source_identity = _condition(statement, "sts:SourceIdentity")
    assert _list_expressions(source_identity, "values") == (
        "local.approval_caller_source_identity",
    )
    assert _local_literal(body, "approval_caller_source_identity")
    assert len(_conditions(statement)) == 3


def test_approval_caller_can_only_start_and_observe_exact_publisher() -> None:
    body = _read(APPROVAL_TERRAFORM)
    policy = _document(body, "approval_caller")
    allow = _allow_statements(policy)

    assert len(allow) == 2
    assert {action for statement in allow for action in _actions(statement)} == {
        "codebuild:BatchGetBuilds",
        "codebuild:StartBuild",
    }
    assert all(
        _list_expressions(statement, "resources") == ("local.approval_publisher_project_arn",)
        for statement in allow
    )
    assert "kms:" not in policy
    assert "s3:" not in policy
    assert "ecr:" not in policy
    start = next(statement for statement in allow if "codebuild:StartBuild" in _actions(statement))
    start_conditions = _conditions(start)
    assert len(start_conditions) == 2
    null_condition = next(
        condition for condition in start_conditions if 'test     = "Null"' in condition
    )
    assert _list_expressions(null_condition, "values") == ('"false"',)
    allowed_names_condition = next(
        condition
        for condition in start_conditions
        if 'test     = "ForAllValues:StringEquals"' in condition
    )
    assert re.search(
        r"(?m)^\s*values\s*=\s*"
        r"local\.approval_publisher_environment_names\s*$",
        allowed_names_condition,
    )
    poll = next(
        statement for statement in allow if "codebuild:BatchGetBuilds" in _actions(statement)
    )
    assert not _conditions(poll)
    assert _list_expressions(
        body[body.index("approval_publisher_environment_names") :],
        "approval_publisher_environment_names",
    ) == (
        '"APPROVAL_DECISION"',
        '"EXPECTED_COMMIT"',
        '"FORCED_ROLLBACK_EVIDENCE_JSON"',
    )

    role = _resource(body, "aws_iam_role", "approval_caller")
    assert re.search(
        r"(?m)^\s*assume_role_policy\s*=\s*"
        r"data\.aws_iam_policy_document\.approval_caller_assume\.json\s*$",
        role,
    )
    inline = _resource(body, "aws_iam_role_policy", "approval_caller")
    assert re.search(
        r"(?m)^\s*role\s*=\s*aws_iam_role\.approval_caller\.id\s*$",
        inline,
    )
    assert re.search(
        r"(?m)^\s*policy\s*=\s*data\.aws_iam_policy_document\.approval_caller\.json\s*$",
        inline,
    )

    for suffix in ("a", "b", "c"):
        guard = _document(body, f"approval_caller_override_{suffix}")
        assert re.search(r'(?m)^\s*effect\s*=\s*"Deny"\s*$', guard)
        assert _quoted_list(guard, "actions") == {"codebuild:StartBuild"}
        assert _list_expressions(guard, "resources") == ("local.approval_publisher_project_arn",)
        assert f"for_each = local.approval_caller_override_guard_{suffix}" in guard
        assert 'test     = "Null"' in guard
        assert "variable = statement.value" in guard
        assert _quoted_list(guard, "values") == {"false"}
        assert "kms:" not in guard
        assert "s3:" not in guard
        assert "ecr:" not in guard
        managed = _resource(body, "aws_iam_policy", f"approval_caller_override_{suffix}")
        assert re.search(
            rf"(?m)^\s*policy\s*=\s*data\.aws_iam_policy_document\."
            rf"approval_caller_override_{suffix}\.json\s*$",
            managed,
        )
        attachment = _resource(
            body,
            "aws_iam_role_policy_attachment",
            f"approval_caller_override_{suffix}",
        )
        assert re.search(
            r"(?m)^\s*role\s*=\s*aws_iam_role\.approval_caller\.name\s*$",
            attachment,
        )
        assert re.search(
            rf"(?m)^\s*policy_arn\s*=\s*aws_iam_policy\."
            rf"approval_caller_override_{suffix}\.arn\s*$",
            attachment,
        )

    for suffix, source in (
        ("a", "launcher_denied_override_condition_keys_manage_a"),
        ("b", "launcher_denied_override_condition_keys_manage_b"),
        ("c", "launcher_denied_override_condition_keys_guardrails"),
    ):
        assert re.search(
            rf"(?m)^\s*approval_caller_override_guard_{suffix}\s*=\s*"
            rf"local\.{source}\s*$",
            body,
        )


def test_approval_publisher_trust_is_exact_project_and_source_account() -> None:
    body = _read(APPROVAL_TERRAFORM)
    trust = _document(body, "approval_publisher_assume")
    statements = _statements(trust)
    assert len(statements) == 1
    statement = statements[0]

    assert _actions(statement) == {"sts:AssumeRole"}
    principals = _balanced_block_after(statement, "principals")
    assert _quoted_list(principals, "identifiers") == {"codebuild.amazonaws.com"}
    assert _list_expressions(principals, "identifiers") == ('"codebuild.amazonaws.com"',)

    source_account = _condition(statement, "aws:SourceAccount")
    assert re.search(r'(?m)^\s*test\s*=\s*"StringEquals"\s*$', source_account)
    assert _list_expressions(source_account, "values") == ("local.expected_build_account_id",)

    source_arn = _condition(statement, "aws:SourceArn")
    assert re.search(r'(?m)^\s*test\s*=\s*"ArnEquals"\s*$', source_arn)
    assert _list_expressions(source_arn, "values") == ("local.approval_publisher_project_arn",)
    assert len(_conditions(statement)) == 2

    role = _resource(body, "aws_iam_role", "approval_publisher")
    assert re.search(
        r"(?m)^\s*assume_role_policy\s*=\s*"
        r"data\.aws_iam_policy_document\.approval_publisher_assume\.json\s*$",
        role,
    )


def test_approval_publisher_write_and_sign_allows_are_resource_exact() -> None:
    body = _read(APPROVAL_TERRAFORM)
    policy = _document(body, "approval_publisher")
    allow = _allow_statements(policy)
    assert {action for statement in allow for action in _actions(statement)} == {
        "codeconnections:GetConnection",
        "codeconnections:GetConnectionToken",
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey",
        "kms:Sign",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketVersioning",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:PutObjectRetention",
    }

    put_allows = [
        statement
        for statement in allow
        if "s3:PutObject" in _actions(statement) or "s3:PutObjectRetention" in _actions(statement)
    ]
    assert len(put_allows) == 1
    assert _actions(put_allows[0]) == {"s3:PutObject", "s3:PutObjectRetention"}
    assert _list_expressions(put_allows[0], "resources") == (
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*"',
    )
    assert "source-declarations/" not in put_allows[0]
    assert "release-receipts/" not in put_allows[0]

    sign_allows = [statement for statement in allow if "kms:Sign" in _actions(statement)]
    assert len(sign_allows) == 1
    assert _actions(sign_allows[0]) == {"kms:Sign"}
    assert _list_expressions(sign_allows[0], "resources") == ("aws_kms_key.approval_signing.arn",)
    assert "mcp_source_publisher_signing" not in sign_allows[0]
    assert "image_attestor_signing" not in sign_allows[0]

    log_allow = next(statement for statement in allow if "logs:PutLogEvents" in _actions(statement))
    assert _list_expressions(log_allow, "resources") == (
        '"${aws_cloudwatch_log_group.codebuild_approval_publisher.arn}:*"',
    )
    connection_allow = next(
        statement for statement in allow if "codeconnections:GetConnection" in _actions(statement)
    )
    assert _list_expressions(connection_allow, "resources") == (
        "aws_codestarconnections_connection.openclaw_codebuild.arn",
    )
    evidence_key_allow = next(
        statement for statement in allow if "kms:GenerateDataKey" in _actions(statement)
    )
    assert _list_expressions(evidence_key_allow, "resources") == (
        "aws_kms_key.image_release_evidence.arn",
    )

    inline = _resource(body, "aws_iam_role_policy", "approval_publisher")
    assert re.search(
        r"(?m)^\s*role\s*=\s*aws_iam_role\.approval_publisher\.id\s*$",
        inline,
    )
    assert re.search(
        r"(?m)^\s*policy\s*=\s*"
        r"data\.aws_iam_policy_document\.approval_publisher\.json\s*$",
        inline,
    )


def test_approval_publisher_explicitly_denies_other_evidence_and_signing_keys() -> None:
    policy = _document(_read(APPROVAL_TERRAFORM), "approval_publisher")
    denies = [statement for statement in _statements(policy) if _effect(statement) == "Deny"]

    other_evidence = _statement(policy, "DenyOtherEvidencePrefixWrites")
    assert _actions(other_evidence) == {
        "s3:DeleteObject*",
        "s3:PutObject*",
        "s3:RestoreObject",
    }
    assert _list_expressions(other_evidence, "not_resources") == (
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*"',
    )

    other_signers = [statement for statement in denies if "kms:Sign" in _actions(statement)]
    assert len(other_signers) == 1
    deny = other_signers[0]
    assert _list_expressions(deny, "resources") == (
        "aws_kms_key.image_attestor_signing.arn",
        "aws_kms_key.mcp_source_publisher_signing.arn",
    )


def test_buildspec_bucket_listing_is_separate_and_prefix_exact() -> None:
    policy = _document(_read(APPROVAL_TERRAFORM), "approval_publisher")
    allow = _allow_statements(policy)
    list_statements = [statement for statement in allow if "s3:ListBucket" in _actions(statement)]
    assert len(list_statements) == 1
    listing = list_statements[0]
    assert _actions(listing) == {"s3:ListBucket"}
    assert _list_expressions(listing, "resources") == ("aws_s3_bucket.image_release_evidence.arn",)
    assert _list_expressions(_condition(listing, "s3:prefix"), "values") == (
        "local.approval_publisher_buildspec_s3_key",
    )

    get_statements = [statement for statement in allow if "s3:GetObject" in _actions(statement)]
    assert len(get_statements) == 1
    buildspec_read = get_statements[0]
    assert _actions(buildspec_read) == {"s3:GetObject"}
    assert _list_expressions(buildspec_read, "resources") == (
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_publisher_buildspec_s3_key}"',
    )
    assert "s3:ListBucket" not in _actions(buildspec_read)


def test_approval_buildspec_is_content_addressed_locked_and_self_checked() -> None:
    body = _read(APPROVAL_TERRAFORM)
    buildspec_key = re.search(
        r"(?ms)^\s*approval_publisher_buildspec_s3_key\s*=\s*\((.*?)^\s*\)\s*$",
        body,
    )
    assert buildspec_key is not None
    key_expression = buildspec_key.group(1)
    assert (
        "sha256(local.approval_publisher_buildspec)" in key_expression
        or "local.approval_publisher_buildspec_sha256" in key_expression
    )
    if "local.approval_publisher_buildspec_sha256" in key_expression:
        assert re.search(
            r"(?ms)^\s*approval_publisher_buildspec_sha256\s*=\s*sha256\(\s*"
            r"local\.approval_publisher_buildspec\s*\)\s*$",
            body,
        )

    obj = _resource(body, "aws_s3_object", "approval_publisher_buildspec")
    assert "key                           = local.approval_publisher_buildspec_s3_key" in obj
    assert "content                       = local.approval_publisher_buildspec" in obj
    assert "source_hash                   = local.approval_publisher_buildspec_sha256" in obj
    assert 'server_side_encryption        = "aws:kms"' in obj
    assert "kms_key_id                    = aws_kms_key.image_release_evidence.arn" in obj
    assert 'object_lock_mode              = "GOVERNANCE"' in obj
    assert "object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date" in obj
    assert re.search(r"(?m)^\s*prevent_destroy\s*=\s*true\s*$", obj)

    assert "?versionId=" not in body
    assert "APPROVAL_BUILDSPEC_SHA256" in body
    assert "APPROVAL_BUILDSPEC_KEY" in body
    assert re.search(r"\baws\s+s3api\s+get-object\b|\baws\s+s3\s+cp\b", body)
    assert "sha256sum" in body
    for buildspec_contract in (
        "+refs/heads/dev:refs/remotes/origin/dev",
        'git rev-parse refs/remotes/origin/dev)" = "$EXPECTED_COMMIT"',
        "approval_observation_values",
        "canonical_json_bytes",
        "RSASSA_PSS_SHA_256",
        "--message-type DIGEST",
        "--object-lock-mode GOVERNANCE",
        "--object-lock-retain-until-date",
        "--server-side-encryption aws:kms",
        "--if-none-match '*'",
        'response.get("ObjectLockConfiguration")',
    ):
        assert buildspec_contract in body

    put_commands = re.findall(
        r"aws s3api put-object \\\n(?P<command>.*?--query VersionId --output text)",
        body,
        flags=re.DOTALL,
    )
    assert len(put_commands) == 2
    for command in put_commands:
        for exact_write_contract in (
            '--bucket "$EVIDENCE_BUCKET"',
            "--server-side-encryption aws:kms",
            '--ssekms-key-id "$EVIDENCE_KMS_KEY_ARN"',
            "--bucket-key-enabled",
            "--object-lock-mode GOVERNANCE",
            '--object-lock-retain-until-date "$retain_until"',
            "--if-none-match '*'",
            '--expected-bucket-owner "${local.expected_build_account_id}"',
            "--query VersionId --output text",
        ):
            assert exact_write_contract in command
    assert '--key "$payload_key"' in put_commands[0]
    assert "--body /tmp/approval-payload.json" in put_commands[0]
    assert '--key "$signature_key"' in put_commands[1]
    assert "--body /tmp/approval-payload.sig" in put_commands[1]
    assert "dt.timedelta(days=3650)" in body
    assert "dt.timedelta(hours=1)" in body

    project = _resource(body, "aws_codebuild_project", "approval_publisher")
    assert 'type                = "GITHUB"' in project
    assert 'location            = "https://github.com/noirelumiere00/TeamAgent.git"' in project
    assert 'type     = "CODECONNECTIONS"' in project
    assert "local.approval_publisher_buildspec_s3_key" in project
    assert 'source_version = "refs/heads/dev"' in project
    assert re.search(
        r"(?m)^\s*service_role\s*=\s*aws_iam_role\.approval_publisher\.arn\s*$",
        project,
    )
    assert "group_name = aws_cloudwatch_log_group.codebuild_approval_publisher.name" in project


def test_runtime_boundary_approval_kms_denies_are_complete_and_exact() -> None:
    boundary = _document(_read(RUNTIME_TERRAFORM), "runtime_automation_boundary")
    key_deny = _statement(boundary, "DenyApprovalKms")
    assert _effect(key_deny) == "Deny"
    assert _actions(key_deny) == {
        "kms:CancelKeyDeletion",
        "kms:CreateAlias",
        "kms:CreateGrant",
        "kms:DeleteAlias",
        "kms:DeleteImportedKeyMaterial",
        "kms:DisableKey",
        "kms:EnableKey",
        "kms:ImportKeyMaterial",
        "kms:PutKeyPolicy",
        "kms:ReplicateKey",
        "kms:ScheduleKeyDeletion",
        "kms:Sign",
        "kms:TagResource",
        "kms:UntagResource",
        "kms:Update*",
        "kms:UpdateAlias",
    }
    assert _list_expressions(key_deny, "resources") == (
        '"arn:aws:kms:${var.aws_region}:${local.expected_build_account_id}:${local.approval_signing_key_alias}"',
        "aws_kms_key.approval_signing.arn",
    )
    assert 'resources = ["*"]' not in key_deny


def test_runtime_boundary_covers_approval_iam_objects_and_project() -> None:
    boundary = _document(_read(RUNTIME_TERRAFORM), "runtime_automation_boundary")
    iam_deny = _statement(boundary, "DenyApprovalIam")
    assert _effect(iam_deny) == "Deny"
    assert _actions(iam_deny) == {
        "iam:AttachRolePolicy",
        "iam:CreatePolicyVersion",
        "iam:Delete*",
        "iam:DetachRolePolicy",
        "iam:PutRole*",
        "iam:SetDefaultPolicyVersion",
        "iam:Tag*",
        "iam:Untag*",
        "iam:Update*",
    }
    assert "local.approval_runtime_iam_protected_arns" in iam_deny
    assert 'resources = ["*"]' not in iam_deny
    global_iam_deny = _statement(boundary, "DenyIamSelfEscalation")
    assert _effect(global_iam_deny) == "Deny"
    assert _list_expressions(global_iam_deny, "resources") == ('"*"',)
    assert {
        "iam:AttachRolePolicy",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:CreateRole",
        "iam:DeletePolicy",
        "iam:DeleteRole",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:SetDefaultPolicyVersion",
        "iam:UpdateAssumeRolePolicy",
    } <= _actions(global_iam_deny)

    approval = _read(APPROVAL_TERRAFORM)
    protected_arns = _attribute_list(
        approval[approval.index("approval_runtime_iam_protected_arns") :],
        "approval_runtime_iam_protected_arns",
    )
    assert _list_expressions(
        approval[approval.index("approval_runtime_iam_protected_arns") :],
        "approval_runtime_iam_protected_arns",
    ) == (
        "local.approval_caller_role_arn",
        "local.approval_publisher_role_arn",
        '"arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-a"',
        '"arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-b"',
        '"arn:aws:iam::${local.expected_build_account_id}:policy/${local.approval_caller_role_name}-override-c"',
        '"arn:aws:iam::${local.expected_build_account_id}:policy/${var.project_name}-${var.environment}-approval-reader"',
    )
    assert protected_arns

    project_deny = _statement(boundary, "DenyApprovalProject")
    assert _effect(project_deny) == "Deny"
    assert _actions(project_deny) == {
        "codebuild:DeleteProject",
        "codebuild:UpdateProject",
    }
    assert _list_expressions(project_deny, "resources") == ("local.approval_publisher_project_arn",)
    assert 'resources = ["*"]' not in project_deny


def test_runtime_boundary_covers_approval_and_buildspec_objects() -> None:
    boundary = _document(_read(RUNTIME_TERRAFORM), "runtime_automation_boundary")
    deny = _statement(boundary, "DenyApprovalObjects")
    assert _effect(deny) == "Deny"
    assert _actions(deny) == {
        "s3:DeleteObject*",
        "s3:PutObject*",
        "s3:RestoreObject",
    }
    assert _list_expressions(deny, "resources") == (
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*"',
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_publisher_buildspec_s3_key}"',
    )
    assert 'resources = ["*"]' not in deny

    bucket = _statement(boundary, "DenyApprovalBucketControls")
    assert _effect(bucket) == "Deny"
    assert _actions(bucket) == {
        "s3:DeleteBucketPolicy",
        "s3:PutBucketLifecycleConfiguration",
        "s3:PutBucketObjectLockConfiguration",
        "s3:PutBucketPolicy",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
    }
    assert _list_expressions(bucket, "resources") == ("aws_s3_bucket.image_release_evidence.arn",)


def test_runtime_boundary_conservative_render_fits_managed_policy_limit() -> None:
    boundary = _document(_read(RUNTIME_TERRAFORM), "runtime_automation_boundary")
    key_id = "00000000-0000-0000-0000-000000000000"
    media_key_arn = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/{key_id}"
    approval_key_arn = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/{key_id}"
    evidence_bucket_arn = "arn:aws:s3:::teamagent-dev-image-release-evidence"
    resources_by_sid = {
        "AllowOnlyIdentityPolicyIntersection": ["*"],
        "DenyIamSelfEscalation": ["*"],
        "DenyRoleChaining": ["*"],
        "DenyAuthoritativeMediaLedgerMutation": [
            (f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/teamagent-dev-image-deployment-intents")
        ],
        "DenyAuthoritativeLedgerControlPlaneMutation": [
            (f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/teamagent-dev-image-deployment-intents")
        ],
        "DenyMediaAttestorKeyMutationAndUse": [media_key_arn],
        "DenyMediaAttestorAliasMutation": [
            (f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:alias/teamagent-dev-media-cutover-attestor"),
            media_key_arn,
        ],
        "DenyApprovalKms": [
            (f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:alias/teamagent-dev-mcp-approval"),
            approval_key_arn,
        ],
        "DenyApprovalIam": [
            f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-approval-caller",
            (f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-codebuild-approval-publisher"),
            (f"arn:aws:iam::{ACCOUNT_ID}:policy/teamagent-dev-approval-caller-override-a"),
            (f"arn:aws:iam::{ACCOUNT_ID}:policy/teamagent-dev-approval-caller-override-b"),
            (f"arn:aws:iam::{ACCOUNT_ID}:policy/teamagent-dev-approval-caller-override-c"),
            f"arn:aws:iam::{ACCOUNT_ID}:policy/teamagent-dev-approval-reader",
        ],
        "DenyApprovalObjects": [
            f"{evidence_bucket_arn}/approval-records/mcp/*",
            (
                f"{evidence_bucket_arn}/codebuild-buildspecs/"
                f"teamagent-dev-approval-publisher/{'0' * 64}.yml"
            ),
        ],
        "DenyApprovalBucketControls": [evidence_bucket_arn],
        "DenyApprovalProject": [
            (f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/teamagent-dev-approval-publisher")
        ],
    }

    rendered_statements: list[dict[str, object]] = []
    for statement in _statements(boundary):
        sid_match = re.search(r'(?m)^\s*sid\s*=\s*"([^"]+)"', statement)
        assert sid_match is not None
        sid = sid_match.group(1)
        assert sid in resources_by_sid
        rendered: dict[str, object] = {
            "Sid": sid,
            "Effect": _effect(statement),
            "Action": _ordered_quoted_list(statement, "actions"),
            "Resource": resources_by_sid[sid],
        }
        if sid == "DenyAuthoritativeMediaLedgerMutation":
            rendered["Condition"] = {
                "ForAnyValue:StringLike": {
                    "dynamodb:LeadingKeys": ["media-cutover#*"],
                },
                "Null": {
                    "dynamodb:LeadingKeys": ["false"],
                },
            }
        rendered_statements.append(rendered)

    assert {statement["Sid"] for statement in rendered_statements} == set(resources_by_sid)
    conservative_policy = {
        "Version": "2012-10-17",
        "Statement": rendered_statements,
    }
    conservative_length = len(json.dumps(conservative_policy, separators=(",", ":")))
    assert conservative_length == 5821
    assert conservative_length < 6144


def test_runtime_guard_has_zero_approval_infrastructure_allowances() -> None:
    guard = _read(RUNTIME_GUARD)
    match = re.search(
        r"(?ms)^\s*allowed_runtime_changes='\[(.*?)^\s*\]'\s*$",
        guard,
    )
    assert match is not None
    allowed = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert not {address for address in allowed if "approval" in address.lower()}

    expected_approval_addresses = {
        "aws_kms_key.approval_signing",
        "aws_kms_alias.approval_signing",
        "aws_iam_role.approval_publisher",
        "aws_iam_role.approval_caller",
        "aws_iam_role_policy.approval_caller",
        "aws_iam_policy.approval_caller_override_a",
        "aws_iam_policy.approval_caller_override_b",
        "aws_iam_policy.approval_caller_override_c",
        "aws_iam_role_policy_attachment.approval_caller_override_a",
        "aws_iam_role_policy_attachment.approval_caller_override_b",
        "aws_iam_role_policy_attachment.approval_caller_override_c",
        "aws_iam_role_policy.approval_publisher",
        "aws_iam_policy.approval_reader",
        "aws_iam_role_policy_attachment.approval_reader_main_builder",
        "aws_iam_role_policy_attachment.approval_reader_source_publisher",
        "aws_iam_role_policy_attachment.approval_reader_attestor",
        "aws_iam_role_policy_attachment.approval_reader_promoter",
        "aws_iam_role_policy_attachment.approval_reader_build_launcher",
        "aws_iam_role_policy_attachment.approval_reader_release_launcher",
        "aws_iam_role_policy_attachment.approval_reader_deployment_gate",
        "aws_iam_role_policy_attachment.approval_reader_runtime_automation",
        "aws_cloudwatch_log_group.codebuild_approval_publisher",
        "aws_s3_object.approval_publisher_buildspec",
        "aws_codebuild_project.approval_publisher",
    }
    actual_approval_addresses = {
        f"{resource_type}.{name}"
        for resource_type, name in re.findall(
            r'(?m)^resource "([^"]+)" "([^"]+)"',
            _read(APPROVAL_TERRAFORM),
        )
    }
    assert actual_approval_addresses == expected_approval_addresses

    runtime_protected_addresses = expected_approval_addresses | {
        "aws_iam_policy.runtime_automation_boundary",
        "aws_s3_bucket_policy.image_release_evidence",
    }
    assert allowed.isdisjoint(runtime_protected_addresses)


def test_approval_reader_policy_is_exact_version_only_and_non_mutating() -> None:
    body = _read(APPROVAL_TERRAFORM)
    policy = _document(body, "approval_reader")
    allow = _allow_statements(policy)
    assert {action for statement in allow for action in _actions(statement)} == {
        "kms:Decrypt",
        "kms:Verify",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
    }

    s3_statement = next(
        statement for statement in allow if "s3:GetObjectVersion" in _actions(statement)
    )
    assert _actions(s3_statement) == {
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
    }
    assert _list_expressions(s3_statement, "resources") == (
        '"${aws_s3_bucket.image_release_evidence.arn}/${local.approval_evidence_prefix}/*"',
    )
    assert "source-declarations/" not in s3_statement
    assert "release-receipts/" not in s3_statement

    decrypt = next(statement for statement in allow if "kms:Decrypt" in _actions(statement))
    assert _actions(decrypt) == {"kms:Decrypt"}
    assert _list_expressions(decrypt, "resources") == ("aws_kms_key.image_release_evidence.arn",)

    verify = next(statement for statement in allow if "kms:Verify" in _actions(statement))
    assert _actions(verify) == {"kms:Verify"}
    assert _list_expressions(verify, "resources") == ("aws_kms_key.approval_signing.arn",)

    forbidden = {"kms:Sign", "s3:ListBucket", "s3:PutObject"}
    assert not (forbidden & {action for statement in allow for action in _actions(statement)})

    managed = _resource(body, "aws_iam_policy", "approval_reader")
    assert re.search(
        r"(?m)^\s*policy\s*=\s*"
        r"data\.aws_iam_policy_document\.approval_reader\.json\s*$",
        managed,
    )


def test_approval_reader_policy_is_attached_to_all_eight_readers() -> None:
    body = _read(APPROVAL_TERRAFORM)
    attachment_matches = list(
        re.finditer(r'resource "aws_iam_role_policy_attachment" "([^"]+)"', body)
    )
    attached_roles: set[str] = set()
    attachment_names: set[str] = set()
    for match in attachment_matches:
        block = _balanced_block_after(body[match.start() :], match.group(0))
        if "aws_iam_policy.approval_reader.arn" not in block:
            continue
        attachment_names.add(match.group(1))
        role = re.search(
            r"(?m)^\s*role\s*=\s*(aws_iam_role\.[a-z0-9_]+)\.(?:name|id)\s*$",
            block,
        )
        assert role is not None
        attached_roles.add(role.group(1))

    assert len(attachment_names) == 8
    assert attached_roles == {
        "aws_iam_role.codebuild",
        "aws_iam_role.mcp_source_publisher",
        "aws_iam_role.image_attestor",
        "aws_iam_role.image_promoter",
        "aws_iam_role.codebuild_launcher",
        "aws_iam_role.release_launcher",
        "aws_iam_role.image_deployment_gate",
        "aws_iam_role.runtime_automation",
    }


def test_reader_aggregate_allows_cannot_write_or_sign_approval_authority() -> None:
    body = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TERRAFORM_DIR.glob("*.tf"))
    )
    role_names = {
        "codebuild",
        "mcp_source_publisher",
        "image_attestor",
        "image_promoter",
        "codebuild_launcher",
        "release_launcher",
        "image_deployment_gate",
        "runtime_automation",
    }
    documents = _named_blocks(body, "data", "aws_iam_policy_document")
    inline_policies = _named_blocks(body, "resource", "aws_iam_role_policy")
    managed_policies = _named_blocks(body, "resource", "aws_iam_policy")
    attachments = _named_blocks(body, "resource", "aws_iam_role_policy_attachment")

    managed_documents: dict[str, str] = {}
    for name, block in managed_policies.items():
        match = re.search(
            r"(?m)^\s*policy\s*=\s*"
            r"data\.aws_iam_policy_document\.([a-z0-9_]+)\.json\s*$",
            block,
        )
        if match is not None:
            managed_documents[name] = match.group(1)

    role_documents: dict[str, set[str]] = defaultdict(set)
    for block in inline_policies.values():
        role = re.search(
            r"(?m)^\s*role\s*=\s*"
            r"aws_iam_role\.([a-z0-9_]+)(?:\[[^\]]+\])?\.(?:id|name)\s*$",
            block,
        )
        policy = re.search(
            r"(?m)^\s*policy\s*=\s*"
            r"data\.aws_iam_policy_document\.([a-z0-9_]+)\.json\s*$",
            block,
        )
        if role is not None and policy is not None and role.group(1) in role_names:
            role_documents[role.group(1)].add(policy.group(1))

    for block in attachments.values():
        role = re.search(
            r"(?m)^\s*role\s*=\s*"
            r"aws_iam_role\.([a-z0-9_]+)(?:\[[^\]]+\])?\.(?:id|name)\s*$",
            block,
        )
        policy = re.search(
            r"(?m)^\s*policy_arn\s*=\s*"
            r"aws_iam_policy\.([a-z0-9_]+)(?:\[[^\]]+\])?\.arn\s*$",
            block,
        )
        if role is None or role.group(1) not in role_names:
            continue
        assert policy is not None, (
            f"{role.group(1)} has a non-local managed policy attachment that "
            "must be reviewed for approval-authority access"
        )
        assert policy.group(1) in managed_documents
        role_documents[role.group(1)].add(managed_documents[policy.group(1)])

    assert set(role_documents) == role_names
    for role_name, document_names in role_documents.items():
        assert "approval_reader" in document_names
        for document_name in document_names:
            assert document_name in documents
            for statement in _allow_statements(documents[document_name]):
                checks_sign = _action_matches(statement, "kms:Sign")
                checks_put = _action_matches(statement, "s3:PutObject")
                resources: tuple[str, ...] = ()
                if checks_sign or checks_put:
                    assert re.search(
                        r"(?m)^\s*resources\s*=\s*\[",
                        statement,
                    ), (role_name, document_name)
                    resources = _list_expressions(statement, "resources")
                if checks_sign:
                    assert '"*"' not in resources, (role_name, document_name)
                    assert not any("approval_signing" in resource for resource in resources), (
                        role_name,
                        document_name,
                    )
                if checks_put:
                    assert '"*"' not in resources, (role_name, document_name)
                    assert '"${aws_s3_bucket.image_release_evidence.arn}/*"' not in resources, (
                        role_name,
                        document_name,
                    )
                    assert not any(
                        "approval_evidence_prefix" in resource for resource in resources
                    ), (role_name, document_name)
                if _action_matches(statement, "s3:ListBucket"):
                    assert "approval_evidence_prefix" not in statement, (
                        role_name,
                        document_name,
                    )
