"""PR2-A0 Supply-Chain Adopt の契約テスト。

adopt は「AWS 実体を一切変更せず Terraform state だけを実態へ追いつかせる」経路であり、
既存の prevent_destroy / Object Lock / Delete Deny を一切弱めない。その不変条件を機械で固定する。

各ガードには対になる変異テストを置き、ガードを意図的に壊すと赤くなることを実証する
（緑の実質性を変異で証明するリポジトリ規約に従う）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
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
BOOTSTRAP = ROOT / "infra/deploy/bootstrap_runtime_session.sh"
RUNTIME_EVIDENCE_TF = ROOT / "infra/terraform/runtime_evidence.tf"
RUNBOOK = ROOT / "docs/runbooks/supply_chain_adopt.md"

sys.path.insert(0, str(ROOT / "infra/deploy"))

from supply_chain_adopt_integrity import (  # noqa: E402
    COMPARED_FIELDS,
    CROSSCHECKED_FIELDS,
    IntegrityError,
    _normalize_timestamp,
    compare,
    crosscheck,
    snapshot,
)
from supply_chain_adopt_validate import (  # noqa: E402
    IMPORT_DIFF_IGNORED_ATTRIBUTES,
    IMPORT_INVARIANT_ATTRIBUTES,
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
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


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


def _s3_object_state(entry: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """import 対象 aws_s3_object の state（before/after 共通の素）。

    実体を変更しない adopt では before と after が完全一致する。テストはこの素を片側だけ
    書き換えて「実体を変える import」を作り、validator が拒否することを確かめる。
    """
    state: dict[str, Any] = {
        "bucket": entry["bucket"],
        "key": entry["key"],
        "content_type": "binary/octet-stream",
        "object_lock_mode": "GOVERNANCE",
        "object_lock_retain_until_date": "2099-12-31T00:00:00Z",
        "object_lock_legal_hold_status": None,
        "server_side_encryption": "aws:kms",
        "kms_key_id": "arn:aws:kms:ap-northeast-1:718959508629:key/EXAMPLE",
        "bucket_key_enabled": True,
        "storage_class": "STANDARD",
        "etag": entry["expected_content_sha256"][:32],
        "tags": {},
    }
    state.update(overrides)
    return state


def _adopt_plan(adoptions: list[dict[str, Any]]) -> dict[str, Any]:
    """正常な adopt plan を組み立てる。

    実体と config が完全一致する健全な adopt では、import の actions は ["no-op"] になり
    before == after になる。terraform show -json の resource_changes と同じキー構成にする。
    """
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
        state = _s3_object_state(entry)
        changes.append(
            {
                "address": entry["new_address"],
                "mode": "managed",
                "type": "aws_s3_object",
                "change": {
                    "actions": ["no-op"],
                    "before": copy.deepcopy(state),
                    "after": copy.deepcopy(state),
                    "after_unknown": {},
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
    plan["resource_changes"][1]["change"]["actions"] = ["update"]
    with pytest.raises(AdoptValidationError, match="without an import"):
        validate_plan(plan, adoptions)


# ── PR2-A0.1 / 層1: import が AWS 実体を変更しないことを plan 段階で証明する ──────
#
# importing.id の一致だけでは「実体を変更しない import」の証明にならなかった。
# 実例: adoption #4 の object_lock_retain_until_date を 23:59:59 から 00:00:00 へ
# 短縮する import は、旧 validator を素通りして apply まで到達しえた。


def test_validator_accepts_an_import_whose_before_and_after_match() -> None:
    adoptions = _adoptions()
    validate_plan(_adopt_plan(adoptions), adoptions)


def test_validator_accepts_a_no_op_import() -> None:
    """実体と config が完全一致する健全な adopt は actions が ["no-op"] になる。

    ここを import として数えないと「差分ゼロの正しい plan ほど落ちる」逆転が起きる。
    """
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    assert all(
        item["change"]["actions"] == ["no-op"]
        for item in plan["resource_changes"]
        if "importing" in item["change"]
    )
    validate_plan(plan, adoptions)


def test_validator_accepts_an_import_reported_as_update_when_nothing_changes() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    for item in plan["resource_changes"]:
        if "importing" in item["change"]:
            item["change"]["actions"] = ["update"]
    validate_plan(plan, adoptions)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("object_lock_retain_until_date", "2099-12-31T23:59:59Z"),
        ("object_lock_mode", "COMPLIANCE"),
        ("object_lock_legal_hold_status", "ON"),
        ("content_type", "text/yaml"),
        ("server_side_encryption", "AES256"),
        ("kms_key_id", "arn:aws:kms:ap-northeast-1:718959508629:key/OTHER"),
        ("bucket_key_enabled", False),
        ("storage_class", "GLACIER"),
        ("etag", "0" * 32),
        ("tags", {"owner": "someone"}),
        ("acl", "public-read"),
        ("content", "mutated"),
    ],
)
def test_validator_rejects_an_import_that_changes_the_aws_object(
    attribute: str, value: Any
) -> None:
    """import と同時に実体を変える属性差分が乗った plan を拒否する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    change = plan["resource_changes"][1]["change"]
    change["actions"] = ["update"]
    change["after"][attribute] = value
    with pytest.raises(AdoptValidationError, match="must not change the AWS object"):
        validate_plan(plan, adoptions)


