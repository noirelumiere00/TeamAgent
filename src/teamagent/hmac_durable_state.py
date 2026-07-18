"""Durable, secret-free HMAC runtime state backed by DynamoDB.

Production tasks set ``TEAMAGENT_HMAC_STATE_REQUIRED=1``. In that mode a keyring is usable only
when its generation metadata matches the strongly consistent durable domain record. The record
contains no key material:

* primary/previous ``secret ARN@VersionId`` generation identifiers;
* fixed rotation T0, exclusive deadline, and rotation epoch;
* an AWS-server-time high-water mark and persisted previous-retired bit;
* issuer provenance allow/retire sets and a rollout stage.

The local wall clock is never authoritative in required mode. Every read obtains AWS server time
from the DynamoDB response and advances the shared high-water mark with a conditional write.
Consequently process restarts, multiple workers, clock rollback, and replayed stale task revisions
cannot restore a previous key or authorize an old issuer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

HMAC_STATE_REQUIRED_ENV = "TEAMAGENT_HMAC_STATE_REQUIRED"
HMAC_STATE_TABLE_ENV = "TEAMAGENT_HMAC_STATE_TABLE"
HMAC_STATE_SCOPE_ENV = "TEAMAGENT_HMAC_STATE_SCOPE"
HMAC_ROTATION_EPOCH_ENV = "TEAMAGENT_HMAC_ROTATION_EPOCH"
HMAC_PROVENANCE_ENV = "TEAMAGENT_HMAC_PROVENANCE"
HMAC_WORKER_ID_ENV = "TEAMAGENT_HMAC_WORKER_ID"

_MAX_EPOCH = 9_999_999_999
_ROLLOUT_OVERLAP_S = 900
_GENERATION_RE = re.compile(r"^[!-~]{1,2048}$")
_ROTATION_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVENANCE_RE = re.compile(r"^[a-f0-9]{64}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_WORKER_ID_RE = re.compile(r"^i-[a-f0-9]{8,32}$")
_DOMAIN_ENV = {
    "mail_action": {
        "primary": "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
        "previous": "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
        "t0": "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "legacy_worker": "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
    },
    "report_link": {
        "primary": "REPORT_LINK_HMAC_PRIMARY_GENERATION",
        "previous": "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
        "t0": "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    },
}
_RUNTIME_STAGES = frozenset({"preload", "issuing", "complete", "retired"})


class HmacDurableStateError(RuntimeError):
    """Durable HMAC metadata is unavailable or inconsistent."""


@dataclass(frozen=True)
class HmacRuntimeExpectation:
    """Secret-free metadata compiled from one task revision's environment."""

    domain: str
    primary_generation: str
    previous_generation: str | None
    rotation_started_at: int | None
    deadline: int | None
    rotation_epoch: str
    provenance: str
    legacy_worker_generation: str | None = None
    legacy_worker_deadline: int | None = None


@dataclass(frozen=True)
class HmacDurableSnapshot:
    """One strongly consistent domain record plus trusted response time."""

    domain: str
    revision: int
    primary_generation: str
    previous_generation: str | None
    rotation_started_at: int | None
    deadline: int | None
    rotation_epoch: str
    high_water: int
    previous_retired: bool
    stage: str
    issuer_provenances: frozenset[str]
    retired_generations: frozenset[str]
    retired_provenances: frozenset[str]
    trusted_now: int
    legacy_worker_generation: str | None = None
    legacy_worker_deadline: int | None = None


@dataclass(frozen=True)
class HmacRuntimeDecision:
    """Result used by a keyring without exposing durable-store implementation details."""

    effective_now: int
    previous_eligible: bool
    issuance_allowed: bool
    expectation: HmacRuntimeExpectation


class HmacStateStore(Protocol):
    """Runtime subset required from a durable HMAC state store."""

    def evaluate(self, expectation: HmacRuntimeExpectation) -> HmacRuntimeDecision | None:
        """Validate/advance durable state and return a fail-closed runtime decision."""

    def attest_worker(
        self,
        expectations: tuple[HmacRuntimeExpectation, ...],
    ) -> bool:
        """CAS-attest that one worker loaded every required domain."""


