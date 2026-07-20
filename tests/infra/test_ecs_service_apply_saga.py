from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "ecs_service_apply_saga.py"
ATTEMPT = "12345678-1234-4123-8123-123456789abc"
PLAN_SHA256 = "a" * 64
CLUSTER_ARN = "arn:aws:ecs:ap-northeast-1:718959508629:cluster/teamagent-dev"
MCP_SERVICE_ARN = "arn:aws:ecs:ap-northeast-1:718959508629:service/teamagent-dev/teamagent-dev-mcp"
CONNECT_SERVICE_ARN = (
    "arn:aws:ecs:ap-northeast-1:718959508629:service/teamagent-dev/teamagent-dev-connect-web"
)
OLD_MCP_TASK = "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:50"
NEW_MCP_TASK = "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:51"
OLD_CONNECT_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-connect-web:60"
)
NEW_CONNECT_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-connect-web:61"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ecs_service_apply_saga_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAGA = _load_module()


def _resource(
    *,
    address: str,
    resource_type: str,
    name: str,
    after: dict[str, Any],
    actions: list[str],
    index: int | None = None,
    after_unknown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "change": {
            "actions": actions,
            "before": copy.deepcopy(after),
            "after": copy.deepcopy(after),
            "after_unknown": copy.deepcopy(after_unknown or {}),
        },
    }
    if index is not None:
        value["index"] = index
    return value


def _task_after(family: str, container_name: str) -> dict[str, Any]:
    return {
        "container_definitions": json.dumps(
            [
                {
                    "essential": True,
                    "image": (
                        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                        f"{family}@sha256:{'c' * 64}"
                    ),
                    "name": container_name,
                }
            ],
            separators=(",", ":"),
            sort_keys=True,
        ),
        "cpu": "1024",
        "execution_role_arn": (f"arn:aws:iam::718959508629:role/{family}-execution"),
        "family": family,
        "memory": "2048",
        "network_mode": "awsvpc",
        "requires_compatibilities": ["FARGATE"],
        "runtime_platform": [
            {
                "cpu_architecture": "ARM64",
                "operating_system_family": "LINUX",
            }
        ],
        "tags_all": {},
        "task_role_arn": f"arn:aws:iam::718959508629:role/{family}-task",
        "volume": [],
    }


def _plan(
    *,
    mcp_task: str | None = NEW_MCP_TASK,
    connect_task: str | None = NEW_CONNECT_TASK,
) -> dict[str, Any]:
    mcp_unknown = {"task_definition": True} if mcp_task is None else {}
    connect_unknown = {"task_definition": True} if connect_task is None else {}
    return {
        "format_version": "1.2",
        "terraform_version": "1.12.2",
        "complete": True,
        "errored": False,
        "resource_changes": [
            _resource(
                address="aws_ecs_service.mcp[0]",
                resource_type="aws_ecs_service",
                name="mcp",
                index=0,
                actions=["update"],
                after={
                    "cluster": CLUSTER_ARN,
                    "name": "teamagent-dev-mcp",
                    "task_definition": mcp_task,
                },
                after_unknown=mcp_unknown,
            ),
            _resource(
                address="aws_ecs_task_definition.mcp",
                resource_type="aws_ecs_task_definition",
                name="mcp",
                actions=["create", "delete"],
                after=_task_after("teamagent-dev-mcp", "teamagent-mcp"),
            ),
            _resource(
                address="aws_ecs_service.connect_web[0]",
                resource_type="aws_ecs_service",
                name="connect_web",
                index=0,
                actions=["update"],
                after={
                    "cluster": CLUSTER_ARN,
                    "name": "teamagent-dev-connect-web",
                    "task_definition": connect_task,
                },
                after_unknown=connect_unknown,
            ),
            _resource(
                address="aws_ecs_task_definition.connect_web[0]",
                resource_type="aws_ecs_task_definition",
                name="connect_web",
                index=0,
                actions=["create", "delete"],
                after=_task_after("teamagent-dev-connect-web", "connect-web"),
            ),
            # Full production plans include other ECS resources. Unchanged ones
            # are not within this saga's mutation scope and are intentionally safe.
            _resource(
                address="aws_ecs_service.openclaw[0]",
                resource_type="aws_ecs_service",
                name="openclaw",
                index=0,
                actions=["no-op"],
                after={"name": "teamagent-dev-openclaw"},
            ),
            {
                "address": "aws_s3_bucket.unrelated",
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "unrelated",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            },
        ],
    }


