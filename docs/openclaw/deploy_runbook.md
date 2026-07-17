# OpenClaw production release and deployment Runbook

This is the authoritative OpenClaw path. Production image publication uses the
dedicated provenance-bound CodeBuild project. Deployment of the existing ECS
service uses `infra/terraform/apply_openclaw.sh`. Do not publish with ad-hoc
Buildx, Compose, or the legacy MCP CodeBuild project, and do not update the
existing OpenClaw service through Terraform.

AWS commands below are operator actions. Review their output and account/region
before execution. Never paste secret values into chat, logs, or Git.

## 1. Preconditions

- Source is an exact clean commit on the reviewed repository.
- The versioned S3 source object carries metadata `git-commit` and
  `source-sha256`; the latter is the SHA-256 of that exact ZIP object.
- ECR and the dedicated
  `teamagent-<env>-openclaw-image-builder` CodeBuild project exist.
- The existing ECS task has the four Secrets Manager bindings:
  `TEAMAGENT_MCP_BEARER`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and
  `OPENCLAW_GATEWAY_TOKEN`.
- Bedrock access comes from the OpenClaw task role. Do not inject static AWS
  access keys.

## 2. Local pre-publication verification

This builds and tests ARM64 locally but does not push:

```sh
test -z "$(git status --porcelain --untracked-files=all)"
SOURCE_COMMIT=$(git rev-parse HEAD)
OC_TAG="git-${SOURCE_COMMIT:0:12}"
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:$OC_TAG" \
  --manifest "/tmp/openclaw-$OC_TAG-manifest.json" \
  --evidence-dir "/tmp/openclaw-$OC_TAG-evidence"
(cd /tmp && sha256sum -c "openclaw-$OC_TAG-manifest.json.sha256")
```

The local manifest must say `buildAttestations.registryPublished=false`; it is
test evidence and is deliberately not deployable.

## 3. Production build and registry attestation

Start only the dedicated OpenClaw CodeBuild project. Supply the exact S3 object
version and override all three source identity values:

```sh
R=ap-northeast-1
PROJECT=teamagent-dev-openclaw-image-builder
SOURCE_COMMIT=<40-lowercase-hex>
SOURCE_BRANCH=<reviewed-branch>
SOURCE_ARCHIVE_SHA256=<sha256-of-versioned-source.zip>
SOURCE_VERSION=<s3-object-version-id>

BUILD_ID=$(aws codebuild start-build \
  --region "$R" \
  --project-name "$PROJECT" \
  --source-version "$SOURCE_VERSION" \
  --environment-variables-override \
    "name=SOURCE_COMMIT,value=$SOURCE_COMMIT,type=PLAINTEXT" \
    "name=SOURCE_BRANCH,value=$SOURCE_BRANCH,type=PLAINTEXT" \
    "name=SOURCE_ARCHIVE_SHA256,value=$SOURCE_ARCHIVE_SHA256,type=PLAINTEXT" \
  --query 'build.id' --output text)
aws codebuild batch-get-builds --region "$R" --ids "$BUILD_ID"
```

The build must finish `SUCCEEDED`. Download its
`openclaw-$SOURCE_COMMIT` artifact from the returned S3 artifact location and
extract it locally. Let `OC_MANIFEST` point at
`openclaw-build-manifest.json` in that bundle.

Validate before deployment:

```sh
(cd "$(dirname "$OC_MANIFEST")" && \
  sha256sum -c "$(basename "$OC_MANIFEST").sha256")
jq -e --arg commit "$SOURCE_COMMIT" '
  .schemaVersion == 3 and
  .source.uri == "https://github.com/noirelumiere00/teamagent" and
  .source.commit == $commit and
  .runtime.platform == "linux/arm64" and
  .runtime.uid == 65532 and .runtime.gid == 65532 and
  .runtime.actualImageContractPassed == true and
  .runtime.browserReachabilityValidated == true and
  .runtime.forbiddenPackageOrPluginArtifacts == 0 and
  .runtime.developmentPayloadArtifacts == 0 and
  .scan.critical == 0 and .scan.high == 0 and .scan.secrets == 0 and
  .sbom.physicalNpmMultisetExactMatch == true and
  .sbom.bomRefIntegrity == true and
  .buildAttestations.registryPublished == true and
  .buildAttestations.subjectValidated == true and
  .buildAttestations.sourceValidated == true and
  .buildAttestations.builderValidated == true
' "$OC_MANIFEST"
```

The evidence bundle includes the actual-image contract, physical runtime
inventory, CycloneDX SBOM and npm multiset, vulnerability/secret scans,
Slack/Bedrock plugin inventory, gateway log, and parsed registry
provenance/SPDX documents. Every manifest evidence path is relative to the
manifest directory and remains valid after artifact extraction. Verify every
recorded SHA-256 before archiving:

