"""Build purpose-separated HMAC keyrings and issuance TTLs from the environment.

Exact environment contract (secret values must never be logged or included in errors):

* Mail action tokens:
  ``MAIL_ACTION_HMAC_SECRET`` (the only issuance key),
  ``MAIL_ACTION_HMAC_PREVIOUS_SECRET`` (optional verification-only key),
  ``MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT`` (required with ``PREVIOUS_SECRET``),
  ``MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY=1`` (only for the one-time unframed migration),
  non-secret generation identifiers for the primary and previous secret versions,
  and ``MAIL_ACTION_TTL_S`` (optional issuance TTL, ASCII decimal ``1..86400``).
* Report-link tokens:
  ``REPORT_LINK_HMAC_SECRET`` (the only issuance key),
  ``REPORT_LINK_HMAC_PREVIOUS_SECRET`` (optional verification-only key),
  ``REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT`` (required with ``PREVIOUS_SECRET``),
  ``REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY=1`` (only for the one-time unframed migration),
  non-secret generation identifiers for the primary and previous secret versions,
  and ``REPORT_LINK_TTL_S`` (optional issuance TTL, ASCII decimal ``1..604800``).

TTL variables default to their purpose's maximum only when absent. If present, an empty,
whitespace-padded, signed, non-ASCII, overlong, or out-of-range value is invalid and issuance
fails closed.

Verifier-first rotation uses a persisted, purpose-specific Unix timestamp ``T0``. At ``T0``,
deploy verifiers with the new primary, old previous, and the matching ``...ROTATION_STARTED_AT``.
Issuers may continue using the old key for at most ``HMAC_MAX_ROLLOUT_OVERLAP_S`` (15 minutes),
then cut over to the new primary. The previous key is eligible exactly while::

    now < T0 + 15 minutes + purpose maximum token TTL

Thus an old token issued immediately before the allowed issuer cutover remains verifiable for its
full bounded lifetime. The fixed ``T0`` survives process restarts, and the previous key is removed
at the deterministic exclusive deadline even if its environment variables remain deployed. A
future ``T0`` is accepted only within ``HMAC_MAX_FUTURE_T0_SKEW_S`` of the process clock. A
process-local high-water mark prevents clock rollback or a ``T0`` reset from re-enabling an expired
previous key. IaC must persist ``T0`` immutably and atomically remove the previous generation,
secret, T0, and legacy marker;
``validate_hmac_rotation_transition`` is the secret-free preflight contract for that boundary. The
former deploy-time ``...PREVIOUS_SECRET_VALID_UNTIL`` variables are not part of this contract.

Primary keys that look like datastore DSNs or Slack credentials, equal any credential environment
variable (or any sufficiently long non-HMAC environment value) visible to the process, or overlap
another HMAC purpose are rejected. A legacy value is accepted byte-for-byte only by the explicitly
bounded previous verification path so old tokens can migrate; it is never an issuance fallback or
subject to primary-key validation. In particular, there is no ``DATABASE_URL`` fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, SupportsIndex, TypedDict

MAIL_ACTION_HMAC_SECRET = "MAIL_ACTION_HMAC_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_SECRET = "MAIL_ACTION_HMAC_PREVIOUS_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT = "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT"
MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY = "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY"
MAIL_ACTION_HMAC_PRIMARY_GENERATION = "MAIL_ACTION_HMAC_PRIMARY_GENERATION"
MAIL_ACTION_HMAC_PREVIOUS_GENERATION = "MAIL_ACTION_HMAC_PREVIOUS_GENERATION"
MAIL_ACTION_TTL_S = "MAIL_ACTION_TTL_S"

REPORT_LINK_HMAC_SECRET = "REPORT_LINK_HMAC_SECRET"
REPORT_LINK_HMAC_PREVIOUS_SECRET = "REPORT_LINK_HMAC_PREVIOUS_SECRET"
REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT = "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"
REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY = "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY"
REPORT_LINK_HMAC_PRIMARY_GENERATION = "REPORT_LINK_HMAC_PRIMARY_GENERATION"
REPORT_LINK_HMAC_PREVIOUS_GENERATION = "REPORT_LINK_HMAC_PREVIOUS_GENERATION"
REPORT_LINK_TTL_S = "REPORT_LINK_TTL_S"

HMAC_PURPOSE_MAIL_DRAFT = "teamagent.mail-action.draft"
HMAC_PURPOSE_CALENDAR_EVENT = "teamagent.mail-action.event"
HMAC_PURPOSE_REPORT_LINK = "teamagent.report-link"

MAIL_ACTION_MAX_TOKEN_TTL_S = 60 * 60 * 24
REPORT_LINK_MAX_TOKEN_TTL_S = 60 * 60 * 24 * 7
HMAC_MAX_ROLLOUT_OVERLAP_S = 15 * 60
# A verifier may observe IaC's T0 slightly before its local wall clock reaches T0. Keep this much
# smaller than the rollout overlap: it is an explicit upper bound, not an unbounded grace period.
HMAC_MAX_FUTURE_T0_SKEW_S = 5 * 60

_MAX_UNIX_TIMESTAMP_S = 9_999_999_999
_MIN_SECRET_BYTES = hashlib.sha256().digest_size
_MAX_SECRET_BYTES = 4096
_HMAC_CONFIGURATION_ENVS = frozenset(
    {
        MAIL_ACTION_HMAC_SECRET,
        MAIL_ACTION_HMAC_PREVIOUS_SECRET,
        MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT,
        MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY,
        MAIL_ACTION_HMAC_PRIMARY_GENERATION,
        MAIL_ACTION_HMAC_PREVIOUS_GENERATION,
        MAIL_ACTION_TTL_S,
        REPORT_LINK_HMAC_SECRET,
        REPORT_LINK_HMAC_PREVIOUS_SECRET,
        REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT,
        REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY,
        REPORT_LINK_HMAC_PRIMARY_GENERATION,
        REPORT_LINK_HMAC_PREVIOUS_GENERATION,
        REPORT_LINK_TTL_S,
    }
)
_DATASTORE_SCHEMES = frozenset(
    {
        "amqp",
        "amqps",
        "cassandra",
        "cockroachdb",
        "elasticsearch",
        "etcd",
        "influxdb",
        "kafka",
        "mariadb",
        "memcached",
        "mongo",
        "mongodb",
        "mssql",
        "mysql",
        "nats",
        "neo4j",
        "opensearch",
        "oracle",
        "postgres",
        "postgresql",
        "redis",
        "rediss",
        "rabbitmq",
        "sqlserver",
    }
)
_DATASTORE_ENV_WORDS = frozenset(
    {
        "CASSANDRA",
        "COCKROACHDB",
        "DATABASE",
        "DB",
        "ELASTICSEARCH",
        "MARIADB",
        "MONGO",
        "MONGODB",
        "MSSQL",
        "MYSQL",
        "NEO4J",
        "OPENSEARCH",
        "ORACLE",
        "POSTGRES",
        "POSTGRESQL",
        "REDIS",
        "SQLSERVER",
    }
)
_CREDENTIAL_RESOURCE_ENV_WORDS = frozenset(
    {
        "BROKER",
        "CACHE",
        "DATASOURCE",
        "JDBC",
        "WEBHOOK",
    }
)
_DIRECT_CREDENTIAL_ENV_WORDS = frozenset(
    {
        "AUTH",
        "AUTHORIZATION",
        "CRED",
        "CREDENTIAL",
        "CREDENTIALS",
        "CREDS",
        "PASS",
        "PASSWD",
        "PASSWORD",
        "PWD",
        "SECRET",
        "TOKEN",
    }
)
_SLACK_CREDENTIAL_PREFIXES = (
    "xoxb-",
    "xoxp-",
    "xoxs-",
    "xoxa-",
    "xoxr-",
    "xoxe-",
    "xapp-",
)
_HMAC_DOMAIN_FRAME = b"teamagent-hmac\x00v1\x00"


class HmacKeyConfigurationError(RuntimeError):
    """No safe purpose-specific primary key is available for issuance."""


class _HmacKeyringSecretSlots:
    """Non-dataclass storage keeps key material out of dataclass serializers."""

    __slots__ = ("_legacy_verification_keys", "_primary", "_verification_keys")

    _legacy_verification_keys: tuple[bytes, ...]
    _primary: bytes
    _verification_keys: tuple[bytes, ...]

    def sign(self, payload: bytes, *, purpose: str, digest_bytes: int) -> bytes:
        """Sign a purpose-framed payload with the primary key only."""
        message = _domain_separated_message(purpose, payload)
        return hmac.new(self._primary, message, hashlib.sha256).digest()[:digest_bytes]

    def verify(
        self,
        payload: bytes,
        signature: bytes,
        *,
        purpose: str,
        digest_bytes: int,
    ) -> bool:
        """Compare a purpose-framed payload against every eligible key without an early exit.

        A primary-key match does not skip previous-key comparisons. Even a wrong-length signature
        is compared with every eligible key before the final length check.
        """
        message = _domain_separated_message(purpose, payload)
        matched = 0
        for key in self._verification_keys:
            expected = hmac.new(key, message, hashlib.sha256).digest()[:digest_bytes]
            matched |= int(hmac.compare_digest(expected, signature))
        return len(signature) == digest_bytes and bool(matched)

    def verify_legacy_previous(
        self,
        payload: bytes,
        signature: bytes,
        *,
        digest_bytes: int,
    ) -> bool:
        """Verify an unframed legacy token with eligible previous keys only.

        The primary is deliberately excluded so newly issued tokens can never regress to the
        pre-domain-separation format. This path exists only for the bounded production migration.
        """
        matched = 0
        for key in self._legacy_verification_keys:
            expected = hmac.new(key, payload, hashlib.sha256).digest()[:digest_bytes]
            matched |= int(hmac.compare_digest(expected, signature))
        return len(signature) == digest_bytes and bool(matched)


@dataclass(frozen=True, repr=False, eq=False, slots=True, init=False)
class HmacKeyring(_HmacKeyringSecretSlots):
    """Hold one issuance key and all currently eligible verification keys.

    Key material enters through the explicit constructor and is stored only in inherited slots.
    Consequently, dataclass equality/hash generation never touches secrets and
    ``dataclasses.asdict`` has no serializable fields. Equality and hashing retain object identity
    semantics.
    """

    def __init__(
        self,
        _primary: bytes,
        _verification_keys: tuple[bytes, ...],
        _legacy_verification_keys: tuple[bytes, ...] = (),
    ) -> None:
        object.__setattr__(self, "_primary", _primary)
        object.__setattr__(self, "_verification_keys", _verification_keys)
        object.__setattr__(self, "_legacy_verification_keys", _legacy_verification_keys)

    def __repr__(self) -> str:
        return "HmacKeyring(<redacted>)"

    def __str__(self) -> str:
        return "HmacKeyring(<redacted>)"

    def __reduce_ex__(
        self,
        protocol: SupportsIndex,
        /,
    ) -> str | tuple[Any, ...]:
        """Reject generic serialization rather than copying key material into a payload."""
        del protocol
        raise TypeError("HmacKeyring serialization is disabled")


class HmacRotationContractResult(TypedDict):
    """Secret-free, machine-readable IaC transition validation result."""

    ok: bool
    code: str
    previous_deadline: int | None


class _RotationRuntimeState:
    """Process-local monotonic state for one purpose/previous-key generation."""

    __slots__ = ("deadline", "expired", "high_water", "started_at")

    def __init__(self, *, started_at: int, deadline: int, high_water: int) -> None:
        self.started_at = started_at
        self.deadline = deadline
        self.high_water = high_water
        self.expired = high_water >= deadline

    def __repr__(self) -> str:
        return "_RotationRuntimeState(<redacted>)"


# A process-random keyed fingerprint lets the runtime recognize a previous-key generation without
# retaining a dictionary key that is brute-forceable when the legacy value itself was short.
_RUNTIME_FINGERPRINT_KEY = os.urandom(hashlib.sha256().digest_size)
_rotation_runtime_states: dict[tuple[str, bytes], _RotationRuntimeState] = {}
_purpose_clock_high_water: dict[str, int] = {}
_rotation_runtime_lock = threading.Lock()


def _domain_separated_message(purpose: str, payload: bytes) -> bytes:
    """Frame purpose and payload injectively before HMAC evaluation."""
    if type(purpose) is not str or not purpose or len(purpose) > 255:
        raise ValueError("invalid HMAC purpose")
    try:
        purpose_bytes = purpose.encode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid HMAC purpose") from exc
    if type(payload) is not bytes:
        raise TypeError("HMAC payload must be bytes")
    return (
        _HMAC_DOMAIN_FRAME
        + len(purpose_bytes).to_bytes(1, "big")
        + purpose_bytes
        + len(payload).to_bytes(8, "big")
        + payload
    )


def _parse_bounded_ascii_decimal(raw: object, *, minimum: int, maximum: int) -> int | None:
    """Parse a bounded, unsigned ASCII decimal without unbounded ``int`` conversion."""
    if type(raw) is not str or not raw or len(raw) > len(str(maximum)):
        return None
    value = 0
    for char in raw:
        if char < "0" or char > "9":
            return None
        value = value * 10 + (ord(char) - ord("0"))
        if value > maximum:
            return None
    return value if value >= minimum else None


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if type(value) is not int or value < minimum or value > maximum:
        return None
    return value


def _generation_identifier(value: object) -> str | None:
    """Validate a stable, non-secret generation identifier without returning it in errors."""
    if type(value) is not str or not value or len(value) > 2048:
        return None
    if value != value.strip():
        return None
    for char in value:
        ordinal = ord(char)
        if ordinal < 0x21 or ordinal > 0x7E:
            return None
    return value


def hmac_previous_key_deadline(rotation_started_at: object, max_token_ttl_s: object) -> int | None:
    """Return the exclusive previous-key deadline for validated bounded inputs."""
    started_at = _bounded_int(
        rotation_started_at,
        minimum=0,
        maximum=_MAX_UNIX_TIMESTAMP_S,
    )
    max_ttl = _bounded_int(
        max_token_ttl_s,
        minimum=1,
        maximum=REPORT_LINK_MAX_TOKEN_TTL_S,
    )
    if started_at is None or max_ttl is None:
        return None
    window = HMAC_MAX_ROLLOUT_OVERLAP_S + max_ttl
    if started_at > _MAX_UNIX_TIMESTAMP_S - window:
        return None
    return started_at + window


def _contract_result(
    ok: bool,
    code: str,
    previous_deadline: int | None = None,
) -> HmacRotationContractResult:
    return {
        "ok": ok,
        "code": code,
        "previous_deadline": previous_deadline,
    }


def validate_hmac_rotation_transition(
    *,
    deployed_primary_generation: object,
    deployed_previous_generation: object | None,
    deployed_rotation_started_at: object | None,
    proposed_primary_generation: object,
    proposed_previous_generation: object | None,
    proposed_rotation_started_at: object | None,
    now: object,
    max_token_ttl_s: object,
) -> HmacRotationContractResult:
    """Validate IaC's persisted previous-key/T0 transition without accepting secrets.

    Callers pass stable non-secret identifiers such as ``secret ARN + version ID``, timestamps, and
    the purpose's exported maximum token TTL. ``code == "ok"`` is the sole passing result. The
    transition enforces:

    * primary changes carry the deployed primary forward as the proposed previous generation;
    * previous-generation and T0 presence are atomic;
    * primary, previous, and T0 remain immutable throughout an active rotation;
    * a new T0 is at most ``HMAC_MAX_FUTURE_T0_SKEW_S`` in the future; and
    * removal occurs at or after the deterministic exclusive deadline.

    This helper cannot persist history itself. IaC must feed the deployed state (not freshly
    generated values) into every preflight so immutability survives process restarts.
    """
    current = _bounded_int(now, minimum=0, maximum=_MAX_UNIX_TIMESTAMP_S)
    max_ttl = _bounded_int(
        max_token_ttl_s,
        minimum=1,
        maximum=REPORT_LINK_MAX_TOKEN_TTL_S,
    )
    if current is None:
        return _contract_result(False, "invalid_now")
    if max_ttl not in (MAIL_ACTION_MAX_TOKEN_TTL_S, REPORT_LINK_MAX_TOKEN_TTL_S):
        return _contract_result(False, "invalid_max_token_ttl")

    deployed_primary = _generation_identifier(deployed_primary_generation)
    if deployed_primary is None:
        return _contract_result(False, "invalid_deployed_primary_generation")
    proposed_primary = _generation_identifier(proposed_primary_generation)
    if proposed_primary is None:
        return _contract_result(False, "invalid_proposed_primary_generation")

    deployed_previous_present = deployed_previous_generation is not None
    proposed_previous_present = proposed_previous_generation is not None
    deployed_previous = (
        _generation_identifier(deployed_previous_generation) if deployed_previous_present else None
    )
    proposed_previous = (
        _generation_identifier(proposed_previous_generation) if proposed_previous_present else None
    )
    if deployed_previous_present and deployed_previous is None:
        return _contract_result(False, "invalid_deployed_previous_generation")
    if proposed_previous_present and proposed_previous is None:
        return _contract_result(False, "invalid_proposed_previous_generation")
    if deployed_previous_present != (deployed_rotation_started_at is not None):
        return _contract_result(False, "deployed_pair_mismatch")
    if proposed_previous_present != (proposed_rotation_started_at is not None):
        return _contract_result(False, "proposed_pair_mismatch")
    if deployed_previous == deployed_primary:
        return _contract_result(False, "deployed_generation_reuse")
    if proposed_previous == proposed_primary:
        return _contract_result(False, "proposed_generation_reuse")

    deployed_t0: int | None = None
    proposed_t0: int | None = None
    deployed_deadline: int | None = None
    proposed_deadline: int | None = None
    if deployed_previous_present:
        deployed_t0 = _bounded_int(
            deployed_rotation_started_at,
            minimum=0,
            maximum=_MAX_UNIX_TIMESTAMP_S,
        )
        deployed_deadline = hmac_previous_key_deadline(deployed_t0, max_ttl)
        if deployed_t0 is None or deployed_deadline is None:
            return _contract_result(False, "invalid_deployed_t0")
    if proposed_previous_present:
        proposed_t0 = _bounded_int(
            proposed_rotation_started_at,
            minimum=0,
            maximum=_MAX_UNIX_TIMESTAMP_S,
        )
        proposed_deadline = hmac_previous_key_deadline(proposed_t0, max_ttl)
        if proposed_t0 is None or proposed_deadline is None:
            return _contract_result(False, "invalid_proposed_t0")

    if deployed_previous_present:
        if proposed_primary != deployed_primary:
            return _contract_result(False, "primary_generation_changed", deployed_deadline)
        if proposed_previous_present and proposed_previous != deployed_previous:
            return _contract_result(False, "previous_generation_changed", deployed_deadline)
        if proposed_previous_present and deployed_t0 != proposed_t0:
            return _contract_result(False, "t0_changed", deployed_deadline)
    elif proposed_primary != deployed_primary:
        if not proposed_previous_present:
            return _contract_result(False, "primary_changed_without_previous")
        if proposed_previous != deployed_primary:
            return _contract_result(False, "previous_generation_mismatch", proposed_deadline)
    elif proposed_previous_present:
        return _contract_result(False, "previous_without_primary_change", proposed_deadline)

    if deployed_previous_present and not proposed_previous_present:
        if deployed_deadline is None or current < deployed_deadline:
            return _contract_result(False, "removal_before_deadline", deployed_deadline)
        return _contract_result(True, "ok", deployed_deadline)

    if proposed_previous_present:
        if proposed_t0 is None or proposed_deadline is None:
            return _contract_result(False, "invalid_proposed_t0")
        if proposed_t0 - current > HMAC_MAX_FUTURE_T0_SKEW_S:
            return _contract_result(False, "future_t0", proposed_deadline)
        if current >= proposed_deadline:
            return _contract_result(False, "expired_previous_not_removed", proposed_deadline)
        return _contract_result(True, "ok", proposed_deadline)

    return _contract_result(True, "ok")


def coerce_epoch_seconds(now: int | None) -> int | None:
    """Return a bounded Unix timestamp, or ``None`` for malformed input/clock values."""
    try:
        if now is not None:
            return _bounded_int(now, minimum=0, maximum=_MAX_UNIX_TIMESTAMP_S)
        measured = time.time()
        if type(measured) not in (int, float):
            return None
        return _bounded_int(int(measured), minimum=0, maximum=_MAX_UNIX_TIMESTAMP_S)
    except Exception:
        return None


def validate_epoch_seconds(value: object) -> int | None:
    """Validate an integer timestamp claim without coercing strings, floats, or booleans."""
    return _bounded_int(value, minimum=0, maximum=_MAX_UNIX_TIMESTAMP_S)


def add_token_ttl(issued_at: int, ttl_s: int) -> int | None:
    """Add a validated TTL without allowing the bounded timestamp domain to overflow."""
    issued = validate_epoch_seconds(issued_at)
    ttl = _bounded_int(ttl_s, minimum=1, maximum=REPORT_LINK_MAX_TOKEN_TTL_S)
    if issued is None or ttl is None or issued > _MAX_UNIX_TIMESTAMP_S - ttl:
        return None
    return issued + ttl


def _load_token_ttl_s(
    *, env_name: str, default_s: int, maximum_s: int, explicit_ttl_s: object | None
) -> int | None:
    """Load one purpose's issuance TTL; any present malformed configuration poisons issuance."""
    try:
        configured_raw = os.environ.get(env_name)
        configured = (
            default_s
            if configured_raw is None
            else _parse_bounded_ascii_decimal(
                configured_raw,
                minimum=1,
                maximum=maximum_s,
            )
        )
        if configured is None:
            return None
        if explicit_ttl_s is None:
            return configured
        return _bounded_int(explicit_ttl_s, minimum=1, maximum=maximum_s)
    except Exception:
        return None