def _deployment_configuration(
    *,
    maximum: int = 200,
    minimum: int = 100,
    enable: bool = True,
    rollback: bool = True,
) -> dict[str, Any]:
    return {
        "deploymentCircuitBreaker": {
            "enable": enable,
            "rollback": rollback,
        },
        "maximumPercent": maximum,
        "minimumHealthyPercent": minimum,
    }


def _network(
    *,
    subnet: str,
    security_group: str,
    public_ip: str = "ENABLED",
) -> dict[str, Any]:
    return {
        "awsvpcConfiguration": {
            "assignPublicIp": public_ip,
            "securityGroups": [security_group],
            "subnets": [subnet],
        }
    }


def _service(
    *,
    service_arn: str,
    service_name: str,
    task_definition: str,
    network: dict[str, Any],
    deployment: dict[str, Any] | None = None,
    desired_count: int = 1,
) -> dict[str, Any]:
    deployment_configuration = deployment or _deployment_configuration()
    return {
        "serviceArn": service_arn,
        "serviceName": service_name,
        "clusterArn": CLUSTER_ARN,
        "status": "ACTIVE",
        "launchType": "FARGATE",
        "schedulingStrategy": "REPLICA",
        "deploymentController": {"type": "ECS"},
        "taskDefinition": task_definition,
        "deploymentConfiguration": copy.deepcopy(deployment_configuration),
        "networkConfiguration": copy.deepcopy(network),
        "desiredCount": desired_count,
        "runningCount": desired_count,
        "pendingCount": 0,
        "deployments": [
            {
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "desiredCount": desired_count,
                "runningCount": desired_count,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
            }
        ],
    }


