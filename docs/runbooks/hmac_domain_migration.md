# HMAC domain-separation production migration

Status: **deployment NO-GO by default**. This runbook changes live capability-token signing and
must not be shortened into an image-only or task-definition-only deploy.

Current confirmed defect:

- MCP task definition `:55` and connect-web `:53` inject `MAIL_ACTION_HMAC_SECRET` from the
  database-url Secrets Manager secret.
- Existing draft, event, and `/r` tokens can therefore have signatures made with the database URL.
- Connect-web `:53` and canary `:14` remain documented production anchors. Canary `:14` is outside
  the HMAC path and must not change. Connect-web `:53` is an abort target only before a new
  report-link issuer is enabled.

Terraform creates two secret **containers**, never secret versions or values:

- `teamagent/<environment>/hmac/mail-action`
- `teamagent/<environment>/hmac/report-link`

Secret payloads must be independent random values of at least 256 bits. Do not copy a database URL,
Slack token, OAuth secret, or one domain's key into the other domain. Put values through an approved
Secrets Manager path that reads from stdin or a protected file; never place a value in Terraform,
tfvars, shell arguments, command output, tickets, or logs. Record only each returned VersionId.

## Immutable timing

Use a separate fixed `T0` for each keyring if the domains are rolled independently. Every service
in one domain receives the exact same T0 and generation IDs.

| Domain / token | True maximum TTL | Issuer cutover deadline | Legacy removal deadline |
|---|---:|---:|---:|
| mail draft / schedule action | 86,400 s | `T0_mail + 900` | `T0_mail + 87,300` |
| calendar event action | 86,400 s | `T0_mail + 900` | `T0_mail + 87,300` |
| legacy worker Slack-fallback draft/event v1 | 86,400 s | `T0_mail + 900` | `T0_mail + 87,300` |
| report link | 604,800 s | `T0_report + 900` | `T0_report + 605,700` |

Configured issuance TTLs may be shorter, but removal always uses the true maximum above. T0 is
written once before the first verifier revision. Never recompute it on restart, retry, rollback, or
the next day.

## Stage 0 — inventory and evidence

Do not read secret values. With an approved read-only production session:

1. Capture full task-definition JSON for MCP `:55`, connect-web `:53`, morning-digest, and any live
   legacy Socket Mode worker. Record image digests, execution/task roles, environment, secret
   `valueFrom` references, and VersionIds only.
2. Confirm whether morning-digest and the legacy worker are live issuers. If either cannot be
   inventoried, rollout is NO-GO.
3. Resolve the exact VersionId behind the database-url generation used by the old task revisions.
   Record it as `hmac_legacy_database_url_version_id`; never fetch `SecretString`.
4. If an inventoried worker could sign draft/event tokens through the historical Slack fallback,
   identify the exact Slack secret VersionId loaded by that worker revision and record it as
   `hmac_legacy_slack_bot_version_id`. Do not infer it from the secret's current `AWSCURRENT`
   version. If the loaded version cannot be proved, quiesce the worker and wait a full 86,400
   seconds after its last possible issuance before initialization; otherwise rollout is NO-GO.
5. Create private, short-lived test artifacts before cutover:
   - one old draft token,
   - one old calendar-event token,
   - one old report-link token.
   Keep tokens out of command lines and logs. Use a test user/object and a mode-0600 local file.
6. Save a secret-free preflight manifest containing asserted deployed/proposed
   `secret ARN@VersionId`, T0, and the task/domain map for MCP, morning-digest, connect-web, and the
   worker. For mail legacy migration, include the exact Slack-fallback generation in
   `legacy_worker_generation`; use `null` for every other domain/phase. Also create the exact
   live-control file described by
   `scripts/hmac_rollout_gate.py`: service/rule names, the morning-digest ECS cluster ARN, rotation
   epoch, expected candidate and rollback workload provenances, pre-registered rollback task
   ARNs/image digests, worker instance plus distinct candidate/rollback archive digests, forbidden
   signing revisions (including connect-web `:53`), and the unchanged canary `:14` target.
   Candidate and rollback task identities must not alias; every ECS/worker provenance in the
   control file must be distinct. Provenance binds the immutable image/archive, rotation epoch,
   every applicable primary/previous generation and T0, and the legacy-worker generation. Run the
   offline shape check:

   ```bash
   .venv/bin/python scripts/preflight_hmac_rotation.py \
     --manifest /protected/path/hmac-preflight.json
   ```