def load_mail_action_token_ttl_s(*, explicit_ttl_s: object | None = None) -> int | None:
    """Load a mail-token TTL in ``1..24h``; return ``None`` on malformed configuration."""
    return _load_token_ttl_s(
        env_name=MAIL_ACTION_TTL_S,
        default_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
        maximum_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
        explicit_ttl_s=explicit_ttl_s,
    )


def load_report_link_token_ttl_s(*, explicit_ttl_s: object | None = None) -> int | None:
    """Load a report-token TTL in ``1..7d``; return ``None`` on malformed configuration."""
    return _load_token_ttl_s(
        env_name=REPORT_LINK_TTL_S,
        default_s=REPORT_LINK_MAX_TOKEN_TTL_S,
        maximum_s=REPORT_LINK_MAX_TOKEN_TTL_S,
        explicit_ttl_s=explicit_ttl_s,
    )


def _secret_bytes(raw: object) -> bytes | None:
    """Validate a new issuance primary; never use this for migration previous keys."""
    if type(raw) is not str or not raw or raw != raw.strip():
        return None
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        return None
    if len(encoded) < _MIN_SECRET_BYTES or len(encoded) > _MAX_SECRET_BYTES:
        return None
    return encoded


def _migration_previous_secret_bytes(raw: object) -> bytes | None:
    """Encode an explicitly configured legacy previous key byte-for-byte.

    Old token code used the environment string exactly as UTF-8, including short values and
    surrounding whitespace/newlines. Reproducing those bytes is permitted only on the bounded
    verification-only path. Empty, malformed-Unicode, and unreasonably large values remain invalid.
    """
    if type(raw) is not str or not raw:
        return None
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        return None
    if not encoded or len(encoded) > _MAX_SECRET_BYTES:
        return None
    return encoded


