"""EC2 worker（teamagent-dev-worker）の撤去が戻されていないことを確かめる。

2026-09-28 の裁定で、2026-08-03 から停止していた EC2 worker を terraform で撤去した
（ディスクはスナップショットで保存済み）。撤去は次の 3 点でできている。

- ``infra/terraform/worker.tf`` を削除した（インスタンス・SG・db_from_worker・IAM 4 資源）
- ``enable_hmac_worker_deploy`` に「常に false」の validation を付け、worker の HMAC 配布経路を
  plan の時点で止まるようにした
- guard（``validate_exact_runtime_iam_plan``）の必須リストから ``worker_app`` を外した

どれかが戻ると、本番の state と repo が食い違う（空の EC2 が新しい AMI で立つ、guard が
「required IAM resource missing」で全 plan を止める など）ので、ここで赤にする。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
WORKER_DEPLOY_TF = TF_ROOT / "hmac_worker_deploy.tf"
GUARD = ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"

RETIRED_RESOURCES = (
    ("aws_instance", "worker"),
    ("aws_security_group", "worker"),
    ("aws_security_group_rule", "db_from_worker"),
    ("aws_iam_role", "worker"),
    ("aws_iam_role_policy", "worker_app"),
    ("aws_iam_role_policy_attachment", "worker_ssm"),
    ("aws_iam_instance_profile", "worker"),
)
RETIRED_DATA = (
    ("aws_iam_policy_document", "worker_assume"),
    ("aws_iam_policy_document", "worker_app"),
)
RETIRED_OUTPUTS = ("worker_instance_id", "worker_connect_command")
RETIRED_VARIABLES = ("worker_instance_type", "worker_root_gb")

# db-sg に残る 5432 の許可。worker の撤去に巻き込んで消していないことを確かめる。
REMAINING_DB_RULES = (
    "db_from_bastion",
    "db_from_mcp",
    "db_from_connect_web",
    "db_from_ingest",
    "db_from_morning_digest",
)

REMAINING_REQUIRED_IAM = (
    "aws_iam_role_policy.lambda_app",
    "aws_iam_role_policy.mcp_task",
    "aws_iam_role_policy.connect_web_task[0]",
    "aws_iam_role_policy.ingest_task[0]",
    "aws_iam_role_policy.morning_digest_task[0]",
)


def _all_terraform() -> str:
    paths = sorted(TF_ROOT.glob("*.tf"))
    assert paths, "infra/terraform に .tf が見つからない"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _reference(address: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(address)}(?![A-Za-z0-9_])")


def _variable_block(source: str, name: str) -> str:
    marker = f'variable "{name}" {{'
    start = source.index(marker)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated variable block: {name}")


def _guard_required_iam_addresses() -> list[str]:
    guard = GUARD.read_text(encoding="utf-8")
    start = guard.index("validate_exact_runtime_iam_plan() {")
    end = guard.index("\n}\n", start)
    function = guard[start:end]
    marker = "all(. as $address | $plan | resource($address) | converges(.change))"
    assert function.count(marker) == 1
    head = function[: function.index(marker)]
    # 直前の jq 配列リテラル（1 行 1 アドレス）を丸ごと取る。アドレス内の "[0]" と混同しない。
    array = re.search(r'\[\n((?:[ \t]*"[^"\n]+",?\n)+)[ \t]*\][ \t]*\|\s*$', head)
    assert array is not None, "guard の必須 IAM リストを抽出できない"
    return re.findall(r'"([^"]+)"', array.group(1))


def test_worker_tf_is_gone() -> None:
    assert not (TF_ROOT / "worker.tf").exists()


def test_bastion_is_the_only_ec2_instance() -> None:
    declared = re.findall(r'(?m)^resource\s+"aws_instance"\s+"([^"]+)"', _all_terraform())
    assert declared == ["bastion"]


@pytest.mark.parametrize(("kind", "name"), RETIRED_RESOURCES)
def test_retired_worker_resource_is_neither_declared_nor_referenced(kind: str, name: str) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^resource\s+"{kind}"\s+"{name}"\s*\{{', source)
    assert not _reference(f"{kind}.{name}").search(source)


@pytest.mark.parametrize(("kind", "name"), RETIRED_DATA)
def test_retired_worker_data_source_is_neither_declared_nor_referenced(
    kind: str, name: str
) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^data\s+"{kind}"\s+"{name}"\s*\{{', source)
    assert not _reference(f"data.{kind}.{name}").search(source)


@pytest.mark.parametrize("name", RETIRED_OUTPUTS)
def test_retired_worker_output_is_gone(name: str) -> None:
    assert not re.search(rf'(?m)^output\s+"{name}"', _all_terraform())


@pytest.mark.parametrize("name", RETIRED_VARIABLES)
def test_retired_worker_variable_is_gone(name: str) -> None:
    source = _all_terraform()
    assert not re.search(rf'(?m)^variable\s+"{name}"', source)
    assert not _reference(f"var.{name}").search(source)


@pytest.mark.parametrize("name", REMAINING_DB_RULES)
def test_other_database_ingress_rules_survive(name: str) -> None:
    assert re.search(
        rf'(?m)^resource\s+"aws_security_group_rule"\s+"{name}"\s*\{{', _all_terraform()
    )


def test_bastion_keeps_its_ami_data_source() -> None:
    source = _all_terraform()
    assert re.search(r'(?m)^data\s+"aws_ami"\s+"al2023_arm"\s*\{', source)
    assert "data.aws_ami.al2023_arm.id" in source


def test_worker_hmac_deploy_path_is_sealed_by_validation() -> None:
    block = _variable_block(
        WORKER_DEPLOY_TF.read_text(encoding="utf-8"), "enable_hmac_worker_deploy"
    )
    assert re.search(r"(?m)^\s*default\s*=\s*false\s*$", block)
    validation = block[block.index("validation {") :]
    assert re.search(r"(?m)^\s*condition\s*=\s*!var\.enable_hmac_worker_deploy\s*$", validation)
    assert "retired" in validation


def test_guard_no_longer_requires_the_retired_worker_policy() -> None:
    required = _guard_required_iam_addresses()
    assert "aws_iam_role_policy.worker_app" not in required
    assert sorted(required) == sorted(REMAINING_REQUIRED_IAM)
