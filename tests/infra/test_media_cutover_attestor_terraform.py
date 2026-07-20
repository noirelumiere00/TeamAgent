import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ATTESTOR = (ROOT / "infra/terraform/media_cutover_attestor.tf").read_text(
    encoding="utf-8"
)
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
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
    ):
        assert f'"{action}"' in deny


def test_runtime_automation_cannot_mint_media_evidence() -> None:
    read = _statement(RUNTIME, "ReadOnlyAuthoritativeMediaCutoverLedger")
    assert 'actions   = ["dynamodb:GetItem"]' in read
    assert '"media-cutover#*"' in read

    writable = _statement(RUNTIME, "ConditionalRuntimeEvidenceLedger")
    assert '"media-cutover#*"' not in writable

    verify = _statement(RUNTIME, "VerifyOnlyMediaCutoverAttestorKey")
    assert 'actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]' in verify
    assert "kms:Sign" not in verify


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