def _stable_generation(value: object) -> str | None:
    if type(value) is not str or _GENERATION_RE.fullmatch(value) is None:
        return None
    return value


def _bounded_epoch(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > _MAX_EPOCH:
        return None
    return value


def _parse_epoch_text(value: object) -> int | None:
    if type(value) is not str or not value or len(value) > 10 or not value.isascii():
        return None
    if not value.isdecimal():
        return None
    return _bounded_epoch(int(value))


def durable_state_required() -> bool:
    """Return whether production durable-state enforcement is enabled.

    A present value other than the exact string ``1`` is a configuration error, not an opt-out.
    """

    raw = os.environ.get(HMAC_STATE_REQUIRED_ENV)
    if raw is None:
        return False
    if raw != "1":
        raise HmacDurableStateError("durable HMAC state configuration is invalid")
    return True


def load_runtime_expectation(
    *,
    domain: str,
    max_token_ttl_s: int,
) -> HmacRuntimeExpectation | None:
    """Compile one task's non-secret runtime metadata; return ``None`` on any drift."""

    names = _DOMAIN_ENV.get(domain)
    if names is None:
        return None
    primary = _stable_generation(os.environ.get(names["primary"]))
    rotation_epoch = os.environ.get(HMAC_ROTATION_EPOCH_ENV)
    provenance = os.environ.get(HMAC_PROVENANCE_ENV)
    if (
        primary is None
        or type(rotation_epoch) is not str
        or _ROTATION_EPOCH_RE.fullmatch(rotation_epoch) is None
        or type(provenance) is not str
        or _PROVENANCE_RE.fullmatch(provenance) is None
    ):
        return None

    previous_raw = os.environ.get(names["previous"])
    t0_raw = os.environ.get(names["t0"])
    legacy_worker_raw = os.environ.get(names["legacy_worker"]) if domain == "mail_action" else None
    if previous_raw is None and t0_raw is None:
        if legacy_worker_raw is not None:
            return None
        return HmacRuntimeExpectation(
            domain=domain,
            primary_generation=primary,
            previous_generation=None,
            rotation_started_at=None,
            deadline=None,
            rotation_epoch=rotation_epoch,
            provenance=provenance,
        )
    previous = _stable_generation(previous_raw)
    t0 = _parse_epoch_text(t0_raw)
    if previous is None or t0 is None or previous == primary:
        return None
    deadline = t0 + _ROLLOUT_OVERLAP_S + max_token_ttl_s
    if deadline > _MAX_EPOCH:
        return None
    legacy_worker = _stable_generation(legacy_worker_raw) if legacy_worker_raw is not None else None
    if legacy_worker_raw is not None and (
        legacy_worker is None or legacy_worker in {primary, previous}
    ):
        return None
    return HmacRuntimeExpectation(
        domain=domain,
        primary_generation=primary,
        previous_generation=previous,
        rotation_started_at=t0,
        deadline=deadline,
        rotation_epoch=rotation_epoch,
        provenance=provenance,
        legacy_worker_generation=legacy_worker,
        legacy_worker_deadline=deadline if legacy_worker is not None else None,
    )


def _trusted_epoch(response: object) -> int | None:
    if type(response) is not dict:
        return None
    metadata = response.get("ResponseMetadata")
    if type(metadata) is not dict:
        return None
    headers = metadata.get("HTTPHeaders")
    if type(headers) is not dict:
        return None
    raw = headers.get("date")
    if type(raw) is not str:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            return None
        return _bounded_epoch(int(parsed.timestamp()))
    except (OverflowError, TypeError, ValueError):
        return None


def _item_string(item: dict[str, Any], name: str, *, optional: bool = False) -> str | None:
    attribute = item.get(name)
    if attribute is None and optional:
        return None
    if type(attribute) is not dict or type(attribute.get("S")) is not str:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    value = attribute["S"]
    return value if isinstance(value, str) else None


def _item_number(item: dict[str, Any], name: str, *, optional: bool = False) -> int | None:
    attribute = item.get(name)
    if attribute is None and optional:
        return None
    if type(attribute) is not dict or type(attribute.get("N")) is not str:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    raw = attribute["N"]
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    value = _bounded_epoch(int(raw))
    if value is None:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    return value


def _item_bool(item: dict[str, Any], name: str) -> bool:
    attribute = item.get(name)
    if type(attribute) is not dict or type(attribute.get("BOOL")) is not bool:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    value = attribute["BOOL"]
    return value if isinstance(value, bool) else False


def _item_string_set(item: dict[str, Any], name: str) -> frozenset[str]:
    attribute = item.get(name)
    if attribute is None:
        return frozenset()
    if type(attribute) is not dict or type(attribute.get("SS")) is not list:
        raise HmacDurableStateError("durable HMAC state record is invalid")
    values = attribute["SS"]
    if any(
        type(value) is not str
        or not value
        or len(value) > 2048
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
        for value in values
    ):
        raise HmacDurableStateError("durable HMAC state record is invalid")
    return frozenset(values)


def _matches(snapshot: HmacDurableSnapshot, expected: HmacRuntimeExpectation) -> bool:
    return (
        snapshot.domain == expected.domain
        and snapshot.primary_generation == expected.primary_generation
        and snapshot.previous_generation == expected.previous_generation
        and snapshot.rotation_started_at == expected.rotation_started_at
        and snapshot.deadline == expected.deadline
        and snapshot.legacy_worker_generation == expected.legacy_worker_generation
        and snapshot.legacy_worker_deadline == expected.legacy_worker_deadline
        and snapshot.rotation_epoch == expected.rotation_epoch
        and snapshot.stage in _RUNTIME_STAGES
        and expected.primary_generation not in snapshot.retired_generations
        and expected.provenance not in snapshot.retired_provenances
    )


def decision_from_snapshot(
    snapshot: HmacDurableSnapshot,
    expectation: HmacRuntimeExpectation,
) -> HmacRuntimeDecision | None:
    """Evaluate an already-CAS-advanced snapshot; useful for deterministic process tests."""

    if not _matches(snapshot, expectation):
        return None
    previous_eligible = (
        expectation.previous_generation is not None
        and expectation.deadline is not None
        and not snapshot.previous_retired
        and snapshot.high_water < expectation.deadline
    )
    issuance_allowed = (
        snapshot.stage in {"issuing", "complete"}
        and expectation.provenance in snapshot.issuer_provenances
    )
    return HmacRuntimeDecision(
        effective_now=snapshot.high_water,
        previous_eligible=previous_eligible,
        issuance_allowed=issuance_allowed,
        expectation=expectation,
    )


def runtime_expectations_digest(
    expectations: tuple[HmacRuntimeExpectation, ...],
) -> str | None:
    """Return a secret-free digest of exact worker generation/T0 expectations."""

    if not expectations or len({item.domain for item in expectations}) != len(expectations):
        return None
    encoded = json.dumps(
        [
            {
                "deadline": item.deadline,
                "domain": item.domain,
                "previous_generation": item.previous_generation,
                "primary_generation": item.primary_generation,
                "legacy_worker_deadline": item.legacy_worker_deadline,
                "legacy_worker_generation": item.legacy_worker_generation,
                "rotation_epoch": item.rotation_epoch,
                "rotation_started_at": item.rotation_started_at,
            }
            for item in sorted(expectations, key=lambda item: item.domain)
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DynamoDbHmacStateStore:
    """Strongly consistent DynamoDB implementation with conditional high-water updates."""

    def __init__(
        self,
        *,
        table_name: str,
        scope: str,
        region: str,
        client: Any | None = None,
    ) -> None:
        if _TABLE_RE.fullmatch(table_name) is None or _SCOPE_RE.fullmatch(scope) is None:
            raise HmacDurableStateError("durable HMAC state configuration is invalid")
        self._table_name = table_name
        self._scope = scope
        self._region = region
        self._ddb = client

    @classmethod
    def from_env(cls) -> DynamoDbHmacStateStore:
        table_name = os.environ.get(HMAC_STATE_TABLE_ENV, "")
        scope = os.environ.get(HMAC_STATE_SCOPE_ENV, "")
        region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        return cls(table_name=table_name, scope=scope, region=region)

    def _client(self) -> Any:
        if self._ddb is None:
            import boto3

            self._ddb = boto3.session.Session().client("dynamodb", region_name=self._region)
        return self._ddb

    def _key(self, domain: str) -> dict[str, dict[str, str]]:
        return {
            "scope": {"S": self._scope},
            "record": {"S": f"DOMAIN#{domain}"},
        }

    def _read(self, domain: str) -> HmacDurableSnapshot | None:
        response = self._client().get_item(
            TableName=self._table_name,
            Key=self._key(domain),
            ConsistentRead=True,
        )
        trusted_now = _trusted_epoch(response)
        item = response.get("Item") if type(response) is dict else None
        if trusted_now is None or type(item) is not dict:
            return None
        try:
            record_domain = _item_string(item, "domain")
            primary = _stable_generation(_item_string(item, "primary_generation"))
            previous_raw = _item_string(item, "previous_generation", optional=True)
            previous = _stable_generation(previous_raw) if previous_raw is not None else None
            rotation_epoch = _item_string(item, "rotation_epoch")
            stage = _item_string(item, "stage")
            revision = _item_number(item, "revision")
            high_water = _item_number(item, "high_water")
            t0 = _item_number(item, "rotation_started_at", optional=True)
            deadline = _item_number(item, "deadline", optional=True)
            legacy_worker_raw = _item_string(
                item,
                "legacy_worker_generation",
                optional=True,
            )
            legacy_worker = (
                _stable_generation(legacy_worker_raw) if legacy_worker_raw is not None else None
            )
            legacy_worker_deadline = _item_number(
                item,
                "legacy_worker_deadline",
                optional=True,
            )
            if (
                record_domain != domain
                or primary is None
                or (previous_raw is not None and previous is None)
                or type(rotation_epoch) is not str
                or _ROTATION_EPOCH_RE.fullmatch(rotation_epoch) is None
                or stage not in _RUNTIME_STAGES
                or revision is None
                or high_water is None
                or ((previous is None) != (t0 is None))
                or ((previous is None) != (deadline is None))
                or (legacy_worker_raw is not None and legacy_worker is None)
                or ((legacy_worker is None) != (legacy_worker_deadline is None))
                or (
                    legacy_worker is not None
                    and (
                        domain != "mail_action"
                        or previous is None
                        or legacy_worker in {primary, previous}
                        or legacy_worker_deadline != deadline
                    )
                )
            ):
                return None
            return HmacDurableSnapshot(
                domain=domain,
                revision=revision,
                primary_generation=primary,
                previous_generation=previous,
                rotation_started_at=t0,
                deadline=deadline,
                rotation_epoch=rotation_epoch,
                high_water=high_water,
                previous_retired=_item_bool(item, "previous_retired"),
                stage=stage,
                issuer_provenances=_item_string_set(item, "issuer_provenances"),
                retired_generations=_item_string_set(item, "retired_generations"),
                retired_provenances=_item_string_set(item, "retired_provenances"),
                trusted_now=trusted_now,
                legacy_worker_generation=legacy_worker,
                legacy_worker_deadline=legacy_worker_deadline,
            )
        except (HmacDurableStateError, KeyError):
            return None

    @staticmethod
    def _conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if type(response) is not dict:
            return False
        error = response.get("Error")
        return type(error) is dict and error.get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }

    def _advance_once(
        self,
        snapshot: HmacDurableSnapshot,
        expected: HmacRuntimeExpectation,
    ) -> HmacDurableSnapshot | None:
        high_water = max(snapshot.high_water, snapshot.trusted_now)
        should_retire = (
            expected.previous_generation is not None
            and expected.deadline is not None
            and high_water >= expected.deadline
        )
        retired = snapshot.previous_retired or should_retire

        # Every decision performs a revision CAS, even when neither the trusted clock nor the
        # retired bit changes. Otherwise a thread that read just before the deadline could return
        # an eligible previous key after another thread had already persisted retirement.
        expression = (
            "SET high_water = :next, previous_retired = :retired, revision = revision + :one"
        )
        values: dict[str, Any] = {
            ":revision": {"N": str(snapshot.revision)},
            ":old_high_water": {"N": str(snapshot.high_water)},
            ":next": {"N": str(high_water)},
            ":retired": {"BOOL": retired},
            ":one": {"N": "1"},
        }
        if should_retire and expected.previous_generation is not None:
            expression += " ADD retired_generations :retired_generation"
            retired_now = [expected.previous_generation]
            if expected.legacy_worker_generation is not None:
                retired_now.append(expected.legacy_worker_generation)
            values[":retired_generation"] = {"SS": retired_now}
        try:
            self._client().update_item(
                TableName=self._table_name,
                Key=self._key(expected.domain),
                UpdateExpression=expression,
                ConditionExpression="revision = :revision AND high_water = :old_high_water",
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if self._conditional_failure(exc):
                return None
            raise HmacDurableStateError("durable HMAC state update failed") from exc
        retired_generations = snapshot.retired_generations
        if should_retire and expected.previous_generation is not None:
            newly_retired = {expected.previous_generation}
            if expected.legacy_worker_generation is not None:
                newly_retired.add(expected.legacy_worker_generation)
            retired_generations = retired_generations | newly_retired
        return replace(
            snapshot,
            revision=snapshot.revision + 1,
            high_water=high_water,
            previous_retired=retired,
            retired_generations=frozenset(retired_generations),
        )

    def evaluate(self, expectation: HmacRuntimeExpectation) -> HmacRuntimeDecision | None:
        for _attempt in range(8):
            snapshot = self._read(expectation.domain)
            if snapshot is None or not _matches(snapshot, expectation):
                return None
            advanced = self._advance_once(snapshot, expectation)
            if advanced is None:
                continue
            return decision_from_snapshot(advanced, expectation)
        return None

    def attest_worker(
        self,
        expectations: tuple[HmacRuntimeExpectation, ...],
    ) -> bool:
        if not expectations:
            return False
        snapshots: list[HmacDurableSnapshot] = []
        for expectation in expectations:
            snapshot = self._read(expectation.domain)
            if snapshot is None or decision_from_snapshot(snapshot, expectation) is None:
                return False
            snapshots.append(snapshot)
        checked_at = max(snapshot.trusted_now for snapshot in snapshots)
        rotation_epochs = {expectation.rotation_epoch for expectation in expectations}
        provenances = {expectation.provenance for expectation in expectations}
        config_digest = runtime_expectations_digest(expectations)
        if len(rotation_epochs) != 1 or len(provenances) != 1 or config_digest is None:
            return False
        provenance = next(iter(provenances))
        worker_id = os.environ.get(HMAC_WORKER_ID_ENV)
        if type(worker_id) is not str or _WORKER_ID_RE.fullmatch(worker_id) is None:
            return False
        transaction: list[dict[str, Any]] = []
        for snapshot in snapshots:
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": self._key(snapshot.domain),
                        "ConditionExpression": (
                            "revision = :revision AND primary_generation = :primary"
                        ),
                        "ExpressionAttributeValues": {
                            ":revision": {"N": str(snapshot.revision)},
                            ":primary": {"S": snapshot.primary_generation},
                        },
                    }
                }
            )
        transaction.append(
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": {
                        "scope": {"S": self._scope},
                        "record": {"S": f"WORKER#{provenance}"},
                        "provenance": {"S": provenance},
                        "worker_id": {"S": worker_id},
                        "rotation_epoch": {"S": next(iter(rotation_epochs))},
                        "config_digest": {"S": config_digest},
                        "loaded_domains": {
                            "SS": sorted(expectation.domain for expectation in expectations)
                        },
                        "checked_at": {"N": str(checked_at)},
                        "expires_at": {"N": str(checked_at + 300)},
                    },
                }
            }
        )
        try:
            self._client().transact_write_items(TransactItems=transaction)
        except Exception as exc:
            if self._conditional_failure(exc):
                return False
            raise HmacDurableStateError("worker HMAC readiness attestation failed") from exc
        return True


