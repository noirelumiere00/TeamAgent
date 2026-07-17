# OpenClaw release runtime

This directory defines the isolated TeamAgent OpenClaw gateway runtime. The
release contract is intentionally narrow:

- OpenClaw `2026.7.1`, pinned to the reviewed linux/arm64 child digest.
- Distroless Node 24 final image, running as UID/GID `65532`.
- Read-only root filesystem, all capabilities dropped, and
  `no-new-privileges`.
- Only the pinned external Slack and Amazon Bedrock plugins are enabled.
- The browser plugin, Playwright, Codex CLI, Vite, Vitest, TypeScript/tsx, and
  other enumerated compiler/test packages are absent as physical packages and
  declarations. Dangling symlinks are forbidden.
- Runtime secrets are injected only at start and are never build arguments or
  image environment values.

Scope note: the upstream precompiled core retains browser-named shared chunks
that are statically imported by non-browser config, sandbox, session, and
doctor code. Removing those chunks makes the OpenClaw CLI fail at module load.
They are not the browser plugin and contain no Playwright package declaration;
the executable browser capability is removed by deleting
`dist/extensions/browser`, its plugin manifest/package, Playwright, and all
related package/bin entries.

## Supported build and verification path

`infra/openclaw/build-image.sh` is the only supported OpenClaw image build
entrypoint. It refuses a dirty tree, binds the image to the exact current Git
HEAD, builds linux/arm64, runs the runtime contract, creates a Trivy CycloneDX
SBOM, performs vulnerability and secret scans, and writes hash-linked evidence.

Local verification does not push:

```sh
COMMIT=$(git rev-parse HEAD)
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:git-${COMMIT:0:12}" \
  --manifest "/tmp/openclaw-${COMMIT:0:12}-manifest.json" \
  --evidence-dir "/tmp/openclaw-${COMMIT:0:12}-evidence"
```

The helper requires Docker Buildx, `jq`, `sha256sum`, and the exact Trivy
version pinned in `plugins-lock.json`. A successful run verifies:

- linux/arm64 image metadata and UID/GID `65532`;
- read-only rootfs, `cap-drop=ALL`, `no-new-privileges`, and network isolation;
- required-secret fail-closed behavior and child exit-code propagation;
- Slack and Bedrock plugin loading with no browser plugin;
- `/readyz`, graceful SIGTERM shutdown, and zero secret leakage in logs;
- zero forbidden browser-plugin/Playwright/compiler/dev package or plugin
  artifacts and zero dangling symlinks;
- Trivy Critical=0, High=0, Secrets=0;
- exact equality between physical npm package inventory and SBOM npm
  components, plus matching config digest and rootfs DiffIDs across SBOM and
  scan evidence.

Production publication may use the helper's explicit `--push` mode only from
the dedicated provenance-bound build job. Ad hoc Buildx, standalone image, and
Compose publication paths are unsupported.

## Runtime secrets

All four values are required:

- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `OPENCLAW_GATEWAY_TOKEN`
- `TEAMAGENT_MCP_BEARER`

Bedrock uses the task role and `AWS_REGION=ap-northeast-1`; static AWS access
keys must not be injected. `SLACK_DM_ALLOWLIST` is also required by deployment
policy and is validated by the entrypoint.

## Deployment boundary

`docker-compose.yml` is formally decommissioned and contains no services. It
must not be used to bypass the release helper. Production consumes the
verified image digest from the helper manifest through the ECS deployment
runbook.

OpenClaw remains outside the sales-data trust boundary: it receives no
RDS/Secrets/KMS permission or network reachability. Per-user authorization,
RLS, and user OAuth remain enforced by `teamagent-mcp` over the private,
bearer-authenticated streamable HTTP boundary.
