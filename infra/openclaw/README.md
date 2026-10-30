# OpenClaw release boundary

This directory defines the reviewed TeamAgent OpenClaw core runtime and its
fail-closed integration with the repository's canonical provenance and
one-time Terraform release gate. The only locally supported runtime is
OpenClaw `2026.7.1` on a single `linux/arm64` manifest, rebuilt onto the
digest-pinned Chainguard (Wolfi) Node 26 base and run as UID/GID `65532`.

The production bundle contract remains `release.ready=false`. The separate
media subject, exact two-subject receipt emitter, signed final-HEAD registry
evidence, independent review, and real post-apply rollout/rollback evidence
are not complete. A local PASS is therefore not production authorization.

## Runtime contract

The outer loop keeps Haiku 4.5 as `agents.list[0].model.primary` and uses only
`amazon-bedrock/jp.anthropic.claude-sonnet-4-6` as its fallback. Both inference
profiles use the same account and Tokyo endpoint; IAM permits only their JP
backing models (Tokyo/Osaka), alongside the existing Haiku 5.5 migration grant.
No global/US profile is added. Healthy sessions keep the existing Haiku cost.
After failover, OpenClaw keeps the session on Sonnet until a primary recovery
probe succeeds (every five minutes), or `/new` / `/reset` clears the override.
Sonnet charges therefore also cover turns during that recovery interval.

Local schema/behavior evidence: `openclaw@2026.7.1` npm archive, matching
`plugins-lock.json`; `dist/zod-schema.agent-runtime-C02vY4RT.js`
(`AgentModelSchema`, `AgentEntrySchema`) accepts `{ primary, fallbacks }`.
The shipped `docs/concepts/model-failover.md` specifies that agent-level models
need their own `fallbacks`. `dist/errors-XbAR6hS3.js` classifies HTTP 503 as
timeout/overloaded and 429 as rate_limit; `dist/model-fallback-CvSRhgYr.js`
advances the configured candidate chain on these transient failures.
Run the optional schema probe with `OPENCLAW_DIST_DIR` pointing to that
archive's `package/dist`; it also rejects malformed fallback fields.

The final image has no shell, package manager, browser executable, Playwright,
Codex CLI, `jiti`, TypeScript/tsx compiler, Vite/Vitest, build compiler, or
test/fixture/type/source-map payload. `prune-runtime.mjs` computes the package
and module closures before deleting packages or rewriting package metadata.
It then proves the same Slack and Bedrock operation closure still exists and
the representative operations execute with provider calls stubbed under
`--network none`.

The upstream bundles contain two optional compiler paths even when production
configuration does not use them. The pruner verifies the exact reviewed
upstream functions and replaces only these branches with deterministic
fail-closed facades:

- the `jiti/static` extension source-transform loader;
- the TypeScript Code Mode compiler loader, with advertised Code Mode
  languages reduced to JavaScript.

The generic OpenClaw CLI remains because configuration and plugin inspection
are operationally required. Browser-named shared chunks also remain when core
or Control UI modules import them. In particular,
`browser-bridges-*.js` retains generic `child_process.spawn` primitives. This
is not reported as zero reachable browser-named payload. The runtime contract
instead proves that:

- browser plugin and CLI registrations, browser implementation chunks,
  Playwright, Chrome MCP, CDP control code, and browser executables are absent;
- the retained browser bridge public facade cannot find or load a browser
  plugin and fails closed;
- `openclaw browser --help` is unavailable while generic CLI help still works.

The retained `/opt/teamagent/runtime-prune-report.json` contains hashes and
closure edges for independent inspection.

## Control UI closure

Every regular file under `dist/control-ui` is included, not only the 81 ESM
modules. The current closure contains 142 files, including root HTML,
`sw.js`, CSS, webmanifest, provider SVGs, icons, and images. For each file the
report records the on-disk hash/size and the expected HTTP hash/size.

The gateway deterministically adds
`data-openclaw-terminal-enabled="false"` to the served root HTML. Both the
source and transformed representations are hashed. The actual-image test
fetches all 142 paths, re-hashes all 142 on-disk files, checks static
references, and authenticates `/control-ui-config.json`; an unauthenticated
bootstrap request must return 401. Operator terminal support is explicitly
disabled in `openclaw.config.json5`.

## Fargate contract

The Terraform task definition is authoritative. `harden-task-definition.jq`
is its adversarially tested offline parity mirror: it accepts only the fixed
production family, roles, service provenance, one `openclaw` container, one
task-scoped `tmp` volume mounted at `/tmp`, and the exact encrypted EFS access
point mounted at `/tmp/teamagent-openclaw/state`. It rejects sidecars,
additional volumes or mounts, writable paths outside those two exact mounts,
task/container field additions, environment retargeting, role changes, and
image repository changes. The rendered definition enforces:

