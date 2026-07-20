from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).parents[2]
    / "infra"
    / "deploy"
    / "runtime_evidence_guard.py"
)
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
_MCP_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:"
    "task-definition/teamagent-dev-mcp:55"
)
_MEDIA_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:"
    "task-definition/teamagent-dev-tiktok-acquire:6"
)


class FakeAws:
    def __init__(self) -> None:
        self.now = 1_000
        self.mapping_state = "Enabled"
        self.flags = {
            "USE_VIDEO_TOOLS": "false",
            "USE_TIKTOK_TOOLS": "false",
        }
        self.visible_messages = "0"
        self.running_tasks: list[str] = []
        self.pending_tasks: list[str] = []
        self.item: dict[str, Any] | None = None
        self.write_operations: list[str] = []
        self.counter = 0

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
                "UserId": "automation",
                "Account": module.ACCOUNT_ID,
                "Arn": module.AUTOMATION_ARN,
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
                    self.visible_messages
                    if name == module.MEDIA_JOBS_QUEUE
                    else "0"
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
                                    f"{module.ACCOUNT_ID}:"
                                    f"{module.MEDIA_JOBS_DLQ}"
                                ),
                                "maxReceiveCount": "5",
                            },
                            separators=(",", ":"),
                        ),
                    }
                )
            response = {"Attributes": attributes}
        elif service == "ecs" and operation == "describe-services":
            response = {
                "services": [
                    {
                        "serviceName": module.MCP_SERVICE,
                        "taskDefinition": _MCP_TASK,
                    }
                ],
                "failures": [],
            }
        elif service == "ecs" and operation == "describe-task-definition":
            task_definition = self._argument(arguments, "--task-definition")
            if task_definition == _MCP_TASK:
                response = {
                    "taskDefinition": {
                        "taskDefinitionArn": _MCP_TASK,
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
            else:
                raise AssertionError(task_definition)
        elif service == "lambda" and operation == "get-function-configuration":
            response = {
                "FunctionName": module.MEDIA_DISPATCH_FUNCTION,
                "Environment": {"Variables": {"TASKDEF_ARN": _MEDIA_TASK}},
            }
        elif service == "lambda" and operation == "update-event-source-mapping":
            self.write_operations.append(operation)
            self.mapping_state = "Disabled"
            response = {
                "UUID": _MAPPING_UUID,
                "State": self.mapping_state,
            }
        elif service == "dynamodb" and operation == "put-item":
            self.write_operations.append(operation)
            self.item = json.loads(self._argument(arguments, "--item"))
            response = {}
        elif service == "dynamodb" and operation == "get-item":
            response = {"Item": self.item} if self.item is not None else {}
        else:
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
                            f"arn:aws:sqs:{module.REGION}:"
                            f"{module.ACCOUNT_ID}:"
                            f"{module.MEDIA_JOBS_QUEUE}"
                        ),
                        "FunctionArn": (
                            f"arn:aws:lambda:{module.REGION}:"
                            f"{module.ACCOUNT_ID}:function:"
                            f"{module.MEDIA_DISPATCH_FUNCTION}"
                        ),
                        "State": self.mapping_state,
                        "BatchSize": 1,
                        "FunctionResponseTypes": [],
                    }
                ]
            }
        elif service == "ecs" and operation == "list-tasks":
            desired = self._argument(arguments, "--desired-status")
            response = {
                "taskArns": (
                    self.running_tasks
                    if desired == "RUNNING"
                    else self.pending_tasks
                )
            }
        else:
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


def _attest(aws: FakeAws) -> dict[str, Any]:
    return module.attest_media_cutover(
        aws,
        desired_image=_DESIRED_IMAGE,
        lock_receipt={"workflow_id": "lock"},
        sleeper=lambda seconds: setattr(aws, "now", aws.now + int(seconds)),
    )


