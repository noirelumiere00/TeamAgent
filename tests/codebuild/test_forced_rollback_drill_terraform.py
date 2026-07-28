from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRILL_TERRAFORM = ROOT / "infra" / "terraform" / "forced_rollback_drill.tf"
ROLLOUT_EVIDENCE_TERRAFORM = ROOT / "infra" / "terraform" / "openclaw_rollout_evidence.tf"


def _hcl_block(body: str, opening_brace: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "#":
            line_comment = True
        elif character == "/" and following == "/":
            line_comment = True
            index += 1
        elif character == "/" and following == "*":
            block_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return body[opening_brace : index + 1]
        index += 1
    raise AssertionError("unterminated HCL block")


def _declaration(
    body: str,
    kind: str,
    block_type: str,
    name: str,
) -> str:
    match = re.search(
        rf'(?m)^[ \t]*{re.escape(kind)}[ \t]+"{re.escape(block_type)}"'
        rf'[ \t]+"{re.escape(name)}"[ \t]*\{{',
        body,
    )
    if match is None:
        raise AssertionError(f"missing {kind} declaration {block_type}.{name}")
    return _hcl_block(body, body.index("{", match.start(), match.end()))


def _output(body: str, name: str) -> str:
    match = re.search(
        rf'(?m)^[ \t]*output[ \t]+"{re.escape(name)}"[ \t]*\{{',
        body,
    )
    if match is None:
        raise AssertionError(f"missing output {name}")
    return _hcl_block(body, body.index("{", match.start(), match.end()))


def _nested_blocks(body: str, name: str) -> list[str]:
    blocks: list[str] = []
    offset = 0
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(name)}[ \t]*\{{")
    while True:
        match = pattern.search(body, offset)
        if match is None:
            return blocks
        opening = body.index("{", match.start(), match.end())
        block = _hcl_block(body, opening)
        blocks.append(block)
        offset = opening + len(block)


def _statements(document: str) -> list[str]:
    return _nested_blocks(document, "statement")


def _statement(document: str, sid: str) -> str:
    for statement in _statements(document):
        if re.search(
            rf'(?m)^[ \t]*sid[ \t]*=[ \t]*"{re.escape(sid)}"[ \t]*$',
            statement,
        ):
            return statement
    raise AssertionError(f"missing IAM statement {sid}")


def _attribute_expression(block: str, attribute: str) -> str:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(attribute)}[ \t]*=[ \t]*([^#\n]+?)[ \t]*$",
        block,
    )
    if match is None:
        raise AssertionError(f"missing scalar attribute {attribute}")
    return re.sub(r"\s+", "", match.group(1))


def _attribute_list(block: str, attribute: str) -> str:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(attribute)}[ \t]*=[ \t]*\[",
        block,
    )
    if match is None:
        raise AssertionError(f"missing list attribute {attribute}")
    opening = block.index("[", match.start(), match.end())
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(block):
        character = block[index]
        following = block[index + 1] if index + 1 < len(block) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "#":
            line_comment = True
        elif character == "/" and following == "/":
            line_comment = True
            index += 1
        elif character == "/" and following == "*":
            block_comment = True
            index += 1
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return block[opening + 1 : index]
        index += 1
    raise AssertionError(f"unterminated list attribute {attribute}")


def _list_expressions(block: str, attribute: str) -> tuple[str, ...]:
    raw = _attribute_list(block, attribute)
    items: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False
    for offset, character in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            item = re.sub(r"\s+", "", raw[start:offset])
            if item:
                items.append(item)
            start = offset + 1
    item = re.sub(r"\s+", "", raw[start:])
    if item:
        items.append(item)
    return tuple(items)


def _actions(statement: str) -> set[str]:
    return {
        item[1:-1]
        for item in _list_expressions(statement, "actions")
        if item.startswith('"') and item.endswith('"')
    }


def _effect(statement: str) -> str:
    match = re.search(
        r'(?m)^[ \t]*effect[ \t]*=[ \t]*"([^"]+)"[ \t]*$',
        statement,
    )
    return match.group(1) if match is not None else "Allow"


def _conditions(statement: str) -> list[str]:
    return _nested_blocks(statement, "condition")


def _condition(statement: str, variable: str) -> str:
    for condition in _conditions(statement):
        if re.search(
            rf'(?m)^[ \t]*variable[ \t]*=[ \t]*"{re.escape(variable)}"[ \t]*$',
            condition,
        ):
            return condition
    raise AssertionError(f"missing IAM condition for {variable}")


def _assert_condition(
    statement: str,
    *,
    variable: str,
    test: str,
    values: tuple[str, ...],
) -> None:
    condition = _condition(statement, variable)
    assert _attribute_expression(condition, "test") == f'"{test}"'
    assert _list_expressions(condition, "values") == values


def _local_string(body: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*"
        r'(?:\([ \t\r\n]*)?"([^"]+)"(?:[ \t\r\n]*\))?[ \t]*$',
        body,
    )
    if match is None:
        raise AssertionError(f"missing fixed string local {name}")
    return match.group(1)


def _local_integer(body: str, name: str) -> int:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*([0-9]+)[ \t]*$",
        body,
    )
    if match is None:
        raise AssertionError(f"missing fixed integer local {name}")
    return int(match.group(1))


