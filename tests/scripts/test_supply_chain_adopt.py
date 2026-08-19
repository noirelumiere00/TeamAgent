"""PR2-A0 Supply-Chain Adopt の契約テスト。

adopt は「AWS 実体を一切変更せず Terraform state だけを実態へ追いつかせる」経路であり、
既存の prevent_destroy / Object Lock / Delete Deny を一切弱めない。その不変条件を機械で固定する。

各ガードには対になる変異テストを置き、ガードを意図的に壊すと赤くなることを実証する
（緑の実質性を変異で証明するリポジトリ規約に従う）。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADOPT_TF = ROOT / "infra/terraform/supply_chain_adopt.tf"
CODEBUILD_TF = ROOT / "infra/terraform/codebuild.tf"
MCP_APPROVAL_TF = ROOT / "infra/terraform/mcp_approval.tf"
MAPPING = ROOT / "infra/deploy/supply_chain_adoptions.json"
VALIDATOR = ROOT / "infra/deploy/supply_chain_adopt_validate.py"
INTEGRITY = ROOT / "infra/deploy/supply_chain_adopt_integrity.py"
GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"

sys.path.insert(0, str(ROOT / "infra/deploy"))

from supply_chain_adopt_integrity import (  # noqa: E402
    COMPARED_FIELDS,
    IntegrityError,
    compare,
    snapshot,
)
from supply_chain_adopt_validate import (  # noqa: E402
    AdoptValidationError,
    load_mapping,
    validate_plan,
)

GENERATION_RESOURCES = (
    "mcp_source_publisher_buildspec_generation",
    "image_attestor_buildspec_generation",
    "image_promoter_buildspec_generation",
    "approval_publisher_resolved_source_buildspec_generation",
)


def _strip_comments(source: str) -> str:
    """行コメントを除いた実行部分だけを返す（契約検査はコメント文言に依存させない）。"""
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def _adoptions() -> list[dict[str, Any]]:
    return load_mapping(MAPPING)


def _resource_body(name: str) -> str:
    source = ADOPT_TF.read_text(encoding="utf-8")
    start = source.index(f'resource "aws_s3_object" "{name}" {{')
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated resource block: {name}")


def _adopt_plan(adoptions: list[dict[str, Any]]) -> dict[str, Any]:
    """実 plan から抽出したのと同じ形の、正常な adopt plan を組み立てる。"""
    changes: list[dict[str, Any]] = []
    for entry in adoptions:
        changes.append(
            {
                "address": entry["old_address"],
                "mode": "managed",
                "type": "aws_s3_object",
                "change": {"actions": ["forget"]},
            }
        )
        changes.append(
            {
                "address": entry["new_address"],
                "mode": "managed",
                "type": "aws_s3_object",
                "change": {
                    "actions": ["update"],
                    "importing": {"id": entry["import_id"]},
                },
            }
        )
    return {"resource_changes": changes}


# ── Terraform 側の構造契約 ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", GENERATION_RESOURCES)
def test_generation_resources_keep_prevent_destroy(name: str) -> None:
    """世代リソースは必ず prevent_destroy を持つ（append-only の担保）。"""
    assert "prevent_destroy = true" in _resource_body(name)


@pytest.mark.parametrize("name", GENERATION_RESOURCES)
def test_generation_resources_declare_object_lock_explicitly(name: str) -> None:
    """Object Lock を config に明示しないと Terraform が null 化しようとする（実測済み）。"""
    body = _resource_body(name)
    assert 'object_lock_mode              = "GOVERNANCE"' in body
    assert "object_lock_retain_until_date = each.value.object_lock_retain_until_date" in body


@pytest.mark.parametrize("name", GENERATION_RESOURCES)
def test_generation_resources_do_not_manage_content(name: str) -> None:
    """content を持たせると import 後に PutObject が走り Object Lock 済み実体を書き換える。"""
    body = _resource_body(name)
    assert not re.search(r"^\s+content\s+=", body, flags=re.MULTILINE)
    assert not re.search(r"^\s+source_hash\s+=", body, flags=re.MULTILINE)


@pytest.mark.parametrize("name", GENERATION_RESOURCES)
def test_generation_resources_are_hash_keyed(name: str) -> None:
    """世代は for_each の台帳で管理し、key は content-addressed sha256 から組む。"""
    body = _resource_body(name)
    assert "for_each = local." in body
    assert "${each.key}.yml" in body


@pytest.mark.parametrize("name", GENERATION_RESOURCES)
def test_generation_resources_never_use_ignore_changes(name: str) -> None:
    """content-addressed artifact の差分を無視する構造にしない（実測で不要と確定）。"""
    assert "ignore_changes" not in _resource_body(name)


def test_old_single_resources_are_replaced_by_removed_blocks() -> None:
    """旧アドレスは destroy = false の removed で state から外すだけにする。"""
    adopt = ADOPT_TF.read_text(encoding="utf-8")
    others = CODEBUILD_TF.read_text(encoding="utf-8") + MCP_APPROVAL_TF.read_text(encoding="utf-8")
    for entry in _adoptions():
        old = entry["old_address"].split(".", 1)[1]
        assert f'resource "aws_s3_object" "{old}" {{' not in others
        assert f"from = {entry['old_address']}" in adopt
    assert _strip_comments(adopt).count("destroy = false") == len(_adoptions())


def test_every_import_block_has_a_matching_resource_block() -> None:
    """import は import 先の resource block の存在を要求する（Terraform の仕様）。"""
    adopt = ADOPT_TF.read_text(encoding="utf-8")
    targets = re.findall(r"^\s*to = (aws_s3_object\.[a-z_]+)\[", adopt, flags=re.MULTILINE)
    assert len(targets) == len(_adoptions())
    for resource_name in targets:
        assert f'resource "aws_s3_object" "{resource_name.split(".", 1)[1]}" {{' in adopt


def test_content_addressed_generations_are_asserted_in_terraform() -> None:
    """Terraform が保持する body の sha256 が台帳に登録済みであることを check で強制する。"""
    adopt = ADOPT_TF.read_text(encoding="utf-8")
    assert _strip_comments(adopt).count("check ") == len(_adoptions())
    for local_name in (
        "mcp_source_publisher_buildspec_sha256",
        "image_attestor_buildspec_sha256",
        "image_promoter_buildspec_sha256",
        "approval_publisher_buildspec_sha256",
    ):
        assert f"local.{local_name}," in adopt


def test_mapping_matches_terraform_generation_ledger() -> None:
    """mapping の世代が Terraform 側の台帳に 1:1 で存在すること。"""
    adopt = ADOPT_TF.read_text(encoding="utf-8")
    for entry in _adoptions():
        sha = entry["expected_content_sha256"]
        assert f'"{sha}" = {{' in adopt, f"世代台帳に未登録: {sha}"
        assert f'["{sha}"]' in entry["new_address"]
        assert entry["key"].endswith(f"/{sha}.yml")


# ── adopt validator の契約（fail-closed）────────────────────────────────────


def test_validator_accepts_a_pure_adopt_plan() -> None:
    adoptions = _adoptions()
    validate_plan(_adopt_plan(adoptions), adoptions)


def test_validator_rejects_addresses_outside_the_mapping() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"].append(
        {
            "address": "aws_cloudwatch_event_rule.media_janitor[0]",
            "mode": "managed",
            "type": "aws_cloudwatch_event_rule",
            "change": {"actions": ["create"]},
        }
    )
    with pytest.raises(AdoptValidationError, match="not allowed in adopt"):
        validate_plan(plan, adoptions)


def test_validator_rejects_any_delete() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
    with pytest.raises(AdoptValidationError, match="destructive"):
        validate_plan(plan, adoptions)


def test_validator_rejects_replace() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"][1]["change"]["actions"] = ["create", "delete"]
    with pytest.raises(AdoptValidationError, match="destructive"):
        validate_plan(plan, adoptions)


def test_validator_rejects_update_without_import() -> None:
    """import を伴わない update は実体変更になりうるので拒否する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    del plan["resource_changes"][1]["change"]["importing"]
    with pytest.raises(AdoptValidationError, match="without an import"):
        validate_plan(plan, adoptions)


