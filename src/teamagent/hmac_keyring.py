"""Build purpose-separated HMAC keyrings and issuance TTLs from the environment.

Exact environment contract (secret values must never be logged or included in errors):

* Mail action tokens:
  ``MAIL_ACTION_HMAC_SECRET`` (the only issuance key),
  ``MAIL_ACTION_HMAC_PREVIOUS_SECRET`` (optional verification-only key),
  ``MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT`` (required with ``PREVIOUS_SECRET``),
  and ``MAIL_ACTION_TTL_S`` (optional issuance TTL, ASCII decimal ``1..86400``).
* Report-link tokens:
  ``REPORT_LINK_HMAC_SECRET`` (the only issuance key),
  ``REPORT_LINK_HMAC_PREVIOUS_SECRET`` (optional verification-only key),
  ``REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT`` (required with ``PREVIOUS_SECRET``),
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
future ``T0`` is invalid. The former deploy-time ``...PREVIOUS_SECRET_VALID_UNTIL`` variables are
not part of this contract and are not used.

Primary keys that look like datastore DSNs or Slack credentials, equal any credential environment
variable visible to the process, or overlap another HMAC purpose are rejected. A credential-derived
legacy value is allowed only as the explicitly bounded previous verification key so old tokens can
migrate; it is never an issuance fallback. In particular, there is no ``DATABASE_URL`` fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field

MAIL_ACTION_HMAC_SECRET = "MAIL_ACTION_HMAC_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_SECRET = "MAIL_ACTION_HMAC_PREVIOUS_SECRET"
MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT = "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT"
MAIL_ACTION_TTL_S = "MAIL_ACTION_TTL_S"

REPORT_LINK_HMAC_SECRET = "REPORT_LINK_HMAC_SECRET"
REPORT_LINK_HMAC_PREVIOUS_SECRET = "REPORT_LINK_HMAC_PREVIOUS_SECRET"
REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT = "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"
REPORT_LINK_TTL_S = "REPORT_LINK_TTL_S"

MAIL_ACTION_MAX_TOKEN_TTL_S = 60 * 60 * 24
REPORT_LINK_MAX_TOKEN_TTL_S = 60 * 60 * 24 * 7
HMAC_MAX_ROLLOUT_OVERLAP_S = 15 * 60

_MAX_UNIX_TIMESTAMP_S = 9_999_999_999
_MIN_SECRET_BYTES = hashlib.sha256().digest_size
_HMAC_CONFIGURATION_ENVS = frozenset(
    {
        MAIL_ACTION_HMAC_SECRET,
        MAIL_ACTION_HMAC_PREVIOUS_SECRET,
        MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT,
        MAIL_ACTION_TTL_S,
        REPORT_LINK_HMAC_SECRET,
        REPORT_LINK_HMAC_PREVIOUS_SECRET,
        REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT,
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


class HmacKeyConfigurationError(RuntimeError):
    """No safe purpose-specific primary key is available for issuance."""


@dataclass(frozen=True, repr=False)
class HmacKeyring:
    """Hold one issuance key and all currently eligible verification keys."""

    _primary: bytes = field(repr=False)
    _verification_keys: tuple[bytes, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "HmacKeyring(<redacted>)"

    def sign(self, payload: bytes, *, digest_bytes: int) -> bytes:
        """Sign with the primary key only."""
        return hmac.new(self._primary, payload, hashlib.sha256).digest()[:digest_bytes]

    def verify(self, payload: bytes, signature: bytes, *, digest_bytes: int) -> bool:
        """Compare against every eligible key without an early exit.

        A primary-key match does not skip previous-key comparisons. Even a wrong-length signature
        is compared with every eligible key before the final length check.
        """
        matched = 0
        for key in self._verification_keys:
            expected = hmac.new(key, payload, hashlib.sha256).digest()[:digest_bytes]
            matched |= int(hmac.compare_digest(expected, signature))
        return len(signature) == digest_bytes and bool(matched)


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
    if type(raw) is not str or not raw or raw != raw.strip():
        return None
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        return None
    return encoded if len(encoded) >= _MIN_SECRET_BYTES else None


def _same_secret(left: bytes, right_raw: object) -> bool:
    if type(right_raw) is not str or not right_raw:
        return False
    try:
        return hmac.compare_digest(left, right_raw.encode("utf-8"))
    except UnicodeError:
        return False


def _looks_like_credential(raw: str) -> bool:
    lowered = raw.casefold()
    if lowered.startswith("xox") or lowered.startswith("xapp-"):
        return True
    dsn = lowered[5:] if lowered.startswith("jdbc:") else lowered
    scheme, separator, _rest = dsn.partition(":")
    base_scheme = scheme.split("+", 1)[0]
    return bool(separator) and base_scheme in _DATASTORE_SCHEMES


def _is_credential_env_name(name: str) -> bool:
    if name in _HMAC_CONFIGURATION_ENVS:
        return False
    words = frozenset(part for part in name.upper().split("_") if part)
    if words & _DIRECT_CREDENTIAL_ENV_WORDS:
        return True
    if name.upper().endswith(("AUTH", "PASS", "PASSWD", "PASSWORD", "SECRET", "TOKEN")):
        return True
    if "DSN" in words or ({"CONNECTION", "STRING"} <= words):
        return True
    if "KEY" in words and words & {"ACCESS", "API", "PRIVATE", "SECRET", "SIGNING"}:
        return True
    if "URL" in words and "WEBHOOK" in words:
        return True
    return bool(words & {"URI", "URL"}) and bool(words & _DATASTORE_ENV_WORDS)


def _reuses_process_credential(secret: bytes) -> bool:
    for name, raw in os.environ.items():
        if _is_credential_env_name(name) and _same_secret(secret, raw):
            return True
    return False


def _unsafe_primary_credential(raw: str, secret: bytes) -> bool:
    """Central credential shape/reuse gate for every issuance primary."""
    return _looks_like_credential(raw) or _reuses_process_credential(secret)


def _load_keyring(
    *,
    primary_env: str,
    previous_env: str,
    rotation_started_at_env: str,
    other_primary_env: str,
    other_previous_env: str,
    max_token_ttl_s: int,
    now: int | None,
) -> HmacKeyring | None:
    current = coerce_epoch_seconds(now)
    if current is None:
        return None

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
    if previous_raw is None and started_at_raw is None:
        return HmacKeyring(primary, (primary,))
    if previous_raw is None or started_at_raw is None:
        return None

    previous = _secret_bytes(previous_raw)
    if previous is None or hmac.compare_digest(primary, previous):
        return None
    # Shared legacy previous keys are allowed, but a previous key may never be another purpose's
    # current issuance key.
    if _same_secret(previous, os.environ.get(other_primary_env)):
        return None

    latest_start = _MAX_UNIX_TIMESTAMP_S - HMAC_MAX_ROLLOUT_OVERLAP_S - max_token_ttl_s
    started_at = _parse_bounded_ascii_decimal(
        started_at_raw,
        minimum=0,
        maximum=latest_start,
    )
    if started_at is None or started_at > current:
        return None

    previous_deadline = started_at + HMAC_MAX_ROLLOUT_OVERLAP_S + max_token_ttl_s
    verification_keys = (primary, previous) if current < previous_deadline else (primary,)
    return HmacKeyring(primary, verification_keys)


def load_mail_action_hmac_keyring(*, now: int | None = None) -> HmacKeyring | None:
    """Load the mail-action keyring; return ``None`` for every malformed configuration."""
    try:
        return _load_keyring(
            primary_env=MAIL_ACTION_HMAC_SECRET,
            previous_env=MAIL_ACTION_HMAC_PREVIOUS_SECRET,
            rotation_started_at_env=MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT,
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
    "HMAC_MAX_ROLLOUT_OVERLAP_S",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_MAX_TOKEN_TTL_S",
    "MAIL_ACTION_TTL_S",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_MAX_TOKEN_TTL_S",
    "REPORT_LINK_TTL_S",
    "HmacKeyConfigurationError",
    "HmacKeyring",
    "add_token_ttl",
    "coerce_epoch_seconds",
    "load_mail_action_hmac_keyring",
    "load_mail_action_token_ttl_s",
    "load_report_link_hmac_keyring",
    "load_report_link_token_ttl_s",
    "require_mail_action_hmac_keyring",
    "require_report_link_hmac_keyring",
    "validate_epoch_seconds",
]