_store_override: HmacStateStore | None = None
_store_cache: tuple[int, str, str, HmacStateStore] | None = None


def _state_store() -> HmacStateStore:
    global _store_cache
    if _store_override is not None:
        return _store_override
    table = os.environ.get(HMAC_STATE_TABLE_ENV, "")
    scope = os.environ.get(HMAC_STATE_SCOPE_ENV, "")
    pid = os.getpid()
    if (
        _store_cache is None
        or _store_cache[0] != pid
        or _store_cache[1] != table
        or _store_cache[2] != scope
    ):
        _store_cache = (pid, table, scope, DynamoDbHmacStateStore.from_env())
    return _store_cache[3]


def _set_state_store_for_testing(store: HmacStateStore | None) -> None:
    """Test-only dependency injection; production never selects an alternate backend."""

    global _store_override, _store_cache
    _store_override = store
    _store_cache = None


def evaluate_runtime_state(
    *,
    domain: str,
    max_token_ttl_s: int,
) -> HmacRuntimeDecision | None:
    """Evaluate durable state when required, otherwise return ``None`` as an opt-out signal."""

    if not durable_state_required():
        return None
    expectation = load_runtime_expectation(domain=domain, max_token_ttl_s=max_token_ttl_s)
    if expectation is None:
        return None
    try:
        return _state_store().evaluate(expectation)
    except Exception:
        return None


