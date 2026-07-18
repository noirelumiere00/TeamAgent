# HMAC key rotation contract

This contract applies independently to the mail-action and report-link keyrings. Within those
keyrings, draft, calendar-event, and report-link tokens use distinct framed HMAC purposes. It is
the boundary that Terraform, deployment gates, and the shared durable runtime state must preserve
across process restarts.

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
- A legacy worker that historically used the Slack bot token as its draft/event fallback may add
  one separately pinned `MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET` only during the same bounded mail
  legacy window. It is verification-only, accepts version-1 payloads only, shares the immutable
  mail T0/deadline, and is purpose-framed by the exact legacy payload decoder. It cannot sign,
  verify version 2, or cross-verify draft and event tokens.
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

The process-local lock is only the first layer. The runtime and rollout gate bind every epoch to a
shared DynamoDB CAS record containing primary/previous generations, legacy worker generation,
fixed T0/deadline, trusted-time high-water, retirement state, and provenance. Startup and issuance
fail closed when the task's generation is stale or retired. Deployment state must:

1. Prove each live ECS service is on an approved legacy task definition whose secret references
   are pinned to exact VersionIds and fully drain every older task before durable initialization.
   Inventory both `RUNNING` and `STOPPED` task lists, describe exactly every listed ARN, reject
   count/failure mismatches and still-draining tasks, and reconcile service desired/running/pending
   counts. Apply the same family/cluster inventory to in-flight scheduled morning-digest tasks.
   Never infer an already-running task's loaded VersionId from `AWSCURRENT`.
2. Persist `T0` once when the verifier-first rotation begins.
3. Never recompute, reset, or update `T0` while that previous key remains configured.
4. Treat expiry cleanup as an explicit steady-state mode, distinct from the initial 900-second
   issuer cutover. Before its first CAS, prevalidate the exact primary-only candidate task bundle,
   distinct primary-only rollback task identities/images, and distinct candidate/rollback worker
   archives and secret-free environments. Candidate and rollback provenances must be distinct and
   bind the full applicable primary/previous/T0 configuration, immutable image or archive digest,
   workload, epoch, and legacy-worker generation.
5. At or after the exclusive deadline, CAS-authorize a cleanup overlap while the durable snapshot
   still carries previous/T0 for old-process metadata compatibility. Mark the previous generation
   retired immediately, preserve trusted-time high-water, and temporarily authorize exactly the
   old plus prepared new candidate/rollback provenances. The retired previous is never eligible
   during this overlap.
6. Deploy only the prepared primary-only candidate or rollback artifacts. Before finalization,
   prove exact active task identities/artifact digests, complete service and scheduled-task drain,
   and a worker startup attestation strictly newer than its durable restart request. The worker
   deploy must also prove its bot and connect services active and port 8788 listening.
7. CAS-finalize by removing `..._HMAC_PREVIOUS_SECRET`,
   `..._HMAC_PREVIOUS_GENERATION`, `..._HMAC_PREVIOUS_ROTATION_STARTED_AT`, and every
   legacy-worker secret/generation or `..._HMAC_PREVIOUS_IS_LEGACY` marker atomically. Preserve
   retirement history and high-water, retire old provenances, and retain the prepared new rollback
   provenances. The former direct one-shot retirement path is invalid.
8. Initialize the next epoch only after the prior ledger is complete, no cleanup remains
   authorized, and both domain snapshots are primary-only. Carry retired generation/provenance
   sets and high-water into the next epoch.

AWS response time is compared as a server-time offset at each local monotonic receipt. Offset
agreement, not raw elapsed command duration, is bounded; a normal long-running command therefore
does not fail solely because it ran for longer than the response-date spread limit.

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
