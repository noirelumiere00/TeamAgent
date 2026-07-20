import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ATTESTOR = (ROOT / "infra/terraform/media_cutover_attestor.tf").read_text(encoding="utf-8")
RUNTIME = (ROOT / "infra/terraform/runtime_evidence.tf").read_text(encoding="utf-8")


def _statement(body: str, sid: str) -> str:
    match = re.search(rf'sid\s*=\s*"{re.escape(sid)}"', body)
    assert match is not None
    start = match.start()
    next_statement = body.find("\n  statement {", start)
    return body[start:] if next_statement == -1 else body[start:next_statement]


def test_media_cutover_has_a_dedicated_mfa_signer_and_key() -> None:
    assert 'resource "aws_kms_key" "media_cutover_attestor"' in ATTESTOR
    assert 'customer_master_key_spec = "ECC_NIST_P256"' in ATTESTOR
    assert 'resource "aws_iam_role" "media_cutover_attestor"' in ATTESTOR
    assert 'variable = "aws:MultiFactorAuthPresent"' in ATTESTOR
    assert 'values   = ["true"]' in ATTESTOR
    assert "teamagent-production-media-cutover-attestor" in ATTESTOR
    assume = _statement(ATTESTOR, "ExactRootMfaMediaCutoverSession")
    assert '"aws:MultiFactorAuthPresent"' in assume
    assert "ExistingOrganizationRecipientRole" not in ATTESTOR


def test_media_attestor_can_only_create_unique_media_rows() -> None:
    create = _statement(ATTESTOR, "CreateOnlyUniqueMediaCutoverReadyRow")
    assert '"dynamodb:GetItem"' in create
    assert '"dynamodb:PutItem"' in create
    assert '"media-cutover#*"' in create
    assert 'variable = "dynamodb:LeadingKeys"' in create
    assert 'test     = "Null"' in create

    deny = _statement(ATTESTOR, "DenyOtherLedgerMutation")
    for action in (
        "dynamodb:BatchWriteItem",
        "dynamodb:DeleteItem",
        "dynamodb:UpdateItem",
    ):
        assert f'"{action}"' in deny

    authorize = _statement(ATTESTOR, "AtomicallyAuthorizeOneMediaApply")
    assert '"dynamodb:TransactWriteItems"' in authorize
    for key in (
        "intent#*",
        "lock#teamagent/terraform.tfstate",
        "media-cutover#*",
    ):
        assert f'"{key}"' in authorize


def test_runtime_automation_cannot_mint_media_evidence() -> None:
    read = _statement(RUNTIME, "ReadOnlyAuthoritativeMediaCutoverLedger")
    assert 'actions   = ["dynamodb:GetItem"]' in read
    assert '"media-cutover#*"' in read

    writable = _statement(RUNTIME, "ConditionalRuntimeEvidenceLedger")
    assert '"media-cutover#*"' not in writable

    verify = _statement(RUNTIME, "VerifyOnlyMediaCutoverAttestorKey")
    assert 'actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]' in verify
    assert "kms:Sign" not in verify


def test_runtime_boundary_denies_media_ledger_and_table_takeover() -> None:
    runtime_ledger = _statement(RUNTIME, "ConditionalRuntimeEvidenceLedger")
    assert '"ecs-service-apply#*"' in runtime_ledger

    media = _statement(RUNTIME, "DenyAuthoritativeMediaLedgerMutation")
    for action in (
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
    ):
        assert f'"{action}"' in media
    assert '"media-cutover#*"' in media
    assert '"dynamodb:LeadingKeys"' in media

    control = _statement(RUNTIME, "DenyAuthoritativeLedgerControlPlaneMutation")
    for action in (
        "dynamodb:DeleteTable",
        "dynamodb:PutResourcePolicy",
        "dynamodb:DeleteResourcePolicy",
        "dynamodb:RestoreTableFromBackup",
        "dynamodb:RestoreTableToPointInTime",
        "dynamodb:UpdateTable",
        "dynamodb:UpdateTimeToLive",
    ):
        assert f'"{action}"' in control
    assert "aws_dynamodb_table.image_deployment_intents.arn" in control


def test_runtime_boundary_cannot_take_over_media_signing_key() -> None:
    key = _statement(RUNTIME, "DenyMediaAttestorKeyMutationAndUse")
    for action in (
        "kms:CreateGrant",
        "kms:PutKeyPolicy",
        "kms:ScheduleKeyDeletion",
        "kms:Sign",
    ):
        assert f'"{action}"' in key
    assert "aws_kms_key.media_cutover_attestor.arn" in key

    alias = _statement(RUNTIME, "DenyMediaAttestorAliasMutation")
    for action in ("kms:CreateAlias", "kms:DeleteAlias", "kms:UpdateAlias"):
        assert f'"{action}"' in alias
    assert "local.media_cutover_attestor_key_alias" in alias


def test_openclaw_kms_permissions_are_exact_key_arns() -> None:
    rollout = (ROOT / "infra/terraform/openclaw_rollout_evidence.tf").read_text(encoding="utf-8")
    encryption = _statement(rollout, "UseOnlyOpenClawRolloutEncryptionKey")
    signing = _statement(rollout, "SignAndVerifyOnlyOpenClawRolloutResults")
    assert "aws_kms_key.openclaw_rollout_evidence.arn" in encryption
    assert "aws_kms_key.openclaw_rollout_signing.arn" in signing
    wildcard = "arn:aws:kms:ap-northeast-1:718959508629:key/*"
    assert wildcard not in encryption
    assert wildcard not in signing


def test_attestor_cannot_mutate_runtime_or_sign_with_another_key() -> None:
    mutation = _statement(ATTESTOR, "DenyDeploymentAndRuntimeMutation")
    for action in (
        "ecs:RunTask",
        "ecs:UpdateService",
        "lambda:UpdateEventSourceMapping",
        "s3:*",
        "secretsmanager:*",
    ):
        assert f'"{action}"' in mutation

    other_key = _statement(ATTESTOR, "DenySigningWithAnyOtherKey")
    assert 'actions       = ["kms:Sign"]' in other_key
    assert "not_resources = [aws_kms_key.media_cutover_attestor.arn]" in other_key
