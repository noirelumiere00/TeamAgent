# Runbook: State Rebind（PR2-A0.4 — Terraform state binding の live 追従）

**正式契約**: `AWS managed application resources mutation = 0 / Terraform remote state mutation only`

実施順序・正本 tfvars・ZIP 正規化・復旧判断はこの runbook 内で完結する。
既存の台帳を無更新で再実行しないこと。`adopt` は supply-chain S3 ownership の移行専用であり、
旧 TD binding の修復は `state-rebind-precheck → state-rebind-apply` を使う。

## いつ使うか

```
approved release receipt == live（本番は正しい）
Terraform state の binding だけが旧 revision を指す
```

このとき **live を再デプロイして state へ合わせてはならない**（rolling restart は順序が逆）。
state の binding だけを live の exact task-definition revision へ付け替える。

同一アドレスの rebind は removed/import ブロックでは表現できない（removed は config 不在を、
import は config 存在を要求し矛盾する）。よって `state rm → 即 import` を guard 監督下の
唯一の経路として使う。**素の `terraform state rm` / `terraform import` は引き続き禁止。**

## 前提（順序厳守）

1. **PRODUCTION DEPLOYMENT FREEZE** を宣言してから mapping を確定する。freeze 対象:
   - ECS task definition の登録 / service の task-definition 更新
   - 承認済み 8 flag の変更
   - deployment pipeline / admin 手動デプロイ
   - taskdef ARN を参照する Lambda env の変更
   - rebind 対象 consumer の参照先変更
   （generation publisher freeze は継続。無関係な AWS 変更まで止める必要はない）
2. freeze 後に **registry の全8 consumer の live ARN を fresh に再解決**し、
   `infra/deploy/state_rebind_targets.json` の `targets` を確定して merge する。
   targets は state の TD binding が古い consumer だけ（全8件とは限らない）。
   調査時点の ARN を焼かない（2026-08-20 に調査中 `mcp:86` が増えた実例）。
3. 実行 session は可能なら **application write を絞った一時 session** を使う（下記）。

## 実行 session の制限（必須）

trusted automation role を assume する際に **session policy** で許可を絞る。
session policy は権限の交差にしか働かないため、ここに無い操作は role が許可していても拒否される。

**canonical policy は repo にコミットされた 1 ファイルだけ**:
`infra/deploy/state_rebind_session_policy.json`
（内容は `state_rebind.py` の least-privilege 契約と契約テストが exact に固定している。
手元コピーの改変や別 JSON の代用は禁止。）

```bash
# 1. 契約検証 + sha256 採取（evidence）
python3 infra/deploy/state_rebind.py validate-session-policy

# 2. assume-role（session 名は trust policy が固定値で要求）
aws sts assume-role \
  --role-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --role-session-name teamagent-terraform-worker \
  --policy file://infra/deploy/state_rebind_session_policy.json \
  --duration-seconds 3600
```

使用した session policy の JSON と SHA256 を evidence として out ディレクトリへ保存すること
（session policy の適用有無はサーバ側から検証できないため、evidence で補う）。

### 許可 read の由来（2026-08-21 実測。推測での追加は禁止）

| 区分 | action | 根拠 |
|---|---|---|
| v1 実績 | s3/dynamodb backend 6種 + ecs/events/lambda/sts/kms の検証読み 7種 | 初版 policy 下で rm・backend・consumer 検証が実際に使用 |
| 403 実測 | ec2:DescribeImages / ec2:DescribeVpcs / iam:GetUser / kms:ListAliases / secretsmanager:DescribeSecret | 読み取り列挙が不足した初版 policy 下で `terraform import` が AccessDenied で失敗した実ログ（rm 済み・import 未完の中間 state を作った事故） |
| 静的導出 | ec2:DescribeSubnets / ec2:DescribeRouteTables | `data.aws_vpc.default.id` に依存する第二波 data source。第一波の 403 で評価に到達しなかっただけで、config 上 import 時評価が確定している |