The only passing output is `{"code":"ok","ok":true}`. This offline command is not authorization to
deploy. `scripts/hmac_rollout_gate.py` independently reads current ECS/EventBridge metadata,
resolves only Secrets Manager VersionId metadata, reads worker readiness from DynamoDB, and obtains
time from AWS response headers. It compares the manifest assertions with those observations and
accepts at most 60 seconds of manifest clock difference. It never calls `GetSecretValue`.
Long-running deploy scripts pass `--refresh-manifest-now` immediately before each gate; this
changes only the non-secret local-clock assertion. AWS response time remains authoritative and a
local clock more than 60 seconds away still fails closed. Each AWS response date is converted to a
server-minus-local-monotonic offset at receipt. The gate compares those offsets and projects them
to the current monotonic time, so ordinary command duration does not look like clock disagreement.

Direct cutover, wrong previous generation, mid-window generation/T0 replacement, missing task,
task drift, stale operator time, or AWS response offsets that disagree by over 10 seconds is a
hard stop. Any ordinary AWS client failure emits only fixed redacted JSON and no traceback.

## Stage 1 — create dedicated generations

1. Plan and apply only the two secret-container resources if they do not exist. If containers were
   created outside Terraform, import them first. This metadata-only step is the sole permitted
   targeted exception; it must not touch a task or service.
2. Put one independently generated value into each container through the approved secure path.
   Capture only VersionIds.
3. Set primary VersionId variables. For `legacy_migration`, set the pinned database-url VersionId,
   each domain's fixed T0, a fresh `hmac_preflight_epoch_s`, and actually observed deployed
   generation/T0 fields.
4. Render a full plan. Verify:
   - dedicated primary `valueFrom` entries end in `:::<primary VersionId>`;
   - the only database-url HMAC reference is `..._HMAC_PREVIOUS_SECRET`;
   - primary/previous generation environment values end in the matching VersionIds;
   - `..._HMAC_PREVIOUS_IS_LEGACY=1` appears only beside that pinned database-url previous;
   - all services in a domain have identical generation/T0/TTL entries;
   - MCP and morning-digest have MAIL_ACTION; MCP and connect-web have REPORT_LINK;
   - the worker is either identically wired or proven retired;
   - execution roles can read only the domain secrets needed by their tasks;
   - no secret value appears in plan JSON, state, output, or logs.

5. Create the encrypted, PITR-enabled `${project}-${environment}-hmac-state` DynamoDB table and
   attach the checked-in least-privilege runtime/gate IAM policies. The table stores only
   generation IDs, fixed T0/deadline, rotation epoch, trusted-time high-water, retired state,
   workload provenance, short-lived worker readiness, and the rollout ledger.
6. Pre-register pinned legacy task revisions for every live ECS HMAC workload. Each revision must
   reproduce the observed legacy signing configuration with explicit `ARN@VersionId` references;
   unqualified secret ARNs and `AWSCURRENT` are forbidden. Update each service to its approved
   pinned legacy revision, wait for `pendingCount=0`, exactly one completed deployment, and every
   running task to use that exact revision. This is metadata binding only: do not infer the
   VersionId loaded by an already-running task from current secret labels.
