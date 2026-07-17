# OpenClaw release runtime

This directory is the release boundary for the TeamAgent OpenClaw gateway.
The supported image is OpenClaw `2026.7.1` on linux/arm64, rebuilt onto the
digest-pinned distroless Node 24 base and run as UID/GID `65532`.

## Runtime and Fargate contract

The final image contains no shell, package manager, browser executable,
Playwright, Codex CLI, `jiti`, TypeScript/tsx, Vite/Vitest, build compiler, or
test/fixture/type/source-map payload. `prune-runtime.mjs` resolves the actual
production dependency closure and performs a static ESM reachability analysis
from `dist/entry.js` and `dist/index.js`. Unreachable browser registration and
CLI chunks are deleted independent of their generated hash names.

Some browser-named SDK/doctor helper chunks remain because non-browser core
code statically imports them. The retained
`/opt/teamagent/runtime-prune-report.json` records those reachable shared
chunks and proves that:

- the browser plugin registration, CLI registration, executable browser
  extension, and fast-path browser help payload are absent;
- no unreachable browser implementation candidate remains;
- the only `dist/extensions/browser/runtime-api.js` text is data in the
  updater's reviewed sidecar-path inventory, not an import or registration.

Production ECS revisions are rendered by
`infra/openclaw/harden-task-definition.jq`. They enforce:

- `readonlyRootFilesystem=true`;
- `user=65532:65532`, `privileged=false`, and Linux capability drop `ALL`;
- one writable task-scoped empty volume mounted only at `/tmp`;
- the image's canonical ENTRYPOINT/CMD and `/readyz` health check.

The canonical CMD enters `gateway-runtime.mjs`, disables the optional packaged
Node compile-cache respawn, and then `execve`s the official OpenClaw launcher.
This keeps the gateway as PID 1. It avoids the upstream respawn supervisor's
one-second signal escalation turning a clean but slightly slower SIGTERM
shutdown into exit 1; the build gate also rejects a gateway PID 1 that still
has a supervised child process.

AWS Fargate does not accept Docker `no-new-privileges` security options or
`linuxParameters.tmpfs`. Therefore production does **not** claim to enforce
`no-new-privileges`; local Docker contract tests use it as an additional
defense only. The residual risk is recorded in the release manifest. The
writable `/tmp` volume is ephemeral and is lost with the task.

## Build, evidence, and publication

`infra/openclaw/build-image.sh` is the only build/verification implementation.
It refuses a dirty Git tree, binds the image labels and BuildKit provenance to
the exact commit/source archive, builds linux/arm64, and runs the real image
under the isolation contract.

Local verification does not publish:

```sh
COMMIT=$(git rev-parse HEAD)
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:git-${COMMIT:0:12}" \
  --manifest "/tmp/openclaw-${COMMIT:0:12}-manifest.json" \
  --evidence-dir "/tmp/openclaw-${COMMIT:0:12}-evidence"
```

The helper requires the Docker Buildx and Trivy versions pinned in
`plugins-lock.json`, plus `jq`, Python 3, and `sha256sum`. Its schema-3 manifest
covers:

- actual ARM64 image config, nonroot UID/GID, read-only writes, writable
  `/tmp`, capability bounds, secret fail-closed behavior, child exit code,
  `/readyz`, SIGTERM, Slack and Bedrock plugin loading, and browser CLI absence;
- the static browser reachability report and physical absence of `jiti`,
  forbidden tooling, tests, fixtures, source types/maps, and non-root package
  bin declarations;
- Trivy Critical=0, High=0, Secrets=0;
- CycloneDX format derived from the actual document, unique/dangling
  `bom-ref` checks, and exact path/name/version **multiset** equality between
  every physical npm package instance and every npm SBOM component.

The manifest and evidence directory must be siblings. Evidence paths include
the evidence-directory basename and are relative to the manifest's parent, so
the CodeBuild ZIP remains self-contained and every recorded SHA-256 can be
rechecked after extraction.

Production publication is only through the dedicated
`aws_codebuild_project.openclaw_image`, which uses
`infra/codebuild/buildspec.openclaw.yml`. Its BuildKit attestations are accepted
only after all of these are parsed back from the registry and validated:

- the attestation manifest refers to the exact linux/arm64 child digest;
- source repository, commit, branch, source-archive SHA-256, artifact version,
  Dockerfile frontend, base/upstream/plugin material digests all match;
- the SLSA builder ID equals the current CodeBuild build URI;
- the registry SPDX payload is present and non-empty.

BuildKit attestations are not a cryptographic image signature. ECR IAM,
versioned S3 source/evidence, immutable digest selection, and parsed attestation
validation are the current trust boundary; signature verification remains a
documented residual supply-chain risk.

## Deployment and tool scope

`infra/terraform/apply_openclaw.sh <release-manifest.json>` is the only
supported deployment path for the existing service. It rejects local/unpushed
manifests and re-renders the current ECS task definition through the hardening
filter before registering a revision. OpenClaw remains CLI-managed; keep
Terraform `openclaw_image` empty during normal operation.

`docker-compose.yml` is decommissioned and contains no services.

The effective MCP scope is not “four read-only tools.” Its reviewed,
machine-checkable inventory is `effective-tool-scope.json`. A tool is callable
only if it is both in OpenClaw's include list and registered by the deployed
MCP task. The default Terraform task enables twelve entries, including Gmail
draft writes and Slack file delivery; optional gates can expose calendar
writes, external scrape/analysis, job submission, and report/S3 writes.

All four runtime secrets are required and injected through ECS:

- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `OPENCLAW_GATEWAY_TOKEN`
- `TEAMAGENT_MCP_BEARER`

Bedrock uses only the ECS task role and `AWS_REGION`; static AWS access keys
must never be injected.
