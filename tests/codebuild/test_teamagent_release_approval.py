from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEBUILD = ROOT / "infra" / "codebuild"
APPROVAL_MODULE_PATH = CODEBUILD / "teamagent_release_approval.py"
RELEASE_EVIDENCE_MODULE_PATH = CODEBUILD / "release_evidence.py"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


APPROVAL = _load_module(
    "teamagent_release_approval_under_test",
    APPROVAL_MODULE_PATH,
)
RELEASE_EVIDENCE = _load_module(
    "teamagent_release_evidence_for_canonical_comparison",
    RELEASE_EVIDENCE_MODULE_PATH,
)

NOW = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.UTC)
COMMIT = "1" * 40
TREE_OID = "2" * 40
INNER_SHA = "a" * 64
OUTER_SHA = "b" * 64
APPROVED_BY = "arn:aws:iam::718959508629:role/teamagent-dev-approval-caller"
PUBLISHER_PROJECT_ARN = (
    "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-approval-publisher"
)
PUBLISHER_BUILD_ID = "teamagent-dev-approval-publisher:11111111-1111-4111-8111-111111111111"
APPROVAL_KMS_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/11111111-1111-4111-8111-111111111111"
)
DRILL_KMS_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/22222222-2222-4222-8222-222222222222"
)
OBSERVATION_VALUES = {
    "core.base.builder.arm64.digest": f"sha256:{'1' * 64}",
    "core.binary.python.sha256": "2" * 64,
    "media.binary.chromium.sha256": "3" * 64,
    "media.binary.ffmpeg.sha256": "4" * 64,
    "media.binary.node.sha256": "5" * 64,
    "media.binary.python.sha256": "6" * 64,
}


def _authority() -> dict[str, str]:
    return {
        "publisher_project_arn": PUBLISHER_PROJECT_ARN,
        "publisher_build_id": PUBLISHER_BUILD_ID,
        "kms_key_arn": APPROVAL_KMS_KEY_ARN,
    }


def _observations() -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "value": OBSERVATION_VALUES[key],
            "observed_at_utc": "2026-07-24T10:00:00Z",
            "source": f"contract://immutable-observation/{key}",
        }
        for key in APPROVAL.APPROVAL_OBSERVATION_KEYS
    ]


def _drill_manifest() -> dict[str, Any]:
    return {
        "payload": {
            "bucket": "teamagent-dev-image-release-evidence",
            "key": "forced-rollback/manifests/drill-1.json",
            "version_id": "manifest-version-1",
            "sha256": "c" * 64,
        },
        "signature": {
            "key": "forced-rollback/manifests/drill-1.json.sig",
            "version_id": "manifest-signature-version-1",
            "sha256": "d" * 64,
        },
        "kms_key_arn": DRILL_KMS_KEY_ARN,
        "signing_algorithm": "RSASSA_PSS_SHA_256",
    }


def _provisional_campaign() -> dict[str, str]:
    return {
        "campaign_id": "initial-cutover-r1-r2",
        "phase": "R1",
        "payload_version_id": "campaign-payload-version-1",
        "payload_sha256": "e" * 64,
        "signature_version_id": "campaign-signature-version-1",
        "kms_key_arn": DRILL_KMS_KEY_ARN,
        "expires_at_utc": "2026-07-24T12:30:00Z",
    }


def _passed_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "teamagent.core-media-release-approval",
        "approval_id": "11111111-1111-4111-8111-111111111111",
        "pipeline": "mcp",
        "environment": "dev",
        "approved_at_utc": "2026-07-24T11:00:00Z",
        "expires_at_utc": "2026-07-24T13:00:00Z",
        "approved_by": APPROVED_BY,
        "source_commit": COMMIT,
        "source_tree_oid": TREE_OID,
        "contracts": {
            "inner": {"schema_version": 5, "raw_sha256": INNER_SHA},
            "outer": {"schema_version": 3, "raw_sha256": OUTER_SHA},
        },
        "observations": _observations(),
        "gates": {
            "forced_rollback_evidence": {
                "gate_version": 1,
                "state": "PASSED",
                "drill_manifest": _drill_manifest(),
            }
        },
        "decision": "APPROVED: exact release evidence matched",
        "approval_authority": _authority(),
    }