- `readonlyRootFilesystem=true`;
- `user=65532:65532`, `privileged=false`, init process, capability drop `ALL`;
- 120-second graceful stop, canonical image ENTRYPOINT/CMD and `/readyz` health check;
- only the five fixed Secrets Manager bindings and reviewed environment keys.

The canonical CMD uses `execve`, so the gateway is PID 1. Actual-image tests
verify child exit-code propagation and clean SIGTERM exit 0.

Slack DM access has one canonical production input, `SLACK_DM_ALLOWLIST`.
The value `*` becomes exactly `dmPolicy=open` with `allowFrom=["*"]`.
One to 100 unique, comma-separated Slack `U...` IDs become
`dmPolicy=allowlist` with those exact IDs. Missing/empty values, whitespace,
duplicates, mixed wildcard/IDs, and non-`U` identifiers fail in Terraform
plan validation, the task-definition hardener, and the image entrypoint.
The empty Terraform default is deliberately an invalid production sentinel,
not permission to fall back to the baked template.

Fargate cannot enforce Docker `no-new-privileges` or `linuxParameters.tmpfs`.
Production therefore makes no such claim. Local Docker tests use
`no-new-privileges`; production compensates with nonroot UID, capability drop,
read-only rootfs, fixed IAM/network policy, and a task-scoped ephemeral `/tmp`
volume. This remains a documented risk.

## Local build and evidence

`build-image.sh` builds and verifies locally; it never pushes. It requires a
clean attached Git commit and the Buildx/Trivy versions pinned in
`plugins-lock.json`.

```sh
COMMIT=$(git rev-parse HEAD)
SHORT=${COMMIT:0:12}
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:git-$SHORT" \
  --manifest "/tmp/openclaw-$SHORT-manifest.json" \
  --evidence-dir "/tmp/openclaw-$SHORT-evidence"
```

The schema-5 output is explicitly `deploymentCredential=false` and
`promotion.status=LOCAL_GATES_PASSED`. It binds:

- the exact Git commit, tree, deterministic archive hash, and active blocked
  bundle-contract hash;
- the exact single ARM64 image/config/rootfs subject;
- exact BuildKit material set, with extra or missing material rejected;
- runtime inventory and actual-image contract;
- all Control UI source/HTTP assets and dynamic bootstrap result;
- exact Slack/Bedrock operation closures and offline operation smoke;
- Trivy Critical=0, High=0, Secrets=0;
- the absence of all eight findings observed in the latest live C8/H22 image:
  `CVE-2026-12087`, `CVE-2026-13221`, `CVE-2026-33845`,
  `CVE-2026-34182`, `CVE-2026-42010`, `CVE-2026-55200`,
  `CVE-2026-57433`, and `CVE-2026-6100`;
- a CycloneDX SBOM augmented with every merged-rootfs filesystem object and an
  exact path/type/mode/UID/GID/size/link/content equivalence check;
- the exact physical npm package instance path/name/version multiset and `bom-ref`
  integrity;
- a hash index of every evidence file.

The local manifest and adjacent checksum are evidence only. Neither is a
signature, release receipt, or deployment credential.

## Trusted source and promotion integration

The dedicated OpenClaw project uses a full 40-character `dev` SHA from the
fixed GitHub CodeConnection. Before any repository build interface executes,
the embedded buildspec independently fetches `origin/dev` and verifies a
KMS-signed source manifest. That manifest binds the commit object, exact tree,
executable inventory, and bundle-contract hash to immutable S3 VersionIds
under COMPLIANCE Object Lock.

The build role can write only the two quarantine repositories. It cannot
write/sign source evidence, candidate or release repositories, or deployment
resources. The source-free attestor owns actual-image verification and signed
SPDX/in-toto/image evidence. The source-free promoter alone can copy exact
subjects and referrers through verified-candidate to release repositories.

`build-bundle.sh` is the canonical core/media interface and deliberately
stops before Docker or registry work while `release.ready=false`. The local
core-only verifier remains `build-image.sh`.

## Deployment and rollout

Direct task-definition registration and the legacy `apply_openclaw.sh` path
are permanently disabled. Production image changes require:

1. a verified-candidate receipt and immutable signature VersionIds;
2. guarded active/rollback release authorization;
3. a fixed release repository `@sha256` plus the exact generated
   `image_deployment_consumer_manifest`, `image_release_receipt_catalog`, and
   `image_release_consumer_receipt_bindings`;