`terraform import` は対象 resource だけでなく **root module の全 data source を評価する**。
新しい data source を config に足したら、この policy と `state_rebind.py` の契約表の
両方を更新しない限り rebind は 403 で止まる（fail-closed）。

### 禁止事項（2026-08-21 ユーザー裁定）

- **`Allow *` + Deny 型（復旧時の暫定 v2）を本番 operation で再利用しないこと。**
  base role の「Deny に書き忘れた write」がそのまま残る形であり、成功実績として
  evidence に残っているだけの過去の形。契約テストが `Allow *` の混入を拒否する。
- 次の本番使用前に、可能なら read-only の dry-run で policy の十分性を再確認する
  （403 実測リストは「第一波で観測できた分」であり、config が変われば必要 read も変わる）。

## 実施手順（順序厳守）

### 0. ローカル準備と freeze

```bash
cd ~/dev/worktrees/teamagent-state-sync
umask 077
TFVARS="$HOME/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars"
SYNC_OUT="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-state-sync.XXXXXXXX")"
REBIND_OUT="$SYNC_OUT/rebind"
chmod 700 "$SYNC_OUT"
test -f "$TFVARS"
stat -f '%Lp %N' "$TFVARS"   # macOS: 600 を確認
git status --short             # 出力なし
git rev-parse HEAD             # 実施する reviewed commit を evidence に控える
```

正本 tfvars は上記の一つ。コピー版やこの worktree の暗黙 tfvars を使わない。
tfvars を読む guard 操作は必ず `--var-file "$TFVARS"`。snapshot/verify は
tfvars を受け取らず、saved plan の apply は plan に束縛されるため追加引数を付けない。
Terraform は既存の正規 S3 backend / default workspace に初期化済みの checkout を使う。
backend の初期化・credential のセットアップは既存 runbook の管理範囲とする。

**production deployment freeze** を宣言する。TD 登録、service 更新、Lambda code/env 更新、
EventBridge target・8 consumer の activation flag、pipeline/admin のデプロイを停止する。
以下の承認済み ZIP 正規化だけを例外とし、それ以外が動いたら snapshot/mapping/precheck を
採り直す。既存の activation IAM freeze 宣言とは別の運用停止である。

Go 1.25.8 のある実施環境で先に self-test を実行する。
生成先は `infra/terraform/build/tiktok_dispatch.zip` と
`infra/terraform/build/x_dispatch.zip`。archive provider 2.8.0 と同じ ZIP byte に揃えるため、
builder は Go 1.25.8 以外を拒否する。`GOTOOLCHAIN=local` で自動取得しない。

```bash
(cd infra/deploy && GOTOOLCHAIN=local GOCACHE="$SYNC_OUT/go-cache" go test \
  build_dispatcher_archive.go build_dispatcher_archive_test.go)
mkdir -p infra/terraform/build
for component in tiktok x; do
  # output が既存なら上書きせず停止。先に既存生成物を evidence へ退避してから再実行する。
  GOTOOLCHAIN=local GOCACHE="$SYNC_OUT/go-cache" go run \
    infra/deploy/build_dispatcher_archive.go \
    --source-file "infra/terraform/lambda/${component}_dispatch/handler.py" \
    --output "infra/terraform/build/${component}_dispatch.zip" \
    > "$SYNC_OUT/${component}-archive.json"
done
```

期待: Go test が PASS、各 JSON に `output_base64sha256`。Go 版不一致、fixture byte 不一致、
source 読込失敗、既存 output は停止条件。生成 ZIP は実施する同一 checkout の build に置く。

### 1. fresh live の採取と Lambda の packaging 正規化

通常の承認済み運用 session で採取する（この後の rebind 用 narrow session では full snapshot
に必要な全 read を許可していない）。出力は repo 外、0700/0600 で保全する。

```bash
bash infra/deploy/terraform_runtime_guard.sh snapshot \
  --evidence-json-out "$SYNC_OUT/live-before.json" > "$SYNC_OUT/live-before.hcl"
```

