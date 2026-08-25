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

### 修正方針: 案 B（2026-08-25 裁定・案 A は NO-GO）

**ingest の宣言 activator type は `eventbridge_rule_ecs_target` のまま据え置く。**
実態との差（taskdef ポインタが event target の `ecs_target` ではなく dispatch Lambda の
`environment` にある）だけを **ingest 限定の shim** で吸収する。
これは恒久対応ではなく `activation-only compatibility shim`。

- snapshot は `rule_dispatchers.ingest` という**別キー**に入れる。既存 `dispatchers` は
  `dispatchers[$component]` で汎用参照されており tiktok/x の shape（concurrency + SQS mapping）を
  前提にしているため、shape が違う ingest を同居させない。
- ingest dispatch には `lambda:GetFunctionConfiguration` と `lambda:ListTags` のみ使う。
  `GetFunctionConcurrency` / `ListEventSourceMappings` は simulate 実測で **implicitDeny**、
  かつ EventBridge 起動なので SQS mapping は存在しない。**IAM 追加は不要**。
- shim は `ACTIVATION-SHIM(ingest)` タグで grep 可能にし、分岐条件を consumer_id /
  identity の **exact 一致**に限定する。型で束ねる書き方は禁止。
- 件数と ingest 限定性を contract test（`tests/infra/test_ingest_activation_shim_contract.py`）で pin。

**案 A（新 activator type の導入）が NO-GO の理由**: canonical registry を変えると
`release_evidence.py` の追随が原子的に強制され（import 時に registry の activator type を
検証するため）、それは frozen generation surface を触る。触ると 4 世代の
expected generation SHA が動き、adopt の 4 点一致が崩れて新世代 publish が必要になる
＝ Freeze v1 を void させた wave1/wave2 の再演。

---

## 🔴 HARD BLOCKER: activator type の正名化（2026-08-25 裁定）

案 B は技術的負債を意図的に残す。**registry が「ECS target を持たない consumer」を
`eventbridge_rule_ecs_target` と宣言し続ける**＝今回の事故と同種の
「モデルが実態を偽る」状態。これを hard blocker として固定し、順序の飛ばしを禁止する:

```
Activation 中: ingest = temporary lambda-env interpretation（shim）
      ↓
ACTIVATION COMPLETE
      ↓
canonical registry + release_evidence を原子的に正名化
      ↓
新しい approved generation release
      ↓
Freeze 解除 / 次の通常 generation publish
```

**禁止事項**:

- 正名化をしないまま **Freeze を解除しない**
- 正名化をしないまま **次の generation release へ進まない**
- **Hermes A1 が generation publisher を必要とするなら、その前に正名化を片付ける**

正名化の内容（Activation 完了後の PR で原子的に行う）:

1. `image_deployment_consumers.json` の ingest activator type を実態に合わせる
2. `release_evidence.py` を新 type に追随させる
3. `ACTIVATION-SHIM(ingest)` タグの箇所（guard 10 / saga 9 / context 6 = 25）を撤去
4. `buildspec_generation_inputs.json` の expected generation SHA を再導出し、
   新しい approved generation を publish する

**撤去してはいけないもの**（shim ではなく恒久修正）:
`validate_plan` の allowlist への ingest dispatch Lambda 追加と、
`run_activation_task` の ingest network 取得元。どちらもトポロジ由来なので正名化後も残る。

## 🛑 blocker 3: preflight が IAM 不足で止まる（2026-08-25・要 Human Gate ④）

blocker 1（ingest）と blocker 2（A0.2.2c 取込）は解消し、preflight は
**integrity snapshot の採取まで到達**した（4 object）。その先で `snapshot_live()` が
**automation role に無い read 権限**を呼んで止まる。

### 実測（assume-role した実 API 呼び出しで確認。simulate だけに依存していない）

| action | 判定 | guard の呼び出し箇所 | tf での付与 |
|---|---|---|---|
| `lambda:GetFunctionConcurrency` | ❌ AccessDenied | `terraform_runtime_guard.sh:4307`（tiktok / x_buzz の 2 dispatch） | **tf 全体で 0 件**＝誰にも付与されていない |
| `ecs:DescribeTasks` | ❌ AccessDenied | 同 `:4261`（ingest の active task 詳細） | 他 3 role にはあるが automation role には**無い** |

同時に検査した他 20 action は全て可（apigatewayv2 3 種 / s3 head-object・get-bucket-versioning /
lambda GetFunctionConfiguration・ListTags・ListEventSourceMappings 等）。
`simulate-principal-policy` は CLI 名（`s3api` / `apigatewayv2`）を IAM action 名へ
素朴に変換すると誤検知するため、**疑わしいものは実 API 呼び出しで確定**させた。

### これは案 B が作った問題ではない

両 action は **2026-07-17 の `3efc8e0` / `d94a167`** で guard に入ったが、
IAM 側には最初から無い。つまり **automation role で `snapshot_live` が最後まで通ったことは一度も無い**。
ingest の壁（2026-08-06）に到達すらしていなかったのは、この 2 つが先にあったからではなく、
ingest の壁が先に立っていたため。壁を 1 枚ずつ剥がして 3 枚目に到達した状態。

