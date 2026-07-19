from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "image_release_context.py"
PLAN_SCRIPT = ROOT / "infra" / "terraform" / "plan_image_release.sh"
APPLY_SCRIPT = ROOT / "infra" / "terraform" / "apply_image_release_plan.sh"


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
    return {
        "version": 4,
        "terraform_version": "1.12.2",
        "serial": 1234,
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
        ],
    }


def _plan() -> dict[str, Any]:
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
            "enable_tiktok_acquire": {"value": False},
        },
        "configuration": {
            "root_module": {
                "resources": [
                    {"address": "terraform_data.production_image_release_gate"},
                    {"address": "aws_ecs_task_definition.mcp"},
                ],
            }
        },
        "resource_changes": [
            {
                "address": "terraform_data.production_image_release_gate",
                "mode": "managed",
                "change": {"actions": ["delete", "create"]},
            },
            {
                "address": "aws_ecs_task_definition.mcp",
                "mode": "managed",
                "change": {"actions": ["update"]},
            },
        ],
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
    assert value["state"]["managed_address_count"] == 2
    assert value["plan"] == {
        "complete": True,
        "applyable": True,
        "errored": False,
        "managed_change_count": 2,
        "address_ownership_sha256": value["plan"]["address_ownership_sha256"],
        "runtime_images_sha256": value["plan"]["runtime_images_sha256"],
        "delete_change_count": 0,
        "replace_change_count": 0,
        "transition_sha256": value["plan"]["transition_sha256"],
    }
    assert len(CONTEXT.context_sha256(value)) == 64


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda plan, state, backend: plan.update(complete=False), "incomplete"),
        (lambda plan, state, backend: plan.update(applyable=False), "not applyable"),
        (
            lambda plan, state, backend: plan["resource_changes"][1]["change"].update(
                importing={"id": "hostile-import"}
            ),
            "cannot contain import",
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
    state = _state()
    state["resources"].extend(
        [
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
        ]
    )

    binding = CONTEXT._state_binding(state)

    assert binding["managed_address_count"] == 4
    assert binding["_addresses"] == [
        "aws_ecs_task_definition.mcp",
        'module.workers["media"].aws_ecs_task_definition.worker["blue"]',
        'module.workers["media"].aws_ecs_task_definition.worker[0]',
        "terraform_data.production_image_release_gate",
    ]


def test_launchers_reject_injected_terraform_environment_and_unsafe_plan_modes() -> None:
    plan_body = PLAN_SCRIPT.read_text(encoding="utf-8")
    apply_body = APPLY_SCRIPT.read_text(encoding="utf-8")

    for body in (plan_body, apply_body):
        assert "compgen -A variable TF_" in body
        assert "pre-existing Terraform environment is forbidden" in body
        assert "TF_WORKSPACE" not in body
        assert "TF_DATA_DIR" not in body
        assert "TF_CLI_ARGS" not in body
        assert "export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true" in body
        assert "compgen -A variable AWS_ENDPOINT_URL" in body
        assert "unset endpoint_variable AWS_PROFILE AWS_DEFAULT_PROFILE" in body
        assert 'AWS_DEFAULT_REGION="$EXPECTED_REGION"' in body
        assert "AWS_CONFIG_FILE=/dev/null" in body
        assert "AWS_SHARED_CREDENTIALS_FILE=/dev/null" in body
        assert "teamagent-dev-terraform-automation/teamagent-terraform-worker" in body
    for forbidden in (
        "-target",
        "-lock=false",
        "-refresh=false",
        "-refresh-only",
        "-destroy",
        "-generate-config-out",
    ):
        assert forbidden in plan_body
    assert "image_release_context.py" in plan_body
    assert "image_release_context.py" in apply_body
    assert "acquire-deployment-lock" in apply_body
    assert "validate-deployment-preflight" in apply_body
    assert "terraform_apply_supervisor.py" in apply_body
    supervisor = (ROOT / "infra" / "terraform" / "terraform_apply_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert "heartbeat-deployment-lock" in supervisor
    assert "start_new_session=True" in supervisor
    assert "os.killpg" in supervisor
    assert "release-deployment-lock" in apply_body
    assert '"-lock-timeout=5m",' in supervisor
