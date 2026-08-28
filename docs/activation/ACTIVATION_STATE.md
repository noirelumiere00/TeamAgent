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

**Wave3 後の再整合 — Freeze は解除のまま / state rebind #3 待ち（2026-08-27）**

2026-08-26 に幹部公開をゴールとする裁定が出て、AWS 側 freeze を detach したうえで
mcp / OpenClaw の署名リリースを通した。その結果:

- generation は Wave3 へ動いた（4 object publish・v2.violations に実測記録）
- live は 5/5 で state を追い越した（B3 の 3 度目の再発）
- adopt の純粋 4 forget + 4 import はまだ**一度も取れていない**（preflight は blocker 3/4 で停止したまま）

### 体制（2026-08-27 引継）

Activation / Hermes / 製品側を**本セッションが単独で担当**する。
GOAL は全実装の完了。ユーザー指示により**人間の工数を最小限**にし、個別の確認ゲートを
increments ごとに作らず自走する（Human Gate 14 種の枠は残す）。

### 2026-08-26 - 27 の裁定 4 件（確定）

| # | 裁定 | 帰結 |
|---|---|---|
| ① | **Wave3 へ re-baseline** する（Wave2 契約へ戻さない） | Wave2 契約は `/nodejs/bin/node`＝Debian 版 node を要求し、戻すと CVE-2026-14456（trixie fix_deferred）が復活する。台帳は Wave3 の実測世代へ寄せる |
| ② | **Freeze は解除のまま走る**（宣言を live へ合わせる） | AWS 側 attachment は 0 principal のまま。repo 側ゲートだけを active で維持し、差分は宣言に明記（下記「Freeze status」） |
| ③ | **unlock は相乗りさせて後で畳む** | `unlock.active = true` を維持。B1（APP_HTML ピン）/ B2（image-builder 参照の導出化）を同じ unlock に乗せてから一括で relock |
| ④ | **HMAC 契約は根本から変更** | 現行の source_assertions 方式では report_link を表現できず remediation が構造的に詰む（下記「HMAC remediation は現状の repo 契約では実行不能」）。契約側から作り直す |

直前の完了:

1. State Rebind #2 完了（5 target・state == live を 5/5 実測・2026-08-25）
2. A0.2.2a / A0.2.2b / A0.2.2c IAM apply（serial 213）
3. mcp / OpenClaw の署名リリース（2026-08-26・外科的 CLI 経路）

## Current execution HEAD

| 項目 | 値 |
|---|---|
| branch | `activation-execution-base` |
| HEAD | `5ef4e8e`（案 B の裁定・正名化 hard blocker・レビュー結果を記録） |
| allowlist | `execution_base 0eab7f24` + **承認済み 22 commit**・`expected_head = 5ef4e8e9bc30`（`assert-execution-line` 緑・force push なし） |
| dev HEAD | `efee37c`（SOUL 圧縮 hotfix） |

`0eab7f2` は **Activation Candidate / Execution Base**。正式 Activation Baseline は
preflight が純粋 4 forget + 4 import を出した時点で確定する（**未確定**）。

## Freeze status

| freeze | 宣言 state | AWS 側の実態 | 備考 |
|---|---|---|---|
| Generation publisher | **ACTIVE**（repo 側のみ） | **detach 済み・0 principal** | 境界 `2026-08-24T08:24:04Z` は記録として保持 |
| Production deployment | **ACTIVE**（宣言） | **detach 済み・0 principal** | 裁定②により解除運用。live は 5/5 で先行 |
| dev merge | operational_only | — | 安全性は dev tip の不変性に依存させない |

### 宣言 ACTIVE が現に束縛しているもの（2026-08-27 時点）

`state = "active"` で今も動いているのは **repo 側の 2 経路だけ**:

1. CI `activation freeze (frozen surface)` — unlock 宣言なしに frozen surface を触る PR を落とす
2. guard の desired-state binding — `-var=activation_freeze_enabled=true` の注入と、
   plan が freeze リソースを destroy しないことの検査

