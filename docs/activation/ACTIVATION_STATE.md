# ACTIVATION STATE

## MISSION（2026-08-25 設定）

**GOAL: `HERMES DARK RUNTIME DEPLOYED`**

DONE 条件（全て GREEN であること）:

- Production Activation 完了 / 正式 Activation Baseline 確定
- Hermes supply-chain 構築 / upstream・image・digest・provenance 固定
- Hermes runtime を AWS へデプロイ / health check GREEN
- isolation GREEN / rollback readiness GREEN
- **production user traffic は Hermes に流れていない**
- 既存 OpenClaw / MCP / production workloads に regression なし
- evidence 保全済み

```
A0.2.2c → fresh preflight → pure 4 forget + 4 import 判定
  → production adopt plan/apply → post-apply verification
  → ACTIVATION COMPLETE → 正式 Activation Baseline 確定
  → PR2-A1 Hermes supply-chain → PR2-B Hermes dark runtime
  → HERMES DARK RUNTIME DEPLOYED
```

### 自律実行してよい
repo/git/read-only AWS 調査・terraform plan の準備・実装・tests・mutation/contract tests・
CI 修正・PR 作成/rebase/retarget・evidence の redacted summary・manifest/checksum/patch-id/
blob identity 検証・release-chain integrity・live vs desired 比較・runbook と本ファイルの更新。
既承認の設計原則は再確認しない。

### Human Gate 必須（14）
①production AWS mutation ②terraform apply ③state mutation ④IAM apply / scope 変更
⑤Freeze 変更・解除 ⑥secret/credential 権限の追加拡大 ⑦approved scope 拡張
⑧想定外の production drift ⑨invariant violation ⑩production user traffic 切替
⑪Hermes runtime 初回 production deployment ⑫rollback が必要な状態
⑬root credential/account 操作 ⑭signed approval/provenance 不足

### SURPRISE ポリシー
approved scope 内で安全に直せるものは自律修正して続行（test fixture 不整合・自変更由来の
CI 失敗・path/local 名の誤り・read-only checker のバグ・patch-id/blob 検査の実装ミス）。
即 STOP: production drift / 想定外の AWS mutation / IAM scope 拡張が必要 / state mismatch /
evidence・provenance 不成立 / security boundary 変更が必要 / Freeze が効いていない /
root mutation / approved desired と live の不一致。

### GO の束縛
GO はその Gate の exact action のみ。plan SHA / git HEAD / mapping SHA / state serial /
state SHA / target list / resource scope / IAM actions / Freeze state のいずれかが変われば
承認失効 → 再度 Gate を提示する。

### Hermes 原則
`Slack → OpenClaw/Router → Hermes specialist → Claude → MCP`。既存の identity.py /
MCP Gateway / RLS / per-user OAuth / HITL write boundary / OpenClaw security restrictions を
再利用し、tools/adapters を作り直さない。初回は 1 specialist・dark runtime・
production user traffic 0・minimal permissions・explicit network/tool boundary・rollback 可能。
mega-agent にしない。Hermes Slack Pilot は別 MISSION。

### Hermes supply-chain（PR2-A1 の既定 upstream）
| 項目 | 値 |
|---|---|
| tag | `v2026.8.18` / release `v0.20.4` |
| commit | `e624e9fde561e1add9388384012b295fde669ade` |
| image | `docker.io/nousresearch/hermes-agent` |
| index digest | `sha256:22e37bb4ed1b0f50cb6bd991dca7ecacd6c9f29df9b4a20fc989d32bc763ccf6` |
| arm64 digest | `sha256:dd9d587caa787a8c287fc86e8a52537caaeeb16a6ebd3e0f6845c0ab34f90a50` |

方針: `pinned upstream → thin derived image → TeamAgent supply-chain → attest/promote → runtime`。
digest / provenance / source commit を evidence 化する。**mutable tag を runtime source にしない。**

PR2-A0.x activation の**現在地だけ**を持つ単一の記録媒体。会話ログではなくこのファイルが真実源。
経緯・原則・承認済み事項はここを更新し、チャット本文へ再掲しない（2026-08-24 運用裁定）。