7. With pinned legacy revisions fully stable and old revisions drained, refresh `manifest.now`
   from the operator clock and CAS-initialize the state from live observations:

   ```bash
   .venv/bin/python scripts/hmac_rollout_gate.py \
     --manifest /protected/path/hmac-preflight.json \
     --control /protected/path/hmac-control.json \
     --action initialize
   ```

   Initialization re-fetches each approved task definition, service deployment/task set, scheduled
   target, and candidate secret VersionId metadata. It paginates and describes both `RUNNING` and
   `STOPPED` ECS inventories, rejects any `desiredStatus=STOPPED` task that is still draining,
   reconciles list/describe counts with service desired/running/pending counts, and inventories
   in-flight morning-digest tasks by cluster and task family. The resulting ledger stage must be
   `initialized`. A retry against an active record fails; never delete/reset the record to get a
   new T0.

Do not apply a task with an unpinned secret ARN, `AWSCURRENT`, an empty VersionId, or a generation
identifier assembled from proposed rather than observed deployed state.

Terraform may register task definitions and mutate ECS service/EventBridge runtime targets only
inside one complete saved plan. The always-replaced production release gate and HMAC live gate
consume the same one-use intent under the shared apply lock; apply-time pre/post gates re-fetch the
registered candidates and live inventories around each mutation. Direct promotion scripts are
permanently disabled. `apply_resilience.sh` excludes every HMAC workload and canary `:14`; it is
not a rollout path.

## Stage 2 — report-link verifier first

1. Set `T0_report` once immediately before registering the first connect-web verifier revision.
2. Pre-register both the primary candidate and an HMAC-compatible rollback task/image with the
   same generations, VersionIds, T0, rotation epoch, and runtime-state contract. Deploy connect-web
   verifier preload with:
   - dedicated REPORT_LINK primary,
   - pinned database-url previous,
   - matching primary/previous generation IDs,
   - fixed `T0_report`,
   - `REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY=1`,
   - `REPORT_LINK_TTL_S <= 604800`.
3. Before service update, validate the exact rendered JSON:

   ```bash
   .venv/bin/python scripts/preflight_hmac_rotation.py \
     --manifest /protected/path/hmac-preflight.json \
     --task-definition-json connect_web=/protected/path/rendered-connect-web.json
   ```

4. Prebuild and authorize the connect-web image separately and record its immutable digest; the
   HMAC promotion path performs no Vault export, source upload, CodeBuild, or force deployment.
   Create the complete plan with `plan_image_release.sh`, review the full task/service change and
   HMAC manifest/control hashes, and set `hmac_runtime_promotion_tasks=["connect_web"]`; the saved
   plan validator rejects a service task-definition change unless both its pre- and post-gate
   resources are present. Apply that exact plan once with
   `apply_image_release_plan.sh`. The apply runs full-payload `pre-register` and a fresh
   `pre-update` immediately before the Terraform-owned service mutation. After the apply succeeds,
   explicitly CAS-advance with `hmac_rollout_gate.py --action connect-web-preloaded` using the same
   manifest/control. That action reaches `connect_web_preloaded` only after proving
   `pendingCount=0`, one completed deployment, and every running task on the exact approved task
   definition. The same proof also rejects
   draining/stopped-old service tasks and in-flight old morning-digest tasks; an empty service is
   not a successful drain proof.
5. Verify connect-web health, current app S3 anchor/source, security headers, and recent logs.
6. Exercise the saved old `/r` token; it must still redirect correctly.
7. MCP and the worker also carry MAIL_ACTION, so they cannot be deployed with report-only HMAC
   changes: doing so would retain the database credential as a mail signing primary. Proceed
   immediately to Stage 3 and deploy their two domains together before `T0_report + 900`.

If all report issuers are not cut over within 900 seconds, stop. Do not reset T0. Follow the
rollback rules below.

## Stage 3 — mail verifier/issuer sequence

MCP and the legacy worker are mixed-role processes: both can execute skills, while the worker also
handles Slack action tokens. A rolling update must not allow a new-key issuer to send a token to an
old-key verifier. Use a short issuance/action maintenance window; do not rely on deployment speed.