def _replace_ledger_claims(
    aws: FakeAws,
    mutation: Any,
) -> None:
    assert aws.item is not None
    claims = json.loads(aws.item["claims_json"]["S"])
    mutation(claims)
    recorded_at = int(aws.item["recorded_at_epoch"]["N"])
    aws.item = module._media_ledger_item(
        claims,
        recorded_at_epoch=recorded_at,
    )


def test_attestation_disables_mapping_waits_900_aws_seconds_and_persists_ready(
    lock_verifier: None,
) -> None:
    aws = FakeAws()

    receipt = _attest(aws)

    assert receipt["kind"] == "teamagent-media-envelope-cutover-receipt"
    assert receipt["claims"]["settle_seconds"] == 900
    assert (
        receipt["claims"]["second_observation"]["observed_at_epoch"]
        - receipt["claims"]["first_observation"]["observed_at_epoch"]
        == 900
    )
    assert aws.write_operations == [
        "update-event-source-mapping",
        "put-item",
    ]
    assert aws.item is not None
    assert aws.item["status"] == {"S": "READY"}

    writes_before = list(aws.write_operations)
    verification = module.verify_media_cutover(
        aws,
        desired_image=_DESIRED_IMAGE,
    )
    assert verification["kind"].endswith("-verification")
    assert aws.write_operations == writes_before


@pytest.mark.parametrize(
    "mutation",
    [
        "producer",
        "queue",
        "running",
        "pending",
        "queue_contract",
    ],
)
def test_attestation_rejects_any_nonquiescent_or_nonexact_state(
    monkeypatch: pytest.MonkeyPatch,
    lock_verifier: None,
    mutation: str,
) -> None:
    aws = FakeAws()
    if mutation == "producer":
        aws.flags["USE_TIKTOK_TOOLS"] = "true"
    elif mutation == "queue":
        aws.visible_messages = "1"
    elif mutation == "running":
        aws.running_tasks = ["arn:aws:ecs:task/running"]
    elif mutation == "pending":
        aws.pending_tasks = ["arn:aws:ecs:task/pending"]
    else:
        original = aws.call

        def wrong_queue(
            service: str,
            operation: str,
            arguments: tuple[str, ...] = (),
            **kwargs: Any,
        ) -> tuple[dict[str, Any], Any]:
            response, http = original(service, operation, arguments, **kwargs)
            if (
                service == "sqs"
                and operation == "get-queue-attributes"
                and "RedrivePolicy"
                in response.get("Attributes", {})
            ):
                response["Attributes"]["VisibilityTimeout"] = "900"
            return response, http

        monkeypatch.setattr(aws, "call", wrong_queue)

    with pytest.raises(module.ContractError):
        _attest(aws)

    assert "put-item" not in aws.write_operations


def test_attestation_rejects_local_wait_without_900_aws_seconds(
    lock_verifier: None,
) -> None:
    aws = FakeAws()

    with pytest.raises(module.ContractError, match="900 AWS seconds"):
        module.attest_media_cutover(
            aws,
            desired_image=_DESIRED_IMAGE,
            lock_receipt={"workflow_id": "lock"},
            sleeper=lambda _seconds: None,
        )

    assert "put-item" not in aws.write_operations