更新規約: フェーズが進むたびに Claude が更新する。過去の詳細は git 履歴で辿る。

---

## Current phase

**fresh preflight adopt-plan — guard の ingest consumer モデル不整合で停止中（2026-08-25）**

直前の完了:

1. State Rebind #2 完了（5 target・state == live を 5/5 実測）
2. A0.2.2a / A0.2.2b IAM apply（saved plan `1e45a741…`）
3. A0.2.2c IAM apply（saved plan `25ff4f88…`・serial 212 → 213）

## Current execution HEAD

| 項目 | 値 |
|---|---|
| branch | `activation-execution-base` |
| HEAD | `868fcb7`（rebind #2 mapping を 5 target へ確定） |
| dev HEAD | `e48cf2e`（PR #324 = A0.2.2c merge 済み） |

`0eab7f2` は **Activation Candidate / Execution Base**。正式 Activation Baseline は
preflight が純粋 4 forget + 4 import を出した時点で確定する（**未確定**）。

## Freeze status

| freeze | state | 境界 / 備考 |
|---|---|---|
| Generation publisher | **ACTIVE** | `2026-08-24T08:24:04Z`（deny を確認した時刻。最後の変更時刻ではない） |
| Production deployment | **ACTIVE** | 同上の enforcement で機械化 |
| dev merge | operational_only | 安全性は dev tip の不変性に依存させない |

**Freeze v2 の定義**（≠ absolute AWS immutability）:
enumerated non-root deployment principals への mechanical deny
+ root は break-glass residual risk（freeze 中は使用禁止・CloudTrail で監視）

- policy: `arn:aws:iam::718959508629:policy/teamagent-dev-activation-freeze`
- attach: user `AIIAdev` + role 9（計 10 principals・10×12 action が explicitDeny 実測）
- tfstate への `s3:PutObject` は allowed のまま（state 操作を温存）
- desired-state binding: `activation_freeze.json.state == "active"` → guard が
  `-var=activation_freeze_enabled=true` を注入。plan 側でも freeze リソースの
  delete/replace を拒否（入力と出力の二重化）

## State serial

| 時点 | serial |
|---|---|
| rebind #1 完了 | 200 |
| freeze policy apply 後 | 201 |
| A0.2.2a/b IAM apply 後 | 212 |
| A0.2.2c IAM apply 後 | **213**（現在値・lineage `745cf6df…` 不変・tf 1.12.2） |

### Rebind #2 の結果（state == live を 5/5 実測・2026-08-25）

| address | state | live | 一致 |
|---|---|---|---|
| `aws_ecs_task_definition.mcp` | `:88` | service `:88` | ✅ |
| `aws_ecs_task_definition.connect_web` | `:73` | service `:73` | ✅ |
| `aws_ecs_task_definition.morning_digest` | `:55` | rule ECS target `:55` | ✅ |
| `aws_ecs_task_definition.canary` | `:25` | rule ECS target `:25` | ✅ |
| `aws_ecs_task_definition.ingest` | `:57` | dispatch Lambda `TASKDEF_ARN` `:57` | ✅ |

**B3（ECS state drift）= 0。** rebind #2 は成立して保持されている。

## Approved commits

### payload（execution line 上）

現在: base `0eab7f2` + 2 commit（P0 session policy / A0.3.2 snapshot injection）
予定: **base + 10 commit**（下記 8 件を追加）

| # | source PR | dev 側 SHA | 内容 |
|---|---|---|---|
| 1 | #315 | `5f539a2` | freeze を機械強制へ（repo lock） |
| 2 | #315 | `639a1b7` | force push 検出の変異対 |
| 3 | #316 | `43af628` | A0.2.2a non-secret read |
| 4 | #318 | `45971f1` | A0.2.2b secret read |
| 5 | #319 | `8777769` | Freeze v2 policy（AWS deny） |
| 6 | #320 | `46e6d0f` | Freeze v2 定義 + root 監視 |
| 7 | #321 | `8a43116` | Freeze v2 ACTIVE 化 + 境界記録 |
| 8 | #322 | （merge 後に確定） | desired-state binding P0 |

取り込みは patch-id と touched blob の**二点照合**で同一性を機械確認する。

