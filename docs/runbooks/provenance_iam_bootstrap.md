# Provenance/IAM one-time bootstrap runbook

This runbook installs only the production provenance control plane needed to
break the initial IAM/deployment-intent cycle. It performs AWS writes when the
`run` command is invoked. The implementation and tests do not execute AWS
operations automatically.

## Preconditions

- Use account `718959508629`, region `ap-northeast-1`.
- Use a clean detached `HEAD` that equals the exact local
  `refs/remotes/origin/dev` commit and a fresh protected `origin/dev` HTTPS
  lookup. The wrapper performs the lookup and transitive child hashing without
  AWS credentials, then executes from an independently fetched read-only
  checkout.
- The three release contracts must remain `release.ready=false`.
- `AIIAdev`, `teamagent-dev-image-builder`,
  `teamagent-dev-raw-files`, `teamagent-tfstate-718959508629`, and
  `teamagent-tflock` must already exist exactly. The bootstrap adopts none of
  them and fails if they are absent or ambiguous.
- The root credentials visible to the CLI must be an explicit
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`
  temporary set authenticated with MFA. Profile or long-term fallback is
  rejected. No root or IAM-user access key is created by this workflow.
- Prepare a parent directory owned by the operator and not writable by group
  or other users. The requested artifact directory itself must not exist.
- The tfvars file must be a regular, non-symlink `0600` file owned by the
  operator.
- Use AWS CLI v2, Terraform 1.12.x, and Python 3.11 or newer. The tool rejects inherited
  `TF_*`/Git selectors, automatic tfvars, an artifact directory inside the
  worktree, and a reused Terraform data directory. It pins the canonical
  AWS/Git/Python/Terraform executable bytes for the run and rejects ignored
  Terraform overrides plus skip-worktree/assume-unchanged index flags. The
  fresh protected-head lookup uses the fixed GitHub HTTPS URL with
  global/system Git config disabled and local transport redirects rejected.
  Every tracked file/symlink byte, type, and executable bit must match
  `HEAD`.

Run the local, AWS-free contract check first:

```bash
bash infra/deploy/bootstrap_provenance_iam.sh validate-contract
```

## One-time run

```bash
install -d -m 0700 "$HOME/.teamagent-bootstrap"
chmod 600 "$HOME/.teamagent-bootstrap/teamagent.tfvars"

bash infra/deploy/bootstrap_provenance_iam.sh run \
  --var-file "$HOME/.teamagent-bootstrap/teamagent.tfvars" \
  --artifact-dir "$HOME/.teamagent-bootstrap/provenance-iam-v1"
```

Do not add `-target`, change the target manifest, run plain `terraform apply`,
or copy objects into a second state. The entrypoint supplies its own fixed
targets and rejects every non-create/non-no-op change.

Expected private artifacts include:

- `bootstrap-invocation.json` and `bootstrap-seed-created.json`;
- `provenance-bootstrap.tfplan` and its JSON rendering;
- `main-state-before.json` / `main-state-after.json`;
- `bootstrap-handoff-claims.json`,
  `bootstrap-handoff-ownership.json`, and
  `bootstrap-handoff-durable.json`;
- ledger transition responses;
- `bootstrap-receipt.json`.

The receipt includes the reviewed contract/seed/tfvars hashes, the
materialized source-tree SHA-256, and executable paths, versions, sizes, and
SHA-256 values. It also records the exact CloudFormation stack ID and hashed
root/session identifiers. Repository cleanliness, fixed branch/origin/commit,
tracked bytes/modes, input hashes, plan bytes, and executable bytes are
rechecked at each mutation boundary.

Before apply, the tool proves exact AWS absence or existing main-state
ownership for every Put/upsert-style create. In particular,
`AIIAdev/require-teamagent-codebuild-launcher-role` must return exact
`NoSuchEntity`; an existing unowned policy is a hard stop.

Success requires all of the following:

- one main-state lineage, with a strictly increasing serial;
- no removed address and no address added beyond the reviewed create actions;
- required launcher, deployment-gate, automation, and OpenClaw connection
  addresses owned by main state;
- no temporary seed role/policy name anywhere in main state;
- ledger terminal state `CONSUMED`;
- seed trust closed before the CloudFormation-owned inline boundary is
  replaced with the issued-session-window deny, and the session probe denied
  afterward;
- seed CloudFormation stack, role, and deny policy deleted.

Archive the entire `0700` directory under the approved audit retention policy.
The receipt hashes the root `UserId` and assumed-role ID rather than storing
those identifiers in clear text.

## PENDING or absent CodeConnections

The first run may end with:

```text
teamagent-dev-openclaw-codebuild  PENDING
teamagent-dev-tiktok-codebuild    PENDING   # only when the media/TikTok stack is enabled
```

This is expected and remains NO-GO. Complete the GitHub App handshake through
the AWS CodeConnections UI, then independently verify each required connection
is `AVAILABLE`. Do not recreate a connection to obtain a different ARN; the
main state owns the connection created by bootstrap.

If TikTok/media is disabled, no TikTok connection instance is required and an
empty TikTok result is safe. The OpenClaw/TeamAgent shared connection is always
required.

## Short-lived runtime session

Root is not accepted by `terraform_runtime_guard.sh`. Enter each guard command
through an exact main-owned STS role:

```bash
bash infra/deploy/bootstrap_runtime_session.sh issue-alarm-challenge \
  --out /secure/path/alarm-challenge.json