def _same_secret(left: bytes, right_raw: object) -> bool:
    if type(right_raw) is not str or not right_raw:
        return False
    try:
        return hmac.compare_digest(left, right_raw.encode("utf-8"))
    except UnicodeError:
        return False


def _looks_like_credential(raw: str) -> bool:
    lowered = raw.casefold()
    if lowered.startswith("jdbc:"):
        return True
    if lowered.startswith(_SLACK_CREDENTIAL_PREFIXES) or lowered.startswith("xox"):
        return True
    scheme, separator, _rest = lowered.partition(":")
    base_scheme = scheme.split("+", 1)[0]
    return bool(separator) and base_scheme in _DATASTORE_SCHEMES


def _is_credential_env_name(name: str) -> bool:
    if name in _HMAC_CONFIGURATION_ENVS:
        return False
    normalized = "".join(
        char if "A" <= char <= "Z" or "0" <= char <= "9" else "_" for char in name.upper()
    )
    words = frozenset(part for part in normalized.split("_") if part)
    if words & _DIRECT_CREDENTIAL_ENV_WORDS:
        return True
    if words & _CREDENTIAL_RESOURCE_ENV_WORDS:
        return True
    if name.upper().endswith(("AUTH", "PASS", "PASSWD", "PASSWORD", "SECRET", "TOKEN")):
        return True
    if "DSN" in words or ({"CONNECTION", "STRING"} <= words):
        return True
    if "KEY" in words and words & {"ACCESS", "API", "PRIVATE", "SECRET", "SIGNING"}:
        return True
    return bool(words & {"URI", "URL"}) and bool(words & _DATASTORE_ENV_WORDS)


