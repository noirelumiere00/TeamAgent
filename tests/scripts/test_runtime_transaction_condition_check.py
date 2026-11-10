"""Transaction lock checks get no new item access or mutation authority."""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra/terraform/runtime_evidence.tf"
SID = "CheckExactSharedRuntimeLockWithinTransaction"


def _statement() -> str:
    source = TF.read_text()
    document = source.split('data "aws_iam_policy_document" "runtime_evidence_automation"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "runtime_evidence_automation"', 1)[0]
    statements = re.findall(r"(?ms)^  statement \{\n(.*?)^  \}", document)
    matches = [value for value in statements if re.search(rf'sid\s*=\s*"{SID}"', value)]
    assert len(matches) == 1
    return matches[0]


def test_lock_check_grants_only_the_missing_condition_action_on_the_existing_table() -> None:
    statement = _statement()
    actions = re.search(r"actions\s*=\s*\[([^\]]*)\]", statement)
    resources = re.search(r"resources\s*=\s*\[([^\]]*)\]", statement)
    assert actions and resources
    assert re.findall(r'"([^"]+)"', actions[1]) == ["dynamodb:ConditionCheckItem"]
    assert resources[1].strip() == "aws_dynamodb_table.image_deployment_intents.arn"
    effect = re.search(r'effect\s*=\s*"([^"]+)"', statement)
    assert effect is None or effect[1] == "Allow"  # Terraform's default is Allow.
    assert TF.read_text().count('"dynamodb:ConditionCheckItem"') == 1
    assert "*" not in statement


def test_lock_check_requires_exact_lock_and_transaction_context_without_missing_keys() -> None:
    statement = _statement()
    conditions = re.findall(r"(?ms)^    condition \{\n(.*?)^    \}", statement)
    actual = set()
    for condition in conditions:
        test = re.search(r'test\s*=\s*"([^"]+)"', condition)
        variable = re.search(r'variable\s*=\s*"([^"]+)"', condition)
        values = re.search(r"values\s*=\s*\[([^\]]*)\]", condition)
        assert test and variable and values
        actual.add((test[1], variable[1], tuple(re.findall(r'"([^"]+)"', values[1]))))
    assert len(conditions) == 4
    assert actual == {
        (
            "ForAllValues:StringEquals",
            "dynamodb:LeadingKeys",
            ("lock#teamagent/terraform.tfstate",),
        ),
        ("ForAnyValue:StringEquals", "dynamodb:EnclosingOperation", ("TransactWriteItems",)),
        ("Null", "dynamodb:LeadingKeys", ("false",)),
        ("Null", "dynamodb:EnclosingOperation", ("false",)),
    }


def test_lock_check_does_not_modify_the_role_boundary_or_broaden_existing_ledger_access() -> None:
    source = TF.read_text()
    boundary = source.split('data "aws_iam_policy_document" "runtime_automation_boundary"', 1)[
        1
    ].split('resource "aws_iam_policy" "runtime_automation_boundary"', 1)[0]
    assert SID not in boundary
    legacy = source.split('sid = "TransitionExactDeploymentIntentLedger"', 1)[1].split(
        'resource "aws_iam_role_policy" "runtime_evidence_automation"', 1
    )[0]
    assert '"dynamodb:ConditionCheckItem"' not in legacy
    assert (
        'values = [\n        "intent#*",\n        "lock#teamagent/terraform.tfstate",\n        "receipt#*",'
        in legacy
    )
