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

**State Rebind #2 に向けた execution line 更新 / Wave2 reconstruction**

直前の完了: Freeze v2 ACTIVE 化（AWS 側 persistent explicit-deny の適用）。

## Current execution HEAD

| 項目 | 値 |
|---|---|
| branch | `activation-execution-base` |
| HEAD | `e40f1c2`（= `0eab7f2` + 承認済み 12 commit） |
| allowlist | base + **12** payload commit（patch-id 12/12・touched blob 全一致で検証済み） |

`0eab7f2` は **Activation Candidate / Execution Base**。正式 Activation Baseline は
preflight が純粋 4 forget + 4 import を出した時点で確定する（未確定）。

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
| freeze policy apply 後 | **201**（現在値・lineage `745cf6df…` 不変） |

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

1. **Production State Rebind #2 GO?** ← 次の停止点
2. IAM APPLY GATE（A0.2.2a/b の saved targeted plan）
3. Gate 5: production adopt-plan GO
4. adopt-apply GO

## Known risks

| リスク | 状態 |
|---|---|
| **root は deny できない** | identity policy をバイパス。SCP と root key 無効化は activation スコープ外。監視のみ（`root_mutation_monitor.py`） |
| B3（ECS state drift）再発 | 5 件が drift 中（mcp/connect_web/morning_digest/canary/ingest）。rebind #2 で解消予定 |
| freeze 中はリリース経路も停止 | automation role も deny 対象。緊急時は `activation_freeze_enabled=false` の再 apply（human gate） |
| 誤 action 名 `s3:GetBucketLifecycleConfiguration` | 既存 statement に 2 箇所 inert 残存。件数を pin して backlog 化 |
| bootstrap closure テストはコメントも走査 | tf のコメントにリソースアドレスやワイルドカード action を書くと誤爆する |

## Next action

1. guard 実走 suite green → #322 push → fresh CI → merge → 最終 SHA / patch-id 採取
2. 8 commit controlled cherry-pick（patch-id 8/8 + touched blob 一致）
3. allowlist を base + 10 へ更新（dev 側・self-reference ガード同梱）
4. Wave2 reconstruction: `d3fe768` → `27fe776` 非台帳部分（`202398f` は取り込まない）
   - unlock scope = frozen surface 上の 2 contract JSON のみ
   - non-ledger blob == approved `27fe776` を exact 照合
5. execution line 自身から manifest 再生成
6. **adopt 4-point equality（4 対象）** と **release-chain integrity（image-builder 含む全対象）** を
   別集合として判定
7. 5 target で `approved desired == live semantics == consumer ref` を 5/5 確認
8. fresh rebind #2 precheck（serial 201 以降の現在値・新 canonical SHA / HEAD / mapping SHA / token。
   旧 token と serial の再利用は禁止）
9. 🛑 **Production State Rebind #2 GO?**
