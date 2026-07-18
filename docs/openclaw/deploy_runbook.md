# OpenClaw trusted release and deployment Runbook

This is the only supported OpenClaw production path. It deliberately fails
closed while the separately owned trusted source/promotion/receipt framework
is unavailable. Do not substitute an ad-hoc Buildx push, arbitrary S3 ZIP
metadata, a local manifest checksum, direct ECR tagging, direct ECS update, or
Terraform target apply.

Normal Terraform operation keeps `openclaw_image=""`; the existing service is
updated only through the receipt-gated helper.

All account, region, repository, cluster, service, task family, IAM role, app
artifact, and canary identities are fixed in
`infra/openclaw/trusted-release-contract.json`; operator environment variables
cannot retarget them.

## 1. Required trust boundary

The shared framework must install both of these immutable, non-symlink files:

```text
/opt/teamagent/trusted-release/bin/trusted-release
/opt/teamagent/trusted-release/contracts/teamagent-openclaw-production-v1.json
```

The installed contract must be byte-identical to the repository copy. The
shared framework, not the OpenClaw builder, owns:

- KMS verification of a trusted publisher statement;
- exact Git commit archive/build-context binding;
- quarantine publication and immutable canonical promotion;
- image, provenance, SBOM, evidence-index signatures and exact referrer set;
- fresh, one-time deployment-receipt issuance and atomic consumption;
- durable previous-task and signed rollout-result records.

The S3 source ZIP and all object metadata are untrusted transport. `.git`
absence is not an exception. If the shared verifier, contract, publisher
statement, or exact commit proof is missing, CodeBuild must stop before
executing a repository script.

## 2. Local ARM64 verification

Run only from a clean attached commit:

```sh
test -z "$(git status --porcelain --untracked-files=all)"
COMMIT=$(git rev-parse HEAD)
SHORT=${COMMIT:0:12}
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:git-$SHORT" \
  --manifest "/tmp/openclaw-$SHORT-manifest.json" \
  --evidence-dir "/tmp/openclaw-$SHORT-evidence"
(cd /tmp && sha256sum -c "openclaw-$SHORT-manifest.json.sha256")
```

The local manifest must satisfy:

```sh
jq -e '
  .schemaVersion == 4 and
  .deploymentCredential == false and
  .promotion.status == "LOCAL_GATES_PASSED" and
  .promotion.registryPublished == false and
  .promotion.canonicalTagPublished == false and
  .runtime.platform == "linux/arm64" and
  .runtime.actualImageContractPassed == true and
  .runtime.controlUiFullAssetClosureValidated == true and
  .materials.exactSetMatch == true and
  .sbom.wholeFilesystemExactMatch == true and
  .sbom.physicalNpmMultisetExactMatch == true and
  .scan.exactSingleLinuxArm64Subject == true and
  .scan.critical == 0 and .scan.high == 0 and .scan.secrets == 0 and
  .scan.allKnownLiveFindingsAbsent == true and
  (.scan.knownLiveFindingIdsAbsent | length) == 8
' "/tmp/openclaw-$SHORT-manifest.json"
```

The latest observed live ARM64 image is C8/H22 and contains OpenSSL
`CVE-2026-34182`, Perl `CVE-2026-12087`/`13221`/`57433`, Python 3.11
`CVE-2026-6100`, GnuTLS `CVE-2026-33845`/`42010`, and libssh2
`CVE-2026-55200`. The candidate is blocked unless its exact single ARM64
subject is C0/H0 and all eight IDs are absent.

The evidence directory includes merged-rootfs inventory, whole-filesystem
CycloneDX SBOM and equivalence result, npm multiset, exact materials,
vulnerability/secret scans, runtime inventory, actual-image contract,
UI 142-file source/HTTP closure, plugin operation smoke, gateway lifecycle,
and an index hashing every evidence file.

## 3. Trusted source and canonical promotion

The trusted source publisher supplies a KMS-signed statement at the fixed
archive path `.trusted-release/source-statement.json`. Operators must not
construct or override source commit/branch/archive claims in `start-build`.
The OpenClaw CodeBuild project uses the Terraform-embedded buildspec and a role
that can read the fixed transport object and write evidence, but cannot call
ECR authentication/write APIs or assume a promoter role.

The shared promoter performs this sequence:

1. Reverify publisher signature, commit archive, source root, build identity,
   and exact build context.
2. Build/test/scan locally; no canonical tag exists yet.
3. Transfer the exact locally gated subject to quarantine.
4. Reverify the exact material set, whole-filesystem SBOM equivalence, all
   evidence hashes, C0/H0/S0, and all eight live-CVE absences.
5. Sign the image, provenance, whole-filesystem SBOM, and evidence index.
6. Atomically promote the exact subject and exact referrer set to immutable
   `git-$SHA`.

Retries must be idempotent and may not delete an approved release. A subject,
source, builder, material, referrer, signature, or scan mismatch is NO-GO.