def _argument(arguments: Sequence[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


class _FakeCli:
    def __init__(self) -> None:
        self.services = {
            MCP_SERVICE_ARN: _service(
                service_arn=MCP_SERVICE_ARN,
                service_name="teamagent-dev-mcp",
                task_definition=OLD_MCP_TASK,
                network=_network(subnet="subnet-b", security_group="sg-mcp"),
            ),
            CONNECT_SERVICE_ARN: _service(
                service_arn=CONNECT_SERVICE_ARN,
                service_name="teamagent-dev-connect-web",
                task_definition=OLD_CONNECT_TASK,
                network=_network(
                    subnet="subnet-a",
                    security_group="sg-connect",
                ),
            ),
        }
        self.list_pages: dict[str, dict[str, Any]] = {
            "": {
                "serviceArns": [
                    "arn:aws:ecs:ap-northeast-1:718959508629:"
                    "service/teamagent-dev/teamagent-dev-openclaw",
                    MCP_SERVICE_ARN,
                ],
                "nextToken": "page-2",
            },
            "page-2": {"serviceArns": [CONNECT_SERVICE_ARN]},
        }
        self.item: dict[str, Any] | None = None
        self.task_definitions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.wait_count = 0
        self.fail_wait_once = False

    def json(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.calls.append((service, operation, tuple(arguments)))
        if (service, operation) == ("ecs", "list-services"):
            token = _argument(arguments, "--next-token") if "--next-token" in arguments else ""
            return copy.deepcopy(self.list_pages[token])
        if (service, operation) == ("ecs", "describe-services"):
            requested = list(arguments[arguments.index("--services") + 1 :])
            return {
                "services": [
                    copy.deepcopy(self.services[service_arn])
                    for service_arn in requested
                    if service_arn in self.services
                ],
                "failures": [],
            }
        if (service, operation) == ("ecs", "update-service"):
            service_arn = _argument(arguments, "--service")
            current = self.services[service_arn]
            task_definition = _argument(arguments, "--task-definition")
            desired_count = int(_argument(arguments, "--desired-count"))
            current["taskDefinition"] = task_definition
            current["desiredCount"] = desired_count
            current["runningCount"] = desired_count
            current["pendingCount"] = 0
            current["deploymentConfiguration"] = json.loads(
                _argument(arguments, "--deployment-configuration")
            )
            current["networkConfiguration"] = json.loads(
                _argument(arguments, "--network-configuration")
            )
            current["deployments"] = [
                {
                    "status": "PRIMARY",
                    "taskDefinition": task_definition,
                    "desiredCount": desired_count,
                    "runningCount": desired_count,
                    "pendingCount": 0,
                    "rolloutState": "COMPLETED",
                }
            ]
            return {"service": copy.deepcopy(current)}
        if (service, operation) == ("ecs", "describe-task-definition"):
            task_definition = _argument(arguments, "--task-definition")
            return copy.deepcopy(self.task_definitions[task_definition])
        if (service, operation) == ("dynamodb", "get-item"):
            return {"Item": copy.deepcopy(self.item)} if self.item is not None else {}
        raise AssertionError((service, operation, arguments))

    def run(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> None:
        self.calls.append((service, operation, tuple(arguments)))
        if (service, operation) == ("dynamodb", "put-item"):
            assert timeout_seconds == 120
            assert _argument(arguments, "--condition-expression") == (
                "attribute_not_exists(record_id)"
            )
            if self.item is not None:
                raise SAGA.SagaError("ConditionalCheckFailedException")
            self.item = json.loads(_argument(arguments, "--item"))
            return
        if (service, operation) == ("dynamodb", "update-item"):
            assert self.item is not None
            values = json.loads(_argument(arguments, "--expression-attribute-values"))
            if self.item["stage"] != values[":applying"]:
                raise SAGA.SagaError("ConditionalCheckFailedException")
            for item_name, value_name in (
                ("plan_sha256", ":plan"),
                ("apply_attempt_id", ":attempt"),
                ("baseline_sha256", ":baseline"),
                ("planned_sha256", ":planned"),
            ):
                if self.item[item_name] != values[value_name]:
                    raise SAGA.SagaError("ConditionalCheckFailedException")
            self.item["stage"] = copy.deepcopy(values[":desired"])
            return
        if (service, operation) == ("ecs", "wait services-stable"):
            assert timeout_seconds == 900
            self.wait_count += 1
            if self.fail_wait_once:
                self.fail_wait_once = False
                raise SAGA.SagaError("waiter failed")
            return
        raise AssertionError((service, operation, arguments))


def _saga(
    cli: _FakeCli,
    *,
    plan: dict[str, Any] | None = None,
    attempt: str = ATTEMPT,
) -> Any:
    return SAGA.EcsServiceApplySaga(
        plan=SAGA._analyze_plan(plan or _plan()),
        plan_sha256=PLAN_SHA256,
        apply_attempt_id=attempt,
        cli=cli,
    )


def _set_live_task(
    cli: _FakeCli,
    *,
    service_arn: str,
    task_definition: str,
) -> None:
    service = cli.services[service_arn]
    service["taskDefinition"] = task_definition
    service["deployments"][0]["taskDefinition"] = task_definition


@pytest.mark.parametrize(
    ("address", "resource_type", "name", "index"),
    [
        ("aws_ecs_service.openclaw[0]", "aws_ecs_service", "openclaw", 0),
        (
            "aws_ecs_task_definition.morning_digest[0]",
            "aws_ecs_task_definition",
            "morning_digest",
            0,
        ),
        (
            'module.hostile.aws_ecs_service.mcp["shadow"]',
            "aws_ecs_service",
            "mcp",
            "shadow",
        ),
    ],
)
def test_plan_rejects_every_mutating_ecs_address_outside_exact_scope(
    address: str,
    resource_type: str,
    name: str,
    index: int | str,
) -> None:
    plan = _plan()
    hostile = _resource(
        address=address,
        resource_type=resource_type,
        name=name,
        index=index if isinstance(index, int) else None,
        actions=["update"],
        after={"name": "hostile"},
    )
    if isinstance(index, str):
        hostile["index"] = index
    plan["resource_changes"].append(hostile)

    with pytest.raises(SAGA.SagaError, match="outside the saga scope"):
        SAGA._analyze_plan(plan)


def test_plan_rejects_address_alias_even_when_metadata_claims_allowed_identity() -> None:
    plan = _plan()
    plan["resource_changes"][0]["address"] = "aws_ecs_service.mcp"

    with pytest.raises(SAGA.SagaError, match="outside the saga scope"):
        SAGA._analyze_plan(plan)


def test_begin_reads_manual_pagination_and_persists_exact_canonical_baseline() -> None:
    cli = _FakeCli()
    saga = _saga(cli)

    saga.begin()

    assert cli.item is not None
    assert cli.item["record_id"] == {"S": f"ecs-service-apply#{ATTEMPT}"}
    assert cli.item["plan_sha256"] == {"S": PLAN_SHA256}
    assert cli.item["apply_attempt_id"] == {"S": ATTEMPT}
    assert cli.item["stage"] == {"S": "APPLYING"}
    baseline = json.loads(cli.item["baseline_json"]["S"])
    assert baseline["mcp"] == {
        "taskDefinition": OLD_MCP_TASK,
        "deploymentConfiguration": _deployment_configuration(),
        "networkConfiguration": _network(
            subnet="subnet-b",
            security_group="sg-mcp",
        ),
        "desiredCount": 1,
    }
    list_calls = [
        arguments
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "list-services")
    ]
    assert len(list_calls) == 2
    assert "--next-token" not in list_calls[0]
    assert list_calls[1][-2:] == ("--next-token", "page-2")


def test_begin_rejects_replay_without_recapturing_or_overwriting_baseline() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    assert cli.item is not None
    original_item = copy.deepcopy(cli.item)
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )

    with pytest.raises(SAGA.SagaError, match="already exists"):
        saga.begin()

    assert cli.item == original_item


@pytest.mark.parametrize("scenario", ["repeat-token", "duplicate-arn", "missing-service"])
def test_begin_rejects_truncated_or_ambiguous_service_inventory(
    scenario: str,
) -> None:
    cli = _FakeCli()
    if scenario == "repeat-token":
        cli.list_pages = {
            "": {"serviceArns": [MCP_SERVICE_ARN], "nextToken": "repeat"},
            "repeat": {
                "serviceArns": [CONNECT_SERVICE_ARN],
                "nextToken": "repeat",
            },
        }
    elif scenario == "duplicate-arn":
        cli.list_pages = {
            "": {"serviceArns": [MCP_SERVICE_ARN], "nextToken": "second"},
            "second": {
                "serviceArns": [MCP_SERVICE_ARN, CONNECT_SERVICE_ARN],
            },
        }
    else:
        cli.list_pages = {"": {"serviceArns": [MCP_SERVICE_ARN]}}

    with pytest.raises(SAGA.SagaError):
        _saga(cli).begin()

    assert cli.item is None


def test_begin_rejects_describe_response_identity_substitution() -> None:
    cli = _FakeCli()
    cli.services[MCP_SERVICE_ARN]["serviceName"] = "teamagent-dev-openclaw"

    with pytest.raises(SAGA.SagaError, match="identity"):
        _saga(cli).begin()

    assert cli.item is None


def test_applied_requires_both_exact_planned_task_definitions_and_stability() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )

    with pytest.raises(SAGA.SagaError, match="planned task definition"):
        saga.finish(outcome="applied")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLYING"}

    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )
    cli.services[CONNECT_SERVICE_ARN]["pendingCount"] = 1
    with pytest.raises(SAGA.SagaError, match="not exactly stable"):
        saga.finish(outcome="applied")
    cli.services[CONNECT_SERVICE_ARN]["pendingCount"] = 0

    saga.finish(outcome="applied")

    assert cli.item["stage"] == {"S": "APPLIED"}
    saga.finish(outcome="applied")


