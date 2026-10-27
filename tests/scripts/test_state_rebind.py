"""PR2-A0.4 state rebind の契約テスト。

「本番は正しく state だけ古い」とき、live を再デプロイせず state の binding だけを
live の exact revision へ付け替える唯一の経路を fail-closed で固定する。
正式契約: AWS managed application resources mutation = 0 / Terraform remote state mutation only。
各ガードは変異で壊すと赤くなることを実証する（リポジトリ規約）。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"
BOOTSTRAP = ROOT / "infra/deploy/bootstrap_runtime_session.sh"
MAPPING = ROOT / "infra/deploy/state_rebind_targets.json"

sys.path.insert(0, str(ROOT / "infra/deploy"))

from state_rebind import (  # noqa: E402
    ALIGNMENT_POINTERS,
    APPROVE_TOKEN,
    BOUND_FIELDS,
    RebindError,
    alignment_candidate,
    build_alignment_plan,
    compare_binding,
    compare_state_to_live,
    expected_approval,
    load_targets,
    verify_pushed_state,
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


def test_production_mapping_is_frozen_with_the_approved_seven_targets() -> None:
    """freeze 後に確定した production mapping の改竄封印。

    値の由来: PRODUCTION DEPLOYMENT FREEZE（2026-08-20 18:15 JST）後の fresh 再解決。
    approved evidence = mcp 系 5 件は署名検証済み release receipt、tiktok_acquire:25 は
    human gate 明示承認（freeze 窓限定）。ここを変える場合は freeze 境界の引き直しと
    human gate の再承認が必要。
    """
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    # 2026-09-15: rebind #4 用に fresh 再解決（mcp 便 r23 で mcp と consumer 4 つを揃えた直後・OpenClaw を追加）
    assert raw["frozen_at"] == "2026-09-15T10:15:00Z"
    expected = {
        "aws_ecs_task_definition.mcp": "teamagent-dev-mcp:104",
        "aws_ecs_task_definition.connect_web[0]": "teamagent-dev-connect-web:82",
        "aws_ecs_task_definition.morning_digest[0]": "teamagent-dev-morning-digest:61",
        "aws_ecs_task_definition.canary[0]": "teamagent-dev-canary:30",
        "aws_ecs_task_definition.ingest[0]": "teamagent-dev-ingest:62",
        "aws_ecs_task_definition.tiktok_acquire[0]": "teamagent-dev-tiktok-acquire:31",
        "aws_ecs_task_definition.openclaw": "teamagent-dev-openclaw:47",
    }
    actual = {t["address"]: t["target_arn"].split("/")[-1] for t in raw["targets"]}
    assert actual == expected
    # x_buzz_worker は state == live (:1) のため対象外（含まれていたら誤り）
    assert "aws_ecs_task_definition.x_buzz_worker" not in actual
    # loader の厳密検証も通ること（consumer 宣言含む）
    assert len(load_targets(MAPPING, require_targets=True)) == 7


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


def test_compare_resolves_count_indexed_addresses() -> None:
    """count リソース（address[0]）は base 名 + index_key=0 で state instance を解決する。"""
    doc = _state_doc()
    doc["resources"][0]["instances"][0]["index_key"] = 0
    compare_state_to_live(doc, "aws_ecs_task_definition.mcp[0]", _describe_doc())
    # index 無し address で indexed instance を引いたら拒否（取り違え防止）
    with pytest.raises(RebindError, match="indexed"):
        compare_state_to_live(doc, "aws_ecs_task_definition.mcp", _describe_doc())


def test_compare_rejects_missing_address() -> None:
    with pytest.raises(RebindError, match="not found in state"):
        compare_state_to_live(_state_doc(), "aws_ecs_task_definition.other", _describe_doc())


# ── binding（precheck した世界 == apply する世界）────────────────────────


def _binding() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mapping_sha256": "a" * 64,
        "alignment_sha256": "d" * 64,
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


# ── state pull の非決定性への耐性（2026-08-20 実測: check_results の並びが揺れる） ──

from state_rebind import _state_canonical_sha256  # noqa: E402


def test_state_sha_ignores_check_results_ordering(tmp_path: Path) -> None:
    """check_results の並び替えだけでは sha が変わらない（apply 照合の誤爆防止）。"""
    base = {"serial": 187, "lineage": "x", "resources": [{"a": 1}]}
    p1 = tmp_path / "s1.json"
    p2 = tmp_path / "s2.json"
    p1.write_text(json.dumps({**base, "check_results": [{"c": 1}, {"c": 2}]}))
    p2.write_text(json.dumps({"check_results": [{"c": 2}, {"c": 1}], **base}))
    assert _state_canonical_sha256(p1) == _state_canonical_sha256(p2)


def test_state_sha_still_detects_resource_tampering(tmp_path: Path) -> None:
    """resource 実体の改変は引き続き検出される（緩めすぎ防止の変異対）。"""
    p1 = tmp_path / "s1.json"
    p2 = tmp_path / "s2.json"
    p1.write_text(json.dumps({"serial": 187, "resources": [{"a": 1}]}))
    p2.write_text(json.dumps({"serial": 187, "resources": [{"a": 2}]}))
    assert _state_canonical_sha256(p1) != _state_canonical_sha256(p2)


def _alignment_fixture(
    tmp_path: Path, short_address: str = "mcp"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state: dict[str, Any] = {"version": 4, "serial": 242, "lineage": "unchanged", "resources": []}
    family_suffix, kind, name_suffix, pointer_address = ALIGNMENT_POINTERS[short_address]
    family = f"teamagent-dev-{family_suffix}"
    old_arn = ARN.replace("teamagent-dev-mcp:86", f"{family}:41")
    new_arn = old_arn.removesuffix(":41") + ":86"
    consumer = {"kind": kind, "name": f"teamagent-dev-{name_suffix}"}
    if kind == "ecs-service":
        consumer["cluster"] = "teamagent-dev"
        pointer = {
            "name": consumer["name"],
            "cluster": "teamagent-dev",
            "task_definition": old_arn,
            "wait_for_steady_state": True,
        }
    elif kind == "events-rule":
        pointer = {
            "rule": consumer["name"],
            "target_id": "morning" if family_suffix == "morning-digest" else "tf-canary",
            "ecs_target": [{"task_definition_arn": old_arn, "task_count": 1}],
        }
    else:
        pointer = {
            "function_name": consumer["name"],
            "environment": [{"variables": {"TASKDEF_ARN": old_arn, "KEEP": "same"}}],
        }

    def resource(address: str, attributes: dict[str, Any]) -> dict[str, Any]:
        resource_type, name = address.split(".")
        instance: dict[str, Any] = {"attributes": attributes, "private": "preserve-me"}
        if name.endswith("[0]"):
            name = name[:-3]
            instance["index_key"] = 0
        return {"mode": "managed", "type": resource_type, "name": name, "instances": [instance]}

    address = f"aws_ecs_task_definition.{short_address}"
    state["resources"].append(resource(address, {"arn": new_arn, "family": family}))
    pointer_resource = resource(pointer_address, pointer)
    state["resources"].append(pointer_resource)
    for archive_name, function_name in (
        ("tiktok_dispatch", "teamagent-dev-tiktok-acquire-dispatch"),
        ("x_dispatch", "teamagent-dev-x-buzz-dispatch"),
    ):
        source = tmp_path / f"infra/terraform/lambda/{archive_name}/handler.py"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"def handler(event, context): return event\n")
        archive = tmp_path / f"infra/terraform/build/{archive_name}.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("handler.py", source.read_bytes())
        dispatch_address = f"aws_lambda_function.{archive_name}[0]"
        attributes = {
            "function_name": function_name,
            "source_code_hash": "old hash",
            "filename": f"build/{archive_name}.zip",
            "environment": [{"variables": {"KEEP": "same"}}],
        }
        if dispatch_address == pointer_address:
            pointer.update(
                {key: value for key, value in attributes.items() if key != "environment"}
            )
        else:
            state["resources"].append(resource(dispatch_address, attributes))
    return state, [
        {"address": address, "family": family, "target_arn": new_arn, "consumer": consumer}
    ]


@pytest.mark.parametrize("short_address", list(ALIGNMENT_POINTERS))
def test_alignment_changes_only_reviewed_leaves_and_preserves_provider_metadata(
    tmp_path: Path, short_address: str
) -> None:
    from state_rebind import _leaf_parent, _state_attributes

    state, targets = _alignment_fixture(tmp_path, short_address)
    reviewed = build_alignment_plan(state, targets, tmp_path)
    candidate = alignment_candidate(state, targets, tmp_path, reviewed)
    restored = copy.deepcopy(candidate)
    for change in reviewed["changes"]:
        parent = _leaf_parent(_state_attributes(restored, change["address"]), change["path"])
        assert parent[change["path"][-1]] == change["after"]
        parent[change["path"][-1]] = change["before"]
    restored["serial"] -= 1
    assert restored == state
    assert candidate["lineage"] == state["lineage"]
    assert len(reviewed["changes"]) == 3


@pytest.mark.parametrize(
    "mutation", ["pointer", "hash", "zip", "source", "arbitrary_path", "target", "consumer"]
)
def test_alignment_refuses_every_unreviewed_change(tmp_path: Path, mutation: str) -> None:
    state, targets = _alignment_fixture(tmp_path)
    reviewed = build_alignment_plan(state, targets, tmp_path)
    if mutation == "pointer":
        state["resources"][1]["instances"][0]["attributes"]["task_definition"] = ARN
    elif mutation == "hash":
        state["resources"][2]["instances"][0]["attributes"]["source_code_hash"] = "changed"
    elif mutation == "zip":
        archive = tmp_path / "infra/terraform/build/tiktok_dispatch.zip"
        with zipfile.ZipFile(archive, "a") as zipped:
            zipped.writestr("extra.py", "unexpected")
    elif mutation == "source":
        (tmp_path / "infra/terraform/lambda/tiktok_dispatch/handler.py").write_text("changed")
    elif mutation == "arbitrary_path":
        reviewed["changes"][0]["path"] = ["wait_for_steady_state"]
    elif mutation == "target":
        targets[0]["target_arn"] = targets[0]["target_arn"].removesuffix(":86") + ":87"
    else:
        targets[0]["consumer"]["name"] = "teamagent-dev-other"
    with pytest.raises(RebindError):
        alignment_candidate(state, targets, tmp_path, reviewed)


def test_alignment_rejects_old_unindexed_openclaw_mapping(tmp_path: Path) -> None:
    state, targets = _alignment_fixture(tmp_path, "openclaw[0]")
    targets[0]["address"] = "aws_ecs_task_definition.openclaw"
    with pytest.raises(RebindError, match="current consumer registry"):
        build_alignment_plan(state, targets, tmp_path)


def test_alignment_verification_allows_backend_serial_increment_only(tmp_path: Path) -> None:
    state, targets = _alignment_fixture(tmp_path)
    candidate = alignment_candidate(
        state, targets, tmp_path, build_alignment_plan(state, targets, tmp_path)
    )
    observed = copy.deepcopy(candidate)
    observed["serial"] += 1
    verify_pushed_state(candidate, observed)
    observed["lineage"] = "changed"
    with pytest.raises(RebindError, match="differs"):
        verify_pushed_state(candidate, observed)


def test_alignment_fixed_consumers_match_the_independent_registry() -> None:
    registry = json.loads((ROOT / "infra/codebuild/image_deployment_consumers.json").read_text())
    assert len(ALIGNMENT_POINTERS) == len(registry["consumers"]) == 8
    for consumer in registry["consumers"]:
        short_address = consumer["terraform_task_definition_address"].removeprefix(
            "aws_ecs_task_definition."
        )
        family, kind, name, _ = ALIGNMENT_POINTERS[short_address]
        assert f"teamagent-dev-{family}" == consumer["ecs_family"]
        if consumer["consumer_id"] == "ingest":
            assert (kind, name) == ("lambda-env", "ingest-dispatch")
        else:
            assert (
                kind
                == {
                    "ecs_service": "ecs-service",
                    "eventbridge_rule_ecs_target": "events-rule",
                    "lambda_taskdef_arn_environment": "lambda-env",
                }[consumer["activator"]["type"]]
            )
            assert f"teamagent-dev-{name}" == consumer["activator"]["identity"]


def test_alignment_candidate_satisfies_the_real_exact_eight_manifest_builder(
    tmp_path: Path,
) -> None:
    from tests.scripts.test_terraform_runtime_guard import _fake_state_from_plan, _safe_plan

    sys.path.insert(0, str(ROOT / "infra/terraform"))
    from image_release_context import ContextError, build_sync_consumer_manifest
    from state_rebind import _leaf_parent, _state_attributes

    _alignment_fixture(tmp_path)
    state = _fake_state_from_plan(_safe_plan())
    targets = []
    for short_address, (family, kind, name, pointer_address) in ALIGNMENT_POINTERS.items():
        address = f"aws_ecs_task_definition.{short_address}"
        target_arn = _state_attributes(state, address)["arn"]
        consumer = {"kind": kind, "name": f"teamagent-dev-{name}"}
        if kind == "ecs-service":
            consumer["cluster"] = "teamagent-dev"
        targets.append(
            {
                "address": address,
                "family": f"teamagent-dev-{family}",
                "target_arn": target_arn,
                "consumer": consumer,
            }
        )
        attributes = _state_attributes(state, pointer_address)
        paths: dict[str, list[Any]] = {
            "ecs-service": ["task_definition"],
            "lambda-env": ["environment", 0, "variables", "TASKDEF_ARN"],
            "events-rule": ["ecs_target", 0, "task_definition_arn"],
        }
        path = paths[kind]
        _leaf_parent(attributes, path)[path[-1]] = target_arn.rsplit(":", 1)[0] + ":999"
    with pytest.raises(ContextError, match="activation points at another"):
        build_sync_consumer_manifest(state)
    reviewed = build_alignment_plan(state, targets, tmp_path)
    candidate = alignment_candidate(state, targets, tmp_path, reviewed)
    assert len(build_sync_consumer_manifest(candidate)["consumers"]) == 8


@pytest.mark.parametrize(
    "failure", ["none", "binding", "compare", "exact8", "zip", "lock", "state", "push"]
)
def test_guard_alignment_stops_before_push_on_failed_checks(tmp_path: Path, failure: str) -> None:
    """Execute the actual guard function using fake AWS/Terraform and helper commands."""
    state, targets = _alignment_fixture(tmp_path)
    reviewed = build_alignment_plan(state, targets, tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    for name, value in (
        ("state-backup.json", state),
        ("rebind-binding.json", {}),
        ("alignment-plan.json", reviewed),
    ):
        (out / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    (out / "rebind-plan.tsv").write_text("reviewed")
    mapping = _mapping_file(tmp_path, targets)
    helper = tmp_path / "fake-helper.py"
    helper.write_text("""import json, os, pathlib, shutil, sys
