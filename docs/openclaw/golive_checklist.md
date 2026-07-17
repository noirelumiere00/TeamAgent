# OpenClaw production go-live checklist

The detailed authority is [deploy_runbook.md](deploy_runbook.md). This
checklist must not be used to reintroduce the retired Terraform/Compose/ad-hoc
Buildx deployment paths.

## Release evidence

- [ ] Source commit is clean, reviewed, and represented by the exact versioned
      S3 source object metadata and SHA-256.
- [ ] Local ARM64 helper run passes with a schema-3 manifest. It correctly says
      `buildAttestations.registryPublished=false` and is not used to deploy.
- [ ] Dedicated `teamagent-<env>-openclaw-image-builder` CodeBuild run succeeds.
- [ ] Downloaded production manifest checksum passes.
- [ ] Registry attestation checks are all true: subject, source, and builder.
- [ ] Runtime contract, browser reachability, `jiti`/dev/test absence,
      Slack/Bedrock plugin inventory, Critical/High/Secrets zero, CycloneDX
      format, npm path/name/version multiset, and `bom-ref` integrity all pass.
- [ ] The exact ECR linux/arm64 child digest in `.image.runtimeRef` is the
      deployment subject.

## Infrastructure and task revision

- [ ] Normal Terraform configuration keeps `openclaw_image=""`; no OpenClaw
      task/service Terraform target is planned or applied.
- [ ] `apply_openclaw.sh --render-only` output was reviewed.
- [ ] Rendered OpenClaw container uses UID/GID 65532, read-only rootfs,
      `privileged=false`, capability drop `ALL`, writable `/tmp` empty volume,
      canonical image ENTRYPOINT/CMD, `/readyz`, and `stopTimeout=30`.
- [ ] IAM roles, four Secrets Manager bindings, CloudWatch logs,
      `AWS_REGION`, and `SLACK_DM_ALLOWLIST` are preserved.
- [ ] No plaintext secret exists in image env or task `environment`.
- [ ] Review explicitly accepts that Fargate cannot enforce
      `no-new-privileges`; no task definition or evidence claims otherwise.
- [ ] `bash infra/terraform/apply_openclaw.sh <production-manifest>` completes
      and ECS reaches `services-stable`.

## Live smoke

- [ ] ECS `/readyz` is healthy; MCP `/healthz` is separately healthy.
- [ ] CloudWatch shows gateway loopback listener without config repair,
      browser activation, or secret values.
- [ ] Slack Socket Mode is connected and a real mention receives a response.
- [ ] A real Bedrock request succeeds through the task role; no static AWS key
      is present.
- [ ] Slack and Amazon Bedrock plugins are loaded; browser is absent.
- [ ] MCP `tools/list` exactly equals the enabled intersection described by
      `infra/openclaw/effective-tool-scope.json`.
- [ ] Operators acknowledge the default set is not read-only: Gmail draft
      creation and Slack file delivery are enabled; all optional write/job
      gates match the approved release.
- [ ] Two-user thread isolation and the adversarial RLS harness pass with no
      outsider data leakage.

## Rollback readiness

- [ ] Previous known-good task definition ARN is recorded.
- [ ] Operator has the rollback command from the Runbook and can wait for
      `services-stable`.
- [ ] Emergency `desired-count 0` is understood to stop only the OpenClaw
      service.

Any unchecked release-evidence, task-isolation, Slack/Bedrock, tool-scope, or
RLS item is a production NO-GO.
