from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "codebuild.tf"
TERRAFORM_DIR = TERRAFORM.parent
README = ROOT / "infra" / "terraform" / "README.md"

START_BUILD_ACTIONS = {
    "codebuild:*",
    "codebuild:RetryBuild",
    "codebuild:RetryBuildBatch",
    "codebuild:StartBuild",
    "codebuild:StartBuildBatch",
}


def _body() -> str:
    return TERRAFORM.read_text(encoding="utf-8")


def _balanced_block_after(body: str, marker: str) -> str:
    marker_offset = body.index(marker)
    opening = body.index("{", marker_offset)
    depth = 0
    for offset in range(opening, len(body)):
        if body[offset] == "{":
            depth += 1
        elif body[offset] == "}":
            depth -= 1
            if depth == 0:
                return body[opening + 1 : offset]
    raise AssertionError(f"unterminated Terraform block after {marker!r}")


def _terraform_sources() -> tuple[tuple[Path, str], ...]:
    return tuple(
        (path, path.read_text(encoding="utf-8")) for path in sorted(TERRAFORM_DIR.glob("*.tf"))
    )


def _terraform_blocks(
    declaration: str,
    terraform_type: str,
) -> tuple[tuple[Path, str, str], ...]:
    pattern = re.compile(
        rf'^\s*{re.escape(declaration)}\s+"{re.escape(terraform_type)}"\s+'
        r'"(?P<name>[^"]+)"\s*\{',
        re.MULTILINE,
    )
    blocks: list[tuple[Path, str, str]] = []
    for path, source in _terraform_sources():
        for match in pattern.finditer(source):
            block = _balanced_block_after(source[match.start() :], match.group(0))
            blocks.append((path, match.group("name"), block))
    return tuple(blocks)


def _unique_terraform_block(
    declaration: str,
    terraform_type: str,
    name: str,
) -> str:
    matches = [
        (path, block)
        for path, block_name, block in _terraform_blocks(declaration, terraform_type)
        if block_name == name
    ]
    if len(matches) != 1:
        locations = ", ".join(str(path) for path, _ in matches) or "none"
        raise AssertionError(
            f"expected exactly one {declaration} {terraform_type}.{name}, "
            f"found {len(matches)} in {locations}"
        )
    return matches[0][1]


def _document(name: str) -> str:
    return _unique_terraform_block("data", "aws_iam_policy_document", name)


def _assignment(block: str, name: str) -> str:
    matches = tuple(
        re.finditer(
            rf"^\s*{re.escape(name)}\s*=\s*(.*?)\s*$",
            block,
            re.MULTILINE,
        )
    )
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} assignment, found {len(matches)}")
    return matches[0].group(1)


def _references_role(block: str, role_name: str) -> bool:
    role = re.fullmatch(
        rf"aws_iam_role\.{re.escape(role_name)}\.(?:id|name)",
        _assignment(block, "role"),
    )
    return role is not None


def _policy_document_reference(block: str, context: str) -> str:
    policy_expression = _assignment(block, "policy")
    match = re.fullmatch(
        r"data\.aws_iam_policy_document\.([A-Za-z0-9_-]+)\.json",
        policy_expression,
    )
    if match is None:
        raise AssertionError(f"cannot resolve {context} policy document from {policy_expression!r}")
    return match.group(1)


def _attached_policy_documents(role_name: str) -> tuple[str, ...]:
    documents: set[str] = set()
    for _, attachment_name, attachment in _terraform_blocks(
        "resource", "aws_iam_role_policy_attachment"
    ):
        if not _references_role(attachment, role_name):
            continue

        policy_arn = _assignment(attachment, "policy_arn")
        policy_reference = re.fullmatch(
            r"aws_iam_policy\.([A-Za-z0-9_-]+)\.arn",
            policy_arn,
        )
        if policy_reference is None:
            raise AssertionError(
                f"cannot resolve attachment {attachment_name!r} policy ARN from {policy_arn!r}"
            )
        policy_name = policy_reference.group(1)
        managed_policy = _unique_terraform_block("resource", "aws_iam_policy", policy_name)
        documents.add(
            _policy_document_reference(
                managed_policy,
                f"managed policy {policy_name!r}",
            )
        )

    for _, inline_name, inline_policy in _terraform_blocks("resource", "aws_iam_role_policy"):
        if not _references_role(inline_policy, role_name):
            continue
        documents.add(
            _policy_document_reference(
                inline_policy,
                f"inline policy {inline_name!r}",
            )
        )

    if not documents:
        raise AssertionError(f"no policy documents attached to role {role_name!r}")
    return tuple(sorted(documents))


