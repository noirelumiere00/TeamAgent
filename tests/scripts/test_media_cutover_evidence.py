from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[2] / "infra" / "deploy" / "runtime_evidence_guard.py"
_SPEC = importlib.util.spec_from_file_location("_media_cutover_evidence", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module
_SPEC.loader.exec_module(module)

_DESIRED_IMAGE = (
    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
    f"teamagent-media-worker@sha256:{'d' * 64}"
)
_LEGACY_IMAGE = (
    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
    f"teamagent-dev-tiktok-acquire@sha256:{'a' * 64}"
)
_MAPPING_UUID = "01234567-89ab-4cde-8fab-0123456789ab"
_INTENT_ID = "12345678-1234-4abc-8def-123456789abc"
_OTHER_INTENT_ID = "87654321-4321-4cba-8fed-cba987654321"
_MIGRATION_SHA256 = "b" * 64
_REVIEWED_PLAN_SHA256 = "c" * 64
_MCP_TASK_DEFINITION = (
    "arn:aws:ecs:ap-northeast-1:718959508629:"
    "task-definition/teamagent-dev-mcp:55"
)
_MCP_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:"
    f"task/{module.MCP_CLUSTER}/{'1' * 32}"
)
_MEDIA_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:"
    "task-definition/teamagent-dev-tiktok-acquire:6"
)
_MEDIA_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/"
    "11111111-2222-4333-8444-555555555555"
)
class FakeAws:
    def __init__(self) -> None:
        self.now = 1_000
        self.identity_arn = module.AUTOMATION_ARN
        self.mapping_state = "Enabled"
        self.flags = {
            "USE_VIDEO_TOOLS": "false",
            "USE_TIKTOK_TOOLS": "false",
        }
        self.visible_messages = "0"
        self.legacy_running_tasks: list[str] = []
        self.legacy_pending_tasks: list[str] = []
        self.service_running_tasks = [_MCP_TASK]
        self.service_pending_tasks: list[str] = []
        self.rollout_state = "COMPLETED"
        self.deployments = 1
        self.item: dict[str, Any] | None = None
        self.write_operations: list[str] = []
        self.counter = 0
        self.evidence = module.ExecutableEvidence(
            path="/usr/local/bin/aws",
            device=1,
            inode=2,
            size=3,
            sha256="e" * 64,
            version="aws-cli/2.31.0 Python/3.13",
        )

    def _http(self) -> Any:
        self.counter += 1
        return module.HttpEvidence(
            date="1970-01-01T00:00:00Z",
            date_epoch=self.now,
            request_id=f"request-{self.counter:08d}",
        )

    @staticmethod
    def _argument(arguments: tuple[str, ...], name: str) -> str:
        return arguments[arguments.index(name) + 1]

    def call(
        self,
        service: str,
        operation: str,
        arguments: tuple[str, ...] = (),
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], Any]:
        if service == "sts" and operation == "get-caller-identity":
            response = {
                "UserId": "session",
                "Account": module.ACCOUNT_ID,
                "Arn": self.identity_arn,
            }
        elif service == "sqs" and operation == "get-queue-url":
            name = self._argument(arguments, "--queue-name")
            response = {
                "QueueUrl": (
                    f"https://sqs.{module.REGION}.amazonaws.com/"
                    f"{module.ACCOUNT_ID}/{name}"
                )
            }
        elif service == "sqs" and operation == "get-queue-attributes":
            queue_url = self._argument(arguments, "--queue-url")
            name = queue_url.rsplit("/", 1)[1]
            attributes = {
                "QueueArn": (
                    f"arn:aws:sqs:{module.REGION}:{module.ACCOUNT_ID}:{name}"
                ),
                "ApproximateNumberOfMessages": (
                    self.visible_messages if name == module.MEDIA_JOBS_QUEUE else "0"
                ),
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
                "MessageRetentionPeriod": "1209600",
                "SqsManagedSseEnabled": "true",
            }
            if name == module.MEDIA_JOBS_QUEUE:
                attributes.update(
                    {
                        "VisibilityTimeout": "180",
                        "RedrivePolicy": json.dumps(
                            {
                                "deadLetterTargetArn": (
                                    f"arn:aws:sqs:{module.REGION}:"
                                    f"{module.ACCOUNT_ID}:{module.MEDIA_JOBS_DLQ}"
                                ),
                                "maxReceiveCount": "5",
                            },
                            separators=(",", ":"),
                        ),
                    }
                )
            response = {"Attributes": attributes}
        elif service == "ecs" and operation == "describe-services":
            deployment = {
                "status": "PRIMARY",
                "rolloutState": self.rollout_state,
                "taskDefinition": _MCP_TASK_DEFINITION,
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
                "failedTasks": 0,
            }
            response = {
                "services": [
                    {
                        "serviceName": module.MCP_SERVICE,
                        "taskDefinition": _MCP_TASK_DEFINITION,
                        "desiredCount": 1,
                        "runningCount": 1,
                        "pendingCount": 0,
                        "deployments": [copy.deepcopy(deployment)]
                        * self.deployments,
                    }
                ],
                "failures": [],
            }
        elif service == "ecs" and operation == "describe-task-definition":
            task_definition = self._argument(arguments, "--task-definition")
            if task_definition == _MCP_TASK_DEFINITION:
                response = {
                    "taskDefinition": {
                        "taskDefinitionArn": _MCP_TASK_DEFINITION,
                        "containerDefinitions": [
                            {
                                "name": "mcp",
                                "environment": [
                                    {"name": name, "value": value}
                                    for name, value in self.flags.items()
                                ],
                            }
                        ],
                    }
                }
            elif task_definition == _MEDIA_TASK:
                response = {
                    "taskDefinition": {
                        "taskDefinitionArn": _MEDIA_TASK,
                        "containerDefinitions": [
                            {
                                "name": "acquire",
                                "image": _LEGACY_IMAGE,
                                "environment": [],
                            }
                        ],
                    }
                }
            else:  # pragma: no cover - fixture assertion
                raise AssertionError(task_definition)
        elif service == "ecs" and operation == "describe-tasks":
            tasks = list(arguments[arguments.index("--tasks") + 1 :])
            response = {
                "tasks": [
                    {
                        "taskArn": task,
                        "taskDefinitionArn": _MCP_TASK_DEFINITION,
                        "group": f"service:{module.MCP_SERVICE}",
                        "desiredStatus": "RUNNING",
                        "lastStatus": "RUNNING",
                    }
                    for task in tasks
                ],
                "failures": [],
            }
        elif service == "lambda" and operation == "get-function-configuration":
            response = {
                "FunctionName": module.MEDIA_DISPATCH_FUNCTION,
                "Environment": {"Variables": {"TASKDEF_ARN": _MEDIA_TASK}},
            }
        elif service == "lambda" and operation == "update-event-source-mapping":
            self.write_operations.append(operation)
            self.mapping_state = "Disabled"
            response = {"UUID": _MAPPING_UUID, "State": self.mapping_state}
        elif service == "kms" and operation == "describe-key":
            response = {
                "KeyMetadata": {
                    "AWSAccountId": module.ACCOUNT_ID,
                    "Arn": _MEDIA_KEY_ARN,
                    "KeyUsage": "SIGN_VERIFY",
                    "KeySpec": "ECC_NIST_P256",
                    "KeyState": "Enabled",
                    "Enabled": True,
                    "KeyManager": "CUSTOMER",
                    "Origin": "AWS_KMS",
                    "MultiRegion": False,
                    "SigningAlgorithms": ["ECDSA_SHA_256"],
                }
            }
        elif service == "kms" and operation == "sign":
            self.write_operations.append(operation)
            message_uri = self._argument(arguments, "--message")
            message = Path(message_uri.removeprefix("fileb://")).read_bytes()
            signature = base64.b64encode(hashlib.sha256(message).digest()).decode()
            response = {"KeyId": _MEDIA_KEY_ARN, "Signature": signature}
        elif service == "kms" and operation == "verify":
            message_uri = self._argument(arguments, "--message")
            signature_uri = self._argument(arguments, "--signature")
            message = Path(message_uri.removeprefix("fileb://")).read_bytes()
            signature = Path(signature_uri.removeprefix("fileb://")).read_bytes()
            response = {
                "KeyId": _MEDIA_KEY_ARN,
                "SignatureValid": signature == hashlib.sha256(message).digest(),
            }
        elif service == "dynamodb" and operation == "put-item":
            self.write_operations.append(operation)
            if self.item is not None:
                raise module.ContractError("conditional put rejected")
            self.item = json.loads(self._argument(arguments, "--item"))
            response = {}
        elif service == "dynamodb" and operation == "get-item":
            response = {"Item": copy.deepcopy(self.item)} if self.item is not None else {}
        else:  # pragma: no cover - fixture assertion
            raise AssertionError((service, operation, arguments))
        return response, self._http()

    def pages(
        self,
        service: str,
        operation: str,
        arguments: tuple[str, ...],
        **_kwargs: Any,
    ) -> list[tuple[dict[str, Any], Any]]:
        if service == "lambda" and operation == "list-event-source-mappings":
            response = {
                "EventSourceMappings": [
                    {
                        "UUID": _MAPPING_UUID,
                        "EventSourceArn": (
                            f"arn:aws:sqs:{module.REGION}:{module.ACCOUNT_ID}:"
                            f"{module.MEDIA_JOBS_QUEUE}"
                        ),
                        "FunctionArn": (
                            f"arn:aws:lambda:{module.REGION}:{module.ACCOUNT_ID}:"
                            f"function:{module.MEDIA_DISPATCH_FUNCTION}"
                        ),
                        "State": self.mapping_state,
                        "BatchSize": 1,
                        "FunctionResponseTypes": [],
                    }
                ]
            }
        elif service == "ecs" and operation == "list-tasks":
            desired = self._argument(arguments, "--desired-status")
            if "--service-name" in arguments:
                values = (
                    self.service_running_tasks
                    if desired == "RUNNING"
                    else self.service_pending_tasks
                )
            else:
                values = (
                    self.legacy_running_tasks
                    if desired == "RUNNING"
                    else self.legacy_pending_tasks
                )
            response = {"taskArns": list(values)}
        else:  # pragma: no cover - fixture assertion
            raise AssertionError((service, operation, arguments))
        return [(response, self._http())]


