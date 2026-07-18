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
4. Create private, short-lived test artifacts before cutover:
   - one old draft token,
   - one old calendar-event token,
   - one old report-link token.
   Keep tokens out of command lines and logs. Use a test user/object and a mode-0600 local file.
5. Save a secret-free preflight manifest containing deployed/proposed
   `secret ARN@VersionId`, T0, and the task/domain map for MCP, morning-digest, connect-web, and the
   worker. Run:

   ```bash
   .venv/bin/python scripts/preflight_hmac_rotation.py \
     --manifest /protected/path/hmac-preflight.json
   ```

The only passing output is `{"code":"ok","ok":true}`. Direct cutover, wrong previous generation,
mid-window generation/T0 replacement, missing task, or task drift is a hard stop.

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

Do not apply a task with an unpinned secret ARN, `AWSCURRENT`, an empty VersionId, or a generation
identifier assembled from proposed rather than observed deployed state.

## Stage 2 — report-link verifier first

1. Set `T0_report` once immediately before registering the first connect-web verifier revision.
2. Deploy connect-web with:
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

4. Verify connect-web health, current app S3 anchor/source, security headers, and recent logs.
5. Exercise the saved old `/r` token; it must still redirect correctly.
6. MCP and the worker also carry MAIL_ACTION, so they cannot be deployed with report-only HMAC
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
   either bot or its local connect-web process.
4. Deploy one MCP revision containing both complete keyrings: the reviewed REPORT_LINK transition
   from Stage 2 and the dedicated MAIL_ACTION primary, pinned database-url previous, fixed T0,
   matching generation IDs, and `MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY=1`. Wait until the service is
   stable and every old MCP task is drained. This is the report and mail issuer cutover. The first
   mixed-role process that could mint a dedicated-key token is the irreversible point; if
   quiescence cannot be proved, the rollout is NO-GO.
5. Deploy morning-digest with the identical MAIL_ACTION keyring, then restore its schedule. Finish
   worker and MCP before both applicable `T0 + 900` cutover deadlines and morning-digest before
   `T0_mail + 900`.
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
2. Use a prebuilt/validated HMAC-compatible rollback image containing this contract.
3. Render and preflight the replacement task JSON before service update.
4. If an unrelated feature is faulty, disable that feature while preserving the keyring.
5. Do not swap the primary/previous generation or reset T0 mid-window. A bad generation discovered
   after issuer cutover is an incident requiring a roll-forward repair, not a credential fallback.

Connect-web `:53` is not a valid rollback target after new report tokens exist. Canary `:14` remains
unchanged throughout.

## Stage 4 — bounded previous removal

Runtime verification rejects the legacy previous at the exclusive deadline even if stale
environment entries remain. Operational removal must still happen promptly:

1. At or after `T0_mail + 87,300`, capture current deployed generations/T0, set mail phase to
   `steady`, clear the mail rotation T0 and dedicated-previous VersionId inputs, set a fresh
   preflight epoch, and update MCP, morning-digest, and the live worker in one reviewed change.
   Previous secret, generation, and T0 must disappear atomically.
2. At or after `T0_report + 605,700`, do the same for MCP, connect-web, and the live worker.
3. Re-run saved legacy-token probes with intentionally unexpired payload claims; verification must
   fail because the previous key is gone. New tokens must continue to work.
4. Verify rendered tasks no longer reference database-url as any HMAC secret. Do not delete or
   rotate the database credential as part of this migration.

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
- rollout and rollback drills preserve primary/previous/T0;
- logs are inspected for errors and secret/token leakage;
- previous removal is scheduled and later proved fail-closed at both deadlines.

Missing evidence in any row is NO-GO. Do not deploy/apply merely because unit tests or Terraform
validation pass.