command = sys.argv[1]
failure = os.environ["FAILURE"]
args = sys.argv[2:]
def value(flag): return pathlib.Path(args[args.index(flag) + 1])
if command == "verify" and failure == "binding": sys.exit(1)
if command == "compare" and failure == "compare": sys.exit(1)
if command == "same-state" and failure == "state": sys.exit(1)
if command == "build-sync-consumer-manifest" and failure == "exact8": sys.exit(1)
if command == "alignment-plan":
    output = value("--out")
    if failure == "zip" and output.name == "alignment-before-push.json": sys.exit(1)
    shutil.copyfile(os.environ["REVIEWED"], output)
elif command == "alignment-candidate": shutil.copyfile(value("--state-json"), value("--out"))
elif command == "build-sync-consumer-manifest": value("--output").write_text("{}")
""")
    shell = tmp_path / "run.sh"
    shell.write_text(
        """set -euo pipefail
die() { echo "$*" >&2; exit 1; }
rebind_require_helpers() { :; }
adopt_assert_out_dir_outside_repo() { :; }
ensure_tmp() { mkdir -p "$TMP_ROOT"; }
secure_existing_file() { echo "$1"; }
adopt_trusted_principal_arn() { echo "arn:approved"; }
acquire_deployment_lock() { echo acquire >> "$CALLS"; }
release_deployment_lock() { echo release >> "$CALLS"; }
rebind_assert_consumer_points_at_target() { :; }
rebind_assert_dispatcher_hashes() { :; }
sha256_file() { command python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
terraform() {
  echo "terraform $*" >> "$CALLS"
  case "$2 $3" in
    "state pull") cat "$OUT/state-backup.json" ;;
    "state push") [ "$FAILURE" != push ] ;;
  esac
}
aws_cli() {
  if [ "$1 $2" = "dynamodb get-item" ]; then
    command python3 -c 'import json,os,time; print(json.dumps({"Item":{"OwnerToken":{"S":"wrong" if os.environ["FAILURE"] == "lock" else "owner"},"ExpiresAt":{"N":str(int(time.time())+7200)}}}))'
  else echo '{}'; fi
}
"""
        + _apply_loop()
        + """