AWS 側の mechanical deny は**効いていない**。`state` を `released` へ倒さない理由と
次回 apply の地雷は `activation_freeze.json` の
`generation_publisher_freeze.state_semantics` に機械可読で置いた。

### AWS 側 detach の実測（CloudTrail us-east-1）

IAM は global イベントなので **ap-northeast-1 で引くと 0 件に見える**（誤診断の罠）。

```
2026-08-24T08:19:16Z  attach 10（user AIIAdev 1 + role 9）
2026-08-26T02:01:54Z  DetachUserPolicy  AIIAdev  OK
2026-08-26T02:04:10Z  AttachUserPolicy  AIIAdev  OK ← 一度戻している
2026-08-26T02:05:44Z  DetachUserPolicy  AIIAdev  OK（確定）
2026-08-26T02:10:37Z - 02:10:50Z  DetachRolePolicy 9 role すべて OK
2026-08-26T02:11:03Z / 02:11:50Z  再実行 2 件 = NoSuchEntityException（冪等な空振り）
```

現況（2026-08-27 実測）: `list-entities-for-policy` の
PolicyUsers / PolicyRoles / PolicyGroups が**いずれも空**。policy 本体は削除していない。

復元用の記録は `scratchpad/freeze_attachments_backup.json`（9 role 名 + user `AIIAdev`）と、
同内容を `activation_freeze.json` の `v2.enforcement_status.restore_record` に転記済み
（scratchpad は揮発するため repo 側が正）。

> ⚠️ **次回 apply の地雷**: 宣言が active なので guard は今後も
> `activation_freeze_enabled=true` を注入する。terraform 経路で apply すると
> **detach 済みの 10 attachment が再作成され、デプロイ principal が再び deny される**。
> Freeze を解除したまま apply する便では plan で必ず確認すること
> （外科的 CLI リリースでは発生しない）。

## Wave3 generation publish（2026-08-26・実測）

detach の **1 時間 39 分後**に、frozen surface の契約 2 ファイルの変更が
新世代の publish を強制した。

| project | 新 key sha256 (先頭 16) | VersionId | 台帳の監視対象 |
|---|---|---|---|
| `teamagent-dev-image-builder` | `9aaf7facd5283120` | `qH02EEVmv.__fjXLiV_Lx7pq8H1pYQf3` | ❌ **対象外** |
| `teamagent-dev-mcp-source-publisher` | `db8a6c2b97c36e68` | `h1CRS9VMm1.oy0XXnTWUrtxJydOlpE0C` | ✅ |
| `teamagent-dev-image-attestor` | `1e1906ae37692b12` | `ZlgfiYPg0Niop6dR5CXgeMw7IXqpTVXP` | ✅ |
| `teamagent-dev-image-promoter` | `554ede59c17e3363` | `fc29cL2MfiSmEDb4Sawa7hYy8qRi77L3` | ✅ |

- publish: `2026-08-26T03:50:16Z - 03:50:17Z`（4 object）
- CodeBuild `UpdateProject`: `03:50:45Z - 03:51:36Z` に 8 回（4 project × 2）
  + `04:07:58Z` に `teamagent-dev-openclaw-provenance-builder` 1 回。principal は `user/AIIAdev`
- **台帳 4 プロジェクトのうち 3 件の世代が変化**（`approval-publisher` は 08-26 に publish 無し＝不変）
- `buildspec_generation_inputs.json#expected_generation_sha256` は **Wave2 の値のまま**＝ stale

台帳 vs live の突合【実測 2026-08-27・`codebuild batch-get-projects` の `source.buildspec`】:

| project | live の参照 | 台帳の expected | 判定 |
|---|---|---|---|
| `mcp-source-publisher` | `db8a6c2b…` | `00f6dc3a…` | ❌ 不一致 |
| `image-attestor` | `1e1906ae…` | `dba4ce93…` | ❌ 不一致 |
| `image-promoter` | `554ede59…` | `a624798a…` | ❌ 不一致 |
| `approval-publisher` | `33e2a643…` | `33e2a643…` | ✅ 一致 |
| `image-builder` | `9aaf7fac…` | **項目なし** | ⚠️ 監視対象外 |