def test_applied_resolves_unknown_planned_arns_by_exact_task_artifact() -> None:
    cli = _FakeCli()
    plan = _plan(mcp_task=None, connect_task=None)
    saga = _saga(cli, plan=plan)
    saga.begin()
    for key, service_arn, task_definition in (
        ("mcp", MCP_SERVICE_ARN, NEW_MCP_TASK),
        ("connect_web", CONNECT_SERVICE_ARN, NEW_CONNECT_TASK),
    ):
        _set_live_task(
            cli,
            service_arn=service_arn,
            task_definition=task_definition,
        )
        task_address = SAGA._SERVICE_SPECS[key].task_address
        task_change = next(
            change for change in plan["resource_changes"] if change["address"] == task_address
        )
        payload = SAGA.task_from_change(task_change["change"]["after"], task=key)
        cli.task_definitions[task_definition] = {
            "taskDefinition": {
                **copy.deepcopy(payload),
                "taskDefinitionArn": task_definition,
                "revision": int(task_definition.rsplit(":", maxsplit=1)[1]),
                "status": "ACTIVE",
            }
        }

    saga.finish(outcome="applied")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLIED"}


def test_failed_restores_every_bound_field_waits_and_verifies_exactly() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    for service_arn, task_definition in (
        (MCP_SERVICE_ARN, NEW_MCP_TASK),
        (CONNECT_SERVICE_ARN, NEW_CONNECT_TASK),
    ):
        service = cli.services[service_arn]
        service["taskDefinition"] = task_definition
        service["desiredCount"] = 3
        service["runningCount"] = 2
        service["pendingCount"] = 1
        service["deploymentConfiguration"] = _deployment_configuration(
            maximum=50,
            minimum=0,
            enable=False,
            rollback=False,
        )
        service["networkConfiguration"] = _network(
            subnet="subnet-hostile",
            security_group="sg-hostile",
            public_ip="DISABLED",
        )
        service["deployments"] = [
            {
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "desiredCount": 3,
                "runningCount": 2,
                "pendingCount": 1,
                "rolloutState": "IN_PROGRESS",
            }
        ]

    saga.finish(outcome="failed")

    assert cli.wait_count == 1
    assert cli.item is not None
    assert cli.item["stage"] == {"S": "RESTORED"}
    assert cli.services[MCP_SERVICE_ARN]["taskDefinition"] == OLD_MCP_TASK
    assert cli.services[MCP_SERVICE_ARN]["deploymentConfiguration"] == (_deployment_configuration())
    assert cli.services[MCP_SERVICE_ARN]["networkConfiguration"] == _network(
        subnet="subnet-b",
        security_group="sg-mcp",
    )
    assert cli.services[MCP_SERVICE_ARN]["desiredCount"] == 1
    update_calls = [
        arguments
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "update-service")
    ]
    assert len(update_calls) == 2
    assert all("--task-definition" in arguments for arguments in update_calls)
    assert all("--deployment-configuration" in arguments for arguments in update_calls)
    assert all("--network-configuration" in arguments for arguments in update_calls)
    assert all("--desired-count" in arguments for arguments in update_calls)