rebind_apply "$OUT" "$OUT/state-backup.json" "approved"
"""
    )
    env = {
        **os.environ,
        "REPO_ROOT": str(tmp_path),
        "TF_DIR": str(tmp_path),
        "TMP_ROOT": str(tmp_path / "private"),
        "REBIND_MAPPING": str(mapping),
        "REBIND_HELPER": str(helper),
        "IMAGE_CONTEXT_HELPER": str(helper),
        "OUT": str(out),
        "REVIEWED": str(out / "alignment-plan.json"),
        "CALLS": str(tmp_path / "calls"),
        "FAILURE": failure,
        "EXPECTED_ACCOUNT_ID": "718959508629",
        "DEPLOYMENT_LOCK_ID": "lock",
        "DEPLOYMENT_LOCK_OWNER": "owner",
    }
    result = subprocess.run(["bash", str(shell)], env=env, capture_output=True, text=True)
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    assert (result.returncode == 0) == (failure == "none"), result.stdout + result.stderr
    assert ("state push" in calls) == (failure in {"none", "push"})
    assert "-force" not in calls
    if failure == "binding":
        assert "state rm" not in calls
    if failure != "none":
        assert "release" not in calls


# ── P0: rebind session policy の least-privilege 契約 ─────────────────────────
#
# 2026-08-21 実測: 読み取り列挙が不足した session policy は terraform import を 403 で
# 殺し「rm 済み・import 未完」の中間 state を作る。復旧に使った Allow-all + Deny 型
# （v2）は恒久化・再利用禁止（同日ユーザー裁定）。canonical policy はここで機械固定する。

from state_rebind import (  # noqa: E402
    APPLICATION_WRITE_DENY_ACTIONS,
    SESSION_POLICY_EXPECTED_STATEMENTS,
    SESSION_POLICY_PATH,
    assert_session_policy_least_privilege,
    evaluate_session_policy,
    load_session_policy,
    validate_session_policy_file,
)

RUNBOOK = ROOT / "docs/runbooks/state_rebind.md"


def _policy_doc() -> dict[str, Any]:
    return load_session_policy(SESSION_POLICY_PATH)


def test_session_policy_file_satisfies_the_least_privilege_contract() -> None:
    """repo の canonical policy がそのまま契約を満たす（CLI と同経路）。"""
    validate_session_policy_file(SESSION_POLICY_PATH)


def test_session_policy_contains_no_wildcard_action_anywhere() -> None:
    """`Allow *` を含む action ワイルドカードの混入を全 statement で拒否（v2 再流入防止）。"""
    doc = _policy_doc()
    for statement in doc["Statement"]:
        actions = statement["Action"]
        for action in actions if isinstance(actions, list) else [actions]:
            assert "*" not in action, f"action ワイルドカード検出: {action}"


def test_session_policy_rejects_allow_star_mutation() -> None:
    """Allow * を注入すると契約検証が赤くなる（committed mutation対）。"""
    doc = _policy_doc()
    doc["Statement"][2]["Action"] = "*"
    with pytest.raises(RebindError, match=r"ワイルドカード|一致しません"):
        assert_session_policy_least_privilege(doc)


@pytest.mark.parametrize("action", sorted(APPLICATION_WRITE_DENY_ACTIONS))
def test_session_policy_explicitly_denies_each_application_write(action: str) -> None:
    """application write は 1 つずつ explicit deny（simulate 相当の決定論評価）。"""
    assert evaluate_session_policy(_policy_doc(), action, "*") == "deny"


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        # v1 実績: state backend / lock / 検証読み
        ("s3:GetObject", "arn:aws:s3:::teamagent-tfstate-718959508629/teamagent/terraform.tfstate"),
        ("s3:PutObject", "arn:aws:s3:::teamagent-tfstate-718959508629/teamagent/terraform.tfstate"),
        ("dynamodb:PutItem", "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock"),
        ("ecs:DescribeTaskDefinition", "*"),
        ("ecs:DescribeServices", "*"),
        ("events:ListTargetsByRule", "*"),
        ("lambda:GetFunctionConfiguration", "*"),
        ("sts:GetCallerIdentity", "*"),
        # 403 実測: terraform import の root data source 評価
        ("ec2:DescribeImages", "*"),
        ("ec2:DescribeVpcs", "*"),
        ("kms:ListAliases", "*"),
        ("iam:GetUser", "arn:aws:iam::718959508629:user/AIIAdev"),
        (
            "secretsmanager:DescribeSecret",
            "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/google_oauth-AbC123",
        ),
        # 静的導出: data.aws_vpc.default.id 依存の第二波
        ("ec2:DescribeSubnets", "*"),
        ("ec2:DescribeRouteTables", "*"),
    ],
)
def test_session_policy_allows_each_required_read(action: str, resource: str) -> None:
    """rebind / import が必要とする read が代表 resource で通る。"""
    assert evaluate_session_policy(_policy_doc(), action, resource) == "allow"


def test_session_policy_scopes_iam_and_secrets_reads_to_exact_resources() -> None:
    """iam:GetUser と DescribeSecret は観測された resource の外に出ない。"""
    doc = _policy_doc()
    assert (
        evaluate_session_policy(doc, "iam:GetUser", "arn:aws:iam::718959508629:user/other")
        == "implicit-deny"
    )
    assert (
        evaluate_session_policy(
            doc,
            "secretsmanager:DescribeSecret",
            "arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:other/secret-XyZ",
        )
        == "implicit-deny"
    )


def test_session_policy_grants_no_write_outside_state_backend_and_lock() -> None:
    """未列挙 write（deny 表に無いものも含む）が implicit-deny に落ちる。"""
    doc = _policy_doc()
    for action in (
        "ecs:RunTask",
        "events:DeleteRule",
        "lambda:UpdateFunctionCode",
        "s3:DeleteObject",
        "iam:PutRolePolicy",
        "secretsmanager:GetSecretValue",
        "codebuild:UpdateProject",
    ):
        assert evaluate_session_policy(doc, action, "*") == "implicit-deny", action


@pytest.mark.parametrize(
    "sid", sorted(set(SESSION_POLICY_EXPECTED_STATEMENTS) - {"DenyApplicationWrites"})
)
def test_session_policy_detects_each_dropped_required_read(sid: str) -> None:
    """必要 read を 1 action 削ると契約検証が赤くなる（committed mutation対）。"""
    doc = _policy_doc()
    for statement in doc["Statement"]:
        if statement.get("Sid") == sid:
            statement["Action"] = list(statement["Action"])[:-1] or ["sts:GetCallerIdentity"]
    with pytest.raises(RebindError, match="一致しません"):
        assert_session_policy_least_privilege(doc)


def test_session_policy_detects_dropped_or_weakened_deny() -> None:
    """Deny の削除・action 削減はどちらも赤くなる。"""
    doc = _policy_doc()
    doc["Statement"] = [s for s in doc["Statement"] if s.get("Sid") != "DenyApplicationWrites"]
    with pytest.raises(RebindError, match="一致しません"):
        assert_session_policy_least_privilege(doc)

    doc = _policy_doc()
    for statement in doc["Statement"]:
        if statement.get("Sid") == "DenyApplicationWrites":
            statement["Action"] = [a for a in statement["Action"] if a != "ecs:UpdateService"]
    with pytest.raises(RebindError, match="一致しません"):
        assert_session_policy_least_privilege(doc)


def test_session_policy_detects_broadened_backend_resources() -> None:
    """state backend の resource を広げる変異（bucket/* 等）が赤くなる。"""
    doc = _policy_doc()
    for statement in doc["Statement"]:
        if statement.get("Sid") == "StateBackend":
            statement["Resource"] = ["arn:aws:s3:::teamagent-tfstate-718959508629/*"]
    with pytest.raises(RebindError, match="Resource"):
        assert_session_policy_least_privilege(doc)


def test_session_policy_write_surface_is_exactly_backend_and_lock() -> None:
    """Allow 側の非 read 動詞は s3:PutObject / dynamodb:PutItem / dynamodb:DeleteItem だけ。"""
    doc = _policy_doc()
    writes = set()
    for statement in doc["Statement"]:
        if statement.get("Effect") != "Allow":
            continue
        for action in statement["Action"]:
            verb = action.split(":", 1)[1]
            if not verb.startswith(("Describe", "Get", "List")) and action != "kms:Decrypt":
                writes.add(action)
    assert writes == {"s3:PutObject", "dynamodb:PutItem", "dynamodb:DeleteItem"}


def test_runbook_points_operators_at_the_canonical_policy() -> None:
    """runbook は canonical policy と検証 CLI を参照し、v2 形の再利用を禁止している。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "infra/deploy/state_rebind_session_policy.json" in text
    assert "validate-session-policy" in text
    assert "再利用しない" in text
    # 旧 inline policy（列挙不足で 403 事故を起こした形）が normative に残っていないこと
    assert '"Sid": "ReadOnlyVerify"' not in text