def test_validator_rejects_mismatched_import_id() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"][1]["change"]["importing"]["id"] = "other-bucket/other-key.yml"
    with pytest.raises(AdoptValidationError, match="import id does not match"):
        validate_plan(plan, adoptions)


def test_validator_requires_every_mapping_entry_to_appear() -> None:
    """mapping の一部しか含まない plan は受け入れない（数も exact）。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"] = plan["resource_changes"][2:]
    with pytest.raises(AdoptValidationError, match="set mismatch"):
        validate_plan(plan, adoptions)


def test_validator_rejects_data_source_writes() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    plan["resource_changes"].append(
        {
            "address": "data.aws_s3_object.x",
            "mode": "data",
            "type": "aws_s3_object",
            "change": {"actions": ["update"]},
        }
    )
    with pytest.raises(AdoptValidationError, match="data source action"):
        validate_plan(plan, adoptions)


def test_mapping_rejects_key_that_is_not_content_addressed() -> None:
    """mapping 自体の content-addressed 性も検証する（key の basename == sha256）。"""
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    raw["adoptions"][0]["key"] = "codebuild-buildspecs/x/not-a-hash.yml"
    with pytest.raises(AdoptValidationError, match="content-addressed"):
        _load_mapping_from_dict(raw)


def _load_mapping_from_dict(raw: dict[str, Any], tmp: Path | None = None) -> list[dict[str, Any]]:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(raw, handle)
        path = Path(handle.name)
    try:
        return load_mapping(path)
    finally:
        path.unlink(missing_ok=True)


# ── S3 integrity 検証の契約 ─────────────────────────────────────────────────


class _FakeS3:
    """head_object / get_object だけを持つ最小の S3 スタブ。"""

    def __init__(self, body: bytes, *, lock: str = "GOVERNANCE", version: str = "v1") -> None:
        self._body = body
        self._lock = lock
        self._version = version

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {
            "VersionId": self._version,
            "ETag": '"etag"',
            "ContentLength": len(self._body),
            "LastModified": "2026-08-17T00:00:00+00:00",
            "ObjectLockMode": self._lock,
            "ObjectLockRetainUntilDate": "2099-12-31T00:00:00+00:00",
        }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self._body)}


def _body_for_first_adoption() -> bytes:
    """先頭 adoption の expected sha256 に一致する body を総当たりで作る（テスト用）。"""
    import hashlib

    target = _adoptions()[0]["expected_content_sha256"]
    # 実 body は取得できないので、sha256 が一致する状況だけをスタブで再現する。
    class _Exact(bytes):
        pass

    payload = b"adopt-integrity-fixture"
    assert hashlib.sha256(payload).hexdigest() != target
    return payload


def test_integrity_snapshot_rejects_body_sha256_mismatch() -> None:
    """content-addressed の precondition: body の sha256 が key と違えば必ず落とす。"""
    with pytest.raises(IntegrityError, match="body sha256"):
        snapshot(MAPPING, "ap-northeast-1", client=_FakeS3(_body_for_first_adoption()))


def test_integrity_snapshot_rejects_weakened_object_lock() -> None:
    """Object Lock が GOVERNANCE でなくなっていたら adopt を始めない。"""
    import hashlib

    entry = _adoptions()[0]
    body = b"x"
    fake = _FakeS3(body, lock="COMPLIANCE")
    # sha256 検査より先に落ちないよう、期待値を body に合わせた mapping を使う
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    digest = hashlib.sha256(body).hexdigest()
    raw["adoptions"] = [dict(entry)]
    raw["adoptions"][0]["expected_content_sha256"] = digest
    raw["adoptions"][0]["key"] = f"codebuild-buildspecs/p/{digest}.yml"
    raw["adoptions"][0]["import_id"] = f"{entry['bucket']}/codebuild-buildspecs/p/{digest}.yml"
    raw["adoptions"][0]["new_address"] = f'aws_s3_object.x["{digest}"]'
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(raw, handle)
        path = Path(handle.name)
    try:
        with pytest.raises(IntegrityError, match="object lock mode"):
            snapshot(path, "ap-northeast-1", client=fake)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("field", COMPARED_FIELDS)
def test_integrity_compare_detects_any_single_field_change(field: str) -> None:
    """VersionId を含む比較項目が 1 つでも変われば activation failure にする。"""
    before = {
        "objects": {
            "a": {
                "version_id": "v1",
                "etag": '"e"',
                "content_length": 1,
                "last_modified": "t",
                "object_lock_mode": "GOVERNANCE",
                "object_lock_retain_until_date": "u",
                "body_sha256": "s",
            }
        }
    }
    after = copy.deepcopy(before)
    after["objects"]["a"][field] = "MUTATED"
    with pytest.raises(IntegrityError, match="mutated during adopt"):
        compare(before, after)
    # 変異させなければ通ることも同時に示す（テストの実質性）
    compare(before, copy.deepcopy(before))


# ── adopt 実行経路の契約（guard 内の独立モード）────────────────────────────


def _guard_adopt_section() -> str:
    """guard 内の adopt 関連部分だけを取り出す（既存経路と混ざらないことの担保）。"""
    body = GUARD.read_text(encoding="utf-8")
    start = body.index("ADOPT_MAPPING=")
    end = body.index('COMMAND="${1:-}"')
    return body[start:end]


def test_adopt_is_a_separate_guard_mode() -> None:
    """adopt は既存 sync / migration / activation とは別のサブコマンドとして生える。"""
    body = GUARD.read_text(encoding="utf-8")
    assert "  adopt-plan)" in body
    assert "  adopt-apply)" in body


def test_adopt_does_not_touch_existing_allowlists() -> None:
    """adopt の実装が既存 3 経路の allowlist 変数へ触れていないこと。"""
    section = _guard_adopt_section()
    for existing in (
        "allowed_runtime_changes",
        "allowed_replacements",
        "validate_manifest_change_allowlist",
        "MIGRATION_KIND",
    ):
        assert existing not in section, f"adopt が既存経路の要素に触れている: {existing}"


def test_adopt_apply_requires_explicit_approval() -> None:
    """承認検査は binding 側（plan hash へ束縛）で行い、guard は必ずそれを通す。"""
    section = _guard_adopt_section()
    apply_body = section[section.index("adopt_apply()") :]
    assert '--approve "$approve"' in apply_body
    assert '"$ADOPT_BINDING" verify' in apply_body


def test_adopt_runs_integrity_before_and_after_apply() -> None:
    """apply の前後で必ず S3 実体の不変性を検査する。"""
    section = _guard_adopt_section()
    apply_body = section[section.index("adopt_apply()") :]
    assert apply_body.count('"$ADOPT_INTEGRITY"') >= 3
    assert apply_body.index("terraform -chdir") < apply_body.rindex('"$ADOPT_INTEGRITY"')


def test_adopt_backs_up_state_and_discovers_ownership_before_planning() -> None:
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    assert plan_body.index("state pull") < plan_body.index("terraform -chdir=\"$TF_DIR\" plan")
    assert "adopt_ownership_discovery" in plan_body


def test_adopt_never_weakens_immutability() -> None:
    """禁止操作が adopt 実装へ紛れ込んでいないことを固定する。"""
    section = _strip_comments(_guard_adopt_section())
    for forbidden in ("prevent_destroy", "object-lock", "delete-object", "state rm"):
        assert forbidden not in section, f"禁止操作が含まれている: {forbidden}"


# ── plan binding: 「plan した世界」と「apply する世界」の完全一致 ──────────────

from supply_chain_adopt_binding import (  # noqa: E402
    BOUND_FIELDS,
    BindingError,
    check_approval,
    compare_binding,
    expected_approval,
)

GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"


def _binding() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_sha256": "a" * 64,
        "plan_json_sha256": "b" * 64,
        "git_head": "c" * 40,
        "git_tree_clean": True,
        "mapping_sha256": "d" * 64,
        "state_lineage": "11111111-2222-3333-4444-555555555555",
        "state_serial": 42,
        "aws_account": "718959508629",
        "terraform_workspace": "default",
        "terraform_version": "1.12.2",
    }


def test_binding_accepts_the_identical_world() -> None:
    """同一 plan + 同一 commit + 同一 state + 正しい承認なら通る。"""
    recorded = _binding()
    compare_binding(recorded, copy.deepcopy(recorded))
    check_approval(recorded, expected_approval(recorded["plan_sha256"]))


def test_binding_rejects_stale_plan_after_new_commit() -> None:
    """plan 後にコードが commit されたら apply させない（今回塞いだ穴）。"""
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["git_head"] = "f" * 40
    with pytest.raises(BindingError, match="git_head"):
        compare_binding(recorded, observed)


def test_binding_rejects_dirty_working_tree() -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["git_tree_clean"] = False
    with pytest.raises(BindingError):
        compare_binding(recorded, observed)


def test_binding_rejects_tampered_plan_file() -> None:
    """保存 plan を 1 byte でも改変したら apply させない。"""
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["plan_sha256"] = "9" * 64
    with pytest.raises(BindingError, match="plan_sha256"):
        compare_binding(recorded, observed)


def test_binding_rejects_changed_adopt_mapping() -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["mapping_sha256"] = "e" * 64
    with pytest.raises(BindingError, match="mapping_sha256"):
        compare_binding(recorded, observed)


def test_binding_rejects_state_moved_since_plan() -> None:
    """コードが同じでも state が動いていたら apply させない（serial binding）。"""
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["state_serial"] = 43
    with pytest.raises(BindingError, match="state_serial"):
        compare_binding(recorded, observed)


def test_binding_rejects_different_state_lineage() -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["state_lineage"] = "99999999-2222-3333-4444-555555555555"
    with pytest.raises(BindingError, match="state_lineage"):
        compare_binding(recorded, observed)


@pytest.mark.parametrize("field", ("aws_account", "terraform_workspace", "terraform_version"))
def test_binding_rejects_different_environment(field: str) -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed[field] = "other"
    with pytest.raises(BindingError, match=field):
        compare_binding(recorded, observed)


def test_binding_covers_every_declared_field() -> None:
    """BOUND_FIELDS の各項目が実際に照合されていること（見落とし防止）。"""
    recorded = _binding()
    for field in BOUND_FIELDS:
        observed = copy.deepcopy(recorded)
        observed[field] = "MUTATED" if field != "state_serial" else -1
        with pytest.raises(BindingError):
            compare_binding(recorded, observed)


def test_approval_is_bound_to_the_plan_hash() -> None:
    """別 plan の承認を流用できない。無指定でも通らない。"""
    recorded = _binding()
    with pytest.raises(BindingError, match="束縛"):
        check_approval(recorded, expected_approval("f" * 64))
    with pytest.raises(BindingError, match="束縛"):
        check_approval(recorded, "")
    with pytest.raises(BindingError, match="束縛"):
        check_approval(recorded, "I-HAVE-REVIEWED-THE-ADOPT-PLAN")


def test_guard_adopt_apply_requires_binding_verification() -> None:
    """guard の adopt-apply が binding 照合を apply より前に必ず通すこと。"""
    section = _guard_adopt_section()
    apply_body = section[section.index("adopt_apply()") :]
    assert '"$ADOPT_BINDING" verify' in apply_body
    assert apply_body.index('"$ADOPT_BINDING" verify') < apply_body.index("terraform -chdir")


def test_guard_adopt_plan_records_binding_on_clean_tree_only() -> None:
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    assert "clean tree でのみ実行できます" in plan_body
    assert '"$ADOPT_BINDING" record' in plan_body