def _provisional_payload() -> dict[str, Any]:
    payload = _passed_payload()
    payload["gates"]["forced_rollback_evidence"] = {
        "gate_version": 1,
        "state": "PROVISIONAL_INITIAL_RELEASE",
        "provisional_campaign": _provisional_campaign(),
    }
    return payload


def _expected(
    *,
    commit: str = COMMIT,
    tree_oid: str = TREE_OID,
    inner_sha: str = INNER_SHA,
    outer_sha: str = OUTER_SHA,
    pipeline: str = "mcp",
    forced_rollback_state: str = "PASSED",
) -> dict[str, Any]:
    return {
        "commit": commit,
        "tree_oid": tree_oid,
        "inner_sha": inner_sha,
        "outer_sha": outer_sha,
        "observations": dict(OBSERVATION_VALUES),
        "pipeline": pipeline,
        "environment": "dev",
        "approved_by": APPROVED_BY,
        "authority": _authority(),
        "forced_rollback_state": forced_rollback_state,
    }


def _validate(
    payload: bytes | dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
    now: dt.datetime = NOW,
) -> dict[str, Any]:
    return APPROVAL.validate_approval_payload(
        payload,
        _expected() if expected is None else expected,
        now=now,
    )


def _set_path(value: Any, path: tuple[str | int, ...], replacement: Any) -> None:
    current = value
    for item in path[:-1]:
        current = current[item]
    current[path[-1]] = replacement


def _delete_path(value: Any, path: tuple[str | int, ...]) -> None:
    current = value
    for item in path[:-1]:
        current = current[item]
    del current[path[-1]]


@pytest.mark.parametrize("as_bytes", [False, True], ids=["dict", "canonical-bytes"])
def test_canonical_passed_payload_is_accepted_and_normalized(as_bytes: bool) -> None:
    payload = _passed_payload()
    original = copy.deepcopy(payload)
    value = APPROVAL.canonical_json_bytes(payload) if as_bytes else payload

    normalized = _validate(value)

    assert normalized == original
    assert normalized is not payload
    payload["decision"] = "APPROVED: caller mutated its input later"
    assert normalized == original


@pytest.mark.parametrize(
    ("state", "payload_factory"),
    [
        ("PASSED", _passed_payload),
        ("PROVISIONAL_INITIAL_RELEASE", _provisional_payload),
    ],
)
def test_each_forced_rollback_gate_variant_is_accepted(
    state: str,
    payload_factory: Any,
) -> None:
    payload = payload_factory()
    expected = _expected(forced_rollback_state=state)

    normalized = _validate(payload, expected=expected)

    assert normalized["gates"]["forced_rollback_evidence"]["state"] == state


@pytest.mark.parametrize("pipeline", ["mcp", "x_buzz"])
def test_each_approval_pipeline_is_accepted_when_expected(pipeline: str) -> None:
    payload = _passed_payload()
    payload["pipeline"] = pipeline

    normalized = _validate(payload, expected=_expected(pipeline=pipeline))

    assert normalized["pipeline"] == pipeline


def test_duplicate_key_in_nested_object_is_rejected() -> None:
    payload = _passed_payload()
    raw = APPROVAL.canonical_json_bytes(payload)
    sha = payload["contracts"]["inner"]["raw_sha256"]
    needle = f'"inner":{{"raw_sha256":"{sha}","schema_version":5}}'.encode()
    replacement = (
        f'"inner":{{"raw_sha256":"{sha}","raw_sha256":"{sha}","schema_version":5}}'
    ).encode()
    assert needle in raw

    with pytest.raises(APPROVAL.ProvenanceError, match="duplicate JSON key: raw_sha256"):
        _validate(raw.replace(needle, replacement))