def test_uses_existing_locked_versioned_sse_kms_bucket_and_prefix_lifecycle() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    existing = ROLLOUT_EVIDENCE_TERRAFORM.read_text(encoding="utf-8")

    assert (
        _local_string(body, "forced_rollback_drill_evidence_bucket_name")
        == "teamagent-dev-openclaw-rollout-evidence"
    )
    assert _local_string(body, "forced_rollback_drill_evidence_prefix") == "forced-rollback-drills/"
    assert (
        re.search(
            r'(?m)^[ \t]*resource[ \t]+"aws_s3_bucket"[ \t]+"',
            body,
        )
        is None
    )
    assert (
        re.search(
            r'(?m)^[ \t]*resource[ \t]+"aws_s3_bucket_policy"[ \t]+"',
            body,
        )
        is None
    )
    assert (
        re.search(
            r"(?m)^[ \t]*resource[ \t]+"
            r'"aws_s3_bucket_object_lock_configuration"[ \t]+"',
            body,
        )
        is None
    )

    bucket = _declaration(
        existing,
        "resource",
        "aws_s3_bucket",
        "openclaw_rollout_evidence",
    )
    versioning = _declaration(
        existing,
        "resource",
        "aws_s3_bucket_versioning",
        "openclaw_rollout_evidence",
    )
    encryption = _declaration(
        existing,
        "resource",
        "aws_s3_bucket_server_side_encryption_configuration",
        "openclaw_rollout_evidence",
    )
    assert _attribute_expression(bucket, "object_lock_enabled") == "true"
    versioning_configuration = _nested_blocks(
        versioning,
        "versioning_configuration",
    )
    assert len(versioning_configuration) == 1
    assert _attribute_expression(versioning_configuration[0], "status") == '"Enabled"'
    encryption_default = _nested_blocks(
        encryption,
        "apply_server_side_encryption_by_default",
    )
    assert len(encryption_default) == 1
    assert _attribute_expression(encryption_default[0], "sse_algorithm") == '"aws:kms"'
    assert (
        _attribute_expression(encryption_default[0], "kms_master_key_id")
        == "aws_kms_key.openclaw_rollout_evidence.arn"
    )

    lifecycle = _declaration(
        body,
        "resource",
        "aws_s3_bucket_lifecycle_configuration",
        "forced_rollback_drill_evidence",
    )
    assert (
        _attribute_expression(lifecycle, "bucket") == "aws_s3_bucket.openclaw_rollout_evidence.id"
    )
    assert set(_list_expressions(lifecycle, "depends_on")) == {
        "aws_s3_bucket_object_lock_configuration.openclaw_rollout_evidence",
        "aws_s3_bucket_server_side_encryption_configuration.openclaw_rollout_evidence",
        "aws_s3_bucket_versioning.openclaw_rollout_evidence",
    }
    rules = _nested_blocks(lifecycle, "rule")
    assert len(rules) == 1
    assert _attribute_expression(rules[0], "status") == '"Enabled"'
    filters = _nested_blocks(rules[0], "filter")
    assert len(filters) == 1
    assert (
        _attribute_expression(filters[0], "prefix") == "local.forced_rollback_drill_evidence_prefix"
    )