def test_failed_partial_restore_remains_reconcilable_until_wait_and_verify() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )
    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )
    cli.fail_wait_once = True

    with pytest.raises(SAGA.SagaError, match="waiter"):
        saga.finish(outcome="failed")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLYING"}

    saga.finish(outcome="failed")

    assert cli.wait_count == 2
    assert cli.item["stage"] == {"S": "RESTORED"}


@pytest.mark.parametrize("field", ["baseline_sha256", "planned_sha256", "plan_sha256"])
def test_finish_rejects_tampered_durable_binding_before_any_ecs_write(
    field: str,
) -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    assert cli.item is not None
    cli.item[field] = {"S": "f" * 64}
    calls_before = len(cli.calls)

    with pytest.raises(SAGA.SagaError, match=r"differs|digest"):
        saga.finish(outcome="failed")

    new_calls = cli.calls[calls_before:]
    assert all(
        (service, operation) != ("ecs", "update-service")
        for service, operation, _arguments in new_calls
    )


def test_private_saved_plan_is_held_and_remeasured_by_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "saved.tfplan"
    payload = b"opaque exact plan"
    path.write_bytes(payload)
    path.chmod(0o600)
    observed_descriptors: list[int] = []

    def show(descriptor: int) -> dict[str, Any]:
        observed_descriptors.append(descriptor)
        assert os.read(descriptor, len(payload)) == payload
        os.lseek(descriptor, 0, os.SEEK_SET)
        return _plan()

    monkeypatch.setattr(SAGA, "_terraform_show_descriptor", show)

    assert (
        SAGA._load_saved_plan(
            path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        == _plan()
    )
    assert len(observed_descriptors) == 1

    path.chmod(0o644)
    with pytest.raises(SAGA.SagaError, match="private"):
        SAGA._load_saved_plan(
            path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_aws_cli_pins_endpoint_scrubs_ambient_authority_and_never_uses_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_ECS", "http://attacker.invalid")
    monkeypatch.setenv("AWS_PROFILE", "attacker")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/attacker.pem")
    observed: dict[str, Any] = {}

    def run(
        command: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(SAGA.subprocess, "run", run)

    assert SAGA._SubprocessAwsCli().json("ecs", "list-services") == {}

    command = observed["command"]
    kwargs = observed["kwargs"]
    assert command[:7] == [
        "aws",
        "--region",
        "ap-northeast-1",
        "--endpoint-url",
        "https://ecs.ap-northeast-1.amazonaws.com",
        "--no-cli-pager",
        "--no-paginate",
    ]
    assert command[7:9] == ["ecs", "list-services"]
    assert "shell" not in kwargs
    environment = kwargs["env"]
    assert environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert environment["AWS_CONFIG_FILE"] == "/dev/null"
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert "AWS_ENDPOINT_URL_ECS" not in environment
    assert "AWS_PROFILE" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "REQUESTS_CA_BUNDLE" not in environment


def test_saved_plan_file_mode_check_uses_owner_read_bit() -> None:
    assert stat.S_IRUSR == 0o400