@pytest.mark.parametrize(
    "approval_id",
    [
        "11111111-1111-3111-8111-111111111111",
        "11111111-1111-4111-7111-111111111111",
        "11111111-1111-4111-8111-11111111111A",
    ],
)
def test_approval_id_must_be_a_canonical_uuid4(approval_id: str) -> None:
    payload = _passed_payload()
    payload["approval_id"] = approval_id

    with pytest.raises(APPROVAL.ProvenanceError, match="UUIDv4"):
        _validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("unexpected",),
        ("contracts", "inner", "path"),
        ("observations", 0, "unexpected"),
        ("gates", "unexpected"),
        ("approval_authority", "unexpected"),
    ],
    ids=["top", "contract", "observation", "gates", "authority"],
)
def test_unknown_field_is_rejected_at_every_schema_layer(
    path: tuple[str | int, ...],
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, "unexpected")

    with pytest.raises(APPROVAL.ProvenanceError, match="unknown"):
        _validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("decision",),
        ("contracts", "inner", "raw_sha256"),
        ("observations", 0, "source"),
        ("gates", "forced_rollback_evidence"),
        ("approval_authority", "publisher_build_id"),
    ],
    ids=["top", "contract", "observation", "gates", "authority"],
)
def test_missing_field_is_rejected_at_every_schema_layer(
    path: tuple[str | int, ...],
) -> None:
    payload = _passed_payload()
    _delete_path(payload, path)

    with pytest.raises(APPROVAL.ProvenanceError, match="missing"):
        _validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("gates", "forced_rollback_evidence", "drill_manifest", "unexpected"),
        (
            "gates",
            "forced_rollback_evidence",
            "drill_manifest",
            "payload",
            "unexpected",
        ),
        (
            "gates",
            "forced_rollback_evidence",
            "drill_manifest",
            "signature",
            "unexpected",
        ),
    ],
    ids=["manifest", "manifest-payload", "manifest-signature"],
)
def test_passed_gate_rejects_unknown_nested_locator_fields(
    path: tuple[str | int, ...],
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, "unexpected")

    with pytest.raises(APPROVAL.ProvenanceError, match="unknown"):
        _validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("gates", "forced_rollback_evidence", "drill_manifest", "kms_key_arn"),
        (
            "gates",
            "forced_rollback_evidence",
            "drill_manifest",
            "payload",
            "version_id",
        ),
        (
            "gates",
            "forced_rollback_evidence",
            "drill_manifest",
            "signature",
            "sha256",
        ),
    ],
    ids=["manifest", "manifest-payload", "manifest-signature"],
)
def test_passed_gate_rejects_missing_nested_locator_fields(
    path: tuple[str | int, ...],
) -> None:
    payload = _passed_payload()
    _delete_path(payload, path)

    with pytest.raises(APPROVAL.ProvenanceError, match="missing"):
        _validate(payload)


def test_provisional_gate_rejects_unknown_and_missing_campaign_fields() -> None:
    payload_with_unknown = _provisional_payload()
    campaign_with_unknown = payload_with_unknown["gates"]["forced_rollback_evidence"][
        "provisional_campaign"
    ]
    campaign_with_unknown["unexpected"] = "unexpected"
    expected = _expected(forced_rollback_state="PROVISIONAL_INITIAL_RELEASE")

    with pytest.raises(APPROVAL.ProvenanceError, match="unknown"):
        _validate(payload_with_unknown, expected=expected)

    payload_with_missing = _provisional_payload()
    del payload_with_missing["gates"]["forced_rollback_evidence"]["provisional_campaign"][
        "signature_version_id"
    ]

    with pytest.raises(APPROVAL.ProvenanceError, match="missing"):
        _validate(payload_with_missing, expected=expected)


@pytest.mark.parametrize(
    ("path", "wrong_type"),
    [
        (("schema_version",), True),
        (("approval_id",), 7),
        (("pipeline",), []),
        (("environment",), {}),
        (("contracts",), []),
        (("contracts", "inner", "schema_version"), "5"),
        (("observations",), {}),
        (("observations", 0, "value"), 7),
        (("gates",), []),
        (("gates", "forced_rollback_evidence", "gate_version"), True),
        (("approval_authority",), []),
    ],
    ids=[
        "schema-bool",
        "approval-id-int",
        "pipeline-list",
        "environment-object",
        "contracts-array",
        "inner-schema-string",
        "observations-object",
        "observation-value-int",
        "gates-array",
        "gate-version-bool",
        "authority-array",
    ],
)
def test_wrong_field_type_is_rejected_as_a_provenance_error(
    path: tuple[str | int, ...],
    wrong_type: Any,
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, wrong_type)

    with pytest.raises(APPROVAL.ProvenanceError):
        _validate(payload)


@pytest.mark.parametrize("payload", ["not a dict", [], bytearray(b"{}\n"), None])
def test_unsupported_payload_type_is_rejected(payload: Any) -> None:
    with pytest.raises(APPROVAL.ProvenanceError, match="bytes or a dict"):
        _validate(payload)