def test_validator_rejects_the_exact_retain_until_shortening_we_found_in_production() -> None:
    """本番で見つかった retain-until 短縮（23:59:59 → 00:00:00）を再現して拒否を実証する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    change = plan["resource_changes"][1]["change"]
    change["actions"] = ["update"]
    change["before"]["object_lock_retain_until_date"] = "2099-12-31T23:59:59Z"
    with pytest.raises(AdoptValidationError, match="object_lock_retain_until_date"):
        validate_plan(plan, adoptions)


@pytest.mark.parametrize("side", ["before", "after"])
def test_validator_rejects_an_import_without_state_to_compare(side: str) -> None:
    """before / after が無い import は「変更しない証明」ができないので拒否する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    del plan["resource_changes"][1]["change"][side]
    with pytest.raises(AdoptValidationError, match=f"change.{side}"):
        validate_plan(plan, adoptions)


def test_validator_rejects_an_import_with_unknown_security_critical_attributes() -> None:
    """plan 時に確定しない security-critical 属性がある import を拒否する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    change = plan["resource_changes"][1]["change"]
    change["after_unknown"] = {"object_lock_retain_until_date": True}
    with pytest.raises(AdoptValidationError, match="unknown at plan time"):
        validate_plan(plan, adoptions)


@pytest.mark.parametrize("attribute", ["bucket", "key"])
def test_validator_rejects_an_import_pointing_at_another_object(attribute: str) -> None:
    """import ID が合っていても、実体が mapping と別のオブジェクトなら拒否する。"""
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    change = plan["resource_changes"][1]["change"]
    change["before"][attribute] = "somewhere-else"
    change["after"][attribute] = "somewhere-else"
    with pytest.raises(AdoptValidationError, match="the mapping declares"):
        validate_plan(plan, adoptions)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("metadata", {"injected": "value"}),
        ("cache_control", "no-store"),
        ("website_redirect", "https://example.invalid/"),
        ("x_future_provider_attribute", "anything"),
    ],
)
def test_import_rejects_any_remote_write_causing_attribute_change(
    attribute: str, value: Any
) -> None:
    """契約 = remote-write-causing update 0。

    security-critical に限らず、provider-configurable な mutable 属性（さらには
    validator が名前を知らない将来の属性まで）の差分が 1 つでもあれば FATAL。
    「Terraform state だけを変えた」という主張は、この全キー比較が成立して初めて成り立つ。
    """
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    change = plan["resource_changes"][1]["change"]
    change["after"][attribute] = value
    with pytest.raises(AdoptValidationError, match="must not change the AWS object"):
        validate_plan(plan, adoptions)


def test_import_invariant_ignore_list_stays_empty() -> None:
    """「変更を許す属性」を黙って増やせないようにする。

    S3 の API 呼び出しを要する属性をここへ足すと、validator の「実体を変更しない」という
    保証が嘘になる。増やすときは必ずレビューを通すこと。
    """
    assert IMPORT_DIFF_IGNORED_ATTRIBUTES == frozenset()


def test_import_invariant_covers_every_object_lock_and_content_attribute() -> None:
    for attribute in (
        "bucket",
        "key",
        "object_lock_mode",
        "object_lock_retain_until_date",
        "object_lock_legal_hold_status",
        "content",
        "source_hash",
        "etag",
        "server_side_encryption",
        "kms_key_id",
    ):
        assert attribute in IMPORT_INVARIANT_ATTRIBUTES


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
    """head_object / get_object だけを持つ最小の S3 スタブ。

    本番の失敗モードを再現する: get_object は VersionId ごとの body を保持し、
    version 指定の無い get は「head の後に差し替えられたかもしれない最新 body」を返す。
    これにより VersionId 固定を外した実装は TOCTOU テストで必ず赤くなる。
    """

    def __init__(
        self,
        body: bytes,
        *,
        lock: str = "GOVERNANCE",
        version: str | None = "v1",
    ) -> None:
        self._lock = lock
        self._version = version
        self._versions: dict[str, bytes] = {} if version is None else {version: body}
        self._latest = body
        self.get_calls: list[dict[str, Any]] = []

    def swap_latest(self, body: bytes) -> None:
        """head の後に別 body へ差し替えられた状況を再現する（version は据え置き）。"""
        self._latest = body

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        head: dict[str, Any] = {
            "ETag": '"etag"',
            "ContentLength": len(self._latest),
            "LastModified": "2026-08-17T00:00:00+00:00",
            "ObjectLockMode": self._lock,
            "ObjectLockRetainUntilDate": "2099-12-31T00:00:00+00:00",
        }
        if self._version is not None:
            head["VersionId"] = self._version
        return head

    def get_object(
        self,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        VersionId: str | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        self.get_calls.append({"Key": Key, "VersionId": VersionId})

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        if VersionId is None:
            return {"Body": _Body(self._latest)}
        return {"Body": _Body(self._versions[VersionId])}


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


def _mapping_for_body(body: bytes) -> Path:
    """body の sha256 に整合する 1 エントリ mapping を一時ファイルへ書く（テスト用）。"""
    import hashlib
    import tempfile

    entry = _adoptions()[0]
    digest = hashlib.sha256(body).hexdigest()
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    raw["adoptions"] = [dict(entry)]
    raw["adoptions"][0]["expected_content_sha256"] = digest
    raw["adoptions"][0]["key"] = f"codebuild-buildspecs/p/{digest}.yml"
    raw["adoptions"][0]["import_id"] = f"{entry['bucket']}/codebuild-buildspecs/p/{digest}.yml"
    raw["adoptions"][0]["new_address"] = f'aws_s3_object.x["{digest}"]'
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(raw, handle)
        return Path(handle.name)


# ── PR2-A0.1 / P1-1: body 読みは HeadObject の VersionId に固定する ───────────
#
# head と get が別コールである以上、version を固定しないと両者の間で差し替えられた
# body を「head 時点の実体」として誤採取しうる（TOCTOU）。Object Lock 対象バケットは
# versioning が前提なので、VersionId を確定できないオブジェクトは fail-closed で拒否する。


def test_probe_reads_the_body_pinned_to_the_head_version() -> None:
    """get_object は必ず HeadObject が返した VersionId で呼ぶこと。"""
    body = b"pinned-body"
    fake = _FakeS3(body, version="vHEAD")
    mapping = _mapping_for_body(body)
    try:
        snapshot(mapping, "ap-northeast-1", client=fake)
    finally:
        mapping.unlink(missing_ok=True)
    assert fake.get_calls, "get_object が呼ばれていない"
    assert all(call["VersionId"] == "vHEAD" for call in fake.get_calls)


def test_probe_survives_a_body_swap_between_head_and_get() -> None:
    """head の後に body が差し替えられても、head 時点の version を読むこと（TOCTOU）。"""
    body = b"original-body"
    fake = _FakeS3(body, version="vHEAD")
    fake.swap_latest(b"attacker-swapped-body")
    mapping = _mapping_for_body(body)
    try:
        result = snapshot(mapping, "ap-northeast-1", client=fake)
    finally:
        mapping.unlink(missing_ok=True)
    import hashlib

    probe = next(iter(result["objects"].values()))
    assert probe["body_sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("version", [None, "", "null"])
def test_probe_rejects_objects_without_a_determinable_version(version: str | None) -> None:
    """VersionId が確定できないオブジェクトの integrity は証明できないので FATAL。"""
    body = b"unversioned-body"
    fake = _FakeS3(body, version=version if version not in ("", "null") else None)
    if version in ("", "null"):
        # head が空文字 / "null" を返すケースを直接再現する
        original = fake.head_object

        def head_with_bad_version(Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            head = original(Bucket=Bucket, Key=Key)
            head["VersionId"] = version
            return head

        fake.head_object = head_with_bad_version  # type: ignore[method-assign]
    mapping = _mapping_for_body(body)
    try:
        with pytest.raises(IntegrityError, match="VersionId"):
            snapshot(mapping, "ap-northeast-1", client=fake)
    finally:
        mapping.unlink(missing_ok=True)


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
    assert plan_body.index("state pull") < plan_body.index('terraform -chdir="$TF_DIR" plan')
    assert "adopt_ownership_discovery" in plan_body


def test_adopt_never_weakens_immutability() -> None:
    """禁止操作が adopt 実装へ紛れ込んでいないことを固定する。"""
    section = _strip_comments(_guard_adopt_section())
    for forbidden in ("prevent_destroy", "object-lock", "delete-object", "state rm"):
        assert forbidden not in section, f"禁止操作が含まれている: {forbidden}"


# ── plan binding: 「plan した世界」と「apply する世界」の完全一致 ──────────────

from supply_chain_adopt_binding import (  # noqa: E402
    APPROVE_TOKEN,
    BOUND_FIELDS,
    SCHEMA_VERSION,
    BindingError,
    assert_usable_principal,
    check_approval,
    compare_binding,
    expected_approval,
)

TRUSTED_SESSION_ARN = (
    "arn:aws:sts::718959508629:assumed-role/"
    "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
)
ROOT_ARN = "arn:aws:iam::718959508629:root"

GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"


def _binding() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "plan_sha256": "a" * 64,
        "plan_json_sha256": "b" * 64,
        "git_head": "c" * 40,
        "git_tree_clean": True,
        "mapping_sha256": "d" * 64,
        "state_lineage": "11111111-2222-3333-4444-555555555555",
        "state_serial": 42,
        "aws_account": "718959508629",
        "aws_principal_arn": TRUSTED_SESSION_ARN,
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


# ── PR2-A0.1: runbook の承認トークン仕様が実装と乖離しないこと ────────────────
#
# runbook に固定文字列だけを書いていた時期があり、そのとおり打つと check_approval が
# 必ず fail-closed した。実装（expected_approval）を正本として runbook 側の worked
# example を機械照合し、どちらかを変えたら赤くなるようにする。

_TOKEN_CONTRACT_RE = re.compile(
    r"<!--\s*approval-token-contract\s*\n"
    r"\s*plan_sha256:\s*(?P<sha>[0-9a-f]{64})\s*\n"
    r"\s*approve:\s*(?P<approve>\S+)\s*\n"
    r"\s*-->",
)


def _extract_runbook_token_contract(text: str) -> tuple[str, str]:
    match = _TOKEN_CONTRACT_RE.search(text)
    if match is None:
        raise AssertionError("runbook に approval-token-contract ブロックがありません")
    return match.group("sha"), match.group("approve")


def test_runbook_documents_the_exact_approval_token_contract() -> None:
    """runbook の worked example を実装で再計算して完全一致すること。"""
    sha, documented = _extract_runbook_token_contract(RUNBOOK.read_text(encoding="utf-8"))
    assert documented == expected_approval(sha)


def test_runbook_token_contract_is_mutation_sensitive() -> None:
    """runbook 側 or 実装側を弄ったら必ず赤くなることを変異で実証する。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    sha, documented = _extract_runbook_token_contract(text)

    # 変異1: runbook の approve を 1 文字変える → 照合が落ちる
    mutated = text.replace(documented, documented[:-1] + ("0" if documented[-1] != "0" else "1"))
    mutated_sha, mutated_approve = _extract_runbook_token_contract(mutated)
    assert mutated_approve != expected_approval(mutated_sha)

    # 変異2: 実装の prefix 長を変えた相当の値 → 照合が落ちる
    assert f"{APPROVE_TOKEN}:{sha[:15]}" != expected_approval(sha)
    assert f"{APPROVE_TOKEN}:{sha[:17]}" != expected_approval(sha)


