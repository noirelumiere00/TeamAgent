# OpenClaw production go-live checklist

正準手順は `docs/openclaw/deploy_runbook.md` と
`infra/terraform/README.md` です。未チェック項目が1つでもあれば production
は NO-GO です。

## Source and contract

- [ ] clean exact `origin/dev` の final commit/tree を独立レビュー済み
- [ ] `openclaw_bundle_contract.json` の変更を独立レビュー済み
- [ ] `release.ready=true` は core/media 実装と実像証跡の後に別変更で承認済み
- [ ] publisher source manifest が full commit、tree、commit object、
      executable inventory、contract hash を束縛
- [ ] source manifest/signature の exact S3 VersionId、KMS verify、
      COMPLIANCE retention を確認
- [ ] OpenClaw CodeBuild role は quarantine 以外へ image writeできず、
      source evidence を署名・上書きできない

## Local runtime evidence

- [ ] final HEAD の `build-image.sh` が schema-4 manifest を生成
- [ ] manifest の commit/tree/archive/contract hash が final HEAD と一致
- [ ] `deploymentCredential=false`、registry/promotion fields はすべて false
- [ ] exact material set と evidence index が一致
- [ ] whole-filesystem SBOM と rootfs inventory が完全一致
- [ ] physical npm package instance と `bom-ref` が完全一致
- [ ] runtime は UID 65532、read-only rootfs、`/tmp`のみwrite、
      cap-drop、PID 1、`/readyz`、SIGTERM exit 0
- [ ] browser executable/Playwright/CDP control path は不在
- [ ] Control UI 全assetのdisk/HTTP hash、認証あり/なしbootstrapを検証
- [ ] single `linux/arm64` subject が Critical=0、High=0、Secrets=0
- [ ] live既知8 CVE が候補に存在しない

## Registry and release

- [ ] core と media の2 subject が実装され、exact bundle receiptを生成
- [ ] quarantine tag は full 40-character commit とsubject名に固定
- [ ] attestor が exact digest、OCI labels、binary probes、Trivy、SPDX、
      in-toto、signatures、referrer setを検証
- [ ] promoterだけが candidate/release repositoryへ immutable copy
- [ ] active/rollback receipt の key、VersionId、signature VersionId、expiry、
      release digestを照合

## Terraform deployment

- [ ] `openclaw_image` は固定 release repository の `@sha256`
- [ ] `image_release_evidence.openclaw` は exact immutable VersionId
- [ ] `plan_image_release.sh` が worktree外に full saved planを作成
- [ ] planに `-target`、import、destroy、refresh-only、未知値、意図しない
      IAM/SG/schedule/image差分がない
- [ ] same saved planを `apply_image_release_plan.sh` で一度だけapply
- [ ] one-time intent と receipt claims が atomicに消費された
- [ ] taskは ARM64、UID 65532、read-only rootfs、`/tmp`のみwrite、
      cap-drop ALL、canonical ENTRYPOINT/CMD、`/readyz`
- [ ] ECS circuit breaker/rollbackが有効

## Live functional gates

- [ ] exact new revisionで `services-stable`
- [ ] same revision/networkのone-off task exit 0
- [ ] task-role credentialで Bedrock `Converse` 成功
- [ ] MCP `tools/list` が既定12件かつreviewed 28件内
- [ ] Slack Socket Modeとexact mention/reply成功
- [ ] rollout結果を署名・耐久記録
- [ ] 強制失敗で durable previous revisionへの自動復旧を実証
- [ ] startup failure、rollback、task exit、Slack/Bedrock/MCP alarmを確認

## Current status

- [x] hardened core runtime source is integrated locally
- [x] canonical provenance/Terraform path is fail closed
- [ ] media subject and exact two-subject receipt emitter
- [ ] guarded post-apply functional rollback integration
- [ ] signed final-HEAD registry and live production evidence

したがって現在の判定は **production NO-GO** です。