**構造的原因**: `infra/terraform/codebuild.tf` が契約 JSON を `filebase64()` で
buildspec 本文へ直接埋め込む（例 `codebuild.tf:3045` = core_media 契約）。
契約 1 バイトの変更が buildspec 本文の sha256 を動かし、content-addressed key が変わる＝
**新世代の publish が構造的に強制される**。unlock は「その path を触ってよい」という許可であって
「世代が動かない」保証ではない。

**射程ずれの再発**: `image-builder` は世代が動いたのに `expected_generation_sha256` の
監視対象に入っていない（v1 で違反を 6 件と誤報告した原因と同型）。
`frozen_change_surface.generation_release_chain_projects` は 6 プロジェクトを列挙しているのに
監視器は 4 プロジェクトしか見ていない。

## State serial

| 時点 | serial |
|---|---|
| rebind #1 完了 | 200 |
| freeze policy apply 後 | 201 |
| A0.2.2a/b IAM apply 後 | 212 |
| A0.2.2c IAM apply 後 | 213 |
| 2026-08-27 時点 | **214**【申告・引継値。本記録では tfstate 未再読】 |

### Rebind #2 の結果（2026-08-25 時点では state == live を 5/5 実測）

| address | rebind #2 の state | 2026-08-27 の live【実測】 | 一致 |
|---|---|---|---|
| `aws_ecs_task_definition.mcp` | `:88` | service `:92` | ❌ |
| `aws_ecs_task_definition.connect_web` | `:73` | service `:78` | ❌ |
| `aws_ecs_task_definition.morning_digest` | `:55` | rule ECS target `:59` | ❌ |
| `aws_ecs_task_definition.canary` | `:25` | rule ECS target `:29` | ❌ |
| `aws_ecs_task_definition.ingest` | `:57` | dispatch Lambda `TASKDEF_ARN` `:61` | ❌ |

**B3（ECS state drift）が 3 度目の再発。5/5 すべてが乖離。**
原因は 2026-08-26 の署名リリース便を Freeze 解除下の外科的 CLI 経路で通したこと
（裁定②の下では違反ではない）。→ **state rebind #3**（Human Gate ③）が必要。

`teamagent-dev-openclaw:41` は稼働中だが **terraform state に該当リソースが無い**
（ECS + EFS が未取込）。rebind の 5 target にも入らないため drift ではなく **管理外**として扱う。
取込は Activation 完了後の別便（既知の残宿題）。

## Approved commits

### payload（execution line 上）

**現況【実測 2026-08-27】**: base `0eab7f24` + **承認済み 22 commit** = `5ef4e8e9bc30`。
`activation_freeze_check.py assert-execution-line` が緑（force push / 履歴改変なし）。
上の「予定: base + 10 commit」は取り込み済みで、以後も allowlist が唯一の台帳。

取り込みは patch-id と touched blob の**二点照合**で同一性を機械確認する。

### control-plane metadata（execution line に入らない）

`infra/deploy/activation_execution_allowlist.json` は **dev 側が authoritative**。
allowlist を更新する commit は payload に数えない（自己参照を避けるため）。

⚠️ #315 の patch は allowlist の**コピー**を execution line にも持ち込む。これは stale な
inert コピーなので、検証は必ず dev 側から実行する（self-reference ガードは実装済み・
`assert_execution_line` が repo HEAD == execution ref のとき die する）。

## Pending gates（2026-08-27 更新）

Freeze 解除は裁定②で済んでいるため、旧「Freeze v2 解除 GO」は**消滅**した。
残る停止点は次のとおり。

1. **state rebind #3 GO**（Human Gate ③）— 5/5 drift の解消。以後の plan はこれが前提
2. **台帳の Wave3 再ベースライン**（裁定①）— `expected_generation_sha256` を実測世代へ寄せ、
   `image-builder` を監視対象へ入れる（射程ずれの根治）