@pytest.fixture
def lock_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(aws: FakeAws, _receipt: Any) -> dict[str, Any]:
        return {
            "workflow_id": "11111111-2222-4333-8444-555555555555",
            "lease_expires_at": 10_000,
            "verified_at_epoch": aws.now,
        }

    monkeypatch.setattr(module, "verify_runtime_workflow_lock", verify)


def _prepare(aws: FakeAws, *, sleeper: Any | None = None) -> dict[str, Any]:
    return module.prepare_media_cutover(
        aws,
        desired_image=_DESIRED_IMAGE,
        image_deployment_intent_id=_INTENT_ID,
        migration_contract_sha256=_MIGRATION_SHA256,
        reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
        lock_receipt={"workflow_id": "lock"},
        sleeper=(
            sleeper
            if sleeper is not None
            else lambda seconds: setattr(aws, "now", aws.now + int(seconds))
        ),
    )


def _attest(aws: FakeAws, challenge: dict[str, Any]) -> dict[str, Any]:
    aws.identity_arn = module.MEDIA_ATTESTOR_ARN
    receipt = module.attest_media_cutover(
        aws,
        challenge=challenge,
        desired_image=_DESIRED_IMAGE,
        image_deployment_intent_id=_INTENT_ID,
        migration_contract_sha256=_MIGRATION_SHA256,
        reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
    )
    aws.identity_arn = module.AUTOMATION_ARN
    return receipt