def _reuses_process_credential(secret: bytes) -> bool:
    for name, raw in os.environ.items():
        if name in _HMAC_CONFIGURATION_ENVS:
            continue
        try:
            candidate = raw.encode("utf-8")
        except UnicodeError:
            continue
        # Named credential variables are always compared. Unknown variables are compared once their
        # value is long enough to plausibly be a generated secret. The only exemptions are the
        # explicit HMAC configuration variables, whose cross-purpose checks are handled separately.
        if (
            _is_credential_env_name(name) or len(candidate) >= _MIN_SECRET_BYTES
        ) and hmac.compare_digest(secret, candidate):
            return True
    return False


def _unsafe_primary_credential(raw: str, secret: bytes) -> bool:
    """Central credential shape/reuse gate for every issuance primary."""
    return _looks_like_credential(raw) or _reuses_process_credential(secret)


def _previous_key_runtime_fingerprint(purpose: str, previous: bytes) -> bytes:
    fingerprint = hmac.new(_RUNTIME_FINGERPRINT_KEY, digestmod=hashlib.sha256)
    purpose_bytes = purpose.encode("ascii")
    fingerprint.update(len(purpose_bytes).to_bytes(2, "big"))
    fingerprint.update(purpose_bytes)
    fingerprint.update(len(previous).to_bytes(4, "big"))
    fingerprint.update(previous)
    return fingerprint.digest()