3. Gate 5: production adopt-plan GO（純粋 4 forget + 4 import の判定到達後）
4. adopt-apply GO
5. **unlock relock GO**（Human Gate ⑤）— 裁定③の相乗りが終わってから一括で畳む
6. **HMAC 契約の作り直し GO**（裁定④）— 幹部公開とは混ぜない

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
- shim は `ACTIVATION` + `-SHIM(ingest)` タグ（正名化で全撤去済み）で grep 可能にし、
  分岐条件を consumer_id / identity の **exact 一致**に限定していた。型で束ねる書き方は禁止だった。
- 件数と ingest 限定性は contract test で pin していた
  （正名化後は `tests/infra/test_ingest_canonical_activator_contract.py` が
  「タグが 1 つも残っていない」「型で分岐している」を逆向きに pin する）。

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
   → 正名化後の型は `eventbridge_rule_` + `lambda_taskdef_arn_environment`
2. `release_evidence.py` を新 type に追随させる
3. shim タグの箇所（guard 10 / saga 9 / context 6 = 25）を撤去し、
   分岐を consumer_id のリテラル一致から **activator type** へ移す
4. `buildspec_generation_inputs.json` の expected generation SHA を再導出し、
   新しい approved generation を publish する

**1〜3 は `prep/rename-0828` で先行執筆済み（2026-08-28 夜）。4 は未着手**
（SHA 再導出は台帳再ベースラインが確定させる入力集合に依存するため分離した）。

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

## HMAC remediation plan（案 β・2026-08-25・**要 Human Gate**）

裁定: **案 β 採用**（`hmac/report-link` を canonical desired として復元。`report-link-hmac` を
approved desired へ昇格させる案 α は NO-GO）。以下は read-only 調査に基づく remediation 設計。
**apply も Freeze 解除もまだ行わない。**

### 調査で判明した前提（設計を変える 3 点）

**① REPORT_LINK の consumer は 3 つではなく 2 つ。**

| workload | MAIL_ACTION | REPORT_LINK |
|---|---|---|
| `mcp` | ✅ | ✅ |
| `connect_web` | ✅（live のみ） | ✅ |
| `morning_digest` | ✅ | ❌ 持たない |

tf 上 `local.report_link_hmac_secrets` を含むのは `fargate.tf`（mcp）と
`connect_web.tf` のみ。guard も report purpose の consumer を mcp + connect_web と定義している。

**② tfvars に HMAC 設定が 1 行も無い（全 worktree で 0 件）。**

`teamagent-build` / `teamagent-activation` / `teamagent` のどの tfvars にも `hmac` が無く、
`hmac_rollout_control_path` も未設定。したがって Terraform desired は全て default:

- `report_link_hmac_rollout_phase = "blocked"` / `*_secret_arn = ""` / `*_primary_version_id = ""`
- → `local.*_primary_value_from = ""`

`local.mail_action_hmac_secrets` / `report_link_hmac_secrets` は primary entry を
**無条件で** taskdef へ入れるため、いま plan を取ると **MAIL_ACTION も REPORT_LINK も
valueFrom が空**になる。つまり **remediation は report-link 単独では成立しない**
（report-link だけ直すと mail HMAC が本番で壊れる）。

**③ live の MAIL_ACTION も repo で表現できない。**

live は `MAIL_ACTION_HMAC_SECRET → teamagent/dev/database-url`。
guard の contract はこれを許容するが、tf の `mail_action_hmac_secret_arn` validation は
`teamagent/dev/hmac/mail-action-XXXXXX` しか許さない。
＝ **report-link と同じ「live にしか無い」問題が mail_action にもある。**

さらに `*_config_ready` は `hmac_live_manifest_path` と `hmac_rollout_control_path` の
実ファイル存在を要求するが、**どちらも存在しない**。

### canonical secret creation / migration method

Terraform は secret を **作らない**（全て `data` 参照。`hmac_rotation.tf` 冒頭
「Secret values never enter Terraform」）。よって canonical secret の作成は
**Terraform 外の管理操作**であり、state にも plan にも値は入らない。

権限の実測:

| principal | GetSecretValue | CreateSecret | PutSecretValue |
|---|---|---|---|
| `AIIAdev`（MFA） | ✅ | ✅ | ✅ |
| automation role | **implicitDeny** | ✅ | ✅ |