期待: live evidence と HCL が生成される。既存 live contract、account、取得権限の不一致は停止。
TikTok / X の両方について次を行う。X の hash が既に一致するなら更新を省略する。

```bash
# TikTok の例。X は FUNCTION=teamagent-dev-x-buzz-dispatch、component=x。
FUNCTION=teamagent-dev-tiktok-acquire-dispatch
component=tiktok
aws lambda get-function --region ap-northeast-1 --function-name "$FUNCTION" \
  > "$SYNC_OUT/${component}-function-before.json"
curl --fail --silent --show-error \
  --url "$(jq -er '.Code.Location' "$SYNC_OUT/${component}-function-before.json")" \
  --output "$SYNC_OUT/${component}-before.zip"
python3 - "$component" "$SYNC_OUT" <<'PY'
import base64, hashlib, json, pathlib, sys, zipfile
component, out = sys.argv[1], pathlib.Path(sys.argv[2])
payload = (out / f"{component}-before.zip").read_bytes()
before = json.loads((out / f"{component}-function-before.json").read_text())
assert base64.b64encode(hashlib.sha256(payload).digest()).decode() == before["Configuration"]["CodeSha256"]
with zipfile.ZipFile(out / f"{component}-before.zip") as archive:
    assert archive.namelist() == ["handler.py"], "unexpected live ZIP entries; stop"
    source = pathlib.Path(f"infra/terraform/lambda/{component}_dispatch/handler.py")
    assert archive.read("handler.py") == source.read_bytes(), "handler differs; stop"
print("live handler == reviewed source")
PY
```

JSON 内の presigned URL や state/env をチャット・git・通常ログへ貼らない。
変更前 ZIP の SHA256 が保存した `.Configuration.CodeSha256` と一致することも確認する。
これが戻し用 ZIP になる。source が一致し、生成 ZIP hash と live hash が違う場合だけ人が更新する。

```bash
aws lambda update-function-code --region ap-northeast-1 \
  --function-name "$FUNCTION" \
  --revision-id "$(jq -er '.Configuration.RevisionId' "$SYNC_OUT/${component}-function-before.json")" \
  --zip-file "fileb://$PWD/infra/terraform/build/${component}_dispatch.zip" \
  > "$SYNC_OUT/${component}-code-update.json"
aws lambda wait function-updated-v2 --region ap-northeast-1 --function-name "$FUNCTION"
aws lambda get-function-configuration --region ap-northeast-1 --function-name "$FUNCTION" \
  > "$SYNC_OUT/${component}-function-after.json"
jq -e --slurpfile archive "$SYNC_OUT/${component}-archive.json" \
  --slurpfile before "$SYNC_OUT/${component}-function-before.json" \
  '.LastUpdateStatus == "Successful" and .State == "Active" and
   .CodeSha256 == $archive[0].output_base64sha256 and
   .Environment == $before[0].Configuration.Environment' \
  "$SYNC_OUT/${component}-function-after.json"
```

期待: 更新または既存 hash 一致、wait 成功、jq が `true`。
RevisionId 競合、ZIP/CodeSha256 不一致、更新失敗、env/TASKDEF_ARN 等の予期しない変化は停止。
Lambda の正規化は application write であり、rebind 用 narrow session とは分ける。

```bash
bash infra/deploy/terraform_runtime_guard.sh snapshot \
  --evidence-json-out "$SYNC_OUT/live-normalized.json" > "$SYNC_OUT/live-normalized.hcl"
```

以降はこの snapshot が正本。snapshot は tfvars の代用品ではない。

### 2. mapping を確定して commit

freeze 後の live consumer 参照、ACTIVE TD 全文、approved release receipt を照合し、
`infra/deploy/state_rebind_targets.json` の frozen_at・description・必要な targets を更新する。
現在の台帳は **2026-09-15 の古い7件**であり、そのまま実行しない。
対応する `tests/scripts/test_state_rebind.py` の固定 mapping fixture も更新して commit する。