def _advance_purpose_clock_locked(*, purpose: str, current: int) -> int:
    """Advance one purpose clock and all of its states while the runtime lock is held."""
    effective_current = max(current, _purpose_clock_high_water.get(purpose, current))
    _purpose_clock_high_water[purpose] = effective_current
    for (state_purpose, _fingerprint), state in _rotation_runtime_states.items():
        if state_purpose != purpose:
            continue
        if effective_current > state.high_water:
            state.high_water = effective_current
        if state.high_water >= state.deadline:
            state.expired = True
    return effective_current


def _advance_rotation_high_water(*, purpose: str, current: int) -> int:
    """Advance and return the process-local effective clock for this purpose.

    This runs before any configuration validation and while the previous-key/T0 pair is absent.
    Thus a stale deployment cannot introduce or reintroduce a key against a rolled-back wall clock
    after this process already observed a later timestamp.
    """
    with _rotation_runtime_lock:
        return _advance_purpose_clock_locked(purpose=purpose, current=current)


def _previous_key_runtime_eligible(
    *,
    purpose: str,
    previous: bytes,
    started_at: int,
    deadline: int,
    current: int,
) -> bool | None:
    """Apply bounded future skew, immutable T0, and a process-local clock high-water mark.

    ``None`` means the rotation configuration changed incompatibly and the whole keyring must fail
    closed. ``False`` means the configuration is well formed but this previous key has expired.
    """
    runtime_key = (purpose, _previous_key_runtime_fingerprint(purpose, previous))
    with _rotation_runtime_lock:
        # Reload and advance the purpose high-water mark in the same critical section as state
        # creation. A thread that captured an older wall-clock value before another thread observed
        # the deadline can therefore never create a fresh state that makes previous eligible again.
        effective_current = _advance_purpose_clock_locked(purpose=purpose, current=current)
        state = _rotation_runtime_states.get(runtime_key)
        if state is None:
            if started_at - effective_current > HMAC_MAX_FUTURE_T0_SKEW_S:
                return None
            state = _RotationRuntimeState(
                started_at=started_at,
                deadline=deadline,
                high_water=effective_current,
            )
            _rotation_runtime_states[runtime_key] = state
        else:
            # The same purpose/previous key defines one immutable rotation generation. Changing T0
            # (or its derived deadline) must never buy that key a fresh verification window.
            if state.started_at != started_at or state.deadline != deadline:
                return None
            if effective_current > state.high_water:
                state.high_water = effective_current
            if state.high_water >= state.deadline:
                state.expired = True
        return not state.expired


