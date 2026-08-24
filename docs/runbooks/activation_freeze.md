# Runbook: Activation Freeze v2（機械強制版）

**裁定日**: 2026-08-24（ユーザー裁定）

## なぜ機械強制にしたか

generation publisher freeze v1（2026-08-20 18:15 JST 発効）は **2 度破られた**。

| 波 | 時刻 (JST) | 起きたこと |
|---|---|---|
| 1 | 08-20 19:48-19:49 | buildspec 3 objects publish + CodeBuild UpdateProject ×7（vulkan-loader ドリフト対応） |
| 2 | 08-21 16:16-16:18 | buildspec 3 objects publish + CodeBuild UpdateProject ×7（openssl CVE-2026-14456 対応） |

原因は「口頭の freeze 合意だけを hard safety control にしていたこと」。
dev merge freeze も同様に複数回破られている（KARTE / vulkan / CVE / mail）。

したがって安全境界を次のように移した:

```
❌ dev tip が静止していることに依存する
✅ activation-execution-base + approved commit allowlist + fast-forward only
✅ frozen surface の変更は CI で落とす（宣言なしには触れない）
```

## 3 つの宣言ファイル

| ファイル | 役割 |
|---|---|
| `infra/deploy/activation_freeze.json` | freeze 状態の唯一の宣言（v1 失効の根拠・v2 境界・unlock） |
| `infra/deploy/activation_execution_allowlist.json` | execution line の hard boundary（承認済み commit 列） |
| `infra/deploy/activation_freeze_check.py` | 判定のみ。AWS へはアクセスしない |

## Freeze v2 の境界の引き方（順序厳守）

**「最後に変更された時刻」を境界にしてはならない。** 破られた直後の時刻を境界にすると、
「止まっていない状態」を freeze と呼ぶことになる。

```
1. publisher / deployment entry point を実際に停止する
2. 他セッション・自動処理が走らない状態を確認する
3. CloudTrail / CodeBuild / S3 を fresh read し、publish 0 / UpdateProject 0 を確認
4. その瞬間を v2.started_at として記録し、state を active にする  ← 人間が行う
```

`state=active` にするには `v2.started_at` が必須（空なら checker が FATAL）。
逆に `state=pending_v2` のまま境界を書き込むのも矛盾として拒否される。

**現在の状態は `pending_v2`**（v1 失効・v2 未確定）。これは最も危険な期間なので、
`pending_v2` でも frozen surface は enforce される。

## frozen surface

- **18 generation inputs**: `infra/deploy/buildspec_generation_inputs.json` の `inputs` を
  **単一の真実源として参照**する（手書きリストは陳腐化して守れなくなるため置かない）
- **publisher 判定パス**: `additional_publisher_paths` に列挙

`terraform import` と同様、generation SHA は 18 inputs の内容から決まる。
どれか 1 つでも変わると新しい generation の publish が強制され、freeze が破れる。

### 意図的に frozen surface を変えたいとき

同じ PR 内で `activation_freeze.json` の `unlock` を宣言する:

```json
"unlock": {
  "active": true,
  "scope_paths": ["infra/codebuild/teamagent_runtime_contract.json"],
  "reason": "Generation Re-baseline v2 の approved input commit 取り込み",
  "gate": "human gate 2026-08-24"
}
```

- `scope_paths` は **実際に変更した frozen path と exact 一致**（過剰 unlock は拒否）
- `reason` と `gate`（human 承認の出所）が無ければ FATAL
- 宣言が diff に現れることで human gate が効く。unlock を作業後に消し忘れると
  「active なのに scope_paths が残っている」検査で落ちる

## execution line の hard boundary

```bash
python3 infra/deploy/activation_freeze_check.py assert-execution-line
```

検査内容:

1. `execution_base` が execution line の祖先である（履歴が作り直されていない）
2. `expected_head` が現 HEAD の祖先または一致（**force push / rebase の検出**）
3. `base..HEAD` の commit 列が `approved_commits` と **SHA も subject も exact 一致**
4. HEAD == `expected_head`

allowlist は **dev 側に置く**（execution line に置くと自己参照になる）。
SHA は 40 桁完全形のみ（短縮 SHA は衝突と取り違えを許すため禁止）。

`excluded_commits` には「取り込まない commit とその理由」を残す。特に:

- **`202398f`（台帳のみの再生成）の単独 cherry-pick は恒久禁止。**
  「execution line に存在しない入力の SHA を台帳だけが持つ」事故になる。
  台帳は execution line 自身の inputs から再生成する

## CI 配線

`.github/workflows/ci.yml` の `activation-freeze` job が全 PR で
`assert-frozen-surface --base <merge-base> --head HEAD` を実行する
（`fetch-depth: 0` が必須。shallow では merge-base が引けない）。

## AWS 側の persistent explicit-deny（Freeze v2 の本体）

