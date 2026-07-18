# OpenClaw production go-live checklist

The authority is `docs/openclaw/deploy_runbook.md`. A local manifest, adjacent
checksum, arbitrary S3 ZIP/metadata, direct ECR tag, or direct ECS update is
never a substitute for the trusted path.

## Shared trust and source

- [ ] The out-of-source trusted-release executable and contract are present,
      non-symlinks, independently reviewed, and byte-identical to the
      repository integration contract.
- [ ] A trusted publisher KMS-signs the exact Git commit archive/build context;
      `.git` absence and S3 metadata cannot bypass commit proof.
- [ ] The OpenClaw CodeBuild role has no ECR auth/write or promoter-assume
      permission, and the buildspec is Terraform-embedded.
- [ ] No operator-controlled environment override selects commit, repository,
      account, family, service, or promotion target.

## Local and registry evidence

- [ ] The clean final commit passes the schema-4 local ARM64 build.
- [ ] The local manifest says `deploymentCredential=false`,
      `LOCAL_GATES_PASSED`, and no registry/canonical publication.
- [ ] Exact material set passes; missing and extra executable/remote materials
      are rejected.
- [ ] The merged-rootfs inventory and CycloneDX SBOM are exactly equivalent
      for path/type/mode/UID/GID/size/link/content, and npm package multisets
      and `bom-ref`s are exact.
- [ ] Every evidence file is indexed, hashed, signed, and bound to the exact
      deploy subject.
- [ ] Runtime contract proves UID 65532, read-only rootfs, `/tmp` write only,
      cap-drop, secret fail-closed, exit-code propagation, PID 1, `/readyz`,
      and SIGTERM exit 0.
- [ ] Slack/Bedrock representative operations load and execute with provider
      calls stubbed and network disabled.
- [ ] Browser/Playwright executables and usable browser control are absent;
      retained browser-named/shared child-process payload is honestly recorded
      and its public facade fails closed.
- [ ] `jiti`, TypeScript/tsx compiler, Vite/Vitest, dev/test/type/source-map
      payload, and non-root package CLIs are absent.
- [ ] All 142 Control UI files pass on-disk and HTTP hash checks; transformed
      root HTML and authenticated/unauthenticated bootstrap config pass.
- [ ] Candidate is one exact `linux/arm64` manifest with C0/H0/S0 and none of
      the eight live findings:
      `12087`, `13221`, `33845`, `34182`, `42010`, `55200`, `57433`, `6100`.
- [ ] Canonical `git-$SHA` is created only after all gates, from the exact
      quarantined subject plus exact signed referrers; retry is immutable and
      idempotent.

## Deployment receipt and task

- [ ] A fresh KMS-signed one-time receipt binds exact image/source/builder,
      SBOM/provenance/signatures/referrers/evidence/scan, app S3 VersionId and
      all four app provenance anchors, current/previous task state, rendered
      payload, and deployment/canary intent.
- [ ] `--render-only` rejects an absent shared verifier and any stale,
      replayed, retargeted, or self-certified receipt.
- [ ] Rendered task has one OpenClaw container, one `/tmp` volume/mount, no
      sidecar, no `/data` or extra mount/volume, fixed roles/logs/secrets/env,
      ARM64 Fargate, read-only rootfs, UID 65532, nonprivileged, and cap-drop
      `ALL`.
- [ ] Review explicitly accepts that Fargate cannot enforce
      `no-new-privileges`; no production evidence claims otherwise.
- [ ] ECS circuit breaker and rollback are enabled.
- [ ] Receipt consumption is atomic immediately before service update and
      durably records the previous task ARN.

## Automatic live rollout gates

- [ ] ECS reaches `services-stable` on the exact new task revision.
- [ ] One-off canary task exits 0 on the exact revision and service network.
- [ ] Bedrock `Converse` succeeds using ECS task-role credentials; no static
      AWS key is present.
- [ ] MCP `tools/list` exactly equals the 12 default-enabled tools and remains
      within the reviewed 28-entry scope.
- [ ] Slack Socket Mode is connected and the exact canary mention receives the
      expected reply.
- [ ] The shared framework signs and durably records the rollout result.
- [ ] Startup alarm covers both `openclaw_config_invariant_violation` and
      `openclaw_entrypoint_error`; rollout gate failure alarm is active.
- [ ] A forced canary failure test has demonstrated automatic restoration of
      the durable previous task ARN.
- [ ] The separate adversarial RLS harness passes without outsider leakage.

Any unchecked item is production NO-GO. Merge also remains NO-GO until another
independent review approves the exact final commit and its evidence.
