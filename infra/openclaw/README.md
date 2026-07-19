# OpenClaw release boundary

This directory defines the reviewed TeamAgent OpenClaw core runtime and its
fail-closed integration with the repository's canonical provenance and
one-time Terraform release gate. The only locally supported runtime is
OpenClaw `2026.7.1` on a single `linux/arm64` manifest, rebuilt onto the
digest-pinned distroless Node 24 base and run as UID/GID `65532`.

The production bundle contract remains `release.ready=false`. The separate
media subject, exact two-subject receipt emitter, signed final-HEAD registry
evidence, and guarded post-apply functional rollback integration are not
complete. A local PASS is therefore not production authorization.

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

The Terraform task definition is authoritative. `harden-task-definition.jq`
is its adversarially tested offline mirror: it accepts only the fixed
production family, roles, service provenance, one `openclaw` container, and
one task-scoped
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

The schema-4 output is explicitly `deploymentCredential=false` and
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
3. a fixed release repository `@sha256` plus exact
   `image_release_evidence.openclaw`;
4. one full saved plan created by `plan_image_release.sh`;
5. one-time application of that exact plan by
   `apply_image_release_plan.sh`.

The planner and apply supervisor bind clean exact `origin/dev`, backend,
workspace, state lineage/serial and ownership, contract hash, complete signed
release graph, one-time intent, and receipt claims. A started plan is never
retried; an ambiguous failure requires reconciliation and fresh authorization.
See `infra/terraform/README.md`.

After apply, production GO additionally requires automated checks for exact
ECS revision stability, an isolated one-off task, task-role Bedrock
`Converse`, exact MCP tool inventory, Slack connection/mention/reply, signed
durable result, and automatic rollback to the durable previous task.
`run-live-rollout-gates.mjs` contains the validation logic but is not yet
wired to the one-time Terraform flow. The release contract stays closed until
that integration is independently reviewed.

The effective MCP authority is `effective-tool-scope.json`. The default
Terraform task currently exposes 12 tools; the reviewed OpenClaw include list
contains 28. This scope is not read-only: Gmail draft and Slack file-delivery
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
media/bundle and post-apply rollback integrations are complete, an independent
review approves the final commit, and real signed
CodeBuild/ECR/Fargate/Slack/Bedrock/tools-list evidence passes.
