# Runbook: Supply-Chain Adopt（content-addressed buildspec の世代 adopt）

対象: `terraform_runtime_guard.sh` の `adopt-plan` / `adopt-apply` モードを使った Terraform state の adopt 操作。

adopt は既存の sync / runtime migration / activation とは**完全に独立した経路**で、それらの validator・allowlist には一切関与しない（許可範囲は sync より狭い）。terraform を実行してよいのは guard だけ、というリポジトリの契約に従い、adopt も guard 内のモードとして実装している。判定ロジックは `supply_chain_adopt_validate.py`（fail-closed）と `supply_chain_adopt_integrity.py`（S3 実体の不変性検査）が持つ。

---

## 1. なぜこれが必要か

evidence バケット上の buildspec は content-addressed key（`codebuild-buildspecs/<project>/<body の sha256>.yml`）で置かれ、Object Lock GOVERNANCE(2099-12-31) と bucket policy の Delete 無条件 Deny で不変化されている。さらに `runtime_evidence.tf` の `DenyReleaseEvidenceObjectMutation` により、apply を実行できる唯一の principal（`terraform-runtime-automation`）はこのバケットへ `s3:PutObject` できない。

つまり **Terraform はこれらのオブジェクトを作れない**。新世代は admin が publish し、Terraform は「既に存在する不変 artifact を state に記録する（adopt）」役割に徹する。

従来は単一リソースの `key` を `sha256(body)` から導出していたため、body の入力（契約 JSON・helper スクリプト）が変わるたびに `key` が変わり、`aws_s3_object.key` は ForceNew なので replacement 判定になった。しかし `prevent_destroy` が plan 段階で停止するため、**dev HEAD 自体が apply 不能**になっていた（2026-08-17 の契約更新以降、4 本で発生）。

実測した状態は次のとおりで、**壊れていたのは tfstate だけ**だった。

| 層 | 状態 |
|---|---|
| S3 実体 | ✅ 新世代が publish 済み・世代は削除されず堆積（事実上 append-only） |
| CodeBuild プロジェクト | ✅ 新 key を参照済み |
| Terraform config | ✅ 新 key を要求 |
| **Terraform state** | ❌ 旧 key のまま取り残されていた |

## 2. 新モデル（hash-keyed append-only generation）

世代を key（= body の sha256）で持つ台帳（`infra/terraform/supply_chain_adopt.tf` の `locals`）にし、`for_each` で管理する。既存エントリは削除しない（削除しようとすると `prevent_destroy` が停止させる）。

`aws_s3_object` に `content` は持たせない。持たせると import 直後に PutObject を伴う update が planned され、Object Lock 済みの実体を書き換えてしまうため。代わりに content-addressed 性を次の二重で担保する。

1. Terraform の `check` ブロック — Terraform が保持する body の sha256 が台帳に登録済みであること
2. `supply_chain_adopt_integrity.py` — S3 body の SHA256 が key に埋まった sha256 と一致すること

`ignore_changes` は使わない（実測で不要と確認済み）。ただし各世代の `content_type` / `object_lock_retain_until_date` は**実体の値**を書く。既存の単一リソース定義は `text/yaml` / `23:59:59` を宣言していたが、admin が publish した実体は `binary/octet-stream` / `00:00:00` で食い違っていた。実体に合わせないと import 直後に差分が出る。

⚠️ `object_lock_mode` / `object_lock_retain_until_date` を config に書き忘れると、Terraform はそれらを `null` にしようとする（＝ Object Lock の弱体化）。契約テストで固定しているが、新しい世代を足すときも必ず両方を書くこと。

## 3. 通常運用: 新しい世代を追加する手順

```
1. buildspec の入力（契約 JSON / helper など）を変更し、レビュー・マージする
2. admin が新しい body を evidence バケットへ publish する
     key = codebuild-buildspecs/<project>/<新 body の sha256>.yml
     content_type / SSE / KMS / bucket_key / Object Lock を既存世代と揃える
3. infra/terraform/supply_chain_adopt.tf の該当 generations マップへ 1 エントリ追記
4. infra/deploy/supply_chain_adoptions.json へ adopt 対象として追記
     old_address は無し（初回移行済みのため）。新規世代の import のみ
5. 本 runbook の §4 の adopt 手順を実行する
```

**既存エントリは絶対に削除しない。** 世代は積み上げるだけ。

## 4. adopt の実行手順

### 4-1. plan（read-only。state backup と ownership discovery を含む）

```bash
bash infra/deploy/terraform_runtime_guard.sh adopt-plan \
  --var-file infra/terraform/terraform.tfvars \
  --out /secure/path/adopt-$(date +%Y%m%d-%H%M%S)
```

**`--out` は必ず repository の外を指定する。** repository 配下（相対パス・`../` 経由・
repo 内へ戻る symlink を含む）を指定した場合、guard は plan を作る前に FATAL で停止する。
理由は 2 つある。