The resulting trusted promotion record must identify one `linux/arm64`
manifest and include verified digests for the subject, provenance,
whole-filesystem SBOM, image signature, evidence-index signature, and exact
referrer set. Builder-produced booleans are not trust evidence.

## 4. Trusted deployment receipt

A release manifest is not deployable. Ask the shared framework to issue a
KMS-signed deployment receipt no more than 900 seconds before use. The receipt
must be one-time and bind:

- fixed account/repository/cluster/service/family;
- exact immutable image manifest digest and single ARM64 platform;
- source commit/archive and CodeBuild identity;
- whole-filesystem SBOM, provenance, image signature, evidence index, and
  referrer-set digests;
- C0/H0/S0 and all eight live-CVE absences;
- Connect Web `codebuild/connect-web-app.html` S3 VersionId plus
  `appHtmlSha256`, `manifestSha256`, `buildInputsSha256`, and `dataSha256`;
- current and previous task-definition ARN, canonical current-task hash,
  exact rendered registration-payload hash, deployment intent/plan digest,
  circuit-breaker rollback intent, and post-stable canary intent.

The four app anchors preserve the corrected current application provenance;
an OpenClaw rollout cannot silently select a different app artifact.

## 5. Render without AWS mutation

`--render-only` still requires the shared verifier and a fresh, valid,
unconsumed receipt:

```sh
bash infra/terraform/apply_openclaw.sh \
  --render-only /path/to/current-task-definition.json \
  /path/to/trusted-deployment-receipt.json \
  > /tmp/openclaw-rendered-task.json
```

The current-task fixture must come from the exact fixed family represented in
the receipt. Review that the rendered payload has exactly:

- one container named `openclaw`, no sidecars;
- one `openclaw-tmp` volume mounted writable only at `/tmp`;
- no other volume/mount, including writable `/data`;
- ARM64/Fargate/awsvpc and the fixed task/execution roles;
- immutable ECR digest in the fixed OpenClaw repository;
- UID/GID 65532, read-only rootfs, nonprivileged, capability drop `ALL`;
- canonical image ENTRYPOINT/CMD, `/readyz`, stop timeout, logs, environment,
  and four Secrets Manager bindings.

Any extra field or retargeted family/role/repository/environment is rejected.

## 6. Deploy and automatic rollback

Deployment is:

```sh
bash infra/terraform/apply_openclaw.sh \
  /path/to/trusted-deployment-receipt.json
```

The helper:

1. Re-reads the fixed service/current task and verifies the signed plan.
2. Registers the exact reviewed task payload.
3. Re-reads the service immediately before mutation.
4. Atomically consumes the receipt while durably recording previous/new task
   ARNs.
5. Calls `update-service` with ECS circuit breaker and rollback enabled.
6. Waits for `services-stable`.
7. Runs a one-off task using the exact new revision and service network.
8. Proves ECS task-role Bedrock `Converse`, exact MCP `tools/list`, and the
   reviewed 28-entry maximum scope.
9. Proves Slack connection and an exact canary mention/reply, then cleans the
   canary message.
10. Requires the shared framework to sign and durably store the rollout
    result.

On update, stability, one-off task, Slack, Bedrock, MCP, or durable-record
failure, the helper emits `OpenClawRolloutGateFailure`, restores the durable
previous task ARN, waits stable, and exits nonzero.

Do not manually bypass receipt consumption or the automatic gates. A consumed
receipt cannot be replayed.

## 7. Alarms and health

The OpenClaw startup metric filter matches both structured events:

```text
openclaw_config_invariant_violation
openclaw_entrypoint_error
```

The startup-failure alarm and rollout-gate-failure alarm must be active.
ECS uses `/readyz`; MCP uses `/healthz`. Gateway health alone is not rollout
success.

## 8. Tool scope

`infra/openclaw/effective-tool-scope.json` is the machine-readable authority.
The reviewed OpenClaw include list has 28 entries; the default Terraform MCP
task registers 12:

```text
search, clientkarte, proposal_draft, proposal_review,
mail_summary, mail_followup, mail_to_internal_context,
mail_reply, mail_draft, morning_digest, oauth_connect, knowledge_deliver
```

The exact deployed `tools/list` must equal the enabled set. This is **not read-only**:
Gmail draft creation and Slack file delivery are enabled, and
optional gates can add calendar writes, jobs, reports, and S3 writes.

## 9. Residual risks and production gate

- Fargate does not support Docker `no-new-privileges`; production does not
  claim it. Local tests do.
- Fargate has no task `tmpfs`; `/tmp` is a task-scoped ephemeral volume.
- Browser-named shared payload and generic child-process primitives remain,
  although executable browser control paths are removed and the bridge facade
  fails closed.
- The shared trusted-release implementation is outside this change and must
  receive its own review.
- Local provider-stubbed tests cannot establish real ECR referrers, Fargate
  behavior, Slack auth/reply, Bedrock task-role access, or live MCP scope.

Production is NO-GO until the shared framework is present and independently
reviewed, the final OpenClaw commit is independently reviewed, and the real
CodeBuild/ECR/Fargate/Slack/Bedrock/tools-list gates all pass.
