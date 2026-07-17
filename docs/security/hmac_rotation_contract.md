# HMAC key rotation contract

This contract applies independently to the mail-action and report-link HMAC purposes. It is the
boundary that Terraform/deployment code must preserve; application code cannot persist rotation
history across process restarts.

## Key invariants

- New tokens are signed only with `MAIL_ACTION_HMAC_SECRET` or
  `REPORT_LINK_HMAC_SECRET`, according to purpose.
- A primary must be 32–4096 UTF-8 bytes with no leading/trailing whitespace. It is rejected when
  it is a datastore/JDBC value, a Slack credential (`xoxb`, `xoxp`, `xoxs`, `xoxa`, `xoxr`,
  `xoxe`, or `xapp`), equal to another HMAC purpose, or equal to a visible credential/long
  environment value.
- `..._HMAC_PREVIOUS_SECRET` is migration-only and verification-only. It reproduces the old
  implementation's non-empty UTF-8 bytes exactly, including short values and trailing newlines.
  Primary validation is never relaxed for this compatibility path.
- Secret values must not be put in plan output, validation results, logs, exception messages, or
  serialized keyring objects.

## Fixed timeline

For a purpose with maximum token TTL `MAX_TTL`, persist one Unix timestamp `T0` and use:

```text
issuer cutover no later than = T0 + 900
previous-key deadline        = T0 + 900 + MAX_TTL
previous key is eligible     = effective_now < previous-key deadline
```

The deadline is exclusive. `MAX_TTL` is the purpose maximum, not the currently configured issuance
TTL:

- mail action: `86400`
- report link: `604800`

Application processes accept a future `T0` only up to
`HMAC_MAX_FUTURE_T0_SKEW_S` (300 seconds). For each purpose/previous-key generation, a
process-local purpose clock and generation state retain a high-water mark. They prevent wall-clock
rollback from introducing or re-enabling a key after its deadline, including while the
previous-key/T0 pair is temporarily absent. The first observed `T0` for that generation is
immutable in the process; any change fails closed.

That high-water mark is intentionally not claimed to survive a restart. Deployment state must
provide the durable guarantee:

1. Persist `T0` once when the verifier-first rotation begins.
2. Never recompute, reset, or update `T0` while that previous key remains configured.
3. At or after the exclusive deadline, remove `..._HMAC_PREVIOUS_SECRET` and
   `..._HMAC_PREVIOUS_ROTATION_STARTED_AT` atomically in the same deployment revision.
4. Do not begin the next generation until the prior pair is absent.

## Machine-readable IaC preflight

IaC tests can call `teamagent.hmac_keyring.validate_hmac_rotation_transition`. It accepts no secret
material—only deployed/proposed presence booleans, deployed/proposed `T0`, trusted current epoch,
and the exported purpose maximum TTL.

Every plan must require `result["ok"] is True` and `result["code"] == "ok"`. The result also
contains `previous_deadline` when one can be derived. Failure codes are stable operational
categories:

- `deployed_pair_mismatch` / `proposed_pair_mismatch`: previous key and T0 are not atomically
  present or absent.
- `t0_changed`: a configured generation's T0 changed.
- `future_t0`: proposed T0 exceeds the 300-second clock-skew ceiling.
- `removal_before_deadline`: proposed removal is too early.
- `expired_previous_not_removed`: the deadline passed but the stale pair remains proposed.
- `invalid_*`: malformed type, timestamp, presence flag, or purpose maximum.

The preflight must receive the actually deployed state. Passing two freshly generated values would
not establish restart-spanning immutability.