def _load_keyring(
    *,
    primary_env: str,
    previous_env: str,
    rotation_started_at_env: str,
    previous_is_legacy_env: str,
    other_primary_env: str,
    other_previous_env: str,
    max_token_ttl_s: int,
    now: int | None,
) -> HmacKeyring | None:
    current = coerce_epoch_seconds(now)
    if current is None:
        return None
    current = _advance_rotation_high_water(purpose=primary_env, current=current)

    primary_raw = os.environ.get(primary_env)
    primary = _secret_bytes(primary_raw)
    if primary is None or not isinstance(primary_raw, str):
        return None
    if _unsafe_primary_credential(primary_raw, primary):
        return None
    # Issuance keys must not overlap either generation of another purpose.
    if _same_secret(primary, os.environ.get(other_primary_env)) or _same_secret(
        primary, os.environ.get(other_previous_env)
    ):
        return None

    previous_raw = os.environ.get(previous_env)
    started_at_raw = os.environ.get(rotation_started_at_env)
    previous_is_legacy_raw = os.environ.get(previous_is_legacy_env)
    if previous_raw is None and started_at_raw is None:
        if previous_is_legacy_raw is not None:
            return None
        return HmacKeyring(primary, (primary,))
    if previous_raw is None or started_at_raw is None:
        return None
    if previous_is_legacy_raw not in (None, "1"):
        return None

    if previous_is_legacy_raw == "1":
        previous = _migration_previous_secret_bytes(previous_raw)
    else:
        previous = _secret_bytes(previous_raw)
        if (
            previous is not None
            and isinstance(previous_raw, str)
            and _unsafe_primary_credential(previous_raw, previous)
        ):
            return None
    if previous is None or hmac.compare_digest(primary, previous):
        return None
    # Shared legacy previous keys are allowed, but a previous key may never be another purpose's
    # current issuance key.
    if _same_secret(previous, os.environ.get(other_primary_env)):
        return None
    if previous_is_legacy_raw is None and _same_secret(
        previous, os.environ.get(other_previous_env)
    ):
        return None

    latest_start = _MAX_UNIX_TIMESTAMP_S - HMAC_MAX_ROLLOUT_OVERLAP_S - max_token_ttl_s
    started_at = _parse_bounded_ascii_decimal(
        started_at_raw,
        minimum=0,
        maximum=latest_start,
    )
    if started_at is None:
        return None

    previous_deadline = hmac_previous_key_deadline(started_at, max_token_ttl_s)
    if previous_deadline is None:
        return None
    previous_eligible = _previous_key_runtime_eligible(
        purpose=primary_env,
        previous=previous,
        started_at=started_at,
        deadline=previous_deadline,
        current=current,
    )
    if previous_eligible is None:
        return None
    # The database credential is admitted only for exact unframed v1 migration payloads. It must
    # never become a general v2 verification key: otherwise anyone holding that credential could
    # mint a purpose-framed token during the migration window.
    verification_keys = (
        (primary, previous) if previous_eligible and previous_is_legacy_raw is None else (primary,)
    )
    legacy_verification_keys = (
        (previous,) if previous_eligible and previous_is_legacy_raw == "1" else ()
    )
    return HmacKeyring(primary, verification_keys, legacy_verification_keys)