def _verify(aws: FakeAws, receipt: dict[str, Any]) -> dict[str, Any]:
    return module.verify_media_cutover(
        aws,
        receipt=receipt,
        desired_image=_DESIRED_IMAGE,
        image_deployment_intent_id=_INTENT_ID,
        migration_contract_sha256=_MIGRATION_SHA256,
        reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
    )


def _rehash_challenge(challenge: dict[str, Any]) -> None:
    challenge["claims_sha256"] = module.canonical_sha256(challenge["claims"])
    unhashed = dict(challenge)
    unhashed.pop("challenge_sha256", None)
    challenge["challenge_sha256"] = module.canonical_sha256(unhashed)


def test_prepare_then_independent_signer_persists_unique_ready_row(
    lock_verifier: None,
) -> None:
    aws = FakeAws()

    challenge = _prepare(aws)
    assert challenge["claims"]["image_deployment_intent_id"] == _INTENT_ID
    assert challenge["claims"]["record_id"] == f"media-cutover#{_INTENT_ID}"
    assert challenge["claims"]["settle_seconds"] == 900
    assert aws.write_operations == ["update-event-source-mapping"]

    receipt = _attest(aws, challenge)
    assert receipt["kind"] == "teamagent-media-envelope-cutover-receipt"
    assert receipt["schema_version"] == 2
    assert receipt["claims"]["attestor_principal_arn"] == module.MEDIA_ATTESTOR_ARN
    assert aws.item is not None
    assert aws.item["status"] == {"S": "READY"}
    assert aws.write_operations == [
        "update-event-source-mapping",
        "sign",
        "put-item",
    ]

    writes_before = list(aws.write_operations)
    verification = _verify(aws, receipt)
    assert verification["kind"].endswith("-verification")
    assert verification["image_deployment_intent_id"] == _INTENT_ID
    assert aws.write_operations == writes_before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("producer", "disabled"),
        ("queue", "empty"),
        ("legacy-running", "RUNNING"),
        ("legacy-pending", "PENDING"),
        ("service-pending", "steady"),
        ("deployment-count", "unique steady"),
        ("rollout", "completed and steady"),
    ],
)
def test_prepare_rejects_nonquiescent_or_nonsteady_runtime(
    lock_verifier: None,
    mutation: str,
    message: str,
) -> None:
    aws = FakeAws()
    if mutation == "producer":
        aws.flags["USE_TIKTOK_TOOLS"] = "true"
    elif mutation == "queue":
        aws.visible_messages = "1"
    elif mutation == "legacy-running":
        aws.legacy_running_tasks = ["arn:aws:ecs:task/running"]
    elif mutation == "legacy-pending":
        aws.legacy_pending_tasks = ["arn:aws:ecs:task/pending"]
    elif mutation == "service-pending":
        aws.service_pending_tasks = ["arn:aws:ecs:task/pending"]
    elif mutation == "deployment-count":
        aws.deployments = 2
    else:
        aws.rollout_state = "IN_PROGRESS"

    with pytest.raises(module.ContractError, match=message):
        _prepare(aws)
    assert "put-item" not in aws.write_operations


