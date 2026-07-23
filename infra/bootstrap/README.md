# Production provenance/IAM bootstrap

This directory contains the only first-install path for the TeamAgent
provenance control plane. It exists because the normal saved-plan guard needs
the automation role and one-use deployment-intent ledger before the main
Terraform state can create them.

The bootstrap does **not** weaken `terraform_runtime_guard.sh`. It is a
different, one-time control-plane workflow with a smaller authority:

1. All three release contracts must be exactly `release.ready=false`, with a
   non-empty blocked reason. This local check runs before the first AWS call.
2. The configured bootstrap principal (an IAM administrator user such as
   `AIIAdev`), using an MFA-authenticated temporary session, creates
   `seed-stack.yaml`. The stack owns one temporary IAM role and its
   explicit-deny managed policy, and no production object. A random nonce is
   bound to the CloudFormation client request token, stack parameters, stack
   tags, role tags, and the local invocation artifact. Cleanup requires that
   exact ownership proof.
3. The bootstrap principal assumes that role for one hour with a fixed external
   ID, session name, and source identity. The role has explicit denies for
   CodeBuild execution, ECR image writes/deletes, KMS signing, release-evidence
   object writes, long-lived IAM credentials, runtime mutation, debug sessions,
   and all role chaining.
4. The wrappers first require a clean detached `HEAD` equal to both the local
   `refs/remotes/origin/dev` and a fresh credential-free HTTPS lookup. They
   independently fetch that commit, verify every tracked blob plus each
   transitive child SHA-256, make the reviewed checkout read-only, and execute
   from that checkout. Bootstrap-principal credentials are restored only after
   this review.
5. The role makes one saved Terraform plan against the existing **main**
   backend. Targets are fixed in `bootstrap_contract.json`; operator-supplied
   targets are impossible. Inherited Git/Terraform control variables and
   automatic tfvars are rejected. Terraform uses a new private data directory,
   AWS CLI v2/Terraform 1.12.x/Python 3.11+/Git executables are pinned by canonical
   identity and SHA-256. Ignored Terraform overrides and
   skip-worktree/assume-unchanged index flags are rejected, and the reviewed
   hashes/versions are written to the receipt. Every tracked file/symlink
   byte, type, and executable bit is matched to `HEAD`; a materialized-tree
   SHA-256 is recorded in the receipt. The fresh `origin/dev` lookup uses the
   fixed HTTPS URL with global/system Git config disabled and rejects local
   transport redirects.
6. `provenance_iam_bootstrap.py` accepts only `create` and `no-op`. Updates,
   deletes, replacements, imports, moved resources, drift, deferred changes,
   plans not marked by Terraform as fixed-target (`complete=false`), runtime
   resources, and unknown dependencies fail closed. Before apply, each
   upsert-style create has an exact AWS absence/ownership probe; this includes
   the `AIIAdev` inline policy, role/user inline policies, ECR lifecycle
   policies, and S3 subresources.
7. A consistent preflight rejects an already-burned ID before seed creation;
   a later conditional item in `teamagent-tflock` closes the race and burns
   the fixed bootstrap ID. The reviewed plan is applied once, then the
   before/after main-state lineage/serial/address sets are reconciled exactly.
   Complete handoff claims and ownership documents are atomically persisted,
   file-fsynced, and directory-fsynced before the ledger can become
   `CONSUMED`.
8. The seed trust policy is closed, its CloudFormation-owned inline boundary
   is replaced with a deny covering the issued-session window, the denial is
   proved with the issued session, the stack/role/policy are deleted, and a
   private receipt records the state handoff.

## Ownership

| Object | During bootstrap | Terminal owner |
|---|---|---|
| Temporary seed role and deny policy | CloudFormation seed stack | None; revoked and deleted |
| Provenance IAM/KMS/S3/ECR/CodeBuild/CodeConnections | Main Terraform backend from birth | Main Terraform |
| Deployment-intent table and gate role | Main Terraform backend from birth | Main Terraform |
| Runtime automation role and recipient-ack key/signer | Main Terraform backend from birth | Main Terraform |
| Runtime automation permissions boundary | Main Terraform backend from birth | Main Terraform; runtime identity cannot modify it |
| One-use bootstrap ledger row | Durable audit row in existing backend lock table | Retained audit evidence |
| Local plan/state/receipt files | Operator-owned `0700` directory, `0600` files | Operator audit archive |

There is no bootstrap Terraform state and no `terraform import`/`state rm`.
Consequently, no AWS object can be claimed simultaneously by bootstrap and
main state. The handoff is a verified transition of the existing main-state
serial and address set, not a copy between two state files.

## Failures

- A failure is retryable only when a consistent ledger read proves the row
  absent and retirement proves the exact seed stack, role, and deny policy
  absent. The failure receipt then says
  `FAILED_REVIEWED_RETRY_ALLOWED`; a fresh review is still required.
- Once the ledger request may have been accepted, ambiguous `PREPARED` or
  `APPLYING` responses are reconciled by the exact nonce and transitioned to
  `RECONCILE_REQUIRED`. A terminal `CONSUMED` row is observed but never
  rewritten. The tool does not retry or mint a second bootstrap.
  Using an MFA-authenticated temporary session for the configured bootstrap
  principal, run only the reviewed `reconcile-retire` command against the
  original artifact directory. It never calls `terraform apply` and never
  reapplies a consumed plan; it reconciles current main-state ownership when
  needed and idempotently retires only the nonce-owned seed.
- A `PREPARED`, `APPLYING`, `RECONCILE_REQUIRED`, or `CONSUMED` row blocks a
  second invocation because creation uses
  `attribute_not_exists(LockID)`.

## CodeConnections and release contracts

An absent GitHub CodeConnection may be created by the create-only plan. AWS
creates it as `PENDING`; this is a safe terminal bootstrap state. The receipt
records `PENDING` or `AVAILABLE`, but never treats `PENDING` as build-ready.
The TeamAgent, OpenClaw, and TikTok build launchers enumerate the exact
connection, reject pagination/ambiguity, then require an exact `AVAILABLE`
`GetConnection` response before any evidence write or CodeBuild start.

The bootstrap never changes a release contract. Build and release launchers
continue to reject `release.ready=false` before their first AWS call.

See [the operator runbook](../../docs/runbooks/provenance_iam_bootstrap.md).
