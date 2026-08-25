"""ingest の activation preflight が RunTask に渡す network 設定の契約。

ingest の EventBridge rule は ECS target を持たず dispatch Lambda を起動するため、
network 設定は event target ではなく Lambda の env に載っている。
guard がそれを取り違えると RunTask が subnets/securityGroups=null で拒否され、
activation preflight が起動できない。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"

# ingest の rule は Lambda target なので ecs_target は空に正規化される。
# static_environment は live 実測（本番 dispatch Lambda の env から TASKDEF_ARN を除いたもの）と同形。
_SNAPSHOT = {
    "targets": {
        "ingest": {"critical": {"ecs_target": {}}},
        "canary": {
            "critical": {
                "ecs_target": {
                    "network_configuration": {
                        "subnets": ["subnet-canary-a"],
                        "security_groups": ["sg-canary"],
                        "assign_public_ip": True,
                    }
                }
            }
        },
    },
    "rule_dispatchers": {
        "ingest": {
            "static_environment": {
                "CLUSTER_ARN": "arn:aws:ecs:ap-northeast-1:718959508629:cluster/teamagent-dev",
                "TASK_FAMILY": "teamagent-dev-ingest",
                "SUBNETS": "subnet-aaa,subnet-bbb,subnet-ccc",
                "SG_ID": "sg-ingest",
                "INGEST_MAX_RUNTIME_HOURS": "6",
            }
        }
    },
}

_INGEST_EXPRESSION = """
.rule_dispatchers.ingest.static_environment as $env |
{
  awsvpcConfiguration:{
    subnets:($env.SUBNETS | split(",")),
    securityGroups:[$env.SG_ID],
    assignPublicIp:"ENABLED"
  }
}
"""

# 修正前の取得元。ingest には ecs_target が無いので null しか出ない。
_LEGACY_EXPRESSION = """
{
  awsvpcConfiguration:{
    subnets:.targets.ingest.critical.ecs_target.network_configuration.subnets,
    securityGroups:.targets.ingest.critical.ecs_target.network_configuration.security_groups,
    assignPublicIp:"DISABLED"
  }
}
"""


def _jq(expression: str, document: dict) -> dict:
    if shutil.which("jq") is None:
        pytest.skip("jq が無い環境ではこの契約を検証できない")
    result = subprocess.run(
        ["jq", "-c", expression],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_ingest_preflight_network_comes_from_the_dispatch_lambda_environment() -> None:
    network = _jq(_INGEST_EXPRESSION, _SNAPSHOT)["awsvpcConfiguration"]

    assert network["subnets"] == ["subnet-aaa", "subnet-bbb", "subnet-ccc"]
    assert network["securityGroups"] == ["sg-ingest"]
    # 本番 dispatcher（lambda/ingest_dispatch/handler.py）と同じく常に ENABLED。
    assert network["assignPublicIp"] == "ENABLED"


def test_legacy_event_target_source_would_produce_a_null_network_for_ingest() -> None:
    """修正前の取得元では null になることを固定する（この fix が無意味でない証拠）。"""
    network = _jq(_LEGACY_EXPRESSION, _SNAPSHOT)["awsvpcConfiguration"]

    assert network["subnets"] is None
    assert network["securityGroups"] is None


def test_guard_builds_the_ingest_preflight_network_from_the_dispatch_lambda() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    marker = 'if [ "$component" = "ingest" ]; then'
    start = guard.index(marker)
    # ingest 分岐は直後の `  else` まで。else 以降を含めると判定が無意味になる。
    branch = guard[start : guard.index("\n  else", start)]

    # ingest 分岐は Lambda env から組み立て、event target の ecs_target は読まない。
    assert ".rule_dispatchers.ingest.static_environment" in branch
    assert "$env.SUBNETS" in branch
    assert "$env.SG_ID" in branch
    assert "targets[$c].critical.ecs_target" not in branch
    # 空 env で素通りしないよう subnet-/sg- 形式を検査してから使う。
    assert 'startswith("subnet-")' in branch
    assert 'startswith("sg-")' in branch


def test_non_ingest_preflight_still_reads_the_event_target_network() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    start = guard.index('if [ "$component" = "ingest" ]; then')
    else_index = guard.index("  else", start)
    fallback = guard[else_index : else_index + 700]

    # canary など ECS target を持つ consumer は従来どおり event target から読む。
    assert "targets[$c].critical.ecs_target.network_configuration.subnets" in fallback
