# Runbook: config migration（画像を変えない構成変更を guard の正規経路で通す）

対象: `infra/deploy/terraform_runtime_guard.sh`（GUARD_VERSION 26 以降）の
`--runtime-migration <ID>` で **kind: `"config"`** の migration を使う手順。

## 1. 何を通す経路か

| 変更の種類 | sync | migration(runtime / activation) | **migration(config)** |
|---|---|---|---|
| 画像 digest の差し替え | 不可（live と完全一致のみ） | 可（Wolfi cutover 固定契約） | **不可**（from == to == live を三重で強制） |
| EventBridge rule の ENABLE/DISABLE | 不可 | activation のみ | **不可**（no-op 要求） |
| 新規リソースの create（KMS 鍵など） | HMAC gate の `terraform_data` だけ | HMAC gate の `terraform_data` だけ | **manifest の `to.allowed_resource_changes` に列挙した address だけ** |
| IAM policy / その他リソースの update | 固定 allowlist（TD/service/rule/target/dispatcher/ESM）だけ | 同左 | **固定 allowlist + `to.allowed_resource_changes`** |
| task definition の env 差分 | 不可（完全一致） | guard 内の固定 allowlist（HOME 等） | **`to.allowed_env_changes[<component>]` のキーだけ**（secrets は不変） |
| 外部 receipt | 無し | preflight + alarm-delivery + versioning + log-readiness + alarm-migration | **preflight だけ**（他 4 種は指定すると die） |
| preflight の中身 | — | 本番 cluster で Fargate task を起動 + Cosign/Rekor | **task 起動なし**。live fingerprint と contract SHA を receipt に焼くだけ |

典型例: proposal_builder 点灯（`aws_kms_key` / `aws_kms_alias` の create、`aws_iam_role_policy.mcp_task`
の statement 追加、`aws_ecs_task_definition.mcp` の env 追加 → `aws_ecs_service.mcp[0]` の task_definition 更新）。

たとえ話: sync は「積荷と書類が倉庫台帳と 1 文字も違わないことを確認する通関」、runtime migration は
「積荷（画像）を差し替えるための様式」。config は「積荷はそのまま、倉庫の鍵と入館証だけを変える様式」で、
変えてよい鍵と入館証の一覧（allowlist）を書類（manifest）側に持つ。

## 2. manifest の雛形（`infra/deploy/terraform_runtime_migrations.json`）

`from` の値はすべて **live の実測値**（read-only の describe/get で採取）。`to.images` / `to.rule_states` は
`from` と同値でなければ schema で die する。placeholder の採り方は本節末尾の表を参照。

```jsonc
"2026-09-<name>-v1": {
  "kind": "config",
  "enabled": false,                       // review-plan 前は false / reviewed_plan は null
  "expires_at": "2026-10-15T00:00:00Z",   // ISO8601・now より未来
  "requires_migration": null,
  "description": "...",
  "from": {
    "task_definition_arns": { "openclaw": "...:task-definition/teamagent-dev-openclaw:<rev>", "mcp": "...", "connect_web": "...", "ingest": "...", "morning": "...:teamagent-dev-morning-digest:<rev>", "canary": "...", "x_buzz": "...:teamagent-dev-x-buzz-worker:<rev>", "tiktok": "...:teamagent-dev-tiktok-acquire:<rev>" },
    "images": { "openclaw": "<teamagent-openclaw@sha256:…>", "mcp": "<teamagent-mcp@sha256:…>", "connect_web": "<mcp と同じ repo>", "ingest": "…", "morning": "…", "canary": "…", "x_buzz": "…", "tiktok": "<teamagent-media-worker@sha256:…>" },
    "rule_states": { "ingest": "ENABLED", "morning": "ENABLED", "canary": "DISABLED" },
    "dispatcher_code_sha256": { "tiktok": "<CodeSha256>", "x_buzz": "<CodeSha256>" },
    "monitoring": { "container_insights": "disabled" }
  },
  "to": {
    "images": "<from.images と同一>",
    "rule_states": "<from.rule_states と同一>",
    "allowed_resource_changes": [
      "aws_kms_key.proposal_builder_assets",
      "aws_kms_alias.proposal_builder_assets",
      "aws_iam_role_policy.mcp_task",
      "aws_ecs_task_definition.mcp",
      "aws_ecs_service.mcp[0]"
    ],
    "allowed_env_changes": { "mcp": ["USE_PROPOSAL_BUILDER_TOOLS", "..."] }
  },
  "required_preflight_profiles": [],
  "reviewed_inputs": { "image_deployment_intent_id": "<uuidgen | tr 'A-F' 'a-f'>" },
  "reviewed_plan": null
}
```

schema の要点（`migration_to_file`）:

- `from` のキーは `task_definition_arns`(8) / `images`(8) / `rule_states`(3) / `dispatcher_code_sha256`(2) / `monitoring` の 5 つちょうど。
- `to` のキーは `images` / `rule_states` / `allowed_resource_changes` / `allowed_env_changes` の 4 つちょうど。
- `tiktok` の画像は `teamagent-media-worker` 固定（media cutover 後専用。legacy `teamagent-dev-tiktok-acquire` は受理しない）。
- `allowed_resource_changes` は非空・重複なしの terraform address。`terraform_data.` / `data.` で始まる address は書けない。
- `allowed_env_changes` のキーは component id（`openclaw|mcp|connect_web|ingest|morning|canary|tiktok|x_buzz`）、値は `^[A-Z][A-Z0-9_]*$` の配列。
- `required_preflight_profiles` は `[]` 固定。`requires_migration` は `null`。