def load_mail_action_hmac_keyring(*, now: int | None = None) -> HmacKeyring | None:
    """Load the mail-action keyring; return ``None`` for every malformed configuration."""
    try:
        return _load_keyring(
            primary_env=MAIL_ACTION_HMAC_SECRET,
            previous_env=MAIL_ACTION_HMAC_PREVIOUS_SECRET,
            rotation_started_at_env=MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT,
            previous_is_legacy_env=MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY,
            other_primary_env=REPORT_LINK_HMAC_SECRET,
            other_previous_env=REPORT_LINK_HMAC_PREVIOUS_SECRET,
            max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
            now=now,
        )
    except Exception:
        return None


def require_mail_action_hmac_keyring(*, now: int | None = None) -> HmacKeyring:
    keyring = load_mail_action_hmac_keyring(now=now)
    if keyring is None:
        raise HmacKeyConfigurationError("HMAC signing key configuration is invalid")
    return keyring


def load_report_link_hmac_keyring(*, now: int | None = None) -> HmacKeyring | None:
    """Load the report-link keyring; return ``None`` for every malformed configuration."""
    try:
        return _load_keyring(
            primary_env=REPORT_LINK_HMAC_SECRET,
            previous_env=REPORT_LINK_HMAC_PREVIOUS_SECRET,
            rotation_started_at_env=REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT,
            previous_is_legacy_env=REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY,
            other_primary_env=MAIL_ACTION_HMAC_SECRET,
            other_previous_env=MAIL_ACTION_HMAC_PREVIOUS_SECRET,
            max_token_ttl_s=REPORT_LINK_MAX_TOKEN_TTL_S,
            now=now,
        )
    except Exception:
        return None