4. one full saved plan created by `plan_image_release.sh`;
5. one-time application of that exact plan by
   `apply_image_release_plan.sh`.

The planner and apply supervisor bind clean exact `origin/dev`, backend,
workspace, state lineage/serial and ownership, contract hash, complete signed
release graph, one-time intent, and receipt claims. A started plan is never
retried; an ambiguous failure requires reconciliation and fresh authorization.
See `infra/terraform/README.md`.

The canonical Terraform runtime guard now runs
`run-live-rollout-gates.mjs` under the same deployment locks and before it
records `APPLIED`. For a distinct OpenClaw task revision, it checks service
stability, enumerates every running task before and after the Slack canary,
runs an isolated task-role Bedrock/MCP canary, correlates the exact Slack reply
to a candidate task log stream, and verifies a fixed-VersionId,
COMPLIANCE-locked, SSE-KMS result signed by the fixed asymmetric KMS key. A
one-use rollback authorization binds the apply attempt and old/new revisions;
any post-apply gate failure restores and verifies the durable previous
revision before either lock is released.

The effective MCP authority is `effective-tool-scope.json`. The default
Terraform task currently exposes 12 tools; the reviewed OpenClaw include list
contains 29. This scope is not read-only: Gmail draft and Slack file-delivery
operations are among the default tools.