1. Pause morning-digest schedules and quiesce user traffic that can issue or consume draft, event,
   or report tokens through MCP/worker. Confirm no in-flight old task will mint after the cutover.
   If the worker is believed retired, prove that from service/process and traffic inventory before
   omitting it.
2. Set `T0_mail` once immediately before the first mail verifier revision. Keep the already fixed
   report generation/T0 unchanged.
3. Deploy the live legacy worker first with both complete keyrings. Its bot is the draft/event
   action verifier, so it must accept the dedicated MAIL_ACTION generation before any MCP or
   morning-digest issuer resumes. Keep traffic quiesced because the worker can also execute issuer
   skills. The EC2 path requires both `HMAC_PREFLIGHT_MANIFEST` and the exact secret-free
   `HMAC_WORKER_ENV`; `scripts/deploy_to_ec2.sh` validates and installs that file before restarting
   either bot or its local connect-web process. `teamagent.env.base` must contain neither
   `TEAMAGENT_HMAC_REQUIRED_DOMAINS` nor any runtime HMAC key value. The loader clears and rejects
   inherited values, then sets fixed domains from its command argument, fetches only exact
   VersionIds, and attests both loaded domains plus rotation epoch/provenance/instance ID.
   During the one-time migration it may additionally load the pinned historical Slack bot
   VersionId as `MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET`. That key is verification-only, accepts
   only exact version-1 draft/event payloads, uses the mail fixed T0/deadline, and cannot verify a
   different purpose or sign any new token.
   `deploy_to_ec2.sh` performs preparation without restarting, CAS-advances
   `connect_web_preloaded -> worker_verified`, rechecks trusted time/live metadata and the
   prebuilt rollback artifact immediately before issuing the separate restart command. The
   systemd startup check writes a second generation/T0 digest attestation. MCP cutover rejects the
   pre-restart attestation and requires this strictly newer post-restart record.
4. Register and promote one MCP revision containing both complete keyrings through the same
   one-use saved Terraform plan with `hmac_runtime_promotion_tasks=["mcp"]`. Its apply-time gates
   revalidate the registered task and rollback tasks immediately before the service mutation, wait
   for stability, and prove every running MCP task uses the new task definition. Then run
   `hmac_rollout_gate.py --action mcp-stable-and-old-drained` to CAS-advance
   `worker_verified -> mcp_stable_and_old_drained`. The revision
   contains the reviewed REPORT_LINK transition
   from Stage 2 and the dedicated MAIL_ACTION primary, pinned database-url previous, fixed T0,
   matching generation IDs, and `MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY=1`. Wait until the service is
   stable and every old MCP task is drained. This is the report and mail issuer cutover. The first
   mixed-role process that could mint a dedicated-key token is the irreversible point; if
   quiescence cannot be proved, the rollout is NO-GO.
5. While the schedule remains measurably disabled, let the same saved plan transaction replace the
   exact full morning-digest EventBridge target with the identical MAIL_ACTION keyring. The gate
   binds RoleArn, Input, network settings, TaskCount, target ID, retry policy, rule state, and task
   ARN, and restores the prior target on any partial failure. Select `morning_digest` and
   `connect_web` in `hmac_runtime_promotion_tasks` and include connect-web's final reviewed task in
   the plan. After apply, run `hmac_rollout_gate.py --action complete`; it verifies both tasks and
   CAS-advances `mcp_stable_and_old_drained -> complete` only after both services are stable and
   fully drained. Only then restore the digest schedule. Finish every issuer update while trusted
   AWS time is strictly less than its applicable `T0 + 900`.
6. Resume user traffic only after old MCP/worker processes are absent. Exercise the saved old draft
   and event tokens through their real action handlers; both must remain valid.
7. Issue new draft and event tokens from every live issuer. Confirm version `2`, the correct `typ`,
   successful same-purpose handling, owner binding, and cross-purpose rejection. Generate a new
   report link through MCP and consume it through connect-web.