state の ownership/address は fresh `state list` と registry の両方で確認する。
特に OpenClaw は現在 registry/config が `aws_ecs_task_definition.openclaw[0]`、旧台帳は
無印。state に無印しかなければ、別途レビューされた address 移行が必要であり、推測で進めない。
既に正しい TD は mapping から除外する。X が古ければ対象に含める。

期待: 最新の approved live に一致する mapping、ローカル契約テスト成功、clean tree。
署名証拠がない便、8 consumer の欠落、source/address の不一致は停止。

### 3. narrow session と precheck（まだ state を書かない）

AssumeRole の呼出元は MFA 済みの `AIIAdev` 管理者 session に戻しておく。
role trust はこの IAM user と MFA を要求し、root や既存 automation role からの再 assume は使わない。

```bash
python3 infra/deploy/state_rebind.py validate-session-policy
python3 infra/deploy/state_rebind.py validate \
  --mapping infra/deploy/state_rebind_targets.json --require-targets
aws sts assume-role \
  --role-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --role-session-name teamagent-terraform-worker \
  --policy file://infra/deploy/state_rebind_session_policy.json \
  --duration-seconds 3600 > "$SYNC_OUT/rebind-session.json"
```

この短命 credential を次のように設定する（shell の `set -x` は無効にし、値を表示しない）。

```bash
set +x
export AWS_ACCESS_KEY_ID="$(jq -er '.Credentials.AccessKeyId' "$SYNC_OUT/rebind-session.json")"
export AWS_SECRET_ACCESS_KEY="$(jq -er '.Credentials.SecretAccessKey' "$SYNC_OUT/rebind-session.json")"
export AWS_SESSION_TOKEN="$(jq -er '.Credentials.SessionToken' "$SYNC_OUT/rebind-session.json")"
cp infra/deploy/state_rebind_session_policy.json "$SYNC_OUT/state_rebind_session_policy.json"
shasum -a 256 "$SYNC_OUT/state_rebind_session_policy.json" > "$SYNC_OUT/session-policy.sha256"
```

使用した canonical session policy と SHA256 を evidence に保全する。policy を広げるための
`Allow *` や手元代替 JSON は使わない。`bootstrap_runtime_session.sh` は一時 checkout を作り
この policy を渡さないため、この段では同一初期化済み checkout から guard を直接呼ぶ。

```bash
bash infra/deploy/terraform_runtime_guard.sh state-rebind-precheck --var-file "$TFVARS" --out "$REBIND_OUT"
```

precheck は activation 側の正本パス（所有者本人・非 symlink・0600）だけを受け付ける。
全対象の count 式を現在の構成から読み、同じ tfvars を指定した `terraform console` で評価する。
旧 state に残った instance の存在は判定に使わない。count が0、評価失敗、unknown、
未対応の for_each、構成にない address は停止する。console は plan を作らず state を読む。

期待: 正本 tfvars の絶対パスと SHA256、state backup、rebind-plan.tsv、alignment の旧値→新値、binding と専用 approval token、
`✅ rebind precheck 完了`。TD だけでなく consumer pointer と dispatcher hash の差分を人が確認する。
ZIP hash == live CodeSha256、handler == reviewed source もここで再検査される。
既に束縛済み TD、古い mapping、非 ACTIVE、state/resource 形状、ZIP/source/hash の不一致、
権限不足なら書込み前に停止する。

### 4. state のみ追従

```bash
bash infra/deploy/terraform_runtime_guard.sh state-rebind-apply \
  --out "$REBIND_OUT" --var-file "$TFVARS" \
  --approve 'I-HAVE-REVIEWED-THE-STATE-REBIND:<表示された binding SHA256 先頭16桁>'
```