The five required runtime secrets are `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`OPENCLAW_GATEWAY_TOKEN`, `TEAMAGENT_MCP_BEARER`, and the separate
`TEAMAGENT_CALLER_CLAIM_SECRET`. Production also requires the exact
`SLACK_TEAM_ID` and the MCP-only `TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE`; the
workspace ID is not model-provided, and a conditional DynamoDB write makes each
claim one-use across rolling ECS tasks. Both runtimes reject a caller-claim
secret that equals the MCP bearer. Bedrock uses only the ECS task role; static
AWS credentials are forbidden.

See `docs/openclaw/deploy_runbook.md`. Production remains NO-GO until the
media/bundle integration is complete, an independent review approves the
final commit, and real signed rollout and forced-rollback
CodeBuild/ECR/Fargate/Slack/Bedrock/tools-list evidence passes.

## Startup DM catchup

The caller-identity plugin starts recovery inside the existing gateway process.
It does not add a service, change upstream OpenClaw, add drain handling, or
change the ECS task size, count, deployment policy, or Terraform inputs.
The canonical implementation remains `caller-identity-plugin/dist/index.js`.

Recovery is **OFF by default**. Only `DM_CATCHUP_ENABLED=1` enables it, and
`DM_CATCHUP_ALLOWED_USERS` independently limits the users eligible for recovery.
Use a comma-separated list of 1–100 unique Slack `U...` IDs, with no whitespace,
or `*` alone for all users already allowed by the normal Slack DM policy.
Unset/empty scopes, invalid IDs, duplicates, mixed wildcard/IDs, values over
2048 characters, and any enabled value other than `1` leave recovery disabled.
Disabled or empty-scope startup does not register the extra recovery hooks,
call Slack history/readiness APIs, or inspect/create/change the catchup directory
or journal. Existing state is preserved.

When enabled for eligible users, each startup reads one-to-one DM history for
the preceding 30 minutes, with
that cutoff fixed for the whole scan. Before taking the cutoff, recovery waits
up to 120 seconds for authenticated localhost `channels.status` to report the
Slack account connected. The public gateway client requests only `operator.read`
and does not create a device identity. Readiness failure records an unresolved
reason and leaves startup/live reception available. This wait stays in the
background so gateway startup can finish. Limits are 100 DM conversations, 200
history messages per DM (two pages), 20 replayed questions per DM, and 200
replayed questions overall. Questions run oldest first within each DM.
The 100-DM budget counts only eligible, allowlisted DM conversations, not raw
listing rows. Listing uses GET query parameters, 100 rows per page, and a
separate 20-page safety limit. It stops when all explicitly allowed users have
been found or there is no next cursor. A repeated cursor alone does not stop
the scan; the page limit bounds retries and reports `list_page_limit_incomplete`.
Non-DMs, missing/invalid peer IDs, known bots (including Slackbot and this bot),
deleted peers, externally shared DMs and non-allowlisted users are skipped
without reading history. Repeated eligible DM rows are read only once.
One `dm catchup listing pages=N skipped={...}` line aggregates fixed reason
codes and counts, without conversation/user IDs, bodies or email addresses.
Bot/deletion/external classification uses fields present in the listing; no
extra `users:read` scope or user-profile API is introduced.
An incomplete history page budget skips that DM: without the complete reply
history, replay could duplicate an existing answer. An exhausted replay or
conversation budget records an unresolved reason rather than silently claiming
that recovery completed. There is no periodic backfill worker.

Live reception and history share a durable `(team, channel, ts)` reservation.
The public `reply_dispatch` hook checks that exact source before model execution;
`message_received` alone cannot stop the ordinary core dispatch path.
`OPENCLAW_STATE_DIR/slack-catchup/journal.json` stores source/reply timestamps,
started/done states, and unresolved reason counters. It contains no question,
answer, email, or token. The directory is mode 0700, atomic journal writes are
mode 0600, and symlink/corrupt state is rejected. Existing SQLite is untouched.
The existing single-writer gateway lock remains a prerequisite; this is not a
claim service for overlapping gateway tasks. A failed journal disables replay
but leaves normal authorized live processing available, with a warning.

To avoid repeating replies from older images, recovery uses the authenticated
bot's most recent top-level post in each DM as a conservative boundary. Questions
older than that post are skipped and recorded as done for delayed live delivery.
This includes progress, feedback, and digest posts: an unanswered question before
such a post can be missed. This first rollout accepts that loss to prioritize
avoiding duplicate answers; the boundary does not prove that every older question
was answered.

A completed answer also has Slack metadata with `event_type=teamagent.dm_answer_v1`
and `event_payload={source_channel, source_ts, complete:true}`. When an existing
Slack metadata type must be preserved, the marker is nested under its
`event_payload.teamagent_dm_answer_v1`. Exact markers are accepted only from
the authenticated bot. Saved done rows are checked against Slack again after restart.
Replay buffers final payloads and sends the last final and its complete marker
in the same `chat.postMessage`. The public buffered dispatcher applies existing
reply suppression and scrubbing before the plugin sends the prepared payload.

Ordinary native replies retain the native formatting/delivery path. The plugin
correlates the final payload to `message_sent`, attaches the marker with a
metadata-only `chat.update`, and reads it back before marking the journal done.
Overlapping native questions, identical replies, or
transformed reply text can prevent unambiguous correlation; failures are
unresolved rather than guessed as a matching source. The conservative bot-post
boundary still applies on the next startup. An upstream atomic source-metadata
send hook would allow that broad boundary to be narrowed later.

Text questions in top-level DMs are supported. Attachment questions, thread-only
messages, slash/control messages and unsupported outbound media remain unresolved
rather than risk fetching a different question's attachment. Slack DM history
does not enumerate thread replies; those need a separate bounded replies scan.
Questions outside the 30-minute window are not processed. The replay has the
normal DM route and identity, but re-executing a run interrupted after an external
tool action can repeat that action; this is not a transaction across Slack and
all tools.

`im:read` and `im:history` must be granted to the installed bot token. A
`missing_scope` response skips catchup and allows startup/live reception to
continue. Do not print tokens to inspect grants. Check app configuration and
safe API response fields, plus successful metadata persistence/readback, on a
canary installation. Logs use fixed `dm catchup reason=...` codes and a summary
`dm catchup completed scanned=N replayed=N`; unresolved counters persist in the
journal. The replayed count measures dispatches started, not successful answers.

Ship the plugin and entrypoint in the normal OC image release with recovery OFF;
no added ECS cost is needed. Enable `DM_CATCHUP_ENABLED=1` with only 小俣さん's
verified Slack U ID in `DM_CATCHUP_ALLOWED_USERS`, then run real replacement-time
DM, live/history-overlap, already-answered, and ordinary reply checks on the exact
image. Confirm the conservative bot-post skipping behavior before changing the
scope to `*`. Return `DM_CATCHUP_ENABLED=0` to stop recovery without removing state.

Inject both recovery inputs through the operational `register_oc_td_image_env.sh`
path, first scoped only to 小俣さん's verified Slack U ID. That script is not
checked into this repository and was unavailable for inspection; its own
environment replacement/preservation behavior remains unverified. The reviewed
task-definition hardener accepts and preserves these two optional string inputs,
and the entrypoint passes them to the gateway through its explicit environment
allowlist.

Checked-in image promotion uses the existing full `-var-file` saved-plan/guard
path described above. Roll back the image through that same guarded release path.
Keep the journal during rollback; older images ignore it, and a later startup
rechecks it against Slack. Neither rollout nor rollback should delete SQLite or
EFS state. Verify downtime recovery, live/history overlap, already answered DM
skipping, missing scopes, private-file permissions, unresolved counters and
normal native reply metadata on the exact production image before rollout.