def durable_issuance_guard(
    expectation: HmacRuntimeExpectation,
) -> bool:
    """Re-read/CAS durable state immediately before every signature."""

    try:
        decision = _state_store().evaluate(expectation)
    except Exception:
        return False
    return decision is not None and decision.issuance_allowed


def require_runtime_startup(
    domains: tuple[tuple[str, int], ...],
    *,
    worker_attestation: bool = False,
) -> None:
    """Fail startup unless every required domain matches durable state.

    ``worker_attestation`` writes only generation/provenance readiness metadata under a five-minute
    TTL. It never writes or reads HMAC key material.
    """

    if not durable_state_required():
        return
    expectations: list[HmacRuntimeExpectation] = []
    for domain, max_ttl in domains:
        expectation = load_runtime_expectation(domain=domain, max_token_ttl_s=max_ttl)
        if expectation is None:
            raise HmacDurableStateError("durable HMAC startup check failed")
        decision = _state_store().evaluate(expectation)
        if decision is None:
            raise HmacDurableStateError("durable HMAC startup check failed")
        expectations.append(expectation)
    if worker_attestation and not _state_store().attest_worker(tuple(expectations)):
        raise HmacDurableStateError("worker HMAC readiness check failed")


__all__ = [
    "HMAC_PROVENANCE_ENV",
    "HMAC_ROTATION_EPOCH_ENV",
    "HMAC_STATE_REQUIRED_ENV",
    "HMAC_STATE_SCOPE_ENV",
    "HMAC_STATE_TABLE_ENV",
    "HMAC_WORKER_ID_ENV",
    "DynamoDbHmacStateStore",
    "HmacDurableSnapshot",
    "HmacDurableStateError",
    "HmacRuntimeDecision",
    "HmacRuntimeExpectation",
    "decision_from_snapshot",
    "durable_issuance_guard",
    "durable_state_required",
    "evaluate_runtime_state",
    "load_runtime_expectation",
    "require_runtime_startup",
    "runtime_expectations_digest",
]