automation role は素材を**読めない**（least privilege 維持）。よって移送は **AIIAdev で実行**。

手順（**同一 material を移す。新しい乱数を生成しない。既存 secret は削除しない**）:

1. boto3 で `GetSecretValue("teamagent/dev/report-link-hmac")` — **プロセス内のみ**
2. `sha256(material)` を計算（＝ fingerprint。これだけが外に出る）
3. `CreateSecret(Name="teamagent/dev/hmac/report-link", SecretString=material)`
4. canonical を読み直して `sha256` 一致を確認
5. 新 ARN（6 桁 suffix 付き）と `VersionId` を採取（**どちらも非機密**）

禁止: シェルパイプ・一時ファイル・標準出力・plan JSON・証跡・チャットへの平文露出。
出すのは **fingerprint / ARN / VersionId のみ**。

### 3 consumer desired diff（remediation 後の想定）

| workload | 変更内容 |
|---|---|
| `mcp` | `REPORT_LINK_HMAC_SECRET` → `<canonical ARN>:::<VersionId>`。`MAIL_ACTION_HMAC_SECRET` は **裁定待ち**（②③ のため） |
| `connect_web` | 同上（REPORT_LINK のみ） |
| `morning_digest` | REPORT_LINK は無い。`MAIL_ACTION` の扱い次第で **変更 0 にもできる** |

**desired を「live 相当 + canonical 名」にするには tfvars へ最低限**
`report_link_hmac_rollout_phase` / `report_link_hmac_secret_arn` /
`report_link_hmac_primary_version_id` / `hmac_live_manifest_path` /
`hmac_rollout_control_path` / `worker_hmac_artifact_sha256` などの設定が要る
（`report_link_hmac_config_ready` の全条件）。**mail_action 側の裁定が無いと確定できない。**

### Freeze temporary opening scope（最小）

freeze policy は 3 statement / 16 action。必要なのは **1 statement の 3 action だけ**:

| statement | action | 要否 |
|---|---|---|
| DenyWorkloadDeployment… | `ecs:RegisterTaskDefinition` | ✅ 必要（新 taskdef） |
| 〃 | `ecs:UpdateService` | ✅ 必要（mcp / connect_web） |
| 〃 | `events:PutTargets` | ✅ 必要（morning_digest を変える場合のみ） |
| 〃 | `ecs:DeregisterTaskDefinition` / `events:PutRule` / `events:RemoveTargets` / `lambda:UpdateFunctionConfiguration` | ❌ 不要 |
| DenyGenerationPublisher…（5 action） | codebuild 系 | ❌ **開けない** |
| DenyBuildspecGenerationWrites…（4 action） | s3 書込 | ❌ **開けない** |

**Freeze v1 を void させたのは generation publisher 面**なので、そこは閉じたままにする。

### rollback

- 旧 secret `teamagent/dev/report-link-hmac` は**削除しない**（rollback の前提）
- 現行 revision へ戻す: `mcp:88` / `connect_web:73` / `morning_digest:55`
- repo には `hmac_gate_mode = "rollback"` と `hmac_rollout_control_path` の
  rollback taskdef ARN 機構が既にある（`hmac_keyrings.tf`）
- rollback 自体も `ecs:UpdateService` を要するため、Freeze の一時開放は
  **rollback 完了まで**閉じない

### expected AWS mutations

| 対象 | 種別 | 件数 |
|---|---|---|
| Secrets Manager | CreateSecret（canonical） | 1 |
| ECS | RegisterTaskDefinition | 最大 3 |
| ECS | UpdateService | 2（mcp / connect_web） |
| EventBridge | PutTargets | 0〜1（morning を変える場合） |
| CodeBuild / S3 buildspec | — | **0** |

### state impact

新 taskdef revision が state に入る。**rebind #2 が確立した
`mcp:88` / `connect_web:73` / `morning_digest:55` への束縛は上書きされる。**
adopt との順序関係を裁定する必要がある（remediation を先にすると rebind #2 の
mapping と live が再びずれる）。

## 🛑 HMAC remediation は現状の repo 契約では実行不能（2026-08-26・要裁定）

