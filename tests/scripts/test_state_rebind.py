"""PR2-A0.4 state rebind の契約テスト。

「本番は正しく state だけ古い」とき、live を再デプロイせず state の binding だけを
live の exact revision へ付け替える唯一の経路を fail-closed で固定する。
正式契約: AWS managed application resources mutation = 0 / Terraform remote state mutation only。
各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"
BOOTSTRAP = ROOT / "infra/deploy/bootstrap_runtime_session.sh"
MAPPING = ROOT / "infra/deploy/state_rebind_targets.json"

sys.path.insert(0, str(ROOT / "infra/deploy"))

from state_rebind import (  # noqa: E402
    APPROVE_TOKEN,
    BOUND_FIELDS,
    RebindError,
    compare_binding,
    compare_state_to_live,
    expected_approval,
    load_targets,
)

ARN = "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:86"


def _target(**overrides: Any) -> dict[str, Any]:
    entry = {
        "address": "aws_ecs_task_definition.mcp",
        "family": "teamagent-dev-mcp",
        "target_arn": ARN,
        "consumer": {
            "kind": "ecs-service",
            "name": "teamagent-dev-mcp",
            "cluster": "teamagent-dev",
        },
    }
    entry.update(overrides)
    return entry


def _mapping_file(tmp_path: Path, targets: list[dict[str, Any]]) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"schema_version": 1, "targets": targets}))
    return path


# ── mapping の厳密検証 ────────────────────────────────────────────────────


def test_mapping_accepts_a_valid_target(tmp_path: Path) -> None:
    assert len(load_targets(_mapping_file(tmp_path, [_target()]))) == 1


def test_production_mapping_is_frozen_with_the_approved_six_targets() -> None:
    """freeze 後に確定した production mapping の改竄封印。

    値の由来: PRODUCTION DEPLOYMENT FREEZE（2026-08-20 18:15 JST）後の fresh 再解決。
    approved evidence = mcp 系 5 件は署名検証済み release receipt、tiktok_acquire:25 は
    human gate 明示承認（freeze 窓限定）。ここを変える場合は freeze 境界の引き直しと
    human gate の再承認が必要。
    """
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    assert raw["frozen_at"] == "2026-08-20T09:15:00Z"
    expected = {
        "aws_ecs_task_definition.mcp": "teamagent-dev-mcp:86",
        "aws_ecs_task_definition.connect_web": "teamagent-dev-connect-web:71",
        "aws_ecs_task_definition.morning_digest": "teamagent-dev-morning-digest:53",
        "aws_ecs_task_definition.canary": "teamagent-dev-canary:23",
        "aws_ecs_task_definition.ingest": "teamagent-dev-ingest:55",
        "aws_ecs_task_definition.tiktok_acquire": "teamagent-dev-tiktok-acquire:25",
    }
    actual = {t["address"]: t["target_arn"].split("/")[-1] for t in raw["targets"]}
    assert actual == expected
    # x_buzz_worker は state == live (:1) のため対象外（含まれていたら誤り）
    assert "aws_ecs_task_definition.x_buzz_worker" not in actual
    # loader の厳密検証も通ること（consumer 宣言含む）
    assert len(load_targets(MAPPING, require_targets=True)) == 6


def test_empty_targets_are_rejected_when_required(tmp_path: Path) -> None:
    with pytest.raises(RebindError, match="freeze"):
        load_targets(_mapping_file(tmp_path, []), require_targets=True)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda t: t.update(address="aws_ecs_service.mcp"), "address is malformed"),
        (lambda t: t.update(target_arn=ARN.replace(":86", "")), "target_arn is malformed"),
        (lambda t: t.update(family="teamagent-dev-other"), "ARN family"),
        (lambda t: t["consumer"].update(kind="ssm-parameter"), "consumer.kind"),
        (lambda t: t["consumer"].pop("name"), "consumer.name"),
    ],
)
def test_mapping_rejects_malformed_targets(tmp_path: Path, mutate: Any, match: str) -> None:
    target = _target()
    mutate(target)
    with pytest.raises(RebindError, match=match):
        load_targets(_mapping_file(tmp_path, [target]))


def test_mapping_rejects_duplicates(tmp_path: Path) -> None:
    with pytest.raises(RebindError, match="duplicate address"):
        load_targets(_mapping_file(tmp_path, [_target(), _target()]))


# ── state == live の機械比較 ──────────────────────────────────────────────


def _state_doc(image: str = "repo@sha256:abc", env: dict[str, str] | None = None) -> dict[str, Any]:
    container = {
        "name": "mcp",
        "image": image,
        "environment": [{"name": k, "value": v} for k, v in (env or {"A": "1"}).items()],
        "secrets": [{"name": "DB", "valueFrom": "arn:secret"}],
    }
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_ecs_task_definition",
                "name": "mcp",
                "instances": [
                    {
                        "attributes": {
                            "arn": ARN,
                            "revision": 86,
                            "family": "teamagent-dev-mcp",
                            "container_definitions": json.dumps([container]),
                        }
                    }
                ],
            }
        ]
    }


def _describe_doc(
    image: str = "repo@sha256:abc", env: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "taskDefinition": {
            "taskDefinitionArn": ARN,
            "revision": 86,
            "family": "teamagent-dev-mcp",
            "containerDefinitions": [
                {
                    "name": "mcp",
                    "image": image,
                    "environment": [
                        {"name": k, "value": v} for k, v in (env or {"A": "1"}).items()
                    ],
                    "secrets": [{"name": "DB", "valueFrom": "arn:secret"}],
                }
            ],
        }
    }


def test_compare_accepts_identical_state_and_live() -> None:
    compare_state_to_live(_state_doc(), "aws_ecs_task_definition.mcp", _describe_doc())


def test_compare_rejects_image_mismatch() -> None:
    with pytest.raises(RebindError, match="container definitions differ"):
        compare_state_to_live(
            _state_doc(image="repo@sha256:abc"),
            "aws_ecs_task_definition.mcp",
            _describe_doc(image="repo@sha256:def"),
        )


def test_compare_rejects_environment_mismatch() -> None:
    with pytest.raises(RebindError, match="container definitions differ"):
        compare_state_to_live(
            _state_doc(env={"A": "1"}),
            "aws_ecs_task_definition.mcp",
            _describe_doc(env={"A": "2"}),
        )


def test_compare_rejects_arn_or_revision_mismatch() -> None:
    describe = _describe_doc()
    describe["taskDefinition"]["taskDefinitionArn"] = ARN.replace(":86", ":87")
    describe["taskDefinition"]["revision"] = 87
    with pytest.raises(RebindError, match="state arn"):
        compare_state_to_live(_state_doc(), "aws_ecs_task_definition.mcp", describe)


def test_compare_rejects_missing_address() -> None:
    with pytest.raises(RebindError, match="not found in state"):
        compare_state_to_live(_state_doc(), "aws_ecs_task_definition.other", _describe_doc())


# ── binding（precheck した世界 == apply する世界）────────────────────────


def _binding() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mapping_sha256": "a" * 64,
        "git_head": "b" * 40,
        "git_tree_clean": True,
        "state_lineage": "11111111-2222-3333-4444-555555555555",
        "state_serial": 100,
        "state_sha256": "c" * 64,
        "aws_account": "718959508629",
        "aws_principal_arn": (
            "arn:aws:sts::718959508629:assumed-role/"
            "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
        ),
        "targets_count": 6,
    }


def test_binding_accepts_the_identical_world() -> None:
    compare_binding(_binding(), copy.deepcopy(_binding()))


def test_binding_covers_every_declared_field() -> None:
    for field in BOUND_FIELDS:
        observed = copy.deepcopy(_binding())
        observed[field] = "MUTATED" if field not in ("state_serial", "targets_count") else -1
        with pytest.raises(RebindError):
            compare_binding(_binding(), observed)


def test_binding_rejects_root_even_when_both_sides_match() -> None:
    recorded = _binding()
    recorded["aws_principal_arn"] = "arn:aws:iam::718959508629:root"
    with pytest.raises(RebindError, match="root"):
        compare_binding(recorded, copy.deepcopy(recorded))


def test_approval_is_bound_to_the_precheck_binding() -> None:
    assert expected_approval("f" * 64) == f"{APPROVE_TOKEN}:{'f' * 16}"
    assert expected_approval("a" * 64) != expected_approval("b" * 64)


# ── guard 側の実行順序契約 ────────────────────────────────────────────────


def _guard_rebind_section() -> str:
    body = GUARD.read_text(encoding="utf-8")
    return body[body.index("REBIND_MAPPING=") : body.index('COMMAND="${1:-}"')]


def _apply_loop() -> str:
    section = _guard_rebind_section()
    return section[section.index("rebind_apply()") :]


def test_rebind_modes_are_dispatched_and_allowlisted() -> None:
    body = GUARD.read_text(encoding="utf-8")
    assert "  state-rebind-precheck)" in body
    assert "  state-rebind-apply)" in body
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "|state-rebind-precheck|state-rebind-apply)" in bootstrap


def test_rebind_reuses_the_adopt_identity_and_outdir_guards() -> None:
    """第二実装の禁止: identity 検査と repo外 out-dir 検査は adopt と同一関数を使う。"""
    section = _guard_rebind_section()
    assert section.count("adopt_trusted_principal_arn") == 2  # precheck + apply
    assert section.count("adopt_assert_out_dir_outside_repo") == 2


def test_rebind_mapping_path_is_fixed_to_the_repo_ledger() -> None:
    """mapping は引数で差し替えられない（別 mapping の注入を防ぐ）。"""
    section = _guard_rebind_section()
    assert 'REBIND_MAPPING="$REPO_ROOT/infra/deploy/state_rebind_targets.json"' in section
    body = GUARD.read_text(encoding="utf-8")
    dispatch = body[body.index("  state-rebind-precheck)") :]
    assert "--mapping" not in dispatch[: dispatch.index("esac")]


def test_precheck_backs_up_state_before_anything_mutates() -> None:
    section = _guard_rebind_section()
    precheck = section[section.index("rebind_precheck()") : section.index("rebind_apply()")]
    assert "state pull" in precheck
    assert "state rm" not in precheck  # precheck は read-only


RM_CMD = 'terraform -chdir="$TF_DIR" state rm'
IMPORT_CMD = 'terraform -chdir="$TF_DIR" import'


def test_apply_verifies_binding_and_takes_the_lock_before_any_state_rm() -> None:
    loop = _apply_loop()
    assert loop.index('"$REBIND_HELPER" verify') < loop.index(RM_CMD)
    assert loop.index("acquire_deployment_lock") < loop.index(RM_CMD)


def test_apply_rebinds_one_address_at_a_time_with_immediate_import_and_verify() -> None:
    """条件1: rm → 即 import → verify を 1 address 内で完結してから次へ。

    ループ本体に rm と import と compare が 1 回ずつ現れ、rm の直後（他の rm を挟まず）
    に import が来ることを固定する。「全 rm → 全 import」の一括方式は必ず赤になる。
    """
    loop = _apply_loop()
    body = loop[loop.index("while IFS=") :]
    assert body.count(RM_CMD) == 1
    assert body.count(IMPORT_CMD) == 1
    assert body.count('"$REBIND_HELPER" compare') == 1
    assert body.index(RM_CMD) < body.index(IMPORT_CMD) < body.index('"$REBIND_HELPER" compare')


def test_apply_reverifies_the_consumer_pointer_right_before_each_rm() -> None:
    """review 中に live が動いた場合は STALE として rm 前に停止する（動的追随はしない）。"""
    loop = _apply_loop()
    body = loop[loop.index("while IFS=") :]
    assert body.index("rebind_assert_consumer_points_at_target") < body.index(RM_CMD)


def test_every_failure_path_stops_instead_of_continuing() -> None:
    """条件6: 途中失敗で次の resource へ進まない（rm/import/compare すべて die 直結）。"""
    loop = _apply_loop()
    body = loop[loop.index("while IFS=") :]
    for step in (RM_CMD, IMPORT_CMD, '"$REBIND_HELPER" compare'):
        after = body[body.index(step) :]
        assert "die" in after[: after.index("\n\n")] or "die" in after[:400]


def test_consumer_pointer_check_rejects_mismatch_not_adopts_it() -> None:
    section = _guard_rebind_section()
    checker = section[
        section.index("rebind_assert_consumer_points_at_target()") : section.index(
            "rebind_precheck()"
        )
    ]
    assert "STALE MAPPING" in checker
    assert "die" in checker
