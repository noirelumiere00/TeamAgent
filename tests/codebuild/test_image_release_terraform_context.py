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


def _task_arn(consumer: dict[str, Any], revision: int = 1) -> str:
    return (
        "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
        f"{consumer['ecs_family']}:{revision}"
    )


def _image(consumer: dict[str, Any], index: int) -> str:
    return (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"{consumer['release_repository']}@sha256:{format(index + 1, 'x') * 64}"
    )


def _task_attributes(consumer: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "arn": _task_arn(consumer),
        "id": _task_arn(consumer),
        "family": consumer["ecs_family"],
        "container_definitions": json.dumps(
            [
                {
                    "name": consumer["container_name"],
                    "image": _image(consumer, index),
                }
            ],
            separators=(",", ":"),
        ),
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


def _consumer_manifest() -> dict[str, Any]:
    consumers: list[dict[str, Any]] = []
    for index, consumer in enumerate(REGISTRY["consumers"]):
        _, activation, _ = _consumer_activation(consumer)
        snapshot = {
            "image": _image(consumer, index),
            "task_definition_arn": _task_arn(consumer),
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


def _state() -> dict[str, Any]:
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
                _task_attributes(consumer, index),
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


def _plan() -> dict[str, Any]:
    manifest = _consumer_manifest()
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
        attributes = _task_attributes(consumer, index)
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
            "mcp_image": {
                "value": (
                    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                    f"teamagent-mcp@sha256:{'a' * 64}"
                )
            },
            "openclaw_image": {
                "value": (
                    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                    f"teamagent-openclaw@sha256:{'b' * 64}"
                )
            },
            "media_worker_image": {"value": ""},
            "tiktok_acquire_image": {"value": ""},
            "enable_media_worker": {"value": False},
            "enable_tiktok_acquire": {"value": False},
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
    plan = _plan()
    media_image = (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-media-worker@sha256:{'c' * 64}"
    )
    plan["variables"]["enable_media_worker"]["value"] = True
    plan["variables"]["enable_tiktok_acquire"]["value"] = True
    plan["variables"]["media_worker_image"]["value"] = media_image

    value = CONTEXT.build_context(
        plan=plan,
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert len(value["plan"]["runtime_images_sha256"]) == 64

    plan["variables"]["media_worker_image"]["value"] = (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-dev-tiktok-acquire@sha256:{'c' * 64}"
    )
    with pytest.raises(CONTEXT.ContextError, match="generic media release digest"):
        CONTEXT.build_context(
            plan=plan,
            state=_state(),
            backend_metadata=_backend(),
            workspace="default",
        )


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


def test_activation_enable_requires_receipt_mode_and_rejects_mode_downgrade() -> None:
    plan = _plan()
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
        state=_state(),
        backend_metadata=_backend(),
        workspace="default",
    )

    assert value["consumer_manifest"]["mode"] == "receipt-required"

    downgraded = plan["variables"]["image_deployment_consumer_manifest"]["value"]
    downgraded["mode"] = "no-image-transition"
    with pytest.raises(CONTEXT.ContextError, match="derived comparison"):
        CONTEXT.validate_consumer_manifest(downgraded)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest["consumers"][0].update(release_repository="hostile"),
        lambda manifest: manifest["consumers"][0]["after"].update(image=None),
        lambda manifest: manifest["consumers"][0]["before"].update(
            image=(
                f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'f' * 64}"
            )
        ),
    ],
)
def test_consumer_manifest_rejects_repository_unknown_and_live_before_drift(
    mutation: Any,
) -> None:
    manifest = _consumer_manifest()
    mutation(manifest)

    with pytest.raises(CONTEXT.ContextError):
        CONTEXT.validate_consumer_manifest(manifest)


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