def test_dict_payload_rejects_json_primitive_subclasses() -> None:
    class SpoofedString(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    payload = _passed_payload()
    payload["kind"] = SpoofedString("teamagent.not-the-approval-kind")

    with pytest.raises(APPROVAL.ProvenanceError, match="non-built-in"):
        _validate(payload)


def test_dict_payload_cycle_is_rejected_as_a_provenance_error() -> None:
    payload: dict[str, Any] = {}
    payload["cycle"] = payload

    with pytest.raises(APPROVAL.ProvenanceError, match="container cycle"):
        _validate(payload)


def test_deep_dict_payload_is_rejected_as_a_provenance_error() -> None:
    payload: dict[str, Any] = {}
    current = payload
    for _ in range(65):
        child: dict[str, Any] = {}
        current["nested"] = child
        current = child

    with pytest.raises(APPROVAL.ProvenanceError, match="nesting limit"):
        _validate(payload)


def test_json_parser_value_error_is_wrapped_as_a_provenance_error() -> None:
    oversized_integer = b"1" * 5000
    raw = b'{"schema_version":' + oversized_integer + b"}\n"

    with pytest.raises(APPROVAL.ProvenanceError, match="not valid JSON"):
        _validate(raw)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), 2, "schema_version"),
        (("kind",), "teamagent.other-approval", "kind mismatch"),
        (("pipeline",), "tiktok", "pipeline is unsupported"),
        (("pipeline",), "x_buzz", "pipeline mismatch"),
        (("environment",), "prod", "environment is unsupported"),
    ],
)
def test_schema_kind_pipeline_and_environment_mismatch_are_rejected(
    path: tuple[str | int, ...],
    value: Any,
    message: str,
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, value)

    with pytest.raises(APPROVAL.ProvenanceError, match=message):
        _validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("approved_at_utc",), "2026-07-24T11:00:00+00:00"),
        (("expires_at_utc",), "2026-07-24T13:00:00.000Z"),
        (("approved_at_utc",), "2026-02-30T11:00:00Z"),
        (("observations", 0, "observed_at_utc"), "2026-07-24 10:00:00Z"),
    ],
    ids=["offset", "fractional", "invalid-date", "observation-format"],
)
def test_noncanonical_or_invalid_timestamp_is_rejected(
    path: tuple[str | int, ...],
    value: str,
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, value)

    with pytest.raises(
        APPROVAL.ProvenanceError,
        match=r"timestamp|valid UTC|canonical non-empty text",
    ):
        _validate(payload)


def test_future_approval_is_rejected() -> None:
    payload = _passed_payload()
    payload["approved_at_utc"] = "2026-07-24T12:00:01Z"
    payload["expires_at_utc"] = "2026-07-24T13:00:01Z"

    with pytest.raises(APPROVAL.ProvenanceError, match="must not be in the future"):
        _validate(payload)


@pytest.mark.parametrize(
    ("approved_at", "expires_at", "message"),
    [
        ("2026-07-24T11:00:00Z", "2026-07-24T12:00:00Z", "expired"),
        (
            "2026-07-24T11:00:00Z",
            "2026-07-24T11:00:00Z",
            "must be after approved_at_utc",
        ),
    ],
)
def test_invalid_or_expired_approval_window_is_rejected(
    approved_at: str,
    expires_at: str,
    message: str,
) -> None:
    payload = _passed_payload()
    payload["approved_at_utc"] = approved_at
    payload["expires_at_utc"] = expires_at

    with pytest.raises(APPROVAL.ProvenanceError, match=message):
        _validate(payload)


def test_observation_after_approval_time_is_rejected() -> None:
    payload = _passed_payload()
    payload["observations"][0]["observed_at_utc"] = "2026-07-24T11:00:01Z"

    with pytest.raises(APPROVAL.ProvenanceError, match="must not be later"):
        _validate(payload)


@pytest.mark.parametrize("field", ["source_commit", "source_tree_oid"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("A" * 40, "40-character lowercase Git OID"),
        ("1" * 39, "40-character lowercase Git OID"),
        ("3" * 40, "mismatch"),
    ],
)
def test_commit_and_tree_oid_format_or_binding_mismatch_is_rejected(
    field: str,
    value: str,
    message: str,
) -> None:
    payload = _passed_payload()
    payload[field] = value

    with pytest.raises(APPROVAL.ProvenanceError, match=message):
        _validate(payload)