repo 側の機械強制は **repo 経由の変更しか止められない**。AWS を直接叩く経路
（admin 手動 / 他セッション / 長期 credential）は素通りする。実際、repo lock を入れた後も
2026-08-21 に `RegisterTaskDefinition` ×10 + `UpdateService` ×4 + `PutTargets` ×4 が走り、
**state rebind 完了後に B3 を作り直した**。よって mutation できる principal 側へ
persistent な explicit Deny を置く（session policy は当該 session にしか効かないため
hard control にしない）。

実装: `infra/terraform/activation_freeze_policy.tf`
有効化: `var.activation_freeze_enabled = true`（既定 false）

### 対象 principal（census 由来・推測で広げない）

| 種別 | 対象 | 根拠 |
|---|---|---|
| user | AIIAdev | CloudTrail で UpdateProject / RegisterTaskDefinition / UpdateService / PutTargets / PutRule / DeregisterTaskDefinition を実行。simulate で 5/6 が allowed |
| role | runtime_automation | manage-a/b が ECS/events/lambda/codebuild の mutation を許可 |
| role | codebuild_launcher / approval_caller / openclaw_publisher / release_launcher / release_control_updater / image_deployment_gate / media_cutover_attestor / tiktok_build_launcher | StartBuild 経由の generation publish 経路（CloudTrail + repo policy census） |

### 🔴 Freeze v2 の正確な定義（root は break-glass 例外）

**「production mutation が機械的に不可能な状態」とは呼ばない**（2026-08-24 ユーザー裁定）:

```
Freeze v2 =
  enumerated non-root deployment principals が mechanical に deny される
  + root は explicit break-glass exception
  + freeze 期間中は root credential / session の使用を禁止（運用規律）
  + CloudTrail で root mutation = 0 を継続監視
```

root の残存リスクは**未解決のまま明示的に残す**。SCP 導入と root key の無効化 / 削除は
この activation のついでにはやらない（別スコープ）。

継続監視:

```bash
python3 infra/deploy/root_mutation_monitor.py --since <Freeze v2 境界の UTC 時刻>
```

CloudTrail API が失敗したら「0 件」ではなく **検査不能=違反扱い**で非ゼロ終了する
（ExpiredToken を空結果と誤読して偽 green を出した実害があるため）。

**root mutation ベースライン（2026-08-24 実測）**: 2026-07-01 以降で 64 件
（DeregisterTaskDefinition 40 / PutTargets 23 / PutRule 1、最新 2026-07-17T17:55 JST）。
**freeze v1 窓（08-20T09:15Z）以降は 0 件**。監視はこの 0 を維持しているかを見る。

### root は identity policy では止められない

root は **identity-based policy と permissions boundary をバイパス**する。
CloudTrail 実測でも root が `PutTargets` ×23 / `DeregisterTaskDefinition` ×40 を実行している。
root の封鎖には **SCP** が必要で、本 policy の射程外（別 human gate）。
root 静的キーの無効化も別タスクとして未了。**「freeze したから絶対に動かない」とは言えない。**

### 適用手順（production mutation。human gate 必須）

guard の boundary が `iam:PutRolePolicy` / `iam:AttachUserPolicy` を自己拒否するため、
guard 経由では適用できない。A0.2 と同じ **AIIAdev による saved targeted plan** を使う。

```bash
# 1. saved targeted plan（repo 外・0600）
terraform -chdir=infra/terraform plan -input=false \
  -var-file=<0600 tfvars> -var=activation_freeze_enabled=true \
  -target=aws_iam_policy.activation_freeze \
  -target=aws_iam_user_policy_attachment.activation_freeze_aiia_dev \
  -target=aws_iam_role_policy_attachment.activation_freeze \
  -out=/secure/path/freeze.tfplan

# 2. human review（IAM のみ・Deny statement の内容と attach 先を行単位で確認）
#    → 🛑 FREEZE POLICY APPLY GO を得る。review 後の再 plan は禁止

# 3. 保存済み plan のみを apply
terraform -chdir=infra/terraform apply /secure/path/freeze.tfplan
```

### Freeze v2 の発効判定（apply 後）

**「最後の変更時刻」を境界にしない。** 次を全て満たした時刻を `v2.started_at` に記録する:

```
simulate-principal-policy で各 principal の mutation が explicitDeny
in-flight build = 0
freeze 後の StartBuild / RegisterTaskDefinition / UpdateService /
PutTargets / UpdateFunctionConfiguration / UpdateProject /
generation PutObject が 0
```

確認できたら `activation_freeze.json` の `state` を `active` にし
`v2.started_at` を記録する（checker が state=active に started_at を必須化している）。

## 解除

`state` を `released` にするのは activation 全体（adopt 完了 + 検証 green）の後。
production deployment freeze の解除とは別判断。