placeholder の採り方（すべて read-only）:

| placeholder | コマンド |
|---|---|
| service TD（mcp / connect_web / openclaw） | `aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-mcp teamagent-dev-connect-web teamagent-dev-openclaw --query 'services[].taskDefinition'` |
| morning / canary TD | `aws events list-targets-by-rule --rule teamagent-dev-morning-digest-weekday --query 'Targets[].EcsParameters.TaskDefinitionArn'`（canary は `teamagent-dev-canary-hourly`） |
| ingest / tiktok / x_buzz TD | `aws lambda get-function-configuration --function-name teamagent-dev-<ingest\|tiktok-acquire\|x-buzz>-dispatch --query Environment.Variables.TASKDEF_ARN` |
| image digest | `aws ecs describe-task-definition --task-definition <arn> --query 'taskDefinition.containerDefinitions[0].image'` |
| dispatcher CodeSha256 | `aws lambda get-function-configuration --function-name … --query CodeSha256` |
| rule state | `aws events describe-rule --name … --query State` |
| container_insights | `aws ecs describe-clusters --clusters teamagent-dev --include SETTINGS --query 'clusters[0].settings'` |
| intent id | `uuidgen \| tr 'A-F' 'a-f'`（UUIDv4） |
| reviewed_plan | §3 の `review-plan` の `--out` 実体 |

## 3. コマンド列（同一 commit・clean tree・AIIAdev の MFA 一時 session）

```bash
# 0) live の実測（read-only）→ manifest candidate を commit / merge
bash infra/deploy/bootstrap_runtime_session.sh snapshot --evidence-json-out /secure/snap.json

# 1) preflight（Fargate task なし。live fingerprint と contract SHA を receipt に焼く）
bash infra/deploy/bootstrap_runtime_session.sh preflight \
  --migration <ID> --out /secure/preflight.json

# 2) review-plan（外部 receipt は --preflight-receipt だけ）
bash infra/deploy/bootstrap_runtime_session.sh review-plan \
  --var-file <tfvars> --out /secure/reviewed-plan.json \
  --runtime-migration <ID> --preflight-receipt /secure/preflight.json

# 3) reviewed-plan.json を manifest の reviewed_plan に貼り enabled=true にする commit（migrations.json だけ）→ merge

# 4) plan → verify → apply（sync / runtime と同じ）
bash infra/deploy/bootstrap_runtime_session.sh plan \
  --var-file <tfvars> --out /secure/plan.tfplan \
  --runtime-migration <ID> --preflight-receipt /secure/preflight.json
bash infra/deploy/bootstrap_runtime_session.sh verify --plan /secure/plan.tfplan
bash infra/deploy/bootstrap_runtime_session.sh apply --plan /secure/plan.tfplan --out /secure/apply-receipt.json
```

`--alarm-delivery-receipt` / `--versioning-receipt` / `--log-readiness-receipt` / `--alarm-migration-receipt` /
`--prior-apply-receipt` を config kind に渡すと die する。

## 4. 時間制約と順序

| 制約 | 値 | 根拠 |
|---|---|---|
| preflight receipt の有効期限 | **7200 s** | `write_preflight_receipt` / `verify_preflight_receipt` |
| plan receipt の有効期限（plan → apply） | **3600 s** | plan の `EXPIRES` / `verify_receipt` |
| review-plan → plan の間に変えてよい file | **`infra/deploy/terraform_runtime_migrations.json` だけ** | `assert_review_commit_transition` |
| receipt が束縛するもの | git commit・guard/jq/manifest SHA・全 `*.tf` の config manifest SHA・live fingerprint | `write_preflight_receipt` / plan receipt |

したがって 1) → 4) を **2 時間以内・同一 tree** で通す。3) の commit に migrations.json 以外を混ぜると 2) の receipt は無効になり、
preflight からやり直しになる。guard 本体・`*.tf` を触った場合も同様（SHA が変わる）。

## 5. config kind が通さないもの（設計上の制限）

- **画像の変更**: manifest（from == to）・live（`validate_migration_source`）・core（desired == live）・
  plan（TD image == desired）・`runtime_guard.tf`（to.images == desired == live）の全段で拒否。
- **rule / dispatcher / event source mapping の変更**: sync と同じ no-op 要求。
- **secrets の変更**: `allowed_env_changes` は env だけ。secrets は完全一致。
- **replace（create/delete）の拡張**: `allowed_resource_changes` は create と update にだけ効く。replace を許すのは
  guard 固定の `allowed_replacements`（8 TD と HMAC gate）だけ。
- **drift の取り込み**: `allowed_resource_changes` は drift の allowlist には足さない。drift があれば sync と同じ文面で die。
- **新規 KMS 鍵の ARN を同一 plan 内の IAM policy が参照する変更**: policy 文字列が unknown になり
  `validate_exact_runtime_iam_plan` が die する。鍵の create（段 ①）と、その ARN を使う IAM/env（段 ③）は
  **別便の config migration** に分ける（`proposal_builder_assets.tf` ヘッダの 3 段運用と同じ）。

## 6. tfvars 側で必要なもの

- config kind は sync と違い **HMAC deployed 世代（`*_hmac_deployed_*`）を live から導出しない**。
  runtime/activation kind と同じく tfvars の値を使う（`hmac_keyrings.tf` の `*_config_ready` が要求する項目一式）。
- `image_deployment_consumer_manifest` / `image_release_receipt_catalog` / `image_release_consumer_receipt_bindings` は
  guard が live state から生成して `-var-file` で注入する（`config-derived.tfvars.json`）。tfvars に書かない。