- 生成物（`adopt.tfplan` / `adopt-plan.json` / `adopt-binding.json` / `state-backup.json` /
  `state-list.txt` / `integrity-*.json`）は untracked file として working tree を dirty にし、
  apply 時の binding 照合（`git_tree_clean`）が**必ず**失敗する。
- `state-backup.json` は Terraform state の全文であり、機微な値を含む。repository へ持ち込まない。

出力ディレクトリと成果物は owner-only（ディレクトリ 700 / ファイル 600）で作成される。

このコマンドは順に次を行う。1 つでも失敗したら中断する。

1. `terraform state pull` で state を退避（perm 600）
2. ownership discovery — 旧アドレスが state に**存在し**、新アドレスが state に**存在しない**こと
3. adopt 前の S3 integrity snapshot（body SHA256 と Object Lock の precondition を含む）
4. `terraform plan -out`（保存 plan）
5. adopt plan validation（fail-closed。mapping 外・create・delete・replace を全拒否）

### 4-2. apply（明示の承認が必要）

承認トークンは **plan に束縛されている**。固定文字列ではなく、その plan の SHA256 を
連結した次の書式でなければ `check_approval()` が拒否する
（実装: `infra/deploy/supply_chain_adopt_binding.py` の `expected_approval()`）。

```
I-HAVE-REVIEWED-THE-ADOPT-PLAN:<plan_sha256 の先頭16文字>
```

`plan_sha256` の入手方法は 2 通り。どちらも同じ値になる。

- `adopt-plan` が完了時に標準出力へ出す（`承認は plan SHA256 に束縛されます: ...`）
- `<out_dir>/adopt-binding.json` の `plan_sha256` フィールド

```bash
PLAN_SHA256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  /secure/path/adopt-.../adopt-binding.json)"

bash infra/deploy/terraform_runtime_guard.sh adopt-apply --out /secure/path/adopt-... \
  --approve "I-HAVE-REVIEWED-THE-ADOPT-PLAN:${PLAN_SHA256:0:16}"
```

書式の worked example（この値は contract test が実装から再計算して照合している。
手で書き換えないこと）。

<!-- approval-token-contract
plan_sha256: 9842a7b6f7f24f5924bfbb2da4be3222d1c9e5c8b2bfdef222fb105d80eec6e3
approve:     I-HAVE-REVIEWED-THE-ADOPT-PLAN:9842a7b6f7f24f59
-->

| 入力 | 結果 |
| --- | --- |
| 上記書式（この plan の SHA256 先頭16文字を連結） | 受理 |
| `:` 以降が無い裸のトークン | **拒否**（束縛されていない） |
| 別 plan の SHA256 を連結したトークン | **拒否**（承認の流用不可） |
| `--approve` 未指定 | **拒否** |

plan を取り直すと `plan_sha256` が変わるため、**承認トークンも取り直しになる**。


apply は直前に validation と integrity を再実行し、apply 後にも integrity を再取得して比較する。
比較項目は VersionId / ETag / ContentLength / LastModified / ObjectLockMode / retain-until / body SHA256 で、**1 項目でも変化したら activation failure** として扱う。

### 4-3. post-activation の確認

adopt 後、通常の guarded plan が clean になることを確認する。

```bash
bash infra/deploy/terraform_runtime_guard.sh plan --var-file ... --out ...
```

## 5. 禁止事項

- `prevent_destroy` の解除・緩和（`removed { destroy = false }` は解除ではないので可）
- Object Lock / retain-until / bucket policy の Delete Deny の緩和
- 既存 sync / runtime migration / activation の allowlist・validator の変更
- `aws_s3_object` に `content` を持たせること
- raw な state 直接操作（手動での取り込み・除去・対象限定フラグ・無制限 apply）
- mapping（`supply_chain_adoptions.json`）に列挙されていない対象への操作
- 世代台帳からのエントリ削除

## 6. 想定される失敗と対処

| 症状 | 意味 | 対処 |
|---|---|---|
| `ownership discovery 失敗: 新アドレスが既に state にあります` | 既に adopt 済み | 二重実行。plan からやり直す必要はない |
| `body sha256 ... != content-addressed key sha256` | S3 実体が key と矛盾している | **adopt を中止**。publish 手順を調査する |
| `object lock mode is ... expected GOVERNANCE` | Object Lock が弱まっている | **adopt を中止**。バケット設定を調査する |
| `adopt plan が不変条件を満たしません` | plan に adopt 以外の変更が混ざっている | plan を破棄。tfvars と worktree の状態を確認する |
| `plan 作成後に S3 実体が変化しました` | 並行して publish が走った | plan からやり直す |
| `adopt により AWS 実体が変化しました` | **想定外**。adopt が実体を書き換えた | 直ちに停止し `state-backup.json` を保全して調査する |

## 7. ロールバック

adopt は AWS 実体を変更しないため、ロールバックは state のみが対象になる。
`plan` が退避した `state-backup.json` が復旧の起点。復旧が必要な場合は、raw な state 操作を単独で行わず、本 runbook の管理者と手順を決めてから実施すること。