token は precheck の出力をそのまま使う。正本 tfvars のパスと SHA256 も binding と token に束縛される。
異なるコピー・正本の内容変更は最初の rm より前に停止する。apply は承認済み SHA の private copy で
全対象の構成上の存在を再確認し、その同じ copy を全 import に使う。
`enable_connect_web=false` のとき connect-web[0] は存在しないため、rm を一度も行わず停止する。
mapping/commit/state/principal/alignment/ZIP が変わると
承認は無効。共有 deployment lock 下で TD を1件ずつ rm→即 import→比較し、参照先/hash の
限定 state 候補を検査して non-force push する。途中失敗なら次の対象へ進まない。
期待: 各 `state == live verified` / `✅ rebound`、最後に `✅ state rebind 完了`。
最終 state と限定変更の証拠を保全する。serial は進むため242や固定増分を成功条件にしない。

追加の証拠ファイルは `alignment-plan.json`、`state-before-alignment.json`、
`state-aligned-candidate.json`、`aligned-consumer-manifest.json`、
`state-immediately-before-push.json`、`state-final.json`。state 全文を含むため0600で保全する。

### 5. strict sync と完了判定

通常の full-read / intent 作成を許可した automation session に戻し、同じ reviewed checkout で実施する。

```bash
bash infra/deploy/terraform_runtime_guard.sh plan \
  --var-file "$TFVARS" --runtime-sync --out "$SYNC_OUT/sync.tfplan"
bash infra/deploy/terraform_runtime_guard.sh verify --plan "$SYNC_OUT/sync.tfplan"
terraform -chdir=infra/terraform show -json "$SYNC_OUT/sync.tfplan" > "$SYNC_OUT/sync-plan.json"
jq -e 'all(.resource_changes[]?;
  .mode == "data" or (.type | startswith("aws_") | not) or
  (.change.actions == ["no-op"] and .change.importing == null))' \
  "$SYNC_OUT/sync-plan.json"
```

期待: `✅ guarded plan`、`sync / strict-equality`、`✅ read-only検証完了`。
exact-8 の TD 全文・image・activation、および archive hash が新たな live と一致する。
**全 AWS managed resource が import なしの no-op の場合だけ完了**。sync 自体は TD replacement 等を許すため、
guard 成功だけで「本番無変更」とは判定しない。AWS の作成/更新/削除/import があれば apply せず停止し、
個別の通常変更としてレビューする。intent ID 等を含む `terraform_data` の control gate は
毎回差分を持ち得るため、既存 guard validator が認めた差分であることを別に確認する。
今回は saved plan を apply する必要はない。

通常 plan は refresh/state lock に加えて **production deployment intent を prepare する**。
read-only と扱わない（`prepare_image_deployment_intent`）。verify は読み取り検証。
source_code_hash/archive_file の不一致や flags/HMAC/IAM 等の他の drift が出たら、tfvars や
manifest の偽装・guard 緩和で通さない。

## 停止時・戻し方

- ZIP 正規化前、precheck のみなら application/state は未変更。証拠を保全して中止する。
- Lambda を戻す必要がある場合は、変更前 ZIP/hash と現在 RevisionId を再確認し、人が同じ
  update-function-code 手順で保存 ZIP を戻す。正規化前 hash へ戻るため同期も未完に戻る。
  以後の snapshot・mapping・token・plan・receipt は採り直す。
- TD import や限定 push の途中失敗は部分成功があり得る。freeze を維持し、最初の backup、
  state-after、候補、AWS describe、失敗出力を保全する。旧 token の再実行、全件 rm、
  `state push -force`、lock の強制解除はしない。
- 共有 lock は失敗時 TTL まで残る。human 裁定で backup 復元か失敗箇所の取り込みを選ぶ。
  backup 復元は同じ lineage の後続更新を上書きしないことを確認した別の管理操作であり、
  この手順に無条件のコピペ復旧コマンドは置かない。最新 state の保全と再承認が先。
- 部分成功後の mapping に既に束縛済み TD を残すと precheck が止まる。alignment だけ未完の
  状態も通常の再実行対象にせず、証拠を基にした復旧判断を行う。

## 証拠の保全

binding / rebind-plan.tsv / alignment-plan / per-address の describe と state 比較結果 /
session policy と SHA256 / 最終 state を repo 外の0700/0600で保全する。
freeze は activation 全体の完了まで解除しない。state/env/credential を git や通常ログへ貼らない。
