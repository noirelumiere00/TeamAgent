from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

import teamagent.hmac_durable_state as durable_state_module
from teamagent.hmac_durable_state import (
    DynamoDbHmacStateStore,
    HmacDurableSnapshot,
    HmacRuntimeExpectation,
    ServiceProcessIdentity,
    _service_process_identity,
    _set_state_store_for_testing,
    decision_from_snapshot,
    durable_issuance_guard,
    evaluate_runtime_state,
    runtime_expectations_digest,
)
from teamagent.hmac_keyring import (
    HMAC_PURPOSE_MAIL_DRAFT,
    HmacKeyConfigurationError,
    load_mail_action_hmac_keyring,
)

_PRIMARY = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:mail@primary-version"
_PREVIOUS = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:db@legacy-version"
_LEGACY_WORKER = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:slack/bot-token@legacy-version"
)
_PROVENANCE = "a" * 64
_STALE_PROVENANCE = "b" * 64
_T0 = 2_000_000_000
_DEADLINE = _T0 + 900 + 86_400


def _expectation(
    *,
    provenance: str = _PROVENANCE,
    legacy_worker: bool = False,
) -> HmacRuntimeExpectation:
    return HmacRuntimeExpectation(
        domain="mail_action",
        primary_generation=_PRIMARY,
        previous_generation=_PREVIOUS,
        rotation_started_at=_T0,
        deadline=_DEADLINE,
        rotation_epoch="rotation-2026-07-18",
        provenance=provenance,
        legacy_worker_generation=_LEGACY_WORKER if legacy_worker else None,
        legacy_worker_deadline=_DEADLINE if legacy_worker else None,
    )


def _snapshot(
    *,
    now: int,
    high_water: int | None = None,
    previous_retired: bool = False,
    provenance: str = _PROVENANCE,
    legacy_worker: bool = False,
) -> HmacDurableSnapshot:
    return HmacDurableSnapshot(
        domain="mail_action",
        revision=7,
        clock_revision=3,
        primary_generation=_PRIMARY,
        previous_generation=_PREVIOUS,
        rotation_started_at=_T0,
        deadline=_DEADLINE,
        rotation_epoch="rotation-2026-07-18",
        high_water=now if high_water is None else high_water,
        previous_retired=previous_retired,
        stage="issuing",
        issuer_provenances=frozenset({_PROVENANCE}),
        retired_generations=frozenset({_PREVIOUS}) if previous_retired else frozenset(),
        retired_provenances=frozenset({_STALE_PROVENANCE}),
        trusted_now=now,
        legacy_worker_generation=_LEGACY_WORKER if legacy_worker else None,
        legacy_worker_deadline=_DEADLINE if legacy_worker else None,
    )


def _ddb_item(snapshot: HmacDurableSnapshot) -> dict[str, dict[str, object]]:
    item: dict[str, dict[str, object]] = {
        "scope": {"S": "teamagent/dev"},
        "record": {"S": "DOMAIN#mail_action"},
        "domain": {"S": snapshot.domain},
        "revision": {"N": str(snapshot.revision)},
        "clock_revision": {"N": str(snapshot.clock_revision)},
        "primary_generation": {"S": snapshot.primary_generation},
        "rotation_epoch": {"S": snapshot.rotation_epoch},
        "high_water": {"N": str(snapshot.high_water)},
        "previous_retired": {"BOOL": snapshot.previous_retired},
        "stage": {"S": snapshot.stage},
        "issuer_provenances": {"SS": sorted(snapshot.issuer_provenances)},
    }
    if snapshot.previous_generation is not None:
        item["previous_generation"] = {"S": snapshot.previous_generation}
    if snapshot.rotation_started_at is not None:
        item["rotation_started_at"] = {"N": str(snapshot.rotation_started_at)}
    if snapshot.deadline is not None:
        item["deadline"] = {"N": str(snapshot.deadline)}
    if snapshot.legacy_worker_generation is not None:
        item["legacy_worker_generation"] = {"S": snapshot.legacy_worker_generation}
    if snapshot.legacy_worker_deadline is not None:
        item["legacy_worker_deadline"] = {"N": str(snapshot.legacy_worker_deadline)}
    if snapshot.retired_generations:
        item["retired_generations"] = {"SS": sorted(snapshot.retired_generations)}
    if snapshot.retired_provenances:
        item["retired_provenances"] = {"SS": sorted(snapshot.retired_provenances)}
    if snapshot.cleanup_stage is not None:
        item["cleanup_stage"] = {"S": snapshot.cleanup_stage}
    return item