### 必要な変更（最小）

read-only 2 action の追加のみ。write は 1 つも要らない。

- `lambda:GetFunctionConcurrency` → resource は **dispatch 2 本の exact ARN**
  （`teamagent-dev-tiktok-acquire-dispatch` / `teamagent-dev-x-buzz-dispatch`）。
  **ingest dispatch は含めない**（案 B は意図的にこの API を使わない設計にしたため不要）
- `ecs:DescribeTasks` → resource は `teamagent-dev` クラスタの task

代替案（guard から concurrency 検査を外す）は**既存の security control を弱める**ため推奨しない。

## 🛑 blocker 4: live の HMAC selector が repo に存在しない（2026-08-25・要裁定）

A0.2.2d の apply で IAM の壁は解消し、`snapshot_live()` は**最後まで完走**するようになった
（`live-before.json` 生成まで到達）。その直後の HMAC 契約検査で停止する。

```
deployed HMAC primary selector is outside the exact contract
★ 実デプロイHMAC metadataがpurpose consumer間で不整合です
```

### 実測

live の 3 consumer（mcp / connect_web / morning_digest）の taskdef が持つ値:

| 変数 | live の値（suffix 伏せ） | 契約 |
|---|---|---|
| `MAIL_ACTION_HMAC_SECRET` | `teamagent/dev/database-url-XXXXXX` | ✅ 契約内（`database-url` は許容） |
| `REPORT_LINK_HMAC_SECRET` | `teamagent/dev/**report-link-hmac**-XXXXXX` | ❌ 契約は `teamagent/dev/**hmac/report-link**-XXXXXX` |

名前の並びが違う（`hmac/report-link` ではなく `report-link-hmac`）。

### repo との突合

| 観点 | 結果 |
|---|---|
| repo 内で `hmac/report-link` を参照 | **10 件**（`hmac_rotation.tf` の変数 validation・guard の契約パターン） |
| repo 内で `report-link-hmac` を参照 | **0 件** |
| git 全履歴で `report-link-hmac` が存在した commit | **0 件** |
| Secrets Manager の実体 | `teamagent/dev/report-link-hmac`（作成 2026-08-05）。`teamagent/dev/hmac/report-link` は**存在しない** |
| tfvars の `report_link_hmac_secret_arn` / `mail_action_hmac_secret_arn` | **未設定**（＝ tf 的には valueFrom が空になる） |

### いつ入ったか

3 consumer の taskdef 登録時刻は**すべて記録済み違反 window の中**:

| taskdef | registeredAt (JST) |
|---|---|
| `teamagent-dev-mcp:88` | 2026-08-21 17:50:28 |
| `teamagent-dev-connect-web:73` | 2026-08-21 17:50:31 |
| `teamagent-dev-morning-digest:55` | 2026-08-21 17:54:08 |

記録済み違反 window = `2026-08-21T08:45:58Z - 08:54:15Z`（JST 17:45:58 - 17:54:15）＝
`production_deployment_freeze.violations` に B3 再発として残っている out-of-band デプロイ。

### 判定: approved desired ≠ live（STOP）

live は **repo に一度も存在しない secret selector** を参照している。
これは ingest の件（live == desired で guard だけが stale）とは**性質が違う**。

さらに **rebind #2 は state をこの taskdef revision 群（mcp:88 等）へ束縛済み**である点に注意。

### やってはいけないこと

guard の契約パターンに `report-link-hmac` を足して緑にするのは、
「モデルを実態に合わせて security control を緩める」方向であり、
案 A/案 B の裁定で否定された筋。**どちらが正か**の裁定が先。

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

### blocker 1-b: 案 A が frozen surface と衝突した（解決済み・案 B へ再構成）

**結末**: 2026-08-25 に案 B GO / 案 A NO-GO。PR #327（`82a6b59`）は破棄し、
`fix/guard-ingest-lambda-observation` で作り直した。以下は判断根拠の記録。

blocker 1 の修正（PR #327 / `82a6b59`）は CI で 2 つ落ちる。**原因は同一**:

| CI job | 結果 |
|---|---|
| activation freeze (frozen surface) | `FROZEN SURFACE 違反: infra/codebuild/release_evidence.py` |
| pytest 3.11 / 3.13 / 3.14 | `StaleManifestError: STALE MANIFEST — generation inputs が Generation Baseline から変化した` |
| gitleaks / terraform / trivy | pass |

`infra/codebuild/release_evidence.py` は `buildspec_generation_inputs.json` の
**18 input のうちの 1 つ**（frozen surface）。触ると 4 世代の expected generation SHA が動く。
本 PR で frozen surface に触れているのは**この 1 ファイルだけ**（他 15 ファイルは surface 外）。

**分離不可であることを実測で確認**: `release_evidence.py` は import 時に canonical
consumer manifest を読んで activator type を検証する（`release_evidence.py:2484`）。
registry を hybrid にして `release_evidence.py` を据え置くと
`EvidenceError: Terraform image consumer manifest is invalid` で fail-closed になる。
= 「registry の type 変更」と「release_evidence.py の追随」は原子的。

