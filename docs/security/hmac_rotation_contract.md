# HMAC key rotation contract

This contract applies independently to the mail-action and report-link keyrings. Within those
keyrings, draft, calendar-event, and report-link tokens use distinct framed HMAC purposes. It is
the boundary that Terraform/deployment code must preserve; application code cannot persist
rotation history across process restarts.

## Key invariants

- New tokens are version 2 and signed only with `MAIL_ACTION_HMAC_SECRET` or
  `REPORT_LINK_HMAC_SECRET`, according to keyring. Before HMAC evaluation, the implementation
  injectively frames a protocol label, exact token purpose, and payload length. A valid draft
  signature therefore cannot verify as an event or report signature even if a key were
  accidentally reused.
- A primary must be 32–4096 UTF-8 bytes with no leading/trailing whitespace. It is rejected when
  it is a datastore/JDBC value, a Slack credential (`xoxb`, `xoxp`, `xoxs`, `xoxa`, `xoxr`,
  `xoxe`, or `xapp`), equal to another HMAC purpose, or equal to a visible credential/long
  environment value.
- `..._HMAC_PREVIOUS_SECRET` is verification-only. Version 2 tokens use the same purpose framing
  with an eligible previous dedicated generation. During the one-time production migration, the
  legacy database-URL generation can verify only unframed version 1 tokens and only when
  `..._HMAC_PREVIOUS_IS_LEGACY=1`. IaC emits that marker only when the pinned previous generation is
  the reviewed database-url `ARN@VersionId`; rendered-task preflight enforces both sides. Unframed
  verification excludes the primary and accepts only the exact old payload shapes; it disappears
  with the bounded previous generation. Dedicated-to-dedicated rotations never enable unframed
  verification. Primary validation is never relaxed.
- Each secret reference is pinned to one Secrets Manager VersionId. Terraform and preflight carry
  stable non-secret generation identifiers (`secret ARN@VersionId`) for primary and previous.
  Secret names without VersionIds, `AWSCURRENT`, and plaintext secret values are not generations.
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
process-local purpose clock and generation state retain a high-water mark. Advancing that
high-water mark, reloading it, and creating/updating a generation state happen under one lock.
Once any thread observes the deadline, a stale thread cannot create a keyring that re-enables the
previous key. This also prevents wall-clock rollback from introducing or re-enabling a key while
the previous-key/T0 pair is temporarily absent. The first observed `T0` for that generation is
immutable in the process; any change fails closed.

That high-water mark is intentionally not claimed to survive a restart. Deployment state must
provide the durable guarantee:

1. Persist `T0` once when the verifier-first rotation begins.
2. Never recompute, reset, or update `T0` while that previous key remains configured.
3. At or after the exclusive deadline, remove `..._HMAC_PREVIOUS_SECRET`,
   `..._HMAC_PREVIOUS_GENERATION`, `..._HMAC_PREVIOUS_ROTATION_STARTED_AT`, and any
   `..._HMAC_PREVIOUS_IS_LEGACY` marker atomically in the same deployment revision.
4. Do not begin the next generation until the prior pair is absent.

## Machine-readable IaC preflight

IaC and deployment code call `teamagent.hmac_keyring.validate_hmac_rotation_transition`. It accepts
no secret material—only deployed/proposed primary and previous generation identifiers,
deployed/proposed `T0`, trusted current epoch, and the exported purpose maximum TTL.

Every plan must require `result["ok"] is True` and `result["code"] == "ok"`. The result also
contains `previous_deadline` when one can be derived. Failure codes are stable operational
categories:

- `primary_changed_without_previous`: direct cutover would drop live tokens.
- `previous_generation_mismatch`: a newly proposed previous is not the deployed primary.
- `primary_generation_changed` / `previous_generation_changed`: a generation changed during an
  active window.
- `previous_without_primary_change`: a previous generation was introduced without a rotation.
- `purpose_generation_reuse`: mail and report primaries resolve to the same secret resource.
- `deployed_pair_mismatch` / `proposed_pair_mismatch`: previous key and T0 are not atomically
  present or absent.
- `t0_changed`: a configured generation's T0 changed.
- `future_t0`: proposed T0 exceeds the 300-second clock-skew ceiling.
- `removal_before_deadline`: proposed removal is too early.
- `expired_previous_not_removed`: the deadline passed but the stale pair remains proposed.
- `invalid_*`: malformed generation identifier, type, timestamp, or purpose maximum.

The preflight must receive the actually deployed state. Passing two freshly generated values would
not establish restart-spanning immutability.

`scripts/preflight_hmac_rotation.py` is the executable gate. Its strict JSON manifest also maps the
proposed domain config to every issuer/verifier service. With `--task-definition-json TASK=PATH`,
it verifies rendered ECS environment and secret entries, exact VersionId pins, generation/reference
agreement, legacy-marker/generation agreement, bounded TTLs, and absence of plaintext HMAC secrets.
With `--worker-env PATH`, it applies the same checks to the exact secret-free EC2 `hmac.env`.
It prints only result codes/scopes.