class _ConditionalFailureError(Exception):
    def __init__(self, code: str = "ConditionalCheckFailedException") -> None:
        self.response = {"Error": {"Code": code}}


class _FakeDynamoDb:
    def __init__(self, snapshot: HmacDurableSnapshot) -> None:
        self.item = _ddb_item(snapshot)
        self.now = snapshot.trusted_now
        self.lock = threading.Lock()
        self.pause_first_read = False
        self.first_read_captured = threading.Event()
        self.allow_first_read = threading.Event()
        self.read_count = 0
        self.update_count = 0
        self.extra_items: dict[str, dict[str, dict[str, object]]] = {}
        self.transactions: list[list[dict[str, object]]] = []

    def get_item(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["ConsistentRead"] is True
        key = kwargs["Key"]
        assert isinstance(key, dict)
        record = str(key["record"]["S"])
        with self.lock:
            self.read_count += 1
            read_number = self.read_count
            source = self.item if record == "DOMAIN#mail_action" else self.extra_items.get(record)
            item = (
                {name: dict(value) for name, value in source.items()}
                if source is not None
                else None
            )
            trusted_now = self.now
        if self.pause_first_read and read_number == 1:
            self.first_read_captured.set()
            assert self.allow_first_read.wait(timeout=5)
        return {
            **({"Item": item} if item is not None else {}),
            "ResponseMetadata": {"HTTPHeaders": {"date": formatdate(trusted_now, usegmt=True)}},
        }

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.update_count += 1
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        condition = str(kwargs["ConditionExpression"])
        expected_condition = (
            "revision = :config_revision"
            " AND (attribute_not_exists(clock_revision)"
            " OR clock_revision = :clock_revision)"
            " AND high_water = :old_high_water"
            " AND previous_retired = :old_retired"
            " AND primary_generation = :primary"
            " AND rotation_epoch = :epoch"
        )
        expected_condition += (
            " AND previous_generation = :previous"
            if "previous_generation" in self.item
            else " AND attribute_not_exists(previous_generation)"
        )
        assert condition == expected_condition
        with self.lock:
            revision = int(str(self.item["revision"]["N"]))
            clock_revision = int(str(self.item.get("clock_revision", {"N": "0"})["N"]))
            high_water = int(str(self.item["high_water"]["N"]))
            condition_matches = (
                revision == int(str(values[":config_revision"]["N"]))
                and clock_revision == int(str(values[":clock_revision"]["N"]))
                and high_water == int(str(values[":old_high_water"]["N"]))
                and self.item["previous_retired"] == values[":old_retired"]
                and self.item["primary_generation"] == values[":primary"]
                and self.item["rotation_epoch"] == values[":epoch"]
                and (
                    self.item.get("previous_generation") == values[":previous"]
                    if ":previous" in values
                    else "previous_generation" not in self.item
                )
            )
            if not condition_matches:
                raise _ConditionalFailureError()
            self.item["high_water"] = {"N": str(values[":next"]["N"])}
            self.item["clock_revision"] = {"N": str(clock_revision + 1)}
            retired = bool(values[":retired"]["BOOL"])
            self.item["previous_retired"] = {"BOOL": retired}
            if ":retired_generation" in values:
                self.item["retired_generations"] = {"SS": list(values[":retired_generation"]["SS"])}
        return {"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(self.now, usegmt=True)}}}

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        items = kwargs["TransactItems"]
        assert isinstance(items, list) and items
        self.transactions.append(items)
        return {"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(self.now, usegmt=True)}}}


@pytest.fixture(autouse=True)
def _clear_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_state_store_for_testing(None)
    for name in (
        "TEAMAGENT_HMAC_STATE_REQUIRED",
        "TEAMAGENT_HMAC_STATE_TABLE",
        "TEAMAGENT_HMAC_STATE_SCOPE",
        "TEAMAGENT_HMAC_ROTATION_EPOCH",
        "TEAMAGENT_HMAC_PROVENANCE",
        "TEAMAGENT_HMAC_RESTART_REQUIRE_COMPLETE",
        "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
        "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    _set_state_store_for_testing(None)


def _configure_runtime(monkeypatch: pytest.MonkeyPatch, *, provenance: str = _PROVENANCE) -> None:
    values = {
        "TEAMAGENT_HMAC_STATE_REQUIRED": "1",
        "TEAMAGENT_HMAC_STATE_TABLE": "teamagent-dev-hmac-state",
        "TEAMAGENT_HMAC_STATE_SCOPE": "teamagent/dev",
        "TEAMAGENT_HMAC_ROTATION_EPOCH": "rotation-2026-07-18",
        "TEAMAGENT_HMAC_PROVENANCE": provenance,
        "MAIL_ACTION_HMAC_PRIMARY_GENERATION": _PRIMARY,
        "MAIL_ACTION_HMAC_PREVIOUS_GENERATION": _PREVIOUS,
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT": str(_T0),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_trusted_high_water_survives_restart_and_clock_rollback() -> None:
    fake = _FakeDynamoDb(_snapshot(now=_DEADLINE - 1))
    first = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    before = first.evaluate(_expectation())
    assert before is not None and before.previous_eligible

    fake.now = _DEADLINE
    at_deadline = first.evaluate(_expectation())
    assert at_deadline is not None and not at_deadline.previous_eligible

    # A fresh process/store object and a rolled-back trusted response cannot undo persisted state.
    restarted = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    fake.now = _DEADLINE - 10_000
    after_restart = restarted.evaluate(_expectation())
    assert after_restart is not None and not after_restart.previous_eligible


def test_cleanup_overlap_accepts_old_and_primary_only_metadata_but_never_expired_key() -> None:
    new_provenance = "c" * 64
    snapshot = replace(
        _snapshot(now=_DEADLINE, previous_retired=True),
        stage="complete",
        issuer_provenances=frozenset({_PROVENANCE, new_provenance}),
        cleanup_stage="authorized",
    )
    old = decision_from_snapshot(snapshot, _expectation())
    primary_only = HmacRuntimeExpectation(
        domain="mail_action",
        primary_generation=_PRIMARY,
        previous_generation=None,
        rotation_started_at=None,
        deadline=None,
        rotation_epoch="rotation-2026-07-18",
        provenance=new_provenance,
    )
    replacement = decision_from_snapshot(snapshot, primary_only)

    assert old is not None and old.issuance_allowed and not old.previous_eligible
    assert replacement is not None and replacement.issuance_allowed
    unauthorized = decision_from_snapshot(
        snapshot,
        replace(primary_only, provenance="d" * 64),
    )
    assert unauthorized is not None and not unauthorized.issuance_allowed

    finalized = replace(
        snapshot,
        previous_generation=None,
        rotation_started_at=None,
        deadline=None,
        cleanup_stage=None,
        issuer_provenances=frozenset({new_provenance}),
        retired_provenances=frozenset({_PROVENANCE}),
    )
    assert decision_from_snapshot(finalized, _expectation()) is None
    assert decision_from_snapshot(finalized, primary_only) is not None


def test_cleanup_primary_only_runtime_cas_binds_durable_previous_pair() -> None:
    new_provenance = "c" * 64
    snapshot = replace(
        _snapshot(now=_DEADLINE, previous_retired=True),
        stage="complete",
        issuer_provenances=frozenset({_PROVENANCE, new_provenance}),
        cleanup_stage="authorized",
    )
    primary_only = replace(
        _expectation(provenance=new_provenance),
        previous_generation=None,
        rotation_started_at=None,
        deadline=None,
    )
    fake = _FakeDynamoDb(snapshot)
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )

    decision = store.evaluate(primary_only)

    assert decision is not None and decision.issuance_allowed
    assert not decision.previous_eligible
    assert fake.item["previous_generation"] == {"S": _PREVIOUS}
    assert fake.item["previous_retired"] == {"BOOL": True}


def test_deadline_interleaving_cannot_reenable_previous() -> None:
    fake = _FakeDynamoDb(_snapshot(now=_DEADLINE - 1))
    fake.pause_first_read = True
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    results: list[bool] = []

    def evaluate() -> None:
        decision = store.evaluate(_expectation())
        results.append(bool(decision and decision.previous_eligible))

    first = threading.Thread(target=evaluate)
    second = threading.Thread(target=evaluate)
    first.start()
    assert fake.first_read_captured.wait(timeout=5)
    fake.now = _DEADLINE
    second.start()
    second.join(timeout=5)
    fake.allow_first_read.set()
    first.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert results == [False, False]
    assert fake.item["previous_retired"] == {"BOOL": True}


def test_hot_runtime_decisions_do_not_serialize_on_domain_writes() -> None:
    fake = _FakeDynamoDb(_snapshot(now=_T0 + 100))
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    workers = 64
    barrier = threading.Barrier(workers)
    decisions: list[bool] = []
    decision_lock = threading.Lock()

    def evaluate() -> None:
        barrier.wait(timeout=5)
        decision = store.evaluate(_expectation())
        with decision_lock:
            decisions.append(bool(decision and decision.issuance_allowed))

    threads = [threading.Thread(target=evaluate) for _index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert decisions == [True] * workers
    assert fake.item["revision"] == {"N": "7"}
    assert fake.item["clock_revision"] == {"N": "3"}
    assert fake.update_count == 0


def test_service_process_identity_binds_main_pid_start_and_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    app = release_root / "app"
    executable = app / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"runtime")
    executable.chmod(0o555)
    monkeypatch.setenv("TEAMAGENT_HMAC_MAIN_PID", "321")
    monkeypatch.setenv("TEAMAGENT_HMAC_RELEASE_ROOT", str(release_root))
    monkeypatch.setenv("TEAMAGENT_HMAC_RELEASE_TREE_SHA256", "1" * 64)
    monkeypatch.setenv("TEAMAGENT_HMAC_RUNTIME_EXECUTABLE_SHA256", "2" * 64)
    monkeypatch.setattr(
        durable_state_module,
        "_process_observation",
        lambda _pid: (
            5000,
            _T0 + 50,
            b"python -m teamagent.connect_web",
            app,
            executable,
        ),
    )
    monkeypatch.setattr(durable_state_module, "file_sha256", lambda _path: "2" * 64)
    monkeypatch.setattr(durable_state_module, "verify_release", lambda *_args, **_kwargs: True)

    assert _service_process_identity("connect") == ServiceProcessIdentity(
        pid=321,
        started_ticks=5000,
        started_at=_T0 + 50,
        cwd=str(app),
        executable=str(executable),
        executable_sha256="2" * 64,
        release_root=str(release_root),
        release_tree_sha256="1" * 64,
    )
    assert _service_process_identity("bot") is None


def test_restart_attestation_requires_exact_nonce_and_post_request_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation = _expectation()
    fake = _FakeDynamoDb(_snapshot(now=_T0 + 100))
    config_digest = runtime_expectations_digest((expectation,))
    assert config_digest is not None
    nonce = "f" * 64
    artifact = "c" * 64
    release_root = "/opt/teamagent/releases/candidate"
    release_tree = "d" * 64
    runtime_executable = "e" * 64
    fake.extra_items[f"RESTART#{expectation.rotation_epoch}#{expectation.provenance}"] = {
        "scope": {"S": "teamagent/dev"},
        "record": {"S": f"RESTART#{expectation.rotation_epoch}#{expectation.provenance}"},
        "stage": {"S": "requested"},
        "restart_nonce": {"S": nonce},
        "provenance": {"S": expectation.provenance},
        "artifact_sha256": {"S": artifact},
        "config_digest": {"S": config_digest},
        "release_root": {"S": release_root},
        "release_tree_sha256": {"S": release_tree},
        "runtime_executable_sha256": {"S": runtime_executable},
        "requested_at": {"N": str(_T0 + 90)},
    }
    monkeypatch.setenv("TEAMAGENT_HMAC_WORKER_ID", "i-0123456789abcdef0")
    monkeypatch.setenv("TEAMAGENT_HMAC_RESTART_NONCE", nonce)
    monkeypatch.setenv("TEAMAGENT_HMAC_SERVICE", "connect")
    monkeypatch.setenv("TEAMAGENT_HMAC_ARTIFACT_SHA256", artifact)
    monkeypatch.setenv("TEAMAGENT_HMAC_SERVICE_HEALTH", "1")
    monkeypatch.setattr(
        durable_state_module,
        "_service_process_identity",
        lambda _service: ServiceProcessIdentity(
            pid=321,
            started_ticks=5000,
            started_at=_T0 + 91,
            cwd=f"{release_root}/app",
            executable=f"{release_root}/app/.venv/bin/python",
            executable_sha256=runtime_executable,
            release_root=release_root,
            release_tree_sha256=release_tree,
        ),
    )
    monkeypatch.setattr(
        durable_state_module,
        "_service_health_verified",
        lambda service, pid, ticks: service == "connect" and pid == 321 and ticks == 5000,
    )
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )

    assert store.attest_worker((expectation,))
    service_record = fake.transactions[-1][-1]["Put"]["Item"]  # type: ignore[index]
    assert service_record["restart_nonce"] == {"S": nonce}
    assert service_record["main_pid"] == {"N": "321"}
    assert service_record["process_started_at"] == {"N": str(_T0 + 91)}
    assert service_record["health_verified"] == {"BOOL": True}
    assert service_record["active_port"] == {"N": "8788"}
    assert service_record["port_owner_pid"] == {"N": "321"}
    assert service_record["health_endpoint"] == {"S": "http://127.0.0.1:8788/healthz"}

    restart_item = fake.extra_items[
        f"RESTART#{expectation.rotation_epoch}#{expectation.provenance}"
    ]
    restart_item["stage"] = {"S": "complete"}
    monkeypatch.setenv("TEAMAGENT_HMAC_RESTART_REQUIRE_COMPLETE", "1")
    assert store.attest_worker((expectation,))
    assert fake.transactions[-1][-1]["Put"]["Item"]["main_pid"] == {"N": "321"}  # type: ignore[index]
    restart_item["stage"] = {"S": "requested"}
    assert not store.attest_worker((expectation,))
    monkeypatch.delenv("TEAMAGENT_HMAC_RESTART_REQUIRE_COMPLETE")

    monkeypatch.setattr(
        durable_state_module,
        "_service_health_verified",
        lambda _service, _pid, _ticks: False,
    )
    assert not store.attest_worker((expectation,))
    monkeypatch.setattr(
        durable_state_module,
        "_service_health_verified",
        lambda service, pid, ticks: service == "connect" and pid == 321 and ticks == 5000,
    )
    monkeypatch.setenv("TEAMAGENT_HMAC_RESTART_NONCE", "e" * 64)
    assert not store.attest_worker((expectation,))
    monkeypatch.setenv("TEAMAGENT_HMAC_RESTART_NONCE", nonce)
    monkeypatch.setattr(
        durable_state_module,
        "_service_process_identity",
        lambda _service: ServiceProcessIdentity(
            pid=321,
            started_ticks=5000,
            started_at=_T0 + 89,
            cwd=f"{release_root}/app",
            executable=f"{release_root}/app/.venv/bin/python",
            executable_sha256=runtime_executable,
            release_root=release_root,
            release_tree_sha256=release_tree,
        ),
    )
    assert not store.attest_worker((expectation,))


def test_stale_generation_and_provenance_replay_fail_closed() -> None:
    current = _snapshot(now=_T0 + 100)
    stale_generation = replace(_expectation(), primary_generation=f"{_PRIMARY}-stale")
    assert decision_from_snapshot(current, stale_generation) is None
    assert decision_from_snapshot(current, _expectation(provenance=_STALE_PROVENANCE)) is None


def test_primary_only_runtime_succeeds_after_durable_retirement_cleanup() -> None:
    expectation = replace(
        _expectation(),
        previous_generation=None,
        rotation_started_at=None,
        deadline=None,
    )
    snapshot = replace(
        _snapshot(now=_DEADLINE + 1, previous_retired=True),
        previous_generation=None,
        rotation_started_at=None,
        deadline=None,
        stage="complete",
        retired_generations=frozenset({_PREVIOUS}),
    )

    fake = _FakeDynamoDb(snapshot)
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    decision = store.evaluate(expectation)

    assert decision is not None
    assert not decision.previous_eligible
    assert decision.issuance_allowed
    assert "previous_generation" not in fake.item


def test_legacy_worker_generation_is_durable_and_retires_with_previous() -> None:
    expectation = _expectation(legacy_worker=True)
    fake = _FakeDynamoDb(_snapshot(now=_DEADLINE, legacy_worker=True))
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )

    decision = store.evaluate(expectation)

    assert decision is not None and not decision.previous_eligible
    assert set(fake.item["retired_generations"]["SS"]) == {
        _PREVIOUS,
        _LEGACY_WORKER,
    }
    assert (
        store.evaluate(
            replace(
                expectation,
                legacy_worker_generation=f"{_LEGACY_WORKER}-stale",
            )
        )
        is None
    )


def _process_decision(
    snapshot_values: dict[str, Any],
    expectation_values: dict[str, Any],
    output: Any,
) -> None:
    snapshot_values["issuer_provenances"] = frozenset(snapshot_values["issuer_provenances"])
    snapshot_values["retired_generations"] = frozenset(snapshot_values["retired_generations"])
    snapshot_values["retired_provenances"] = frozenset(snapshot_values["retired_provenances"])
    decision = decision_from_snapshot(
        HmacDurableSnapshot(**snapshot_values),
        HmacRuntimeExpectation(**expectation_values),
    )
    output.put(bool(decision and decision.previous_eligible))


def test_multiprocess_observes_persisted_retirement() -> None:
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    retired = _snapshot(now=_DEADLINE, previous_retired=True)
    process = ctx.Process(
        target=_process_decision,
        args=(asdict(retired), asdict(_expectation()), output),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert output.get(timeout=2) is False


def test_subprocess_has_no_process_local_reenable_path(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    retired = asdict(_snapshot(now=_DEADLINE, previous_retired=True))
    for name in ("issuer_provenances", "retired_generations", "retired_provenances"):
        retired[name] = sorted(retired[name])
    state_path.write_text(
        json.dumps({"snapshot": retired, "expectation": asdict(_expectation())}),
        encoding="utf-8",
    )
    code = """
import json, sys
from teamagent.hmac_durable_state import (
    HmacDurableSnapshot, HmacRuntimeExpectation, decision_from_snapshot,
)
raw = json.load(open(sys.argv[1], encoding="utf-8"))
for name in ("issuer_provenances", "retired_generations", "retired_provenances"):
    raw["snapshot"][name] = frozenset(raw["snapshot"][name])
decision = decision_from_snapshot(
    HmacDurableSnapshot(**raw["snapshot"]),
    HmacRuntimeExpectation(**raw["expectation"]),
)
raise SystemExit(0 if decision is not None and not decision.previous_eligible else 9)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(state_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_runtime_guard_rereads_store_immediately_before_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    fake = _FakeDynamoDb(_snapshot(now=_T0 + 100))
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    _set_state_store_for_testing(store)

    decision = evaluate_runtime_state(domain="mail_action", max_token_ttl_s=86_400)
    assert decision is not None and decision.issuance_allowed
    fake.item["retired_provenances"] = {"SS": [_PROVENANCE]}
    assert not durable_issuance_guard(decision.expectation)


def test_loaded_keyring_cannot_sign_after_provenance_is_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "dedicated-mail-key-" + "k" * 40)
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
        "postgresql://legacy:password@db.internal/teamagent",
    )
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    fake = _FakeDynamoDb(_snapshot(now=_T0 + 100))
    store = DynamoDbHmacStateStore(
        table_name="teamagent-dev-hmac-state",
        scope="teamagent/dev",
        region="ap-northeast-1",
        client=fake,
    )
    _set_state_store_for_testing(store)

    keyring = load_mail_action_hmac_keyring(now=1)
    assert keyring is not None
    fake.item["retired_provenances"] = {"SS": [_PROVENANCE]}
    with pytest.raises(HmacKeyConfigurationError):
        keyring.sign(b"payload", purpose=HMAC_PURPOSE_MAIL_DRAFT, digest_bytes=16)