### control-plane metadata（execution line に入らない）

`infra/deploy/activation_execution_allowlist.json` は **dev 側が authoritative**。
allowlist を更新する commit は payload に数えない（自己参照を避けるため）。

⚠️ #315 の patch は allowlist の**コピー**を execution line にも持ち込む。これは stale な
inert コピーなので、検証は必ず dev 側から実行する（self-reference ガードを追加予定）。

## Pending gates

1. **guard ingest activator 修正 PR の merge GO?** ← 次の停止点（下記「発見」参照）
2. Gate 5: production adopt-plan GO
3. adopt-apply GO
4. Freeze v2 解除 GO（別セッションのリリース便が待機中）

---

## 発見（2026-08-25）: guard の ingest consumer モデルが実態と 1 リファクタ分ずれている

fresh preflight adopt-plan が `snapshot_live()` で fail-closed:

```
★ teamagent-dev-ingest-weekly の ECS target が一意ではありません
```

### 判定: **production drift ではない。guard 側の stale な read-only モデル。**

live / repo desired / 承認済み rebind mapping の**三者が一致**している:

| 情報源 | ingest の起動経路 |
|---|---|
| live AWS（実測） | rule → Lambda `teamagent-dev-ingest-dispatch` → `TASKDEF_ARN` = `teamagent-dev-ingest:57` |
| repo desired | `infra/terraform/ingest_schedule.tf:426-431`（`arn = aws_lambda_function.ingest_dispatch[0].arn`・`ecs_target` 無し） |
| 承認済み rebind mapping | `state_rebind_targets.json` ingest = `kind: "lambda-env"` / `teamagent-dev-ingest-dispatch` |

導入コミットは `6ec7129`（2026-08-06「feat(ingest): 二重起動ガードと取り込みの高速化」）。
live への反映は **2026-08-21T08:45:58Z - 08:54:15Z**（Lambda `LastModified` と一致）で、
これは `activation_freeze.json` の `production_deployment_freeze.violations` に
**B3 再発として既に記録済み**の事象（`lambda_env_update: teamagent-dev-ingest-dispatch → teamagent-dev-ingest:57`）。
未記録の production mutation ではない。

一方 guard 側は `snapshot_live()`（`terraform_runtime_guard.sh:4168-4190`）と
consumer registry（同 `:5136-5147`・`activator.type = "eventbridge_rule_ecs_target"`）が
旧トポロジのままで、**ingest だけ**が取り残されている。
morning / canary は今も直接 ECS target を持っており（実測 `:55` / `:25`）、影響は ingest 1 consumer に限定。

### 影響

- guard は `snapshot_live` を通れないため、**plan / apply を含む全経路が実行不能**。
  adopt-plan だけでなく通常のリリース経路も同じ壁に当たる。
- 最後に guard が通ったリリースは 2026-08-17。トポロジが live で変わったのが 08-21 なので矛盾はない。

### 修正方針（実装中・PR 予定）

新 activator type `eventbridge_rule_lambda_taskdef_arn_environment` を追加する。
実行可否は rule state が決め、起動 taskdef は dispatch Lambda の `TASKDEF_ARN` が決める、というハイブリッド。

- snapshot は `rule_dispatchers.ingest` という**別キー**に入れる。既存 `dispatchers` は
  `dispatchers[$component]` で汎用参照されており tiktok/x の shape（concurrency + SQS mapping）を
  前提にしているため、shape が違う ingest を同居させない。
- ingest dispatch には `lambda:GetFunctionConfiguration` と `lambda:ListTags` のみ使う。
  `GetFunctionConcurrency` / `ListEventSourceMappings` は simulate 実測で **implicitDeny**、
  かつ EventBridge 起動なので SQS mapping は存在しない。**IAM 追加は不要**。
- 未知 activator type の fail-closed（`else error` / `else false`）は維持する。

## Known risks