@pytest.mark.parametrize("contract_name", ["inner", "outer"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("A" * 64, "lowercase SHA-256"),
        ("c" * 64, "raw_sha256 mismatch"),
    ],
)
def test_inner_and_outer_contract_sha_format_or_binding_mismatch_is_rejected(
    contract_name: str,
    value: str,
    message: str,
) -> None:
    payload = _passed_payload()
    payload["contracts"][contract_name]["raw_sha256"] = value

    with pytest.raises(APPROVAL.ProvenanceError, match=message):
        _validate(payload)


@pytest.mark.parametrize("contract_name", ["inner", "outer"])
def test_inner_and_outer_contract_schema_version_mismatch_is_rejected(
    contract_name: str,
) -> None:
    payload = _passed_payload()
    payload["contracts"][contract_name]["schema_version"] -= 1

    with pytest.raises(APPROVAL.ProvenanceError, match="schema_version"):
        _validate(payload)


@pytest.mark.parametrize(
    "case",
    ["missing", "extra", "duplicate", "wrong-order", "wrong-value"],
)
def test_observation_set_must_be_exact_unique_sorted_and_value_bound(case: str) -> None:
    payload = _passed_payload()
    observations = payload["observations"]
    if case == "missing":
        observations.pop()
    elif case == "extra":
        observations.append(
            {
                "key": "media.binary.z-extra.sha256",
                "value": "7" * 64,
                "observed_at_utc": "2026-07-24T10:00:00Z",
                "source": "contract://immutable-observation/extra",
            }
        )
    elif case == "duplicate":
        observations[1]["key"] = observations[0]["key"]
        observations[1]["value"] = observations[0]["value"]
    elif case == "wrong-order":
        observations[0], observations[1] = observations[1], observations[0]
    elif case == "wrong-value":
        observations[0]["value"] = f"sha256:{'9' * 64}"
    else:
        raise AssertionError(f"unknown test case: {case}")

    with pytest.raises(APPROVAL.ProvenanceError):
        _validate(payload)


@pytest.mark.parametrize(
    "decision",
    [
        "APPROVED:no required space",
        "REJECTED: evidence did not match",
        " APPROVED: leading whitespace",
    ],
)
def test_decision_requires_exact_approved_prefix(decision: str) -> None:
    payload = _passed_payload()
    payload["decision"] = decision

    with pytest.raises(APPROVAL.ProvenanceError, match="decision"):
        _validate(payload)


@pytest.mark.parametrize(
    "approved_by",
    [
        "arn:aws:iam::718959508629:role/teamagent-dev-other-caller",
        "not-an-iam-role-arn",
    ],
)
def test_approved_by_must_be_the_fixed_expected_role(approved_by: str) -> None:
    payload = _passed_payload()
    payload["approved_by"] = approved_by

    with pytest.raises(APPROVAL.ProvenanceError, match="approved_by"):
        _validate(payload)


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        (
            "publisher_project_arn",
            "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-other-publisher",
        ),
        (
            "publisher_build_id",
            "teamagent-dev-approval-publisher:22222222-2222-4222-8222-222222222222",
        ),
        (
            "kms_key_arn",
            "arn:aws:kms:ap-northeast-1:718959508629:key/33333333-3333-4333-8333-333333333333",
        ),
    ],
)
def test_each_approval_authority_value_is_fixed(
    field: str,
    different_value: str,
) -> None:
    payload = _passed_payload()
    payload["approval_authority"][field] = different_value

    with pytest.raises(APPROVAL.ProvenanceError, match="approval_authority mismatch"):
        _validate(payload)


@pytest.mark.parametrize("base_variant", ["passed", "provisional"])
def test_forced_rollback_gate_rejects_mixed_variants(base_variant: str) -> None:
    payload = _passed_payload() if base_variant == "passed" else _provisional_payload()
    expected_state = "PASSED" if base_variant == "passed" else "PROVISIONAL_INITIAL_RELEASE"
    gate = payload["gates"]["forced_rollback_evidence"]
    if base_variant == "passed":
        gate["provisional_campaign"] = _provisional_campaign()
    else:
        gate["drill_manifest"] = _drill_manifest()

    with pytest.raises(APPROVAL.ProvenanceError, match="unknown"):
        _validate(
            payload,
            expected=_expected(forced_rollback_state=expected_state),
        )