def test_runbook_never_instructs_a_bare_approval_token() -> None:
    """`--approve` の手順行に、plan へ束縛されていない裸トークンが残っていないこと。"""
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        if "--approve" not in line or APPROVE_TOKEN not in line:
            continue
        after = line.split(APPROVE_TOKEN, 1)[1]
        assert after.startswith(":"), f"裸の承認トークンが手順に残っている: {line.strip()}"


def test_approval_token_is_bound_to_the_plan_hash() -> None:
    """承認は plan ごとに変わり、裸トークン・別 plan の承認は拒否される。"""
    sha_a = hashlib.sha256(b"plan-a").hexdigest()
    sha_b = hashlib.sha256(b"plan-b").hexdigest()
    assert expected_approval(sha_a) != expected_approval(sha_b)

    check_approval({"plan_sha256": sha_a}, expected_approval(sha_a))
    with pytest.raises(BindingError):
        check_approval({"plan_sha256": sha_a}, APPROVE_TOKEN)
    with pytest.raises(BindingError):
        check_approval({"plan_sha256": sha_a}, expected_approval(sha_b))
    with pytest.raises(BindingError):
        check_approval({"plan_sha256": sha_a}, "")


# ── PR2-A0.1: adopt-plan の --out が repository 配下なら plan 前に FATAL ───────
#
# 成果物は untracked file として working tree を dirty にし、apply 時の
# git_tree_clean 照合を必ず落とす。加えて state-backup.json は state 全文（機微）。
# runbook の注意書きではなく guard 側で機械的に拒否する。