def test_drill_writes_require_sse_kms_compliance_for_3650_days() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    policy = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "forced_rollback_drill",
    )
    role_policy = _declaration(
        body,
        "resource",
        "aws_iam_role_policy",
        "forced_rollback_drill",
    )

    assert _local_string(body, "forced_rollback_drill_object_lock_mode") == "COMPLIANCE"
    assert _local_integer(body, "forced_rollback_drill_retention_days") == 3650
    assert _local_string(body, "forced_rollback_drill_evidence_object_arn") == (
        "${aws_s3_bucket.openclaw_rollout_evidence.arn}/"
        "${local.forced_rollback_drill_evidence_prefix}*"
    )
    assert _attribute_expression(role_policy, "role") == "aws_iam_role.forced_rollback_drill.id"
    assert (
        _attribute_expression(role_policy, "policy")
        == "data.aws_iam_policy_document.forced_rollback_drill.json"
    )

    put = _statement(
        policy,
        "PutOnlyCompliantForcedRollbackDrillEvidence",
    )
    assert _effect(put) == "Allow"
    assert _actions(put) == {"s3:PutObject"}
    assert _list_expressions(put, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    _assert_condition(
        put,
        variable="s3:x-amz-server-side-encryption",
        test="StringEquals",
        values=('"aws:kms"',),
    )
    _assert_condition(
        put,
        variable="s3:x-amz-server-side-encryption-aws-kms-key-id",
        test="StringEquals",
        values=("aws_kms_key.openclaw_rollout_evidence.arn",),
    )
    _assert_condition(
        put,
        variable="s3:object-lock-mode",
        test="StringEquals",
        values=("local.forced_rollback_drill_object_lock_mode",),
    )
    _assert_condition(
        put,
        variable="s3:object-lock-remaining-retention-days",
        test="NumericGreaterThanEquals",
        values=("tostring(local.forced_rollback_drill_retention_days)",),
    )

    retention = _statement(
        policy,
        "ExtendOnlyCompliantForcedRollbackDrillRetention",
    )
    assert _effect(retention) == "Allow"
    assert _actions(retention) == {"s3:PutObjectRetention"}
    assert _list_expressions(retention, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    _assert_condition(
        retention,
        variable="s3:object-lock-mode",
        test="StringEquals",
        values=("local.forced_rollback_drill_object_lock_mode",),
    )
    _assert_condition(
        retention,
        variable="s3:object-lock-remaining-retention-days",
        test="NumericGreaterThanEquals",
        values=("tostring(local.forced_rollback_drill_retention_days)",),
    )


def test_runtime_automation_can_store_and_exactly_redownload_compliant_drill_evidence() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    policy = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "runtime_automation_forced_rollback_drill_evidence",
    )
    role_policy = _declaration(
        body,
        "resource",
        "aws_iam_role_policy",
        "runtime_automation_forced_rollback_drill_evidence",
    )

    assert _attribute_expression(role_policy, "role") == "aws_iam_role.runtime_automation.id"
    assert (
        _attribute_expression(role_policy, "policy")
        == "data.aws_iam_policy_document.runtime_automation_forced_rollback_drill_evidence.json"
    )
    assert _local_string(body, "forced_rollback_drill_object_lock_mode") == "COMPLIANCE"
    assert _local_integer(body, "forced_rollback_drill_retention_days") == 3650
    assert len(_statements(policy)) == 4

    listing = _statement(policy, "ListOnlyForcedRollbackDrillEvidence")
    assert _effect(listing) == "Allow"
    assert _actions(listing) == {"s3:ListBucket"}
    assert _list_expressions(listing, "resources") == (
        "aws_s3_bucket.openclaw_rollout_evidence.arn",
    )
    assert len(_conditions(listing)) == 1
    _assert_condition(
        listing,
        variable="s3:prefix",
        test="StringLike",
        values=('"${local.forced_rollback_drill_evidence_prefix}*"',),
    )

    read = _statement(policy, "ReadOnlyForcedRollbackDrillEvidence")
    assert _effect(read) == "Allow"
    assert _actions(read) == {
        "s3:GetObject",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
    }
    assert _list_expressions(read, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    assert _conditions(read) == []

    put = _statement(policy, "PutOnlyCompliantForcedRollbackDrillEvidence")
    assert _effect(put) == "Allow"
    assert _actions(put) == {"s3:PutObject"}
    assert _list_expressions(put, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    assert len(_conditions(put)) == 4
    _assert_condition(
        put,
        variable="s3:x-amz-server-side-encryption",
        test="StringEquals",
        values=('"aws:kms"',),
    )
    _assert_condition(
        put,
        variable="s3:x-amz-server-side-encryption-aws-kms-key-id",
        test="StringEquals",
        values=("aws_kms_key.openclaw_rollout_evidence.arn",),
    )
    _assert_condition(
        put,
        variable="s3:object-lock-mode",
        test="StringEquals",
        values=("local.forced_rollback_drill_object_lock_mode",),
    )
    _assert_condition(
        put,
        variable="s3:object-lock-remaining-retention-days",
        test="NumericGreaterThanEquals",
        values=("tostring(local.forced_rollback_drill_retention_days)",),
    )

    retention = _statement(policy, "ExtendOnlyCompliantForcedRollbackDrillRetention")
    assert _effect(retention) == "Allow"
    assert _actions(retention) == {"s3:PutObjectRetention"}
    assert _list_expressions(retention, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    assert len(_conditions(retention)) == 2
    _assert_condition(
        retention,
        variable="s3:object-lock-mode",
        test="StringEquals",
        values=("local.forced_rollback_drill_object_lock_mode",),
    )
    _assert_condition(
        retention,
        variable="s3:object-lock-remaining-retention-days",
        test="NumericGreaterThanEquals",
        values=("tostring(local.forced_rollback_drill_retention_days)",),
    )

    assert all(
        not action.startswith("kms:")
        for statement in _statements(policy)
        for action in _actions(statement)
    )


def test_drill_aggregate_signer_is_dedicated_and_algorithm_pinned() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    key = _declaration(
        body,
        "resource",
        "aws_kms_key",
        "forced_rollback_drill_signing",
    )
    alias = _declaration(
        body,
        "resource",
        "aws_kms_alias",
        "forced_rollback_drill_signing",
    )
    key_policy = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "forced_rollback_drill_signing_key",
    )
    role_policy = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "forced_rollback_drill",
    )

    assert _attribute_expression(key, "key_usage") == '"SIGN_VERIFY"'
    assert _attribute_expression(key, "customer_master_key_spec") == '"RSA_3072"'
    assert (
        _attribute_expression(key, "policy")
        == "data.aws_iam_policy_document.forced_rollback_drill_signing_key.json"
    )
    key_lifecycle = _nested_blocks(key, "lifecycle")
    assert len(key_lifecycle) == 1
    assert _attribute_expression(key_lifecycle[0], "prevent_destroy") == "true"
    assert (
        _attribute_expression(alias, "target_key_id")
        == "aws_kms_key.forced_rollback_drill_signing.key_id"
    )
    assert _local_string(body, "forced_rollback_drill_signing_algorithm") == "RSASSA_PSS_SHA_256"

    signer = _statement(
        key_policy,
        "AllowOnlyForcedRollbackDrillAggregateSigning",
    )
    assert _actions(signer) == {"kms:Sign", "kms:Verify"}
    _assert_condition(
        signer,
        variable="aws:PrincipalArn",
        test="ArnEquals",
        values=("local.forced_rollback_drill_role_arn",),
    )
    _assert_condition(
        signer,
        variable="kms:SigningAlgorithm",
        test="StringEquals",
        values=("local.forced_rollback_drill_signing_algorithm",),
    )

    sign_allows = [
        statement
        for statement in _statements(role_policy)
        if _effect(statement) == "Allow" and "kms:Sign" in _actions(statement)
    ]
    assert len(sign_allows) == 1
    assert _list_expressions(sign_allows[0], "resources") == (
        "aws_kms_key.forced_rollback_drill_signing.arn",
    )
    _assert_condition(
        sign_allows[0],
        variable="kms:SigningAlgorithm",
        test="StringEquals",
        values=("local.forced_rollback_drill_signing_algorithm",),
    )
    assert body.count("aws_kms_key.approval_signing.arn") == 1
    assert (
        _attribute_expression(
            _output(body, "forced_rollback_drill_signing_key_arn"),
            "value",
        )
        == "aws_kms_key.forced_rollback_drill_signing.arn"
    )


def test_drill_trust_is_aiiadev_mfa_fixed_session_without_source_identity() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    trust = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "forced_rollback_drill_assume",
    )
    role = _declaration(
        body,
        "resource",
        "aws_iam_role",
        "forced_rollback_drill",
    )

    assert (
        _local_string(body, "forced_rollback_drill_role_name")
        == "teamagent-dev-forced-rollback-drill"
    )
    assert (
        _local_string(body, "forced_rollback_drill_role_session_name")
        == "teamagent-forced-rollback-drill"
    )
    statements = _statements(trust)
    assert len(statements) == 1
    statement = statements[0]
    assert _actions(statement) == {"sts:AssumeRole"}
    principals = _nested_blocks(statement, "principals")
    assert len(principals) == 1
    assert _attribute_expression(principals[0], "type") == '"AWS"'
    assert _list_expressions(principals[0], "identifiers") == ("data.aws_iam_user.aiia_dev.arn",)
    assert len(_conditions(statement)) == 2
    _assert_condition(
        statement,
        variable="aws:MultiFactorAuthPresent",
        test="Bool",
        values=('"true"',),
    )
    _assert_condition(
        statement,
        variable="sts:RoleSessionName",
        test="StringEquals",
        values=("local.forced_rollback_drill_role_session_name",),
    )
    assert "sts:SetSourceIdentity" not in body
    assert "sts:SourceIdentity" not in body
    assert _attribute_expression(role, "name") == "local.forced_rollback_drill_role_name"
    assert (
        _attribute_expression(role, "assume_role_policy")
        == "data.aws_iam_policy_document.forced_rollback_drill_assume.json"
    )


def test_attached_boundary_denies_all_three_guardrail_classes() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    boundary = _declaration(
        body,
        "data",
        "aws_iam_policy_document",
        "forced_rollback_drill_boundary",
    )
    boundary_policy = _declaration(
        body,
        "resource",
        "aws_iam_policy",
        "forced_rollback_drill_boundary",
    )
    role = _declaration(
        body,
        "resource",
        "aws_iam_role",
        "forced_rollback_drill",
    )

    assert (
        _attribute_expression(boundary_policy, "policy")
        == "data.aws_iam_policy_document.forced_rollback_drill_boundary.json"
    )
    assert (
        _attribute_expression(role, "permissions_boundary")
        == "aws_iam_policy.forced_rollback_drill_boundary.arn"
    )
    intersection = _statement(boundary, "AllowOnlyIdentityPolicyIntersection")
    assert _effect(intersection) == "Allow"
    assert _actions(intersection) == {"*"}
    assert _list_expressions(intersection, "resources") == ('"*"',)

    self_escalation = _statement(boundary, "DenyIamSelfEscalation")
    assert _effect(self_escalation) == "Deny"
    assert _list_expressions(self_escalation, "resources") == ('"*"',)
    assert {
        "iam:AttachRolePolicy",
        "iam:CreatePolicyVersion",
        "iam:DeleteRolePermissionsBoundary",
        "iam:PutRolePermissionsBoundary",
        "iam:PutRolePolicy",
        "iam:SetDefaultPolicyVersion",
        "iam:UpdateAssumeRolePolicy",
    } <= _actions(self_escalation)

    release_signing = _statement(
        boundary,
        "DenyReleaseApprovalKeySigning",
    )
    assert _effect(release_signing) == "Deny"
    assert _actions(release_signing) == {"kms:Sign"}
    assert _list_expressions(release_signing, "resources") == ("aws_kms_key.approval_signing.arn",)

    weaker_mode = _statement(
        boundary,
        "DenyWeakerForcedRollbackDrillRetentionMode",
    )
    assert _effect(weaker_mode) == "Deny"
    assert _actions(weaker_mode) == {"s3:PutObjectRetention"}
    assert _list_expressions(weaker_mode, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    assert len(_conditions(weaker_mode)) == 1
    _assert_condition(
        weaker_mode,
        variable="s3:object-lock-mode",
        test="StringNotEquals",
        values=("local.forced_rollback_drill_object_lock_mode",),
    )

    shorter_period = _statement(
        boundary,
        "DenyShorterForcedRollbackDrillRetentionPeriod",
    )
    assert _effect(shorter_period) == "Deny"
    assert _actions(shorter_period) == {"s3:PutObjectRetention"}
    assert _list_expressions(shorter_period, "resources") == (
        "local.forced_rollback_drill_evidence_object_arn",
    )
    assert len(_conditions(shorter_period)) == 1
    _assert_condition(
        shorter_period,
        variable="s3:object-lock-remaining-retention-days",
        test="NumericLessThan",
        values=("tostring(local.forced_rollback_drill_retention_days)",),
    )


def test_controller_outputs_are_exact() -> None:
    body = DRILL_TERRAFORM.read_text(encoding="utf-8")
    output_names = set(
        re.findall(
            r'(?m)^[ \t]*output[ \t]+"([^"]+)"[ \t]*\{',
            body,
        )
    )
    assert output_names == {
        "forced_rollback_drill_evidence_bucket",
        "forced_rollback_drill_evidence_prefix",
        "forced_rollback_drill_role_arn",
        "forced_rollback_drill_signing_key_arn",
    }
    expected = {
        "forced_rollback_drill_evidence_bucket": ("aws_s3_bucket.openclaw_rollout_evidence.id"),
        "forced_rollback_drill_evidence_prefix": ("local.forced_rollback_drill_evidence_prefix"),
        "forced_rollback_drill_signing_key_arn": ("aws_kms_key.forced_rollback_drill_signing.arn"),
        "forced_rollback_drill_role_arn": ("aws_iam_role.forced_rollback_drill.arn"),
    }
    for name, expression in expected.items():
        assert _attribute_expression(_output(body, name), "value") == expression
