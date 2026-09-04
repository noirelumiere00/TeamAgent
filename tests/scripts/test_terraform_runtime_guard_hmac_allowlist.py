"""guard の HMAC migration allowlist と契約B（hmac_keyrings.tf）の描画キー集合の 1:1 契約。

terraform_runtime_guard.sh の validate_runtime_task_field_allowlist は、live exact source と
planned taskdef の env/secrets 差分を「runtime field + 契約Bが描画する HMAC キー」以外で
許さない。ここでは Terraform 側の描画キー集合を HCL から機械的に抽出し、guard 側の手書き集合と
過不足ゼロで一致することを要求する（片側だけ編集すると赤になる）。
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TF_ROOT = PROJECT_ROOT / "infra" / "terraform"
GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"

# 契約B が描画する集合の抽出が空振りしていないことを示す番人キー（便δ-0 で初回描画される）。
SENTINEL_ENV_KEY = "TEAMAGENT_HMAC_STATE_REQUIRED"
SENTINEL_SECRET_KEY = "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET"

LOCALS_HEADER_RE = re.compile(r"^locals\s*\{", re.MULTILINE)
LOCAL_ASSIGN_RE = re.compile(r"^  ([a-z0-9_]+)\s*=", re.MULTILINE)
LOCAL_REF_RE = re.compile(r"\blocal\.([a-z0-9_]+)\b")
RENDERED_NAME_RE = re.compile(r'\bname\s*=\s*"([A-Z0-9_]+)"')
TASKDEF_HEADER_RE = re.compile(
    r'^resource\s+"aws_ecs_task_definition"\s+"([a-z0-9_]+)"\s*\{',
    re.MULTILINE,
)
GUARD_FUNCTION_RE = re.compile(
    r"validate_runtime_task_field_allowlist\(\) \{.*?(?=\nvalidate_planned_hmac_consumers\(\))",
    re.DOTALL,
)
GUARD_SPEC_RE = re.compile(
    r"'(aws_ecs_task_definition\.([a-z0-9_]+)(?:\[0\])?)\|([a-z_]+)\|([a-z-]+)\|(\[[^\]]*\])\|([a-z]*)'"
)
HMAC_LOCAL_SUFFIXES = ("_environment", "_secrets")


def _hcl_block_bodies(text: str, header: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    """header にマッチする HCL ブロックの (header match, 波括弧内側の本文) を全て返す。"""
    bodies: list[tuple[re.Match[str], str]] = []
    for match in header.finditer(text):
        brace = text.index("{", match.start())
        depth = 0
        for cursor in range(brace, len(text)):
            char = text[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    bodies.append((match, text[brace + 1 : cursor]))
                    break
        else:
            raise AssertionError(f"unterminated HCL block: {match.group(0)!r}")
    return bodies


def _tf_local_assignments() -> dict[str, str]:
    """infra/terraform/*.tf の locals ブロックから `name = <式>` を {name: 式本文} で返す。"""
    assignments: dict[str, str] = {}
    for path in sorted(TF_ROOT.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for _, body in _hcl_block_bodies(text, LOCALS_HEADER_RE):
            matches = list(LOCAL_ASSIGN_RE.finditer(body))
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
                name = match.group(1)
                assert name not in assignments, f"duplicate local: {name}"
                assignments[name] = body[match.end() : end]
    return assignments


def _is_hmac_render_local(name: str) -> bool:
    return "hmac" in name and name.endswith(HMAC_LOCAL_SUFFIXES)


def _rendered_keys(
    name: str,
    assignments: dict[str, str],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """local `name` が描画する env/secrets の name 集合（concat で参照する local も再帰解決）。"""
    assert name not in seen, f"cyclic local reference: {name}"
    body = assignments[name]
    keys = set(RENDERED_NAME_RE.findall(body))
    for ref in LOCAL_REF_RE.findall(body):
        if ref in assignments and _is_hmac_render_local(ref):
            keys |= _rendered_keys(ref, assignments, seen | {name})
    return keys


@dataclass(frozen=True)
class TerraformContract:
    """契約B が taskdef ごとに描画する HMAC キー集合。"""

    env_by_task: dict[str, set[str]]
    secrets_by_task: dict[str, set[str]]
    all_tasks: set[str]

    @property
    def consumers(self) -> set[str]:
        return {
            task for task in self.all_tasks if self.env_by_task[task] or self.secrets_by_task[task]
        }

    @property
    def env(self) -> set[str]:
        return set().union(*self.env_by_task.values())

    @property
    def secrets(self) -> set[str]:
        return set().union(*self.secrets_by_task.values())


def _terraform_contract() -> TerraformContract:
    assignments = _tf_local_assignments()
    env_by_task: dict[str, set[str]] = {}
    secrets_by_task: dict[str, set[str]] = {}
    for path in sorted(TF_ROOT.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for header, body in _hcl_block_bodies(text, TASKDEF_HEADER_RE):
            task = header.group(1)
            assert task not in env_by_task, f"duplicate task definition: {task}"
            env_by_task[task] = set()
            secrets_by_task[task] = set()
            for ref in sorted(set(LOCAL_REF_RE.findall(body))):
                if ref not in assignments or not _is_hmac_render_local(ref):
                    continue
                target = env_by_task if ref.endswith("_environment") else secrets_by_task
                target[task] |= _rendered_keys(ref, assignments)
    return TerraformContract(env_by_task, secrets_by_task, set(env_by_task))


@dataclass(frozen=True)
class GuardSpec:
    address: str
    task: str
    component: str
    container: str
    runtime_env: tuple[str, ...]
    hmac_consumer: bool


@dataclass(frozen=True)
class GuardAllowlist:
    env: tuple[str, ...]
    secrets: tuple[str, ...]
    specs: tuple[GuardSpec, ...]

    @property
    def consumers(self) -> set[str]:
        return {spec.task for spec in self.specs if spec.hmac_consumer}


def _guard_function() -> str:
    function = GUARD_FUNCTION_RE.search(GUARD.read_text(encoding="utf-8"))
    assert function is not None, "validate_runtime_task_field_allowlist が guard にありません"
    return function.group(0)


def _guard_array(function: str, variable: str) -> tuple[str, ...]:
    match = re.search(rf"local {variable}='(\[.*?\])'", function, flags=re.DOTALL)
    assert match is not None, f"{variable} が guard にありません"
    values = json.loads(match.group(1))
    assert all(isinstance(value, str) for value in values), variable
    return tuple(values)


def _guard_allowlist() -> GuardAllowlist:
    function = _guard_function()
    specs = tuple(
        GuardSpec(
            address=match.group(1),
            task=match.group(2),
            component=match.group(3),
            container=match.group(4),
            runtime_env=tuple(json.loads(match.group(5))),
            hmac_consumer=match.group(6) == "hmac",
        )
        for match in GUARD_SPEC_RE.finditer(function)
    )
    assert specs, "guard の taskdef allowlist spec を抽出できません"
    for match in re.finditer(r"'aws_ecs_task_definition\.[^']*'", function):
        assert GUARD_SPEC_RE.fullmatch(match.group(0)), f"spec 形式が不正: {match.group(0)}"
    return GuardAllowlist(
        env=_guard_array(function, "hmac_contract_env"),
        secrets=_guard_array(function, "hmac_contract_secrets"),
        specs=specs,
    )


def test_guard_hmac_allowlist_equals_contract_b_rendered_key_set() -> None:
    terraform = _terraform_contract()
    guard = _guard_allowlist()

    # 抽出が空振りしていない（便δ-0 で初回描画されるキーを tf 側から拾えている）。
    assert SENTINEL_ENV_KEY in terraform.env
    assert SENTINEL_SECRET_KEY in terraform.secrets

    # 過不足ゼロ: tf 描画キー集合 == guard allowlist 集合（env / secrets それぞれ）。
    assert set(guard.env) == terraform.env, {
        "guard_only": sorted(set(guard.env) - terraform.env),
        "terraform_only": sorted(terraform.env - set(guard.env)),
    }
    assert set(guard.secrets) == terraform.secrets, {
        "guard_only": sorted(set(guard.secrets) - terraform.secrets),
        "terraform_only": sorted(terraform.secrets - set(guard.secrets)),
    }
    assert len(guard.env) == len(set(guard.env))
    assert len(guard.secrets) == len(set(guard.secrets))
    assert not set(guard.env) & set(guard.secrets)


def test_guard_hmac_consumers_match_taskdefs_that_render_contract_b() -> None:
    terraform = _terraform_contract()
    guard = _guard_allowlist()

    # guard の spec は tf の aws_ecs_task_definition を過不足なく一度ずつ列挙する。
    assert sorted(spec.task for spec in guard.specs) == sorted(terraform.all_tasks)
    # HMAC consumer フラグは「契約B の local を environment/secrets に concat する taskdef」と一致。
    assert guard.consumers == terraform.consumers
    assert guard.consumers == {"mcp", "connect_web", "morning_digest"}
    # runtime field 列は HMAC キーと重ならない（HMAC は hmac_contract_* だけが出所）。
    for spec in guard.specs:
        assert not set(spec.runtime_env) & (set(guard.env) | set(guard.secrets)), spec


ACCOUNT = "718959508629"
REGION = "ap-northeast-1"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-mcp@sha256:{'a' * 64}"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:teamagent/dev/fixture"
# 便δ-0 直前の live 形状（2026-09-02 に describe-task-definition で実測した HMAC 関連キーのみ）。
# connect-web:78 は契約B が描画しない MAIL_ACTION_HMAC_SECRET を残留させている。
LIVE_HMAC_ENV = {
    "mcp": [],
    "connect_web": ["TEAMAGENT_HMAC_STATE_TABLE", "TEAMAGENT_HMAC_STATE_SCOPE"],
    "morning_digest": [],
}
LIVE_HMAC_SECRETS = {
    "mcp": ["MAIL_ACTION_HMAC_SECRET", "REPORT_LINK_HMAC_SECRET"],
    "connect_web": ["MAIL_ACTION_HMAC_SECRET", "REPORT_LINK_HMAC_SECRET"],
    "morning_digest": ["MAIL_ACTION_HMAC_SECRET"],
}


def _container(name: str, env: dict[str, str], secrets: dict[str, str]) -> dict[str, Any]:
    return {
        "name": name,
        "image": IMAGE,
        "essential": True,
        "environment": [{"name": key, "value": value} for key, value in env.items()],
        "secrets": [{"name": key, "valueFrom": value} for key, value in secrets.items()],
    }


def _task_tf(spec: GuardSpec, container: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": f"teamagent-dev-{spec.container}",
        "task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{spec.container}-task",
        "execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{spec.container}-exec",
        "cpu": "1024",
        "memory": "2048",
        "network_mode": "awsvpc",
        "requires_compatibilities": ["FARGATE"],
        "runtime_platform": [{"cpu_architecture": "X86_64", "operating_system_family": "LINUX"}],
        "volume": [{"name": "runtime-tmp"}],
        "container_definitions": json.dumps([container]),
        "skip_destroy": True,
        "tags_all": {},
    }


def _critical(task: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [
            "jq",
            "-L",
            str(GUARD.parent),
            "-c",
            'include "terraform_runtime_guard"; guard_task_from_tf',
        ],
        input=json.dumps(task),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@dataclass
class AllowlistFixture:
    plan: dict[str, Any]
    snapshot: dict[str, Any]
    containers: dict[str, dict[str, Any]]  # task name -> planned container (after)


def _contract_b_initial_rollout_fixture() -> AllowlistFixture:
    """live=便δ-0 直前 / plan=契約B 初回描画（consumer は全 HMAC キーを得て残留キーを失う）。"""
    terraform = _terraform_contract()
    guard = _guard_allowlist()
    resource_changes = []
    snapshot: dict[str, Any] = {"taskdefs": {}}
    planned: dict[str, dict[str, Any]] = {}
    for spec in guard.specs:
        live_env = {key: f"live-{key}" for key in spec.runtime_env}
        live_env["BASE_FLAG"] = "1"
        live_env.update({key: f"live-{key}" for key in LIVE_HMAC_ENV.get(spec.task, [])})
        live_secrets = {"DATABASE_URL": f"{SECRET_ARN}-db"}
        live_secrets.update(
            {key: f"{SECRET_ARN}-{key}" for key in LIVE_HMAC_SECRETS.get(spec.task, [])}
        )
        before = _task_tf(spec, _container(spec.container, live_env, live_secrets))

        planned_env = dict(live_env)
        planned_env.update(
            {key: f"rendered-{key}" for key in sorted(terraform.env_by_task[spec.task])}
        )
        planned_secrets = {
            key: value for key, value in live_secrets.items() if key not in set(guard.secrets)
        }
        planned_secrets.update(
            {
                key: f"{SECRET_ARN}-rendered-{key}"
                for key in sorted(terraform.secrets_by_task[spec.task])
            }
        )
        after_container = _container(spec.container, planned_env, planned_secrets)
        after = _task_tf(spec, after_container)
        planned[spec.task] = after_container
        resource_changes.append(
            {
                "address": spec.address,
                "type": "aws_ecs_task_definition",
                "change": {"actions": ["create", "delete"], "before": before, "after": after},
            }
        )
        snapshot["taskdefs"][spec.component] = {
            "env": live_env,
            "secrets": live_secrets,
            "critical": _critical(before),
        }
    return AllowlistFixture({"resource_changes": resource_changes}, snapshot, planned)


def _sync_container(fixture: AllowlistFixture, task: str) -> None:
    change = next(
        item["change"]
        for item in fixture.plan["resource_changes"]
        if re.fullmatch(rf"aws_ecs_task_definition\.{task}(\[0\])?", item["address"])
    )
    change["after"]["container_definitions"] = json.dumps([fixture.containers[task]])


def _run_field_allowlist_validator(
    tmp_path: Path, fixture: AllowlistFixture
) -> subprocess.CompletedProcess[str]:
    plan_path = tmp_path / "plan.json"
    snapshot_path = tmp_path / "snapshot.json"
    plan_path.write_text(json.dumps(fixture.plan), encoding="utf-8")
    snapshot_path.write_text(json.dumps(fixture.snapshot), encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            f"GUARD_JQ_DIR={str(GUARD.parent)!r}",
            'die() { echo "★ $*" >&2; return 1; }',
            _guard_function(),
            'validate_runtime_task_field_allowlist "$1" "$2"',
        )
    )
    return subprocess.run(
        ["bash", "-c", script, "validator", str(plan_path), str(snapshot_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_field_allowlist_validator_accepts_contract_b_initial_rollout(tmp_path: Path) -> None:
    fixture = _contract_b_initial_rollout_fixture()
    # connect-web の残留 MAIL_ACTION_HMAC_SECRET は plan で撤去される（validate_planned_hmac_consumers
    # が purpose_absent を要求するため）。fixture がその形になっていることを固定する。
    connect_secrets = {item["name"] for item in fixture.containers["connect_web"]["secrets"]}
    assert "MAIL_ACTION_HMAC_SECRET" not in connect_secrets
    assert "REPORT_LINK_HMAC_SECRET" in connect_secrets
    mcp_env = {item["name"] for item in fixture.containers["mcp"]["environment"]}
    assert SENTINEL_ENV_KEY in mcp_env

    result = _run_field_allowlist_validator(tmp_path, fixture)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("task", "field", "key"),
    [
        ("mcp", "environment", "USE_NEW_FEATURE_FLAG"),
        ("mcp", "secrets", "DATABASE_URL_V2"),
        ("connect_web", "environment", "SEARCH_NEW_KNOB"),
        # HMAC consumer でない taskdef は契約B のキーを得てはならない。
        ("ingest", "environment", SENTINEL_ENV_KEY),
        ("canary", "secrets", "MAIL_ACTION_HMAC_SECRET"),
    ],
)
def test_field_allowlist_validator_rejects_non_contract_changes(
    tmp_path: Path, task: str, field: str, key: str
) -> None:
    fixture = _contract_b_initial_rollout_fixture()
    container = fixture.containers[task]
    value_key = "value" if field == "environment" else "valueFrom"
    container[field].append({"name": key, value_key: "unexpected"})
    _sync_container(fixture, task)

    result = _run_field_allowlist_validator(tmp_path, fixture)

    assert result.returncode == 1
    assert f"aws_ecs_task_definition.{task}" in result.stderr
    assert "許可されたruntime/HMAC field以外も変更します" in result.stderr


def test_field_allowlist_validator_rejects_removal_of_non_contract_live_key(
    tmp_path: Path,
) -> None:
    fixture = _contract_b_initial_rollout_fixture()
    container = copy.deepcopy(fixture.containers["morning_digest"])
    container["environment"] = [
        item for item in container["environment"] if item["name"] != "BASE_FLAG"
    ]
    fixture.containers["morning_digest"] = container
    _sync_container(fixture, "morning_digest")

    result = _run_field_allowlist_validator(tmp_path, fixture)

    assert result.returncode == 1
    assert "aws_ecs_task_definition.morning_digest[0]" in result.stderr
    assert "許可されたruntime/HMAC field以外も変更します" in result.stderr