| リスク | 状態 |
|---|---|
| **root は deny できない** | identity policy をバイパス。SCP と root key 無効化は activation スコープ外。監視のみ（`root_mutation_monitor.py`） |
| B3（ECS state drift）再発 | **解消済み**（rebind #2 完了・state == live を 5/5 実測） |
| guard の consumer モデルが live トポロジに追随していない | ingest で顕在化（上記「発見」）。**他の consumer にも同種の stale が無いか**は未検査 — 8 consumer 分の activator 実態突合を backlog 化 |
| freeze 中はリリース経路も停止 | automation role も deny 対象。緊急時は `activation_freeze_enabled=false` の再 apply（human gate） |
| 誤 action 名 `s3:GetBucketLifecycleConfiguration` | 既存 statement に 2 箇所 inert 残存。件数を pin して backlog 化 |
| bootstrap closure テストはコメントも走査 | tf のコメントにリソースアドレスやワイルドカード action を書くと誤爆する |

## 純粋 4+4 到達を阻む blocker（2 件・2026-08-25 時点）

### blocker 1: guard の ingest consumer モデル（上記「発見」）

`snapshot_live` を通れないため plan 自体が生成できない。

### blocker 2: A0.2.2c の IAM statement が execution line に無い

live には **適用済み**（serial 213）だが、execution line `868fcb7` の
`infra/terraform/runtime_evidence.tf` には `ReadExactConnectAppSnapshotObject` が **無い**
（dev `e48cf2e` には有る＝ commit `8bfdf16` / PR #324）。

このまま adopt-plan を取ると、plan に「承認済み IAM statement を削除する update」が混入し、
純粋 4 forget + 4 import にならない（かつ承認済み apply を巻き戻す方向の差分になる）。

→ guard 修正の cherry-pick と**同時に** `8bfdf16` も execution line へ取り込む必要がある。

### allowlist の現況

`activation_execution_allowlist.json`（dev 側 authoritative）は
`execution_base 0eab7f24` + **12 commit**、`expected_head = e40f1c23…`。
一方 execution line の実 HEAD は `868fcb7` で、`e40f1c23` の先に 4 commit ある:

| SHA | 内容 | allowlist 記載 |
|---|---|---|
| `c63e489` | Wave2 1/2 vulkan-loader ドリフト追随 | ❌ |
| `296720e` | Wave2 2/2 CVE-2026-14456 openssl | ❌ |
| `f973145` | 世代台帳を execution line 自身の inputs から再生成 | ❌ |
| `868fcb7` | rebind #2 mapping を 5 target へ確定 | ❌ |

→ allowlist 更新時に上記 4 + 本 issue 修正 + `8bfdf16` をまとめて記録し、
`expected_head` を新 HEAD に更新する。

## Next action

1. guard ingest activator 修正の実装 + 変異テスト（`fix/guard-ingest-lambda-activator`）
2. CI green → PR 作成 → 🛑 **merge GO?**
3. merge 後 execution line へ controlled cherry-pick（patch-id + touched blob の二点照合）
4. fresh preflight adopt-plan 再走 → **純粋 4 forget + 4 import 判定**
5. 判定到達で 🛑 STOP（STATUS PACKET）→ Activation Baseline 確定
6. production adopt-plan → adopt-apply → post-apply 検証 → ACTIVATION COMPLETE
7. PR2-A1 Hermes supply-chain → PR2-B Hermes dark runtime

### Freeze v2 解除までの残ゲート

別セッションのリリース便（mail 便 + お土産便1・dev `690a8be`・CI 緑・発射準備済み）が
Freeze 解除を待っている。解除可能になる条件は以下が**すべて**満たされたとき:

| # | 条件 | 現状 |
|---|---|---|
| 1 | guard が `snapshot_live` を通る（本 issue の修正） | ❌ 実装中 |
| 2 | preflight が純粋 4 forget + 4 import | ❌ 1 待ち |
| 3 | Activation Baseline 確定 | ❌ 2 待ち |
| 4 | production adopt-apply 完了（state のみ変更・実体不変を機械証明） | ❌ |
| 5 | post-apply 検証（新 4 address 在 / 旧 4 address 不在 / integrity before==after） | ❌ |
| 6 | guarded read-only plan が 0 add / 0 change / 0 destroy | ❌ |
| 7 | 🛑 Freeze 解除 GO（Human Gate ⑤） | ❌ |

**解除の実行はしない。** 上記 1-6 が green になった時点で報告のみ行う。