#### なぜ unlock では解決しないか

frozen surface を触ると expected generation SHA が変わる。すると adopt の
**4 点一致**（repo 導出 SHA == S3 body SHA == content-addressed key == CodeBuild ref）が崩れる。
揃え直すには新世代の publish が必要だが、それは Freeze v2 が禁じている当のものであり、
Freeze v1 を void させた wave1/wave2 の再演になる。
→ **unlock は「freeze を緩める」だけでなく adopt そのものを壊す。**

#### 選択肢

| 案 | 内容 | frozen surface | adopt への影響 |
|---|---|---|---|
| A | unlock を宣言して `release_evidence.py` を変更 | 1 ファイル変更 | ❌ 4 点一致が崩れ、新世代 publish が必要になる |
| B | ingest の宣言 activator type は据え置き、**観測経路と plan/saga 側だけ**修正 | 触らない | ✅ 影響なし |

B で触るのは `terraform_runtime_guard.sh` / `image_release_context.py` /
`ecs_service_apply_saga.py` / `image_release_gate.tf` で、**いずれも frozen surface 外**。

**裁定: B GO / A NO-GO。** 技術的負債は上の「🔴 HARD BLOCKER」で固定した。

#### 案 B の受け入れ条件（すべて機械確認済み）

| # | 条件 | 実測 |
|---|---|---|
| 1 | `release_evidence.py` unchanged | ✅ base から不変 |
| 2 | frozen 18 inputs delta = 0 | ✅ 交差 **0** |
| 3 | 新 activator type 文字列が repo に無い | ✅ 0 件 |
| 4 | canonical registry の ingest type 据え置き | ✅ `eventbridge_rule_ecs_target` |
| 5 | shim は frozen surface 外のみ | ✅ guard 10 / saga 9 / context 6 = 25 箇所 |
| 6 | 触らない指定のファイルが不変 | ✅ registry `.json`/`.py` / `image_release_gate.tf` / `forced_rollback_drill.*` |

`image_release_gate.tf` は型が増えないため追随不要となり、案 A で必要だった変更が消えた。

#### 敵対的レビューで確定した欠陥 5 件（修正済み）

5 観点（漏れ / fail-closed 後退 / frozen surface / 観測の正しさ / テストの実質性）で
並列レビューし、各指摘を**反証専任**で独立検証。10 件確定 / 5 件反証、重複を除くと 5 件。

| # | 重大度 | 欠陥 | 修正 |
|---|---|---|---|
| D1 | HIGH | `validate_plan` の allowlist に ingest dispatch Lambda が無く、ingest taskdef を差し替える release plan が棄却される。**snapshot_live の壁を越えても次段で全断したまま** | allowlist へ追加 |
| D2 | HIGH | activation preflight の RunTask が ingest の network を event target の `ecs_target` から読み、null になって拒否される | 本番 dispatcher と同じく Lambda env から組み立て、`subnet-`/`sg-` 形式を検査 |
| D3 | MEDIUM | shim タグ棚卸しが docs まで走査し、参照先に指定している本ファイルにタグ名を書くと赤くなる | 数の pin は散文を除外。frozen surface 非交差は repo 全体で判定 |
| D4 | HIGH | shim コメントの行長対応で 2 箇所が 3 行になり、2 行固定の contract test が赤 | 全 25 箇所を 2 行形式へ統一 |
| D5 | HIGH | fake plan が ingest dispatch Lambda を恒久 no-op に固定し、**D1 の穴を隠していた** | tiktok/x と同じ `["update"]` + `TASKDEF_ARN` after_unknown へ是正 |

**D1 は shim が作った欠陥ではない。** base `e48cf2e` の guard には `ingest_dispatch` の
文字列が 1 つも無い。`6ec7129`（2026-08-06）が ingest の起動経路を Lambda 経由へ変えたとき、
guard の 2 箇所（`snapshot_live` と `validate_plan` の allowlist）が同時に取り残された。
`snapshot_live` が先に die していたため 2 枚目の壁が露見していなかっただけで、
**修正しないと preflight に到達できない**。

#### 変異テスト台帳（5/5 kill）

| # | 壊した箇所 | 赤になったテスト |
|---|---|---|
| M-D1 | allowlist から ingest dispatch を除去 | `test_runtime_attribute_regressions_fail_closed[target_action]` |
| M-D2 | `run_activation_task` の ingest 分岐を撤去 | `test_guard_builds_the_ingest_preflight_network_from_the_dispatch_lambda` ほか 1 |
| M-D4 | shim コメント 1 箇所を 3 行へ | `test_shim_tag_locations_counts_and_comment_text_are_pinned` |
| M-D5 | shim タグを `tests/` へ 1 箇所増やす（漏れ再現） | 同上 |
| M-B | canonical registry の ingest type を書き換え | `test_ingest_registry_keeps_the_canonical_eventbridge_ecs_activator_type` |

`run_activation_task` は**既存テストで一度も通っていなかった**ため
`tests/scripts/test_ingest_activation_preflight_network.py` を新設した
（修正前の取得元では null になることも固定＝この修正が無意味でない証拠）。

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