```sh
MANIFEST_DIR=$(cd -- "$(dirname -- "$OC_MANIFEST")" && pwd -P)
jq -r '
  [
    .evidence[]?,
    {path:.sbom.path, sha256:.sbom.sha256},
    .sbom.npmInventoryEvidence,
    .scan.vulnerabilityEvidence,
    .scan.secretEvidence,
    .buildAttestations.provenanceEvidence?,
    .buildAttestations.sbomEvidence?
  ] |
  .[] | select(. != null) | "\(.sha256)  \(.path)"
' "$OC_MANIFEST" | (cd "$MANIFEST_DIR" && sha256sum -c -)
```

## 4. Deploy the existing ECS service

OpenClaw is CLI-managed because a previous count-gated Terraform apply deleted
the service. Keep `openclaw_image=""` in normal Terraform operation.

First render the exact registration payload without AWS mutation:

```sh
aws ecs describe-task-definition \
  --region ap-northeast-1 \
  --task-definition teamagent-dev-openclaw \
  --query taskDefinition --output json > /tmp/openclaw-current-task.json
bash infra/terraform/apply_openclaw.sh \
  --render-only /tmp/openclaw-current-task.json "$OC_MANIFEST" \
  | jq '{family,runtimePlatform,volumes,containerDefinitions}'
```

Confirm the `openclaw` container has:

- the manifest's ECR `@sha256:` runtime reference;
- `readonlyRootFilesystem=true`, `user="65532:65532"`,
  `privileged=false`, and `linuxParameters.capabilities.drop=["ALL"]`;
- only `/tmp` mounted from `openclaw-tmp` as writable;
- no task `entryPoint` or `command` override;
- `/readyz` health check and `stopTimeout=30`;
- the canonical gateway remains PID 1 and exits 0 after clean SIGTERM;
- the same IAM roles, Secrets Manager bindings, logging, AWS region, and
  `SLACK_DM_ALLOWLIST` as the current revision.

Then perform the rolling update:

```sh
bash infra/terraform/apply_openclaw.sh "$OC_MANIFEST"
```

The helper registers one hardened revision, updates the existing service, and
waits for `services-stable`. It does not create a service and does not run
Terraform.

## 5. Runtime verification

Inspect the deployed task definition and task:

```sh
aws ecs describe-services \
  --region ap-northeast-1 \
  --cluster teamagent-dev \
  --services teamagent-dev-openclaw
aws ecs describe-task-definition \
  --region ap-northeast-1 \
  --task-definition teamagent-dev-openclaw
```

Required observations:

- ECS health is green on OpenClaw `/readyz`; MCP separately uses `/healthz`.
- CloudWatch shows the canonical gateway listening on loopback `18789`.
- Slack Socket Mode connects and an actual mention receives a reply.
- The Slack and Amazon Bedrock plugins are loaded; browser is not loaded.
- Bedrock requests use the task role.
- Task-definition hardening exactly matches section 4.

Local `--network none` tests prove plugin loading and gateway lifecycle but
cannot prove Slack DNS/authentication. Slack connected plus an actual mention
is therefore a production gate, not merely an informational check.

## 6. Effective MCP tool scope

The scope authority is
`infra/openclaw/effective-tool-scope.json`, checked against
`openclaw.config.json5` by tests. The maximum OpenClaw allowlist is 28 tools,
but the effective set is its intersection with tools registered by the current
MCP task.

Default Terraform enables these twelve:

```text
search, clientkarte, proposal_draft, proposal_review,
mail_summary, mail_followup, mail_to_internal_context,
mail_reply, mail_draft, morning_digest, oauth_connect, knowledge_deliver
```

This is **not read-only**:

- `mail_reply` and `mail_draft` create Gmail drafts but never send;
- `knowledge_deliver` reads Drive and delivers a file through Slack;
- optional `calendar_event` and `schedule_propose` write calendar/draft state;
- optional scrape/research/acquire tools can submit jobs and write reports/S3.

`video_approval`, `operation_log`, and `knowledge_search_url` are in the
OpenClaw allowlist but are not wired by the authoritative Terraform task and
therefore are disabled. Before release, compare live MCP `tools/list` with the
expected enabled entries from the JSON inventory; any extra or missing tool is
a deployment NO-GO.

## 7. Rollback

Rollback to the prior known-good task definition revision:

```sh
aws ecs update-service \
  --region ap-northeast-1 \
  --cluster teamagent-dev \
  --service teamagent-dev-openclaw \
  --task-definition <previous-known-good-arn>
aws ecs wait services-stable \
  --region ap-northeast-1 \
  --cluster teamagent-dev \
  --services teamagent-dev-openclaw
```

Stopping the pilot entirely remains available with `--desired-count 0`.

## 8. Explicit residual risks

- Fargate does not support Docker `no-new-privileges`; production does not
  claim it. Nonroot UID, all-capability drop, read-only rootfs, IAM, and network
  isolation are the compensating controls.
- Fargate does not support task `tmpfs`; `/tmp` is a writable, task-scoped
  empty volume. It is ephemeral, may contain transient state, and must not
  contain long-lived secrets.
- BuildKit provenance/SBOM attestations are parsed and subject-bound but are
  not cryptographic image signatures. ECR IAM and immutable digests remain in
  the trust boundary.
- Local offline tests cannot validate Slack connectivity or a real Bedrock
  invocation; both are production smoke gates.
