# Production provenance/IAM bootstrap

This directory contains the only first-install path for the TeamAgent
provenance control plane. It exists because the normal saved-plan guard needs
the automation role and one-use deployment-intent ledger before the main
Terraform state can create them.

The bootstrap does **not** weaken `terraform_runtime_guard.sh`. It is a
different, one-time control-plane workflow with a smaller authority:

1. All three release contracts must be exactly `release.ready=false`, with a
   non-empty blocked reason. This local check runs before the first AWS call.
2. The exact account root, using an MFA-authenticated temporary session,
   creates `seed-stack.yaml`. The stack owns one temporary IAM role and its
   explicit-deny managed policy, and no production object.
3. Root assumes that role for one hour with a fixed external ID, session name,
   and source identity. The role has explicit denies for CodeBuild execution,
   ECR image writes/deletes, KMS signing, release-evidence object writes,
   long-lived IAM credentials, runtime mutation, debug sessions, and all role
   chaining.
4. The role makes one saved Terraform plan against the existing **main**
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
5. `provenance_iam_bootstrap.py` accepts only `create` and `no-op`. Updates,
   deletes, replacements, imports, moved resources, drift, incomplete plans,
   runtime resources, and unknown dependencies fail closed.
6. A consistent preflight rejects an already-burned ID before seed creation;
   a later conditional item in `teamagent-tflock` closes the race and burns
   the fixed bootstrap ID. The reviewed plan is applied once, then the
   before/after main-state lineage/serial/address sets are reconciled exactly.
7. The seed trust policy is closed, its CloudFormation-owned inline boundary
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
  Reconcile the exact main-state serial and AWS inventory under a separate
  review before writing any recovery code.
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