bash infra/deploy/bootstrap_runtime_session.sh sign-alarm-ack \
  --challenge /secure/path/alarm-challenge.json \
  --out /secure/path/alarm-ack.json

bash infra/deploy/bootstrap_runtime_session.sh attest-alarm-delivery \
  --challenge /secure/path/alarm-challenge.json \
  --recipient-ack /secure/path/alarm-ack.json \
  --out /secure/path/alarm-delivery.json

bash infra/deploy/bootstrap_runtime_session.sh verify \
  --plan /secure/path/runtime.tfplan
```

The wrapper accepts only the guard's actual subcommands. Runtime commands use
session name `teamagent-terraform-worker` and source identity
`teamagent-production-terraform`. Only `sign-alarm-ack` is routed to the
one-hour KMS-Sign-only signer session
`teamagent-alarm-recipient-ack`; this preserves separation from the runtime
role, which explicitly denies CodeBuild starts, ECR image writes, KMS signing,
debug sessions, long-lived IAM credentials, and release-evidence object
mutation. Runtime Terraform uses repo-owned exact-action inline policies under
an immutable permissions boundary that denies IAM self-escalation and all role
chaining. Deployment-gate checks run directly under that bounded runtime
session; no second role is assumed. The role cannot modify its policies or
boundary, mutate the bootstrap seed IAM objects, mutate the durable bootstrap
ledger row, or delete the ledger table. Both root paths require MFA and fixed
source identity.

## Build/release after approval

Do not run this section while any selected contract is blocked or while its
connection is not `AVAILABLE`.

For the root-only operational environment, mint the exact launcher session and
execute one pinned launcher in the same process:

```bash
bash infra/deploy/bootstrap_provenance_session.sh teamagent

bash infra/deploy/bootstrap_provenance_session.sh openclaw

bash infra/deploy/bootstrap_provenance_session.sh release \
  --pipeline mcp \
  --channel active \
  --receipt-key 'release-receipts/mcp/<commit>/<sha256>.json' \
  --receipt-version-id '<version-id>' \
  --receipt-signature-version-id '<signature-version-id>'
```

The wrapper validates the selected release contract before its first AWS call.
Root calls `sts:AssumeRole` only. The build/release AWS calls are made by the
exact launcher session, and every launcher rejects a direct root identity.
The former dedicated IAM-user callers remain compatible, but this workflow
creates no access key for them.

## Failure and reconciliation

If `bootstrap-failure.json` reports `RECONCILE_REQUIRED`, or its
`ledger_state` is `UNKNOWN_RECONCILIATION_REQUIRED`, `PREPARED`, `APPLYING`,
`RECONCILE_REQUIRED`, or `CONSUMED`, stop. Do not delete the durable ledger
row, edit its state, rerun the bootstrap, import objects, or apply the plan
manually. `FAILED_REVIEWED_RETRY_ALLOWED` is emitted only after a consistent
read proves that no ledger row exists and retirement proves the stack, role,
and deny policy absent; even then, start from a fresh review.

For any durable row, run the idempotent recovery command against the original
private artifact directory:

```bash
bash infra/deploy/bootstrap_provenance_iam.sh reconcile-retire \
  --artifact-dir "$HOME/.teamagent-bootstrap/provenance-iam-v1"
```

The command never executes `terraform apply`. A `CONSUMED` row must match the
fsynced handoff hashes and is only retired; an incomplete row may use
`terraform state pull` to reconcile ownership, but the saved plan is never
reapplied. Seed retirement proceeds only after the exact stack ID, nonce,
commit, tags, parameters, CloudFormation resources, role tags, and sole
managed-policy attachment are proved.

Review, at minimum:

1. the saved plan SHA-256 and local file identity;
2. backend lineage and serial before/after;
3. every planned create address versus the current main state;
4. CloudTrail events for seed stack creation, AssumeRole, Terraform API calls,
   revocation, and stack deletion;
5. whether any create succeeded without its state address.

The one-time tool intentionally contains no force, unlock, reset,
state-remove, import, or retry switch.