裁定どおり remediation → adopt の順で設計を進めたが、**承認された形のままでは実行できない**
ことが read-only 調査＋一次ソース照合で確定した。plan を作っても通らないので作らない。

### 一次ソースで検証した 4 点

| # | 事実 | 根拠（実コード確認済み） |
|---|---|---|
| 1 | **primary の ARN を変えるなら previous は必須**。鍵 material が同一でも迂回不可 | `src/teamagent/hmac_keyring.py:519-521` `elif proposed_primary != deployed_primary: if not proposed_previous_present: return _contract_result(False, "primary_changed_without_previous")` |
| 2 | しかも **previous は deployed primary そのもの**でなければならない | 同 `:522-523` `if proposed_previous != deployed_primary: ... "previous_generation_mismatch"` / `hmac_keyrings.tf:762-768` の `previous_generation == deployed_primary_generation` |
| 3 | 失敗時 rollback 経路は EventBridge を**復元**する（`PutRule` を含む） | `infra/terraform/eventbridge_apply_saga.py:1294` `finished_at = self._restore(baseline)` |
| 4 | **live の valueFrom は 5 件すべて VersionId 未 pin** | 自前 live snapshot 実測。`mcp` / `connect_web` / `morning` の MAIL/REPORT すべて `:::` 無し |

### 帰結①: report_link は現行契約で表現できない（構造的に詰み）

1+2 より previous は deployed primary＝`teamagent/dev/report-link-hmac` でなければならない。
ところが `report_link_hmac_previous_secret_arn` の validation は
`(database-url|hmac/report-link)` しか許さない（`hmac_rotation.tf:56-60`）。
**第三の secret である `report-link-hmac` を previous として書けない。**

→ tfvars だけでは不可能。**repo 側の HMAC 契約そのものの変更**（report_link 用の
legacy 変数追加、または新 phase の新設）が要る。これは security-critical 契約の変更なので
別途裁定が要る。

なお **mail_action は表現できる**（deployed primary が `database-url` で、
legacy_migration の previous も `database-url` なので一致する）。report_link だけ非対称。

### 帰結②: そもそも gate が live を読めない（未 pin）

gate は live taskdef の valueFrom が `ARN:::VersionId` であることを要求し、
素の ARN なら `secret_reference_unpinned` で即失敗する（`hmac_rollout_gate.py:932-935`）。
live は 5 件すべて未 pin なので、**IAM を直しても ledger initialize に到達しない**。
先に「pinned legacy revision を登録する」段が要る（`docs/runbooks/hmac_domain_migration.md:123-128`）。

### 帰結③: Freeze 例外は 3 action では足りない

失敗時 rollback は canary / ingest / morning の **3 rule 分の `events:PutRule` を無条件発行**し、
余剰 target があれば `RemoveTargets`、drift 時は `lambda:UpdateFunctionConfiguration` も出す。
3 action だけ開けると「apply 失敗 → rollback も失敗 → reconcile-required」に落ちる。

さらに **例外を閉じた後は rollback 自体が実行できない**（rollback も同じ deny action を使う）。
freeze policy は AIIAdev user にも attach されているため、
runtime_automation だけ例外にすると**緊急時の手動 rollback 主体が残らない**。

### 帰結④: remediation は adopt の純粋 4+4 を壊す

`terraform_data.hmac_live_task_gate` の `triggers_replace` に
`var.image_deployment_intent_id`（リリースごとの UUID）が入るため、
remediation 後に adopt plan を取ると gate 3 件が replace、
count 0→1 になった pre/post_update 最大 6 件が destroy として現れる。
adopt validator は no-op / forget / import しか許さない（`supply_chain_adopt_validate.py:11-17`）。

→ **remediation → adopt の順序は、間に「gate を inactive へ戻す整地 apply」を挟まないと成立しない。**

### 帰結⑤: guard 経由 apply は control ファイル無しでは始まらない

`eventbridge_apply_saga.py:542-549` が saved plan の `hmac_rollout_control_path` が空なら
`SagaError`。HMAC と無関係な apply でも guard 経路なら control 必須。
A0.2.2a-d の IAM apply が通ったのは guard を使わない targeted saved-plan 経路だったため。