def _managed_policy(role_name: str) -> str:
    return "\n".join(
        _document(document_name) for document_name in _attached_policy_documents(role_name)
    )


def _nested_blocks(source: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(
        _balanced_block_after(source[match.start() :], match.group(0))
        for match in pattern.finditer(source)
    )


def _list_expression(block: str, name: str) -> str:
    assignment = re.search(rf"\b{re.escape(name)}\s*=", block)
    if assignment is None:
        raise AssertionError(f"missing {name!r} list assignment")
    opening = assignment.end()
    while opening < len(block) and block[opening].isspace():
        opening += 1
    if opening == len(block) or block[opening] != "[":
        raise AssertionError(f"{name!r} is not a literal list")

    depth = 0
    for offset in range(opening, len(block)):
        if block[offset] == "[":
            depth += 1
        elif block[offset] == "]":
            depth -= 1
            if depth == 0:
                return block[opening + 1 : offset]
    raise AssertionError(f"unterminated {name!r} list")


def _resources_expression(statement: str) -> str:
    expression = re.sub(r"\s+", "", _list_expression(statement, "resources"))
    return expression.strip(",")


def _start_build_allow_statements(policy: str) -> tuple[str, ...]:
    statements = _nested_blocks(policy, re.compile(r"\bstatement\s*\{"))
    return tuple(
        statement
        for statement in statements
        if _effect(statement) == "Allow" and not _actions(statement).isdisjoint(START_BUILD_ACTIONS)
    )


def _assert_start_build_allows_are_pinned(policy: str) -> None:
    statements = _start_build_allow_statements(policy)
    expected_resources = {
        "local.launcher_project_arn",
        "local.launcher_all_project_arns[1]",
        "local.launcher_all_project_arns[2]",
        "local.launcher_all_project_arns[3]",
    }

    resources: set[str] = set()
    for statement in statements:
        expression = _resources_expression(statement)
        assert '"*"' not in expression
        assert (
            "local.launcher_project_arn" in expression
            or "local.launcher_all_project_arns" in expression
        )
        resources.add(expression)

    assert len(statements) == 4
    assert resources == expected_resources


def _string_assignment(block: str, name: str) -> str | None:
    match = re.search(
        rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"\s*$',
        block,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _condition_matches(condition: str, test: str, variable: str) -> bool:
    return (
        _string_assignment(condition, "test") == test
        and _string_assignment(condition, "variable") == variable
    )


def _local_map(name: str) -> dict[str, str]:
    body = _body()
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=\s*\{{", re.MULTILINE)
    matches = tuple(pattern.finditer(body))
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one local map {name!r}, found {len(matches)}")
    map_body = _balanced_block_after(
        body[matches[0].start() :],
        matches[0].group(0),
    )
    entries: dict[str, str] = {}
    for raw_line in map_body.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue
        entry = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)", line)
        if entry is None:
            raise AssertionError(f"cannot parse {name!r} entry {line!r}")
        key = entry.group(1)
        if key in entries:
            raise AssertionError(f"duplicate {name!r} entry {key!r}")
        entries[key] = entry.group(2).removesuffix(",").rstrip()
    return entries


def _statement(document: str, sid: str) -> str:
    for match in re.finditer(r"\bstatement\s*\{", document):
        statement = _balanced_block_after(document[match.start() :], match.group(0))
        if re.search(rf'\bsid\s*=\s*"{re.escape(sid)}"', statement):
            return statement
    raise AssertionError(f"missing IAM statement {sid!r}")


def _actions(statement: str) -> set[str]:
    match = re.search(r"\bactions\s*=\s*\[(.*?)\]", statement, re.DOTALL)
    if match is None:
        raise AssertionError("IAM statement has no actions list")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _effect(statement: str) -> str:
    match = re.search(r'\beffect\s*=\s*"([^"]+)"', statement)
    return match.group(1) if match else "Allow"


def test_main_launcher_is_exact_assume_once_boundary_and_direct_start_is_denied() -> None:
    body = _body()
    documents = _attached_policy_documents("codebuild_launcher")
    policy = _managed_policy("codebuild_launcher")

    assert {
        "codebuild_launcher_core",
        "codebuild_launcher_start",
        "approval_reader",
    } <= set(documents)
    assert 'user_name = "AIIAdev"' in body
    assert "identifiers = [data.aws_iam_user.aiia_dev.arn]" in body
    assert re.search(
        r'^\s*launcher_role_name\s*=\s*"teamagent-dev-codebuild-launcher"$',
        body,
        re.MULTILINE,
    )
    assert "max_session_duration = 10800" in body
    assert "local.launcher_project_arn" in policy
    assert "local.launcher_all_project_arns" in policy
    assert "teamagent-dev-image-builder" in body
    assert "teamagent-dev-mcp-source-publisher" in body
    assert "teamagent-dev-image-attestor" in body
    assert "teamagent-dev-image-promoter" in body
    assert "teamagent-mcp-verified-candidates" in policy
    assert "teamagent-media-worker-verified-candidates" in policy
    assert "teamagent-mcp-quarantine" not in policy
    assert "teamagent-media-worker-quarantine" not in policy
    assert 'repository/teamagent-mcp"' not in policy
    assert 'repository/teamagent-media-worker"' not in policy
    assert "codebuild/source.zip" not in policy
    assert 'resource "aws_iam_user_policy" "aiia_dev_no_direct_start_build"' in body
    direct = _document("aiia_dev_no_direct_start_build")
    direct_deny = _statement(direct, "RequireDedicatedLauncherRole")
    assert _effect(direct_deny) == "Deny"
    assert _actions(direct_deny) == {
        "codebuild:StartBuild",
        "codebuild:RetryBuild",
        "codebuild:RetryBuildBatch",
        "codebuild:StartBuildBatch",
        "codebuild:StartCommandExecution",
        "codebuild:StartSandbox",
        "codebuild:StartSandboxConnection",
    }
    _assert_start_build_allows_are_pinned(policy)


def test_start_build_environment_is_allowlisted_and_fixed_values_are_pinned() -> None:
    body = _body()
    policy = _managed_policy("codebuild_launcher")

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
        "SOURCE_ARCHIVE_VERSION_ID",
        "SOURCE_DECLARATION_VERSION_ID",
        "SOURCE_DECLARATION_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}"' in body
    assert '"IMAGE_TAG"' not in body
    _assert_start_build_allows_are_pinned(policy)

    environment_variable_names = "codebuild:environment.environmentVariables.name"
    start_build_statements = _start_build_allow_statements(policy)
    for statement in start_build_statements:
        conditions = _nested_blocks(statement, re.compile(r"\bcondition\s*\{"))
        null_conditions = tuple(
            condition
            for condition in conditions
            if _condition_matches(condition, "Null", environment_variable_names)
        )
        assert len(null_conditions) == 1
        null_values = re.sub(r"\s+", "", _list_expression(null_conditions[0], "values")).strip(",")
        assert null_values == '"false"'

        allowlist_conditions = tuple(
            condition
            for condition in conditions
            if _condition_matches(
                condition,
                "ForAllValues:StringEquals",
                environment_variable_names,
            )
        )
        assert len(allowlist_conditions) == 1

    provenance_statements = tuple(
        statement
        for statement in start_build_statements
        if _resources_expression(statement) == "local.launcher_project_arn"
    )
    assert len(provenance_statements) == 1
    dynamic_conditions = _nested_blocks(
        provenance_statements[0],
        re.compile(r'\bdynamic\s+"condition"\s*\{'),
    )
    assert len(dynamic_conditions) == 1
    dynamic_condition = dynamic_conditions[0]
    assert re.search(
        r"^\s*for_each\s*=\s*local\.launcher_fixed_environment_values\s*$",
        dynamic_condition,
        re.MULTILINE,
    )
    assert _string_assignment(dynamic_condition, "test") == "ForAllValues:StringEquals"
    assert _string_assignment(dynamic_condition, "variable") == (
        "codebuild:environment.environmentVariables/${condition.key}.value"
    )
    assert re.sub(r"\s+", "", _list_expression(dynamic_condition, "values")) == ("condition.value")

    assert _local_map("launcher_fixed_environment_values") == {
        "GIT_BRANCH": '"dev"',
        "APP_HTML_VERSION_ID": "local.canonical_app_html_version_id",
        "APP_HTML_SHA256": "local.canonical_app_html_sha256",
        "VAULT_MANIFEST_SHA256": "local.canonical_vault_manifest_sha256",
        "BUILD_INPUTS_SHA256": "local.canonical_build_inputs_sha256",
        "BAKED_APP_HTML_VERSION_ID": "local.canonical_baked_app_html_version_id",
        "BAKED_APP_HTML_SHA256": "local.canonical_baked_app_html_sha256",
        "APP_PROVENANCE_SHA256": "local.canonical_app_provenance_sha256",
        "SOURCE_MANIFEST_CONTRACT_SHA256": "local.runtime_contract_sha256",
        "RELEASE_CONTRACT_SHA256": "local.mcp_release_contract_sha256",
    }


def test_source_publisher_can_read_both_app_inputs_but_only_write_source_zip() -> None:
    policy = _document("mcp_source_publisher")

    read = policy.split('sid = "ReadPinnedAppInputs"', maxsplit=1)[1].split(
        "\n  statement {", maxsplit=1
    )[0]
    assert '"s3:GetObject"' in read
    assert '"s3:GetObjectVersion"' in read
    assert '"s3:PutObject"' not in read
    assert "/codebuild/connect-web-app.html" in read
    assert "/codebuild/baked-fallback/connect-web-app.html" in read

    publish = policy.split('sid     = "PublishExactSource"', maxsplit=1)[1].split(
        "\n  statement {", maxsplit=1
    )[0]
    assert 'actions = ["s3:PutObject"]' in publish
    assert "/codebuild/source.zip" in publish
    assert "connect-web-app.html" not in publish


def test_official_dangerous_override_condition_keys_are_explicit_denies() -> None:
    body = _body()
    policy = _managed_policy("codebuild_launcher")

    keys = {
        "codebuild:source",
        "codebuild:source.buildspec",
        "codebuild:source.location",
        "codebuild:secondarySources",
        "codebuild:artifacts",
        "codebuild:secondaryArtifacts",
        "codebuild:environment.image",
        "codebuild:environment.type",
        "codebuild:environment.computeType",
        "codebuild:environment.privilegedMode",
        "codebuild:environment.registryCredential",
        "codebuild:environment.imagePullCredentialsType",
        "codebuild:logsConfig",
        "codebuild:cache",
        "codebuild:serviceRole",
        "codebuild:encryptionKey",
        "codebuild:autoRetryLimit",
    }
    for key in keys:
        assert f'"{key}"' in body
    assert '"codebuild:timeoutInMinutes"' not in body
    assert '"codebuild:queuedTimeoutInMinutes"' not in body
    for key_set in (
        "launcher_denied_override_condition_keys_manage_a",
        "launcher_denied_override_condition_keys_manage_b",
        "launcher_denied_override_condition_keys_guardrails",
    ):
        deny = _balanced_block_after(policy, f"for_each = local.{key_set}")
        assert 'effect    = "Deny"' in deny
        assert 'actions   = ["codebuild:StartBuild"]' in deny
        assert "resources = local.launcher_all_project_arns" in deny
        assert 'test     = "Null"' in deny
        assert "variable = statement.value" in deny
        assert 'values   = ["false"]' in deny
    assert '"ssmmessages:*"' in policy


def test_all_fixed_build_roles_deny_non_plaintext_environment_resolution() -> None:
    dynamic_environment_denies = {
        "codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
        "tiktok_codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
        "mcp_source_publisher": (
            "DenyAllEcrAndEvidenceDeletion",
            {
                "ecr:*",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "image_attestor": (
            "DenySourceAndEvidenceDeletion",
            {
                "codeconnections:GetConnection",
                "codeconnections:GetConnectionToken",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "image_promoter": (
            "DenySourceSigningAndEvidenceWrites",
            {
                "codeconnections:GetConnection",
                "codeconnections:GetConnectionToken",
                "kms:Sign",
                "secretsmanager:GetSecretValue",
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "s3:PutObject",
                "s3:PutObjectRetention",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:StartSession",
                "ssmmessages:*",
            },
        ),
        "openclaw_codebuild": (
            "DenyDynamicEnvironmentAndDebugChannels",
            {
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssmmessages:*",
            },
        ),
    }
    for policy_name, (sid, expected_actions) in dynamic_environment_denies.items():
        policy = _document(policy_name)
        denial = _statement(policy, sid)
        assert _effect(denial) == "Deny"
        assert _actions(denial) == expected_actions


def test_only_source_free_promoter_can_write_release_repositories() -> None:
    body = _body()
    main_builder = _document("codebuild")
    tiktok_builder = _document("tiktok_codebuild")
    openclaw_builder = _document("openclaw_codebuild")
    attestor = _document("image_attestor")
    promoter = _document("image_promoter")

    quarantine_write_actions = {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeImageScanFindings",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    release_write_denies = {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchDeleteImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    assert _actions(_statement(main_builder, "EcrMcpQuarantineWrite")) == (quarantine_write_actions)
    for policy, sid in (
        (main_builder, "DenyMcpCandidateAndReleaseWrite"),
        (tiktok_builder, "DenyTiktokCandidateAndReleaseWrite"),
        (openclaw_builder, "DenyOpenClawCandidateAndReleaseWrite"),
        (attestor, "DenyEveryCandidateAndReleaseRepositoryWrite"),
    ):
        denial = _statement(policy, sid)
        assert _effect(denial) == "Deny"
        assert _actions(denial) == release_write_denies

    promoter_write = _statement(promoter, "WriteOnlyAllowlistedCandidateAndReleaseRepositories")
    assert _effect(promoter_write) == "Allow"
    assert _actions(promoter_write) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }
    for release in (
        "aws_ecr_repository.mcp.arn",
        "aws_ecr_repository.mcp_media.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
        "aws_ecr_repository.tiktok_acquire[0].arn",
    ):
        assert release in promoter
    assert '"kms:Sign"' in promoter
    assert '"s3:PutObject"' in promoter
    assert '"codeconnections:GetConnection"' in promoter
    source_free = body.split('resource "aws_codebuild_project" "image_promoter"', maxsplit=1)[
        1
    ].split('output "image_promoter_project"', maxsplit=1)[0]
    assert 'type      = "NO_SOURCE"' in source_free
    assert "privileged_mode = false" in source_free


def test_every_codebuild_service_trust_pins_source_account_and_project_arn() -> None:
    body = _body()
    trusts = body.split('identifiers = ["codebuild.amazonaws.com"]')[1:]

    assert len(trusts) == 6
    for trust in trusts:
        section = trust.split("\n  }\n}", maxsplit=1)[0]
        assert 'variable = "aws:SourceAccount"' in section
        assert 'variable = "aws:SourceArn"' in section
        assert "arn:aws:codebuild:" in section


def test_release_launcher_accepts_candidate_locator_fields_only_for_active_or_rollback() -> None:
    body = _body()
    policy = _managed_policy("release_launcher")

    for name in (
        "CANDIDATE_RECEIPT_KEY",
        "CANDIDATE_RECEIPT_VERSION_ID",
        "CANDIDATE_RECEIPT_SIGNATURE_KEY",
        "CANDIDATE_RECEIPT_SIGNATURE_VERSION_ID",
    ):
        assert f'"{name}"' in body
    assert "local.release_attestor_environment_names" in policy
    assert 'values   = ["active", "rollback"]' in policy
    assert "ReadCandidateAndReleaseDigest" in policy
    assert re.search(r'sid\s+= "DenyMutationAndAlternateEntryPoints"', policy)
    assert '"ecs:*"' in policy and '"events:*"' in policy


def test_image_deployment_gate_role_is_read_verify_and_ledger_only() -> None:
    body = _body()
    policy = _document("image_deployment_gate")

    assert "ReadExactImmutableReleaseReceipts" in policy
    assert "release-receipts/*" in policy
    verify_signature = _statement(policy, "VerifyAttestorReceiptSignature")
    assert _effect(verify_signature) == "Allow"
    assert _actions(verify_signature) == {
        "kms:DescribeKey",
        "kms:GetPublicKey",
        "kms:Verify",
    }
    assert '"ReadReleaseSubjectAndReferrerGraph"' in policy
    assert '"ecr:BatchGetImage"' in policy
    assert '"ecr:GetLifecyclePolicy"' in policy
    assert '"ecr:ListImageReferrers"' not in policy
    for repository in (
        "aws_ecr_repository.mcp.arn",
        "aws_ecr_repository.mcp_media.arn",
        "aws_ecr_repository.openclaw.arn",
        "aws_ecr_repository.openclaw_media.arn",
        "aws_ecr_repository.tiktok_acquire[0].arn",
    ):
        assert repository in policy
    assert _actions(_statement(policy, "ReadDeploymentLedger")) == {"dynamodb:GetItem"}
    assert _actions(_statement(policy, "PrepareUniqueDeploymentIntent")) == {"dynamodb:PutItem"}
    assert _actions(_statement(policy, "TransitionDeploymentIntentOrHeartbeatLock")) == {
        "dynamodb:UpdateItem"
    }
    assert _actions(_statement(policy, "AtomicallyStartAndConsumeDeployment")) == {
        "dynamodb:TransactWriteItems"
    }
    assert _actions(_statement(policy, "ReleaseOnlySharedDeploymentLock")) == {
        "dynamodb:DeleteItem"
    }
    assert "aws_dynamodb_table.image_deployment_intents.arn" in policy
    assert "PrepareUniqueDeploymentIntent" in policy
    assert "AtomicallyStartAndConsumeDeployment" in policy
    assert "ReleaseOnlySharedDeploymentLock" in policy
    assert policy.count('variable = "dynamodb:LeadingKeys"') == 5
    delete_statement = policy.split(
        '"ReleaseOnlySharedDeploymentLock"',
        maxsplit=1,
    )[1].split('"DenyRuntimeEvidenceAndImageMutation"', maxsplit=1)[0]
    assert '"lock#teamagent/terraform.tfstate"' in delete_statement
    assert '"intent#*"' not in delete_statement
    assert '"receipt#*"' not in delete_statement
    runtime_mutation_deny = _statement(policy, "DenyRuntimeEvidenceAndImageMutation")
    assert _effect(runtime_mutation_deny) == "Deny"
    assert _actions(runtime_mutation_deny) == {
        "codebuild:StartBuild",
        "codebuild:StartBuildBatch",
        "ecr:BatchDeleteImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecs:*",
        "events:*",
        "kms:Sign",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutObject",
        "s3:PutObjectRetention",
    }
    assert 'aws_iam_user_policy" "aiia_dev_image_deployment_gate"' not in body
    trust = _document("image_deployment_gate_assume")
    assert '"arn:aws:iam::718959508629:root"' in trust
    assert 'variable = "aws:PrincipalArn"' in trust
    assert "local.terraform_automation_role_arn" in trust
    assert "data.aws_iam_user.aiia_dev.arn" not in trust


def test_existing_admin_authority_is_documented_as_an_accepted_bypass_risk() -> None:
    body = README.read_text(encoding="utf-8").lower()

    assert "do not rotate, delete, or reduce" in body
    assert "administrators can technically bypass" in body
    assert "administrator iam" in body
    assert "intentionally unchanged" in body