def require_report_link_hmac_keyring(*, now: int | None = None) -> HmacKeyring:
    keyring = load_report_link_hmac_keyring(now=now)
    if keyring is None:
        raise HmacKeyConfigurationError("HMAC signing key configuration is invalid")
    return keyring


__all__ = [
    "HMAC_MAX_FUTURE_T0_SKEW_S",
    "HMAC_MAX_ROLLOUT_OVERLAP_S",
    "HMAC_PURPOSE_CALENDAR_EVENT",
    "HMAC_PURPOSE_MAIL_DRAFT",
    "HMAC_PURPOSE_REPORT_LINK",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_MAX_TOKEN_TTL_S",
    "MAIL_ACTION_TTL_S",
    "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PRIMARY_GENERATION",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_MAX_TOKEN_TTL_S",
    "REPORT_LINK_TTL_S",
    "HmacKeyConfigurationError",
    "HmacKeyring",
    "HmacRotationContractResult",
    "add_token_ttl",
    "coerce_epoch_seconds",
    "hmac_previous_key_deadline",
    "load_mail_action_hmac_keyring",
    "load_mail_action_token_ttl_s",
    "load_report_link_hmac_keyring",
    "load_report_link_token_ttl_s",
    "require_mail_action_hmac_keyring",
    "require_report_link_hmac_keyring",
    "validate_epoch_seconds",
    "validate_hmac_rotation_transition",
]