def _out_dir_probe_script() -> str:
    """guard から --out 判定部分だけを切り出す（terraform / aws は呼ばれない）。"""
    body = GUARD.read_text(encoding="utf-8")
    start = body.index("adopt_canonical_path() {")
    end = body.index("adopt_plan() {")
    return (
        "set -euo pipefail\n"
        'REPO_ROOT="$1"\n'
        "shift\n"
        'die() { echo "$*" >&2; exit 1; }\n'
        + body[start:end]
        + '\nadopt_assert_out_dir_outside_repo "$1"\necho ACCEPTED\n'
    )


def _run_out_dir_check(out_dir: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _out_dir_probe_script(), "probe", str(ROOT), out_dir],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=False,
    )


@pytest.mark.parametrize(
    "relative",
    [
        "",  # repository root そのもの
        "tmp/adopt-out",  # repository 配下のサブディレクトリ
        "infra/../adopt-out",  # ../ を含むが解決後は repository 内
        ".git",  # git ディレクトリ
    ],
)
def test_adopt_out_dir_inside_repository_is_rejected(relative: str, tmp_path: Path) -> None:
    target = str(ROOT / relative) if relative else str(ROOT)
    result = _run_out_dir_check(target, cwd=tmp_path)
    assert result.returncode != 0, f"repository 配下が受理された: {target}"
    assert "repository 配下は指定できません" in result.stderr


def test_adopt_out_dir_relative_path_inside_repository_is_rejected() -> None:
    """cwd が repository 内のときの相対パスも解決してから拒否すること。"""
    result = _run_out_dir_check("./adopt-out", cwd=ROOT)
    assert result.returncode != 0
    assert "repository 配下は指定できません" in result.stderr


def test_adopt_out_dir_symlink_back_into_repository_is_rejected(tmp_path: Path) -> None:
    """repository 外に置いた symlink でも、実体が repository 配下なら拒否すること。"""
    link = tmp_path / "link-into-repo"
    link.symlink_to(ROOT / "adopt-out")
    result = _run_out_dir_check(str(link), cwd=tmp_path)
    assert result.returncode != 0
    assert "repository 配下は指定できません" in result.stderr


