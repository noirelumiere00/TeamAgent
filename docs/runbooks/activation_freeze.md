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

## AWS 側の deny について

publisher 経路を AWS の IAM Deny で塞ぐのは **それ自体が production IAM mutation** なので、
別の human gate を経てから行う。本 runbook の範囲は repo 側の機械強制まで。

## 解除

`state` を `released` にするのは activation 全体（adopt 完了 + 検証 green）の後。
production deployment freeze の解除とは別判断。