def test_true_settle_window_uses_first_latest_to_second_earliest(
    lock_verifier: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aws = FakeAws()
    original_capture = module.capture_media_cutover_state
    captures = 0

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal captures
        result = original_capture(*args, **kwargs)
        captures += 1
        if captures == 3:
            # second.earliest is only 899 seconds after first.latest even
            # though second.latest remains 900 seconds later.
            result["earliest_observed_at_epoch"] -= 1
        return result

    monkeypatch.setattr(module, "capture_media_cutover_state", capture)
    with pytest.raises(module.ContractError, match="900 AWS seconds"):
        _prepare(aws)


def test_local_sleep_without_aws_time_progress_is_rejected(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    with pytest.raises(module.ContractError, match="900 AWS seconds"):
        _prepare(aws, sleeper=lambda _seconds: None)


def test_signer_rejects_tampered_nonce_intent_plan_and_source_inventory(
    lock_verifier: None,
) -> None:
    challenge = _prepare(FakeAws())

    mutations = [
        lambda value: value["claims"].__setitem__(
            "image_deployment_intent_id",
            _OTHER_INTENT_ID,
        ),
        lambda value: value["claims"].__setitem__(
            "reviewed_plan_sha256",
            "f" * 64,
        ),
        lambda value: value["claims"]["first_observation"]["sources"].append(
            copy.deepcopy(value["claims"]["first_observation"]["sources"][0])
        ),
    ]
    for mutation in mutations:
        aws = FakeAws()
        aws.mapping_state = "Disabled"
        tampered = copy.deepcopy(challenge)
        mutation(tampered)
        for observation_name in ("first_observation", "second_observation"):
            observation = tampered["claims"][observation_name]
            observation["state_sha256"] = module.canonical_sha256(
                observation["state"]
            )
            observation["sources_sha256"] = module.canonical_sha256(
                observation["sources"]
            )
        _rehash_challenge(tampered)
        aws.now = tampered["prepared_at_epoch"]
        aws.identity_arn = module.MEDIA_ATTESTOR_ARN
        with pytest.raises(module.ContractError):
            module.attest_media_cutover(
                aws,
                challenge=tampered,
                desired_image=_DESIRED_IMAGE,
                image_deployment_intent_id=_INTENT_ID,
                migration_contract_sha256=_MIGRATION_SHA256,
                reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
            )


def test_same_intent_cannot_be_attested_twice(lock_verifier: None) -> None:
    aws = FakeAws()
    challenge = _prepare(aws)
    _attest(aws, challenge)
    aws.identity_arn = module.MEDIA_ATTESTOR_ARN
    with pytest.raises(module.ContractError, match="conditional put rejected"):
        module.attest_media_cutover(
            aws,
            challenge=challenge,
            desired_image=_DESIRED_IMAGE,
            image_deployment_intent_id=_INTENT_ID,
            migration_contract_sha256=_MIGRATION_SHA256,
            reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
        )


def test_consumed_row_is_verified_only_for_the_exact_attempt_and_plan(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    receipt = _attest(aws, _prepare(aws))
    assert aws.item is not None
    attempt_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    plan_sha256 = "9" * 64
    aws.item.update(
        {
            "status": {"S": "CONSUMED"},
            "apply_attempt_id": {"S": attempt_id},
            "plan_sha256": {"S": plan_sha256},
            "consumed_at_epoch": {"N": str(aws.now)},
        }
    )

    verified = module.verify_media_cutover(
        aws,
        receipt=receipt,
        desired_image=_DESIRED_IMAGE,
        image_deployment_intent_id=_INTENT_ID,
        migration_contract_sha256=_MIGRATION_SHA256,
        reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
        expected_status="CONSUMED",
        apply_attempt_id=attempt_id,
        plan_sha256=plan_sha256,
    )
    assert verified["status"] == "CONSUMED"

    with pytest.raises(module.ContractError):
        module.verify_media_cutover(
            aws,
            receipt=receipt,
            desired_image=_DESIRED_IMAGE,
            image_deployment_intent_id=_INTENT_ID,
            migration_contract_sha256=_MIGRATION_SHA256,
            reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
            expected_status="CONSUMED",
            apply_attempt_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            plan_sha256=plan_sha256,
        )


def test_verifier_rejects_live_drift_signature_tamper_and_other_intent(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    receipt = _attest(aws, _prepare(aws))

    aws.mapping_state = "Enabled"
    with pytest.raises(module.ContractError, match="not Disabled"):
        _verify(aws, receipt)
    aws.mapping_state = "Disabled"

    tampered = copy.deepcopy(receipt)
    tampered["signature_base64"] = base64.b64encode(b"other").decode()
    unhashed = dict(tampered)
    unhashed.pop("receipt_sha256")
    tampered["receipt_sha256"] = module.canonical_sha256(unhashed)
    with pytest.raises(module.ContractError, match="signature hash differs"):
        _verify(aws, tampered)

    with pytest.raises(module.ContractError):
        module.verify_media_cutover(
            aws,
            receipt=receipt,
            desired_image=_DESIRED_IMAGE,
            image_deployment_intent_id=_OTHER_INTENT_ID,
            migration_contract_sha256=_MIGRATION_SHA256,
            reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
        )


def test_verifier_rejects_rehashed_ledger_claims_without_valid_signature(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    receipt = _attest(aws, _prepare(aws))
    assert aws.item is not None
    claims = json.loads(aws.item["claims_json"]["S"])
    claims["reviewed_plan_sha256"] = "f" * 64
    aws.item["claims_json"] = {
        "S": module.canonical_bytes(claims).decode().rstrip("\n")
    }
    aws.item["claims_sha256"] = {"S": module.canonical_sha256(claims)}

    with pytest.raises(module.ContractError):
        _verify(aws, receipt)


def test_nonce_tamper_is_rejected_by_the_managed_signature(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    receipt = _attest(aws, _prepare(aws))
    assert aws.item is not None
    claims = copy.deepcopy(receipt["claims"])
    claims["attestation_nonce"] = "f" * 64
    prepared = {
        key: claims[key] for key in module._media_prepared_claim_keys()
    }
    claims["prepared_claims_sha256"] = module.canonical_sha256(prepared)
    recorded_at = int(aws.item["recorded_at_epoch"]["N"])
    aws.item = module._media_ledger_item(
        claims,
        recorded_at_epoch=recorded_at,
        kms_key_arn=receipt["kms_key_arn"],
        signature_base64=receipt["signature_base64"],
    )
    tampered = copy.deepcopy(receipt)
    tampered["claims"] = claims
    tampered["claims_sha256"] = module.canonical_sha256(claims)
    tampered["ledger"]["item_sha256"] = module.canonical_sha256(aws.item)
    unhashed = dict(tampered)
    unhashed.pop("receipt_sha256")
    tampered["receipt_sha256"] = module.canonical_sha256(unhashed)

    with pytest.raises(module.ContractError, match="signature is invalid"):
        _verify(aws, tampered)


def test_all_task_lists_use_lowercase_next_token(
    lock_verifier: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aws = FakeAws()
    original = aws.pages
    observed: list[tuple[bool, str]] = []

    def pages(
        service: str,
        operation: str,
        arguments: tuple[str, ...],
        **kwargs: Any,
    ) -> list[tuple[dict[str, Any], Any]]:
        if service == "ecs" and operation == "list-tasks":
            assert kwargs.get("token_field") == "nextToken"
            observed.append(
                (
                    "--service-name" in arguments,
                    arguments[arguments.index("--desired-status") + 1],
                )
            )
        return original(service, operation, arguments, **kwargs)

    monkeypatch.setattr(aws, "pages", pages)
    _prepare(aws)
    assert set(observed) == {
        (True, "RUNNING"),
        (True, "PENDING"),
        (False, "RUNNING"),
        (False, "PENDING"),
    }


def test_challenge_binds_the_pinned_aws_executable(lock_verifier: None) -> None:
    aws = FakeAws()
    challenge = _prepare(aws)
    assert challenge["aws_executable"] == asdict(aws.evidence)
    aws.identity_arn = module.MEDIA_ATTESTOR_ARN
    aws.evidence = module.ExecutableEvidence(
        **{**asdict(aws.evidence), "sha256": "0" * 64}
    )
    with pytest.raises(module.ContractError):
        module.attest_media_cutover(
            aws,
            challenge=challenge,
            desired_image=_DESIRED_IMAGE,
            image_deployment_intent_id=_INTENT_ID,
            migration_contract_sha256=_MIGRATION_SHA256,
            reviewed_plan_sha256=_REVIEWED_PLAN_SHA256,
        )