@pytest.mark.parametrize(
    ("payload_factory", "variant_key"),
    [
        (_passed_payload, "drill_manifest"),
        (_provisional_payload, "provisional_campaign"),
    ],
)
def test_forced_rollback_gate_rejects_missing_variant(
    payload_factory: Any,
    variant_key: str,
) -> None:
    payload = payload_factory()
    del payload["gates"]["forced_rollback_evidence"][variant_key]
    expected_state = "PASSED" if variant_key == "drill_manifest" else "PROVISIONAL_INITIAL_RELEASE"

    with pytest.raises(APPROVAL.ProvenanceError, match="missing"):
        _validate(
            payload,
            expected=_expected(forced_rollback_state=expected_state),
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("gates", "forced_rollback_evidence", "gate_version"), 2),
        (("gates", "forced_rollback_evidence", "state"), "SKIPPED"),
    ],
)
def test_forced_rollback_gate_version_and_state_are_fixed(
    path: tuple[str | int, ...],
    value: Any,
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, value)

    with pytest.raises(APPROVAL.ProvenanceError):
        _validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (
            (
                "gates",
                "forced_rollback_evidence",
                "drill_manifest",
                "signing_algorithm",
            ),
            "RSASSA_PKCS1_V1_5_SHA_256",
        ),
        (
            (
                "gates",
                "forced_rollback_evidence",
                "drill_manifest",
                "payload",
                "sha256",
            ),
            "A" * 64,
        ),
    ],
)
def test_passed_gate_rejects_invalid_manifest_semantics(
    path: tuple[str | int, ...],
    value: str,
) -> None:
    payload = _passed_payload()
    _set_path(payload, path, value)

    with pytest.raises(APPROVAL.ProvenanceError):
        _validate(payload)


def test_provisional_gate_rejects_invalid_phase() -> None:
    payload = _provisional_payload()
    payload["gates"]["forced_rollback_evidence"]["provisional_campaign"]["phase"] = "R3"

    with pytest.raises(APPROVAL.ProvenanceError, match="must be R1 or R2"):
        _validate(
            payload,
            expected=_expected(
                forced_rollback_state="PROVISIONAL_INITIAL_RELEASE",
            ),
        )


def test_expected_forced_rollback_state_must_match() -> None:
    expected = _expected(forced_rollback_state="PROVISIONAL_INITIAL_RELEASE")

    with pytest.raises(APPROVAL.ProvenanceError, match="state mismatch"):
        _validate(_passed_payload(), expected=expected)


def test_expected_forced_rollback_state_is_required() -> None:
    expected = _expected()
    del expected["forced_rollback_state"]

    with pytest.raises(APPROVAL.ProvenanceError, match=r"missing=.*forced_rollback_state"):
        _validate(_passed_payload(), expected=expected)


def test_expected_forced_rollback_evidence_must_match_exactly() -> None:
    payload = _passed_payload()
    expected = _expected()
    expected_gate = copy.deepcopy(payload["gates"]["forced_rollback_evidence"])
    expected_gate["drill_manifest"]["payload"]["version_id"] = "other-version"
    expected["forced_rollback_evidence"] = expected_gate

    with pytest.raises(APPROVAL.ProvenanceError, match="evidence mismatch"):
        _validate(payload, expected=expected)


def test_expected_forced_rollback_evidence_comparison_is_type_strict() -> None:
    payload = _passed_payload()
    expected = _expected()
    expected_gate = copy.deepcopy(payload["gates"]["forced_rollback_evidence"])
    expected_gate["gate_version"] = True
    expected["forced_rollback_evidence"] = expected_gate

    with pytest.raises(APPROVAL.ProvenanceError, match="evidence mismatch"):
        _validate(payload, expected=expected)


@pytest.mark.parametrize(
    "expires_at",
    ["2026-07-24T12:00:00Z", "2026-07-24T11:59:59Z"],
)
def test_expired_provisional_campaign_is_rejected(expires_at: str) -> None:
    payload = _provisional_payload()
    payload["gates"]["forced_rollback_evidence"]["provisional_campaign"]["expires_at_utc"] = (
        expires_at
    )

    with pytest.raises(APPROVAL.ProvenanceError, match="provisional campaign is expired"):
        _validate(
            payload,
            expected=_expected(
                forced_rollback_state="PROVISIONAL_INITIAL_RELEASE",
            ),
        )


