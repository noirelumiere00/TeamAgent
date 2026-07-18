# OpenClaw release boundary

This directory defines the reviewed TeamAgent OpenClaw runtime and its
fail-closed integration with the shared trusted-release framework. The only
supported runtime is OpenClaw `2026.7.1` on a single `linux/arm64` manifest,
rebuilt onto the digest-pinned distroless Node 24 base and run as
UID/GID `65532`.

## Runtime contract

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

`harden-task-definition.jq` accepts only the fixed production family, roles,
service provenance, one `openclaw` container, and one task-scoped
`openclaw-tmp` volume mounted writable at `/tmp`. It rejects sidecars,
additional volumes or mounts, writable `/data` or other paths, task/container
field additions, environment retargeting, role changes, and image repository
changes. The rendered definition enforces:

- `readonlyRootFilesystem=true`;
- `user=65532:65532`, `privileged=false`, capability drop `ALL`;
- canonical image ENTRYPOINT/CMD and `/readyz` health check;
- only the four fixed Secrets Manager bindings and reviewed environment keys.

The canonical CMD uses `execve`, so the gateway is PID 1. Actual-image tests
verify child exit-code propagation and clean SIGTERM exit 0.

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

The schema-4 output is explicitly `deploymentCredential=false` and
`promotion.status=LOCAL_GATES_PASSED`. It binds:

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

The S3 ZIP and its metadata are untrusted transport. The dedicated OpenClaw
CodeBuild role has no ECR authentication/write permission and cannot assume a
promotion role. Its Terraform-embedded buildspec requires the out-of-source
shared executable and byte-identical contract at:

```text
/opt/teamagent/trusted-release/bin/trusted-release
/opt/teamagent/trusted-release/contracts/teamagent-openclaw-production-v1.json
```

Before any repository script executes, that framework must verify a
KMS-signed trusted publisher statement bound to the exact commit archive and
build context. After all local tests and scans pass, only the separate trusted
promoter may publish a quarantine subject and atomically promote that exact
subject plus the exact signed referrer set to immutable `git-$SHA`.
Canonical tags must not exist before gates pass; approved retry is idempotent
and must not delete an existing release.

The shared worker is owned outside this OpenClaw change. Until it provides the
required executable and matching contract, CodeBuild, render, and deploy all
fail closed. That is intentional.

## Deployment and rollout

`apply_openclaw.sh` accepts only a fresh KMS-signed one-time deployment
receipt. It does not accept a build manifest, adjacent checksum, builder
booleans, or environment overrides for account/repository/family/service.
The receipt binds the exact image, source archive, builder, signatures,
referrers, whole-filesystem SBOM, evidence index, C0/H0/S0 scan status, eight
live-CVE absences, Connect Web S3 VersionId and four app provenance anchors,
current/previous task ARN and hash, rendered registration payload, and rollout
intent.

Immediately before `update-service`, the helper rechecks the service task ARN
and asks the shared framework to atomically consume the receipt while
durably recording the previous task ARN. ECS deployment circuit breaker and
rollback are enabled. After `services-stable`, an isolated one-off task and
Slack canary must prove:

- task-role Bedrock `Converse`;
- exact MCP `tools/list` and reviewed tool scope;
- Slack connection and exact mention/reply behavior.

Any update, stability, canary, or durable-result failure emits the rollout
failure metric and restores the recorded previous task definition.

The effective MCP authority is `effective-tool-scope.json`. The default
Terraform task currently exposes 12 tools; the reviewed OpenClaw include list
contains 28. This scope is not read-only: Gmail draft and Slack file-delivery
operations are among the default tools.

The four required runtime secrets are `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`OPENCLAW_GATEWAY_TOKEN`, and `TEAMAGENT_MCP_BEARER`. Bedrock uses only the ECS
task role; static AWS credentials are forbidden.

See `docs/openclaw/deploy_runbook.md`. Production remains NO-GO until the
shared trusted framework is present, an independent review approves the final
commit, and real CodeBuild/ECR/Fargate/Slack/Bedrock/tools-list gates pass.