8. Confirm no token, HMAC value, database URL, or signature appears in application/deployment logs.

An old issuer observed after the 900-second cutover deadline is a failed rollout. Do not extend the
previous window by changing T0.

## Rollback without database-credential minting

Before any mixed-role/issuer service has been changed, the verifier-only report stage may be
aborted:

- roll verifier-only connect-web back to `:53`;
- restore only the prior connect-web service target;
- remove the proposed migration config from the next plan;
- investigate, create a new dedicated VersionId if needed, and start a new transition from observed
  deployed state with a new T0.

Do not restart or resume a paused old issuer as a rollback step: that would mint new tokens with the
database credential. Keep token issuance disabled until a dedicated-key-compatible revision is
ready.

After a worker, MCP, or morning-digest revision has been changed—or whenever issuance quiescence
cannot be proved—**never** roll an issuer back to a task or image that signs with
`MAIL_ACTION_HMAC_SECRET=database-url`, falls back to a Slack token, or lacks version-2 domain
separation. Treat a dedicated-primary token as potentially issued and roll forward instead:

1. Keep the same dedicated primary VersionId, previous VersionId, and original T0.
2. Use only the pre-registered HMAC-compatible rollback task/image recorded in the control file.
   The live gate fetches that task JSON and proves the same generation IDs, fixed T0, exact
   VersionIds, rotation epoch, provenance contract, and image digest.
3. Select explicit rollback mode. ECS rollback runs `pre-update --mode rollback`, updates to the
   exact approved rollback ARN, waits for complete service drain, and runs
   `post-update --mode rollback`; a newly registered task or candidate ARN is rejected. Worker
   rollback requires `HMAC_WORKER_MODE=rollback`, the exact approved rollback artifact, and its
   reviewed secret-free rollback environment with `HMAC_WORKER_ADVANCE_STAGE=0`; packaging current
   `HEAD` is forbidden.
4. If an unrelated feature is faulty, disable that feature while preserving the keyring.
5. Do not swap the primary/previous generation or reset T0 mid-window. A bad generation discovered
   after issuer cutover is an incident requiring a roll-forward repair, not a credential fallback.

Connect-web `:53` is not a valid rollback target after new report tokens exist. Canary `:14` remains
unchanged throughout.

## Stage 4 — bounded previous removal

Runtime verification rejects the legacy previous at the exclusive deadline even if stale
environment entries remain. Operational removal is a separate steady-state cleanup, never another
issuer cutover and never the old one-shot retirement action:

1. Create a cleanup manifest whose `deployed` sections exactly describe the
   still-live previous/T0 pairs and whose `proposed` section removes only the selected domain's
   previous generation and T0. Create the complete one-use Terraform saved plan with
   `hmac_gate_mode=cleanup` and the exact `hmac_cleanup_domain`; the gate extracts the full
   registerable MCP, connect-web, and morning-digest payloads directly from that plan, even when one
   workload does not consume the selected domain. Pre-register and record distinct primary-only
   rollback task ARNs/images.
   Prepare distinct reviewed candidate and rollback worker archives and their exact secret-free
   environments. Candidate/rollback task identities, images within a workload, provenances, and
   worker archive digests must not alias.
2. At or after `T0_mail + 87,300`, run the staged authorization CAS:

   ```bash
   .venv/bin/python scripts/hmac_rollout_gate.py \
     --manifest /protected/path/hmac-cleanup-mail.json \
     --control /protected/path/hmac-cleanup-mail-control.json \
     --action prepare-cleanup \
     --domain mail_action \
     --saved-plan /protected/path/hmac-cleanup-mail.tfplan \
     --worker-env /protected/path/worker-primary-only.env \
     --worker-rollback-env /protected/path/worker-primary-only-rollback.env \
     --worker-artifact /protected/path/worker-primary-only.tar.gz \
     --worker-rollback-artifact /protected/path/worker-primary-only-rollback.tar.gz
   ```

   This transaction first proves the complete ledger, exact live/durable configuration, full ECS
   and scheduled-task inventory, expired deadline, all candidate/rollback artifacts, and worker
   archive bindings and persists both the exact proposal and complete saved-plan SHA-256. It leaves
   the durable previous/T0 pair present for old-process metadata
   compatibility, marks the previous generation retired so it cannot verify, and temporarily
   authorizes the old and new provenances. A retry or artifact drift fails closed.