@pytest.mark.parametrize(
    "render",
    [
        lambda payload: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        lambda payload: APPROVAL.canonical_json_bytes(payload).removesuffix(b"\n"),
        lambda payload: APPROVAL.canonical_json_bytes(payload) + b"\n",
    ],
    ids=["pretty", "missing-lf", "extra-lf"],
)
def test_semantically_valid_but_noncanonical_bytes_are_rejected(render: Any) -> None:
    payload = _passed_payload()

    with pytest.raises(APPROVAL.ProvenanceError, match="bytes are not canonical"):
        _validate(render(payload))


def test_invalid_utf8_is_rejected() -> None:
    with pytest.raises(APPROVAL.ProvenanceError, match="must be UTF-8"):
        _validate(b'{"decision":"\xff"}\n')


def test_approval_canonical_bytes_match_release_evidence_implementation() -> None:
    payload = _passed_payload()
    payload["decision"] = "APPROVED: 日本語を含む承認理由"

    approval_bytes = APPROVAL.canonical_json_bytes(payload)

    assert approval_bytes == RELEASE_EVIDENCE.canonical_bytes(payload)
    assert approval_bytes.endswith(b"\n")
    assert not approval_bytes.endswith(b"\n\n")
    assert "日本語".encode() in approval_bytes
    assert b"\\u65e5" not in approval_bytes


def _git_text(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout


def test_real_git_commit_tree_and_contract_bytes_bind_tree_external_approval(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git_text(repository, "init", "-b", "dev")
    _git_text(repository, "config", "user.name", "Approval Fixture")
    _git_text(repository, "config", "user.email", "approval-fixture@example.invalid")

    inner_relative = Path("infra/codebuild/teamagent_runtime_contract.json")
    outer_relative = Path("infra/codebuild/teamagent_core_media_release_contract.json")
    inner_path = repository / inner_relative
    outer_path = repository / outer_relative
    inner_path.parent.mkdir(parents=True)
    inner_path.write_text(
        '{"schema_version":5,"release":{"ready":false}}\n',
        encoding="utf-8",
    )
    outer_path.write_text(
        '{"schema_version":3,"release":{"ready":false}}\n',
        encoding="utf-8",
    )
    (repository / "README.md").write_text("real Git approval fixture\n", encoding="utf-8")
    _git_text(repository, "add", ".")
    _git_text(repository, "commit", "-m", "fixture contracts")

    commit = _git_text(repository, "rev-parse", "HEAD")
    tree_oid = _git_text(repository, "rev-parse", f"{commit}^{{tree}}")
    inner_raw = _git_bytes(repository, "show", f"{commit}:{inner_relative.as_posix()}")
    outer_raw = _git_bytes(repository, "show", f"{commit}:{outer_relative.as_posix()}")
    inner_sha = hashlib.sha256(inner_raw).hexdigest()
    outer_sha = hashlib.sha256(outer_raw).hexdigest()
    assert inner_raw == inner_path.read_bytes()
    assert outer_raw == outer_path.read_bytes()
    assert len(commit) == len(tree_oid) == 40

    payload = _passed_payload()
    payload["source_commit"] = commit
    payload["source_tree_oid"] = tree_oid
    payload["contracts"]["inner"]["raw_sha256"] = inner_sha
    payload["contracts"]["outer"]["raw_sha256"] = outer_sha
    raw_approval = APPROVAL.canonical_json_bytes(payload)
    approval_path = tmp_path / "external-approval.json"
    assert repository not in approval_path.parents

    head_before = _git_text(repository, "rev-parse", "HEAD")
    tree_before = _git_text(repository, "rev-parse", "HEAD^{tree}")
    approval_path.write_bytes(raw_approval)
    normalized = _validate(
        approval_path.read_bytes(),
        expected=_expected(
            commit=commit,
            tree_oid=tree_oid,
            inner_sha=inner_sha,
            outer_sha=outer_sha,
        ),
    )
    head_after = _git_text(repository, "rev-parse", "HEAD")
    tree_after = _git_text(repository, "rev-parse", "HEAD^{tree}")
    tracked_paths = _git_text(repository, "ls-tree", "-r", "--name-only", commit).splitlines()

    assert normalized["source_commit"] == commit
    assert normalized["source_tree_oid"] == tree_oid
    assert head_before == head_after == commit
    assert tree_before == tree_after == tree_oid
    assert "external-approval.json" not in tracked_paths
    assert _git_text(repository, "status", "--porcelain") == ""