@pytest.mark.parametrize("suffix", ["outside", "nested/../outside"])
def test_adopt_out_dir_outside_repository_is_accepted(suffix: str, tmp_path: Path) -> None:
    result = _run_out_dir_check(str(tmp_path / suffix), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ACCEPTED" in result.stdout


def test_adopt_rejects_repository_local_out_dir_before_creating_it() -> None:
    """拒否はディレクトリ作成より前に起きること（順序が本質）。"""
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    assert plan_body.index("adopt_assert_out_dir_outside_repo") < plan_body.index("mkdir -p")


def test_adopt_apply_also_rejects_repository_local_out_dir() -> None:
    """apply も out_dir へ integrity snapshot を書くので同じ検査を通すこと。"""
    section = _guard_adopt_section()
    apply_body = section[section.index("adopt_apply()") :]
    assert "adopt_assert_out_dir_outside_repo" in apply_body


def test_adopt_out_dir_guard_is_mutation_sensitive(tmp_path: Path) -> None:
    """禁止領域の判定を外すと repository 配下が通ってしまうことを変異で実証する。"""
    mutated = _out_dir_probe_script().replace(
        'forbidden_roots+=("$REPO_ROOT")', "forbidden_roots=()"
    )
    result = subprocess.run(
        ["bash", "-c", mutated, "probe", str(ROOT), str(ROOT / "tmp/adopt-out")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert result.returncode == 0 and "ACCEPTED" in result.stdout


# ── PR2-A0.1: adopt 成果物は owner-only で作成する ────────────────────────────


def test_adopt_out_dir_is_created_with_owner_only_umask() -> None:
    """作成〜chmod の窓を塞ぐため mkdir は umask 077 の subshell 内で行うこと。"""
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    assert "(umask 077 && mkdir -p" in plan_body
    assert 'chmod 700 "$out_dir"' in plan_body


def _chmod600_artifacts() -> set[str]:
    """adopt 経路で `chmod 600` の対象になっている成果物名を集める。"""
    names: set[str] = set()
    for line in _guard_adopt_section().splitlines():
        stripped = line.strip()
        if not stripped.startswith("chmod 600 "):
            continue
        names.update(re.findall(r'"\$out_dir/([^"]+)"', stripped))
    return names


@pytest.mark.parametrize(
    "artifact",
    [
        "state-backup.json",
        "state-list.txt",
        "integrity-before.json",
        "adopt.tfplan",
        "adopt-plan.json",
        "adopt-binding.json",
        "integrity-preapply.json",
        "integrity-after.json",
    ],
)
def test_adopt_artifacts_are_owner_only(artifact: str) -> None:
    """state 全文・plan・integrity snapshot はすべて owner-only にすること。"""
    assert artifact in _chmod600_artifacts()


def test_every_adopt_artifact_written_is_owner_only() -> None:
    """将来 out_dir へ成果物が増えたとき chmod 漏れを検出する（列挙漏れ防止）。"""
    section = _guard_adopt_section()
    written = set(re.findall(r'--out "\$out_dir/([^"]+)"', section))
    written.update(re.findall(r'> "\$out_dir/([^"]+)"', section))
    written.update(re.findall(r'-out="\$out_dir/([^"]+)"', section))
    missing = written - _chmod600_artifacts()
    assert not missing, f"chmod 600 されていない adopt 成果物: {sorted(missing)}"


def test_guard_usage_documents_adopt_modes() -> None:
    """手順の真実源が runbook だけにならないよう usage にも adopt を出すこと。"""
    body = GUARD.read_text(encoding="utf-8")
    usage = body[body.index("usage() {") : body.index("die() {")]
    assert "adopt-plan --var-file FILE --out DIR" in usage
    assert "adopt-apply --out DIR --approve TOKEN" in usage


# ── PR2-A0.1 / P0-B: activation を実行した principal を plan と apply で束縛する ─────
#
# binding が aws_account しか持たないと、同一 account 内で principal を差し替えても
# 通ってしまう。実測で clean environment の caller identity が root だったため、
# caller ARN 自体を束縛し、root は plan / apply の両方で明示的に拒否する。


def test_principal_arn_is_a_bound_field() -> None:
    assert "aws_principal_arn" in BOUND_FIELDS


def test_binding_schema_version_rejects_manifests_without_principal_binding() -> None:
    """principal を束縛しない v1 manifest を黙って受け入れないこと。"""
    assert SCHEMA_VERSION == 2
    recorded = _binding()
    recorded["schema_version"] = 1
    with pytest.raises(BindingError, match="schema_version"):
        compare_binding(recorded, _binding())


def test_binding_accepts_same_account_same_role() -> None:
    recorded = _binding()
    compare_binding(recorded, copy.deepcopy(recorded))


def test_binding_rejects_same_account_different_role() -> None:
    """account が同じでも principal が違えば apply させない。"""
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["aws_principal_arn"] = "arn:aws:sts::718959508629:assumed-role/some-other-role/session"
    with pytest.raises(BindingError, match="aws_principal_arn"):
        compare_binding(recorded, observed)


@pytest.mark.parametrize("side", ["recorded", "observed", "both"])
def test_binding_rejects_root_principal(side: str) -> None:
    """root は「両者一致していても」拒否する（account 一致では authorization にならない）。"""
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    if side in ("recorded", "both"):
        recorded["aws_principal_arn"] = ROOT_ARN
    if side in ("observed", "both"):
        observed["aws_principal_arn"] = ROOT_ARN
    with pytest.raises(BindingError, match="root"):
        compare_binding(recorded, observed)


def test_binding_rejects_missing_principal_field() -> None:
    recorded = _binding()
    del recorded["aws_principal_arn"]
    with pytest.raises(BindingError, match="aws_principal_arn"):
        compare_binding(recorded, _binding())


def test_binding_rejects_tampered_principal_field() -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["aws_principal_arn"] = TRUSTED_SESSION_ARN + "x"
    with pytest.raises(BindingError, match="aws_principal_arn"):
        compare_binding(recorded, observed)


def test_binding_rejects_different_account_even_with_same_role_name() -> None:
    recorded = _binding()
    observed = copy.deepcopy(recorded)
    observed["aws_account"] = "111122223333"
    observed["aws_principal_arn"] = TRUSTED_SESSION_ARN.replace("718959508629", "111122223333")
    with pytest.raises(BindingError, match="aws_account"):
        compare_binding(recorded, observed)


@pytest.mark.parametrize(
    "arn",
    [
        ROOT_ARN,
        "arn:aws:iam::111122223333:root",
        "arn:aws-cn:iam::718959508629:root",
    ],
)
def test_assert_usable_principal_rejects_root(arn: str) -> None:
    with pytest.raises(BindingError, match="root"):
        assert_usable_principal(arn, source="test")


@pytest.mark.parametrize("value", ["", "   ", None, 42, "718959508629", "not-an-arn"])
def test_assert_usable_principal_rejects_non_arn(value: Any) -> None:
    with pytest.raises(BindingError):
        assert_usable_principal(value, source="test")


@pytest.mark.parametrize(
    "arn",
    [
        TRUSTED_SESSION_ARN,
        "arn:aws:iam::718959508629:user/deployer",
        "arn:aws:sts::718959508629:assumed-role/role-named-root-suffix/root",
    ],
)
def test_assert_usable_principal_accepts_non_root_arns(arn: str) -> None:
    assert assert_usable_principal(arn, source="test") == arn


def test_guard_resolves_and_binds_caller_principal_in_both_adopt_modes() -> None:
    """plan と apply の両方で live の caller identity を取り直して束縛すること。"""
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    apply_body = section[section.index("adopt_apply()") :]
    for body in (plan_body, apply_body):
        assert "adopt_trusted_principal_arn" in body
        assert '--principal-arn "$principal_arn"' in body
    assert plan_body.index("adopt_trusted_principal_arn") < plan_body.index("state pull")


def test_adopt_reuses_the_existing_trusted_identity_verifier() -> None:
    """adopt 専用の principal 正規化を作らず、既存 verifier をそのまま通すこと。"""
    section = _guard_adopt_section()
    resolver = section[
        section.index("adopt_trusted_principal_arn() {") : section.index("adopt_plan() {")
    ]
    assert "assert_trusted_automation_identity" in resolver
    # canonical principal は verifier が検証した identity から取る（独自加工しない）。
    assert "jq -er '.Arn'" in resolver
    for invented in ("sed ", "awk ", "cut -d", "${arn%%", "${arn##"):
        assert invented not in resolver, f"adopt 独自の principal 加工が入っている: {invented}"


def test_guard_rejects_root_principal_before_any_adopt_work() -> None:
    """guard 側でも root を拒否する（binding 到達前に止める）。"""
    section = _guard_adopt_section()
    resolver = section[
        section.index("adopt_trusted_principal_arn() {") : section.index("adopt_plan() {")
    ]
    assert "arn:aws*:iam::*:root)" in resolver
    assert "root principal では adopt を実行できません" in resolver
    assert resolver.index("arn:aws*:iam::*:root)") < resolver.index(
        "assert_trusted_automation_identity"
    )


def test_trusted_session_arn_is_stable_across_credential_refresh() -> None:
    """canonical principal が session ごとに変わらないことを構成で保証する。

    role の trust policy が sts:RoleSessionName を固定値で StringEquals しているため、
    別の session 名で assume することが STS 側で拒否される。よって plan と apply が
    別 temporary session になっても assumed-role session ARN は同一になる。
    """
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    assume = tf[tf.index('data "aws_iam_policy_document" "runtime_automation_assume"') :]
    assume = assume[: assume.index("\ndata ")]
    assert 'variable = "sts:RoleSessionName"' in assume
    assert 'test     = "StringEquals"' in assume
    assert 'variable = "aws:MultiFactorAuthPresent"' in assume

    session_name = re.search(r'runtime_automation_session_name\s*=\s*"([^"]+)"', tf).group(1)
    guard_arn = re.search(
        r'TRUSTED_AUTOMATION_ARN="([^"]+)"', GUARD.read_text(encoding="utf-8")
    ).group(1)
    bootstrap_arn = re.search(
        r'EXPECTED_SESSION_ARN="(arn:aws:sts::[^"]*terraform-runtime-automation[^"]*)"',
        BOOTSTRAP.read_text(encoding="utf-8"),
    ).group(1)
    assert guard_arn.endswith(f"/{session_name}")
    assert guard_arn == bootstrap_arn


def test_adopt_modes_are_reachable_through_the_approved_session_bootstrap() -> None:
    """guard が受け付ける adopt モードは、承認済み bootstrap からも起動できること。

    ここが外れていると adopt は ambient credential（実測では account root）でしか
    起動できず、trusted automation role に紐づいた Deny ポリシー群が一切効かなくなる。
    """
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    branch = next(
        line
        for line in bootstrap.splitlines()
        if line.lstrip().startswith("snapshot|") and line.rstrip().endswith(")")
    )
    for mode in ("adopt-plan", "adopt-apply"):
        assert f"|{mode}" in branch or branch.lstrip().startswith(f"{mode}|"), (
            f"{mode} が runtime 分岐の case ラベルに無い: {branch.strip()}"
        )


def test_adopt_uses_the_same_trusted_role_branch_as_the_other_runtime_commands() -> None:
    """adopt を独立 case にすると別 ARN を混ぜられるので、必ず既存 runtime 分岐に置く。"""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert bootstrap.count("terraform-runtime-automation/teamagent-terraform-worker") == 1
    guard_arn = re.search(
        r'TRUSTED_AUTOMATION_ARN="([^"]+)"', GUARD.read_text(encoding="utf-8")
    ).group(1)
    assert guard_arn in bootstrap


def test_every_guard_adopt_mode_is_allowlisted_in_the_bootstrap() -> None:
    """guard に adopt モードを増やしたら bootstrap 側の追随を強制する。"""
    guard = GUARD.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    dispatched = set(re.findall(r"^  (adopt-[a-z]+)\)", guard, flags=re.MULTILINE))
    assert dispatched, "guard に adopt モードの dispatch が見つからない"
    for mode in sorted(dispatched):
        assert f"|{mode}" in bootstrap, f"guard の {mode} が bootstrap allowlist に無い"


@pytest.mark.parametrize(
    ("arn", "expected_reject"),
    [
        ("arn:aws:iam::718959508629:root", True),
        ("arn:aws:sts::718959508629:assumed-role/teamagent/worker", False),
        ("arn:aws:iam::718959508629:user/deployer", False),
    ],
)
def test_guard_root_pattern_matches_only_account_root(arn: str, expected_reject: bool) -> None:
    """guard の case パターンが account root だけを拾い、role/user を誤爆しないこと。"""
    script = 'case "$1" in\n  arn:aws*:iam::*:root) echo REJECT ;;\n  *) echo ACCEPT ;;\nesac\n'
    result = subprocess.run(
        ["bash", "-c", script, "probe", arn], capture_output=True, text=True, check=False
    )
    assert ("REJECT" in result.stdout) is expected_reject


# ── PR2-A0.1 / 層2: plan の宣言値と live 実体を突き合わせる ────────────────────
#
# 層1 は plan 内部の before/after 整合しか見ない。plan の before は Terraform が読んだ値
# なので、独立に採取した integrity snapshot と突き合わせて初めて「plan が live 実体を
# 変えない」と言える。


def _live_snapshot(adoptions: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """live 実体の integrity snapshot（boto3 由来なので timestamp は +00:00 表記）。"""
    objects = {}
    for entry in adoptions:
        probe = {
            "bucket": entry["bucket"],
            "key": entry["key"],
            "object_lock_mode": "GOVERNANCE",
            "object_lock_retain_until_date": "2099-12-31T00:00:00+00:00",
            "body_sha256": entry["expected_content_sha256"],
        }
        probe.update(overrides)
        objects[entry["new_address"]] = probe
    return {"schema_version": 1, "objects": objects}


def test_crosscheck_accepts_a_plan_that_matches_the_live_objects() -> None:
    adoptions = _adoptions()
    crosscheck(_live_snapshot(adoptions), _adopt_plan(adoptions), adoptions)


def test_crosscheck_absorbs_the_timestamp_notation_difference() -> None:
    """snapshot は +00:00、plan は Z。同じ時刻を不一致と誤検知しないこと。"""
    assert _normalize_timestamp("2099-12-31T00:00:00Z") == _normalize_timestamp(
        "2099-12-31T00:00:00+00:00"
    )
    assert _normalize_timestamp("2099-12-31T00:00:00Z") != _normalize_timestamp(
        "2099-12-31T23:59:59Z"
    )


def test_crosscheck_rejects_the_exact_retain_until_drift_found_in_production() -> None:
    """live が 23:59:59 なのに plan が 00:00:00 を宣言している状態を拒否する。"""
    adoptions = _adoptions()
    snapshot_doc = _live_snapshot(adoptions)
    target = adoptions[-1]["new_address"]
    snapshot_doc["objects"][target]["object_lock_retain_until_date"] = "2099-12-31T23:59:59+00:00"
    with pytest.raises(IntegrityError, match="object_lock_retain_until_date"):
        crosscheck(snapshot_doc, _adopt_plan(adoptions), adoptions)


@pytest.mark.parametrize("field", [field for field, _ in CROSSCHECKED_FIELDS])
def test_crosscheck_detects_any_single_field_drift(field: str) -> None:
    adoptions = _adoptions()
    snapshot_doc = _live_snapshot(adoptions)
    snapshot_doc["objects"][adoptions[0]["new_address"]][field] = "DRIFTED"
    with pytest.raises(IntegrityError, match="does not match the live AWS objects"):
        crosscheck(snapshot_doc, _adopt_plan(adoptions), adoptions)


def test_crosscheck_rejects_a_plan_without_an_import_to_compare() -> None:
    adoptions = _adoptions()
    plan = _adopt_plan(adoptions)
    for item in plan["resource_changes"]:
        item["change"].pop("importing", None)
    with pytest.raises(IntegrityError, match="no imported state"):
        crosscheck(_live_snapshot(adoptions), plan, adoptions)


def test_crosscheck_rejects_an_object_missing_from_the_snapshot() -> None:
    adoptions = _adoptions()
    snapshot_doc = _live_snapshot(adoptions)
    del snapshot_doc["objects"][adoptions[0]["new_address"]]
    with pytest.raises(IntegrityError, match="missing from the integrity snapshot"):
        crosscheck(snapshot_doc, _adopt_plan(adoptions), adoptions)


def test_guard_crosschecks_the_plan_against_live_before_binding_it() -> None:
    """guard が層1と層2の両方を、binding 記録より前に通すこと（順序が本質）。"""
    section = _guard_adopt_section()
    plan_body = section[section.index("adopt_plan()") : section.index("adopt_apply()")]
    assert '"$ADOPT_INTEGRITY" crosscheck' in plan_body
    assert plan_body.index('"$ADOPT_VALIDATOR"') < plan_body.index('"$ADOPT_INTEGRITY" crosscheck')
    assert plan_body.index('"$ADOPT_INTEGRITY" crosscheck') < plan_body.index(
        '"$ADOPT_BINDING" record'
    )
    # snapshot は crosscheck より前に取れていること（比較対象が無ければ意味がない）。
    assert plan_body.index('"$ADOPT_INTEGRITY" snapshot') < plan_body.index(
        '"$ADOPT_INTEGRITY" crosscheck'
    )


def test_bootstrap_starts_as_the_iam_administrator_not_root() -> None:
    """bootstrap の起点 principal が live の trust policy と同じ主体であること。

    live の teamagent-dev-terraform-runtime-automation は AIIAdev + MFA + 固定 session 名
    しか受け付けない（実測）。bootstrap が root 起点を要求していた実装は stale で、
    root は AWS 側で AssumeRole 自体が拒否されるため、その経路は成立しなかった。
    """
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'ADMIN_ARN="arn:aws:iam::718959508629:user/AIIAdev"' in bootstrap
    assert ':root"' not in bootstrap, "root 起点の要求が残っている"
    assert '[ "$initial_arn" = "$ADMIN_ARN" ]' in bootstrap
    # 起点検査は必ず assume-role より前に置く。
    assert bootstrap.index('"$initial_arn" = "$ADMIN_ARN"') < bootstrap.index("sts assume-role")


def test_bootstrap_admin_matches_the_terraform_trust_principal() -> None:
    """bootstrap の起点と Terraform の trust policy の principal が一致していること。"""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    admin = re.search(r'ADMIN_ARN="([^"]+)"', bootstrap).group(1)
    user_name = admin.rsplit("/", 1)[-1]
    codebuild_tf = CODEBUILD_TF.read_text(encoding="utf-8")
    assert f'user_name = "{user_name}"' in codebuild_tf
    assume = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    assume = assume[assume.index('"runtime_automation_assume"') :]
    assume = assume[: assume.index("\ndata ")]
    assert "data.aws_iam_user.aiia_dev.arn" in assume


# ── PR2-A0.2: trusted automation role の activation 用最小 read 権限 ──────────
#
# integrity 検査（HeadObject / VersionId 固定 GetObject / Object Lock メタデータ）と
# adopt(import) の provider read に必要な read だけを evidence inline policy へ許可する。
# 書き込みは boundary / control-plane / bucket policy の Deny 群が塞いだまま。


def _buildspec_read_statement() -> str:
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    start = tf.index('sid = "ReadExactSupplyChainBuildspecGenerations"')
    return tf[start : tf.index("statement {", start)]


def test_buildspec_read_grant_has_exactly_the_minimal_actions() -> None:
    """許可は read 3 action のみ。ワイルドカード・書き込み系は一切含まない。"""
    stmt = _buildspec_read_statement()
    granted = set(re.findall(r'"(s3:[A-Za-z]+)"', stmt))
    assert granted == {"s3:GetObject", "s3:GetObjectRetention", "s3:GetObjectTagging"}
    assert ":*" not in stmt
    for write in ("Put", "Delete", "Restore", "Create"):
        assert f"s3:{write}" not in stmt


def test_buildspec_read_grant_covers_every_adoption_project_prefix() -> None:
    """mapping が対象とする全プロジェクトの prefix を過不足なくカバーする。

    mapping に新しいプロジェクト族が増えたのに read 権限が追随していない状態
    （今回の 403 の再発）をテストで強制的に検出する。
    """
    stmt = _buildspec_read_statement()
    prefixes = re.findall(r"codebuild-buildspecs/\$\{local\.([a-z_]+)_project_name\}/\*", stmt)
    granted_projects = set(prefixes)
    assert granted_projects == {
        "mcp_source_publisher",
        "image_attestor",
        "image_promoter",
        "approval_publisher",
    }
    # mapping 側の全 project がカバーされていること（mapping から逆引き）
    mapping_projects = {
        entry["key"].split("/")[1].replace("teamagent-dev-", "").replace("-", "_")
        for entry in _adoptions()
    }
    assert mapping_projects <= granted_projects


def test_buildspec_read_grant_has_no_conditions() -> None:
    """condition を付けると既存の Null-condition 数一致テストと衝突しうるため付けない。"""
    assert "condition" not in _buildspec_read_statement()


def test_buildspec_read_grant_lives_in_the_evidence_inline_policy() -> None:
    """statement は runtime_evidence_automation（inline -evidence）の中にあること。

    managed policy 側（manage-a/b/core）に足すと action ハッシュ・statement 数の
    contract test 群と衝突し、6144 文字の size precondition にも影響する。
    """
    tf = RUNTIME_EVIDENCE_TF.read_text(encoding="utf-8")
    doc_start = tf.index('data "aws_iam_policy_document" "runtime_evidence_automation"')
    doc_end = tf.index('resource "aws_iam_role_policy" "runtime_evidence_automation"')
    assert doc_start < tf.index('sid = "ReadExactSupplyChainBuildspecGenerations"') < doc_end