3. Apply that same saved plan once through `apply_image_release_plan.sh`; a different plan cannot
   consume the cleanup authorization. Set the Terraform ECS and worker modes to `cleanup`, with
   `hmac_worker_advance_stage=false`. Every path re-fetches the
   registered artifact and checks its prepared digest. The worker path proves both
   `teamagent-bot` and `teamagent-connect` active, port 8788 listening, and a durable startup
   attestation strictly newer than the restart request. Rollback during this interval uses only
   the prepared primary-only rollback task/archive and remains authorized.
4. After every affected ECS replacement is stable, list/describe counts reconcile, all old and
   draining tasks are gone (including in-flight morning-digest work), and the fresh worker restart
   record is complete, finalize the CAS:

   ```bash
   .venv/bin/python scripts/hmac_rollout_gate.py \
     --manifest /protected/path/hmac-cleanup-mail.json \
     --control /protected/path/hmac-cleanup-mail-control.json \
     --action complete-cleanup \
     --domain mail_action
   ```

   The transaction removes previous/T0/deadline and legacy-worker fields, writes immutable
   retirement history, retires old provenances, and leaves only the new candidate and rollback
   provenances authorized. `--action retire-previous` is deliberately rejected with
   `cleanup_staging_required`; it is not an operator shortcut.
5. At or after `T0_report + 605,700`, build a fresh report cleanup manifest/control/artifact set and
   repeat `prepare-cleanup`, cleanup-mode replacement/drain/restart, and `complete-cleanup` for
   `report_link`. Never reuse the prior cleanup control's identities or provenances.
6. Re-run saved legacy-token probes with intentionally unexpired payload claims; verification must
   fail because the previous key is gone. New tokens and a prepared rollback must continue to
   work.
7. Verify rendered tasks no longer reference database-url or Slack as any HMAC secret. Do not
   delete or rotate the database credential as part of this migration.

A later dedicated-to-dedicated rotation may initialize a new epoch only after the prior ledger is
`complete` and both domain snapshots are primary-only. The CAS carries retirement sets and
trusted-time high-water forward and writes immutable epoch history; operators must not delete or
reset the durable table to begin another rotation.

The matching `..._HMAC_PREVIOUS_IS_LEGACY` marker must disappear in the same revision as each
previous secret/generation/T0. It must never be set for later dedicated-to-dedicated rotations.

## GO / NO-GO gate

Merge readiness and deployment readiness are separate. Deployment remains **NO-GO** until all are
recorded:

- real dedicated secret containers and independently generated versions exist;
- exact primary and legacy VersionIds are captured without reading values;
- full task rendering matches the reviewed manifest for every issuer/verifier;
- execution-role IAM is verified;
- old draft/event/report tokens pass during their bounded windows;
- new draft/event/report tokens use dedicated primaries and pass same-purpose E2E;
- cross-purpose and wrong-owner probes fail;
- issuer cutovers finish within 900 seconds of each fixed T0;
- HMAC-compatible rollback revisions are ready and tested;
- the durable ledger recorded, in order, `connect_web_preloaded`, `worker_verified`,
  `mcp_stable_and_old_drained`, and `complete`;
- startup and issuance tests prove stale/restarted/multiprocess tasks cannot use retired
  generations or provenances;
- rollout and rollback drills preserve primary/previous/T0;
- logs are inspected for errors and secret/token leakage;
- previous removal is scheduled and later proved fail-closed at both deadlines.

Missing evidence in any row is NO-GO. Do not deploy/apply merely because unit tests or Terraform
validation pass.