def test_attestation_waits_for_mapping_to_finish_disabling(
    monkeypatch: pytest.MonkeyPatch,
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    original_call = aws.call
    original_pages = aws.pages
    disabling_polls = 0

    def delayed_call(
        service: str,
        operation: str,
        arguments: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> tuple[dict[str, Any], Any]:
        if service == "lambda" and operation == "update-event-source-mapping":
            aws.write_operations.append(operation)
            aws.mapping_state = "Disabling"
            return (
                {"UUID": _MAPPING_UUID, "State": "Disabling"},
                aws._http(),
            )
        return original_call(service, operation, arguments, **kwargs)

    def delayed_pages(
        service: str,
        operation: str,
        arguments: tuple[str, ...],
        **kwargs: Any,
    ) -> list[tuple[dict[str, Any], Any]]:
        nonlocal disabling_polls
        if (
            service == "lambda"
            and operation == "list-event-source-mappings"
            and aws.mapping_state == "Disabling"
        ):
            disabling_polls += 1
            if disabling_polls == 3:
                aws.mapping_state = "Disabled"
        return original_pages(service, operation, arguments, **kwargs)

    monkeypatch.setattr(aws, "call", delayed_call)
    monkeypatch.setattr(aws, "pages", delayed_pages)

    receipt = _attest(aws)

    action = receipt["claims"]["disable_action"]
    assert [entry["state"] for entry in action["observations"]] == [
        "Disabling",
        "Disabling",
        "Disabled",
    ]
    assert action["settled_at_epoch"] - action["aws_date_epoch"] == 10
    assert (
        receipt["claims"]["second_observation"]["observed_at_epoch"]
        - receipt["claims"]["first_observation"]["observed_at_epoch"]
        == 900
    )


def test_read_only_verification_rejects_live_drift_and_ledger_tamper(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    _attest(aws)

    aws.mapping_state = "Enabled"
    with pytest.raises(module.ContractError, match="not Disabled"):
        module.verify_media_cutover(aws, desired_image=_DESIRED_IMAGE)

    aws.mapping_state = "Disabled"
    assert aws.item is not None
    aws.item["claims_sha256"] = {"S": "f" * 64}
    with pytest.raises(module.ContractError, match="claims differ"):
        module.verify_media_cutover(aws, desired_image=_DESIRED_IMAGE)


def test_read_only_verification_rejects_noncanonical_numeric_ledger_value(
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    _attest(aws)
    assert aws.item is not None
    aws.item["recorded_at_epoch"] = {"N": "1e3"}

    with pytest.raises(module.ContractError, match="not canonical"):
        module.verify_media_cutover(aws, desired_image=_DESIRED_IMAGE)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda claims: claims.__setitem__("unexpected", True),
        lambda claims: claims["shared_lock"].__setitem__(
            "final_verified_at_epoch",
            claims["second_observation"]["observed_at_epoch"] - 1,
        ),
        lambda claims: claims["first_observation"]["sources"][0].__setitem__(
            "operation",
            "unapproved-operation",
        ),
        lambda claims: claims["second_observation"]["state"].__setitem__(
            "tasks",
            {"running": ["arn:aws:ecs:task/active"], "pending": []},
        ),
    ],
)
def test_read_only_verification_rejects_rehashed_adversarial_claims(
    lock_verifier: None,
    mutation: Any,
) -> None:
    aws = FakeAws()
    _attest(aws)

    def mutate_and_rehash(claims: dict[str, Any]) -> None:
        mutation(claims)
        for observation_name in ("first_observation", "second_observation"):
            observation = claims[observation_name]
            observation["state_sha256"] = module.canonical_sha256(
                observation["state"]
            )
            observation["sources_sha256"] = module.canonical_sha256(
                observation["sources"]
            )

    _replace_ledger_claims(aws, mutate_and_rehash)

    with pytest.raises(module.ContractError):
        module.verify_media_cutover(aws, desired_image=_DESIRED_IMAGE)


def test_ecs_task_inventory_uses_the_real_lowercase_pagination_token(
    monkeypatch: pytest.MonkeyPatch,
    lock_verifier: None,
) -> None:
    aws = FakeAws()
    original = aws.pages
    observed_statuses: list[str] = []

    def pages(
        service: str,
        operation: str,
        arguments: tuple[str, ...],
        **kwargs: Any,
    ) -> list[tuple[dict[str, Any], Any]]:
        if service == "ecs" and operation == "list-tasks":
            assert kwargs.get("token_field") == "nextToken"
            observed_statuses.append(
                arguments[arguments.index("--desired-status") + 1]
            )
        return original(service, operation, arguments, **kwargs)

    monkeypatch.setattr(aws, "pages", pages)
    _attest(aws)

    assert set(observed_statuses) == {"RUNNING", "PENDING"}