### その他の未解決

- `worker_hmac_artifact_sha256` は config_ready が 64 hex を無条件要求するが、
  値の出所が repo 内に無い
- DynamoDB ledger の stage 進行（mcp は `worker_verified` 以降）が必要で、
  worker-verified には worker rollback artifact の提示が要る
- mail_action の canonical 化は「**DB 接続文字列を別 secret へ複製する**」ことを意味する
  （live の MAIL_ACTION material は `database-url` の値そのもの）

## Known risks

| リスク | 状態 |
|---|---|
| **root は deny できない** | identity policy をバイパス。SCP と root key 無効化は activation スコープ外。監視のみ（`root_mutation_monitor.py`） |
| B3（ECS state drift）再発 | **再発（3 度目・2026-08-27 実測で 5/5 乖離）**。rebind #2 の成立は 08-25 まで。→ rebind #3 待ち |
| guard の consumer モデルが live トポロジに追随していない | ingest で顕在化（上記「発見」）。**他の consumer にも同種の stale が無いか**は未検査 — 8 consumer 分の activator 実態突合を backlog 化 |
| freeze 中はリリース経路も停止 | **現在は非該当**（08-26 に全 principal detach 済み）。ただし宣言 state=active のため、次の terraform apply で 10 attachment が再作成され再びリリースが止まる。plan で必ず確認する |
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

**解消済み【実測 2026-08-27】**: `activation_execution_allowlist.json`（dev 側 authoritative）は
`execution_base 0eab7f24` + **承認済み 22 commit**、`expected_head = 5ef4e8e9bc30`。
execution line の実 HEAD も `5ef4e8e` で一致し、`assert-execution-line` が緑
（上表の 4 commit は記録済み）。以後この節は allowlist の実測値だけを持つ。

## Next action（2026-08-27 更新）

1. **記録の再整合（本更新）** — Wave3 violation / detach 実態 / 5-5 drift / 裁定 4 件を宣言と
   本ファイルへ反映（完了）
2. **state rebind #3** — 5 target を live（`:92` / `:78` / `:59` / `:29` / `:61`）へ寄せる
   → 🛑 Human Gate ③
3. **台帳の Wave3 再ベースライン**（裁定①）— `expected_generation_sha256` を実測世代へ、
   `image-builder` を監視対象へ。**Wave2 契約へは戻さない**（CVE-2026-14456 が復活するため）
4. guard の blocker 3（IAM 不足）/ blocker 4（HMAC selector 未 pin）を片付けて
   fresh preflight adopt-plan を再走 → **純粋 4 forget + 4 import 判定**
5. Activation Baseline 確定 → production adopt-plan → adopt-apply → post-apply 検証
   → ACTIVATION COMPLETE
6. 正名化（ingest activator type の hard blocker）→ unlock relock（裁定③）
7. PR2-A1 Hermes supply-chain → PR2-B Hermes dark runtime

### Freeze 解除ゲートは消滅（2026-08-26 裁定②）

旧「Freeze v2 解除までの残ゲート」7 条件は、**解除そのものがユーザー裁定で先に行われた**ため
条件表としては失効した。待機していたリリース便（mail 便 / お土産便1）も 08-26 の
署名リリースで通っている。

残っているのは解除の可否ではなく **解除下での整合性**である:

| # | 論点 | 現状 |
|---|---|---|
| 1 | state と live の乖離（5/5） | ❌ rebind #3 待ち |
| 2 | 世代台帳が Wave3 に追随していない | ❌ 再ベースライン待ち |
| 3 | `image-builder` が監視対象外 | ❌ 射程ずれが残存 |
| 4 | guard が preflight を通らない（blocker 3 / 4） | ❌ |
| 5 | 純粋 4 forget + 4 import 判定 | ❌ 未到達 |
| 6 | unlock が立ったまま | ⏸ 裁定③により意図的に維持 |
| 7 | 次の terraform apply で freeze が再 attach される | ⚠️ plan で要確認（上記地雷） |
