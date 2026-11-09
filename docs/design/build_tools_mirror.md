# GitHub Release ツール取得の 504 対策

2026-11-09。AWS / S3 / GitHub には接続せず、リポジトリの定義・運用記録・テストから調査した。
本変更をマージするだけでは稼働中の CodeBuild に反映されない。**持ち主が新 buildspec の公開、CodeBuild の参照更新、ミラー読み取り IAM の追加を別途行う必要がある。Terraform apply は行わない。**

## 対策

対象は `infra/codebuild/` の `image-attestor-buildspec.yml`、`openclaw-provenance-buildspec.yml`、`image-promoter-buildspec.yml`、`tiktok-buildspec.yml`。固定した版・URL・SHA256 は維持する。

各 GitHub Release ダウンロードを、証跡バケット `teamagent-dev-image-release-evidence` の `build-tools/<sha256>/<filename>` → GitHub の順にする。S3 読み取りには `--expected-bucket-owner 718959508629` を付け、取得元にかかわらず既存の SHA256 検証を通す。S3 の取得失敗は GitHub へフォールバックするため、IAM の追加前でも GitHub 側のリトライは利用できる。S3 から取得できたファイルの SHA 不一致は成功扱いにしない。

GitHub 側は `curl --retry 6 --retry-delay 20 --retry-all-errors --retry-max-time 600 --connect-timeout 10 --max-time 90` を共通指定する。GitHub 本体が 504 を返す場合も対象になる。S3 CLI の接続 / 読み取り timeout は 10 / 30 秒。版更新、ハッシュ検証の省略、未固定の別配布元への切り替えはしない。

## 世代の導出・公開・検査の所在

| 対象 | repo 内の導出 | AWS が使用する buildspec | 反映に必要な操作 |
|---|---|---|---|
| image-attestor | `codebuild.tf` の `image_attestor_buildspec_*` の `replace` 連鎖 → `sha256(local.image_attestor_buildspec)` | `codebuild-buildspecs/teamagent-dev-image-attestor/<sha>.yml` | 新しい本文を S3 に追加し、UpdateProject と関連 IAM の exact key を更新 |
| image-promoter | 同 `image_promoter_buildspec_*` → `sha256(local.image_promoter_buildspec)` | `codebuild-buildspecs/teamagent-dev-image-promoter/<sha>.yml` | 同上 |
| OpenClaw provenance builder | `aws_codebuild_project.openclaw_provenance.source.buildspec` の入れ子の `replace` | **インライン本文** | 置換済み本文を UpdateProject。S3 世代キーの更新ではない |
| TikTok builder | `tiktok_image_buildspec_*` の `replace` | **固定キー** `codebuild-buildspecs/<tiktok_project>.yml` | 同キーの新 version を公開。project の参照先は変わらない |

生の YAML ファイルの SHA256 と世代は異なる。helper・契約 JSON の base64、ファイル SHA、KMS key ARN などを埋め込んだ**最終本文の UTF-8 バイト列**の SHA256 が世代になる。`infra/terraform/codebuild.tf` 自体も入力 blob の監視対象なので、IAM だけの変更でも manifest の freshness 更新が要る。ただし本文に影響しない変更を理由に全プロジェクトの世代を変えてはならない。

`infra/deploy/buildspec_generation_inputs.json` は入力の git blob、評価 context、KMS ARN、4 プロジェクトの期待世代を持つ。今回、同 manifest の期待世代を変える対象は attestor / promoter。OpenClaw と TikTok はこの 4 プロジェクトの期待世代に含まれない。

- `infra/deploy/verify_generation_inputs.py` は worktree を `git hash-object --no-filters` で測り、manifest の入力 blob と違えば `STALE MANIFEST` で止める。未コミット変更も対象。
- `infra/terraform/supply_chain_adopt.tf` は世代台帳、`infra/deploy/supply_chain_adoptions.json` は adopt の exact mapping。新しい期待世代と整合させる。state に取り込み済みの世代は削除しない。
- `scripts/aws/release_mcp.sh` は発射前に freshness を検査し、`batch-get-projects` の `source.buildspec` を `infra/deploy/assert_published_generation.py` へ渡す。不一致なら段 1 の StartBuild 前に停止する。ローカルの期待世代だけ更新しても本番は進まない。
- `scripts/aws/release/driver_oc.sh` は OpenClaw の inline 本文に現在の契約 SHA が 2 回あるかを確認する。これは本文全体の世代一致検査ではないため、今回のダウンロード修正の反映は本文を読み戻して確認する必要がある。
- `tests/scripts/test_supply_chain_adopt.py` は入力 freshness、manifest / mapping / 台帳の整合を検査する。`test_release_mcp_generation_preflight.py` は MCP 発射前の世代照合を検査する。`tests/codebuild/` は各 buildspec / IAM / 契約の固定を検査する。

`supply_chain_adopt.tf` のコメントが参照する `infra/deploy/derive_buildspec_generations.py` は調査時点の tree に存在しなかったため、今回追加した。既存の `codebuild.tf` の replace 式をそのまま取り出し、manifest の KMS ARN を埋め、provider / backend のない一時ディレクトリで `terraform console` を評価する。`init`・AWS 認証・元モジュールの実行は不要。過去の `republish_oc_contract` / `apply_apklock.sh` などは tracked な公開コマンドとしては利用できない。

```bash
# ローカルの Terraform CLI が必要。通信せず、完成本文と世代 SHA を出す。
python3 infra/deploy/derive_buildspec_generations.py --output-dir /tmp/build-tools-buildspecs
python3 infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .
```

変更前 HEAD の source-publisher / attestor / promoter の 3 世代が既存 manifest と完全一致することを確認して導出方法を校正した。今回の新世代は attestor `447e6246505c4482e6888439124efe804e3e147c33c8c242c106f975d8969d12`、promoter `aa15b2ac2b8469cbf8363f39a706e369f2fccee82d630d7ae1f6f3775018784f`。source-publisher は不変。新規テストは完成本文からの SHA が manifest と一致することを検査する。これらは未公開の導出値で、live との一致を主張しない。

この導出コマンドの対象は上記 3 プロジェクト。OpenClaw は `codebuild.tf` の `aws_codebuild_project.openclaw_provenance.source.buildspec` の 5 段の置換（provenance / bundle contract / scan gate のファイル SHA と、OpenClaw の署名 / 証跡 KMS ARN）で inline 本文を作る。TikTok は同ファイルの `tiktok_image_buildspec_1` から `tiktok_image_buildspec` の置換（scan gate / ECR image resolver / source provenance / contract の base64 と、TikTok 署名 / 共通証跡 KMS ARN）で本文を作る。いずれも repo の Terraform を直接評価せず、この式だけを provider / backend のない一時モジュールへ取り出すか、列挙した固定 placeholder を同じファイル内容・manifest の ARN で置換する。未置換 placeholder がないことと YAML / shell 構文を確認してから公開する。

## 本番で効かせるまで

以下は**持ち主が後で実施する運用手順**。この変更では実行していない。

1. **開発者 / Claude:** 最新の `origin/dev` に変更を載せ、変更した本文から世代をオフライン再導出する。入力 blob、attestor / promoter の期待世代、adopt mapping、世代台帳、freeze 宣言と対応テストを整合させる。最終本文・SHA・置換に使った KMS ARN / 契約 SHA をレビュー対象として保存する。生 YAML のアップロードや live pin の写経で代用しない。
2. **持ち主:** 実行中の便や並行 publisher を止め、最新の project 設定・IAM・本文・S3 VersionId を採取して戻し先を記録する。repo の freeze は `state=active`、AWS attachment は `mode=declaration_only` で別軸。AWS 側の deny が再度 attached になっていれば、既存の窓の運用に従う。freeze 全体を `released` に変更して回避しない。
3. **持ち主（IAM 管理権限）:** 下記のミラー読み取り権限を追加する。attestor / promoter は新しい S3 buildspec キーを読む `s3:GetObject` と、その exact prefix に対する既存の `s3:ListBucket` 条件も同期する。元の権限・deny を丸ごと置換せず差分だけを加える。
4. **持ち主（証跡公開権限）:** 下記スクリプトでツールのミラーを満たす。GitHub が回復した時間帯に行う。ミラー作成は buildspec の反映とは別作業。
5. **持ち主（S3 公開 / CodeBuild 管理権限）:** レンダリング済み attestor / promoter 本文を、本文の SHA と一致する新キーに追加する。SSE-KMS は `image_release_evidence` の鍵、Object Lock は GOVERNANCE と既存 buildspec 世代の retention 方針を使う。存在する世代は上書きしない。S3 の本文 SHA、キー SHA、VersionId を保存する。OpenClaw は inline の置換済み本文を用意し、TikTok は固定キーに新 version を追加する。TikTok も旧 version を保管する。
6. **持ち主（`codebuild:UpdateProject`、必要時 `iam:PassRole`）:** attestor / promoter の `source.buildspec` を新キーへ変更し、project environment の `IMAGE_ATTESTOR_BUILDSPEC_{KEY,SHA256}` / `IMAGE_PROMOTER_BUILDSPEC_{KEY,SHA256}` も同じ世代へ更新する。OpenClaw は inline 本文を差し替える。既存の source type、接続、ロール、image、環境変数を保存した設定に対して必要箇所だけ変更する。`StartBuild --buildspec-override` は既存の launcher 制約を外すため使わない。
7. **持ち主:** 新規取得した本文と `batch-get-projects` で、repo 導出 SHA = S3 body SHA = key SHA = current ref を確認する。OpenClaw は inline 本文をバイト単位で照合し、TikTok は固定キーの最新本文と VersionId を確認する。MCP は通常の世代プリフライトで再照合する。契約 SHA 自体は今回変更しないので launcher IAM の契約 SHA 条件の値は据え置く。別の契約更新を同時に反映する場合は、そちらの契約 SHA 条件も更新する。
8. **持ち主:** 通常の便を実行し、S3 取得 / GitHub fallback / SHA 検証のログを確認する。apply 禁止が解除され正本を再構築した後、今回の IAM / project / S3 変更を Terraform と state に取り込む。

**Terraform apply は不要であり、現在は禁止されている。** [AWS 引き継ぎメモ](../handoff/aws_resources_and_ops_20261105.md) の 2026-11-09 追記は正本不在と apply 禁止を明記している。[Supply-Chain Adopt runbook](../runbooks/supply_chain_adopt.md) も、publish は admin CLI、Terraform は公開済み artifact を state に取り込む役割と分けている。さらに同 runbook §3 は import-only mapping 未対応を既知の課題としている。従って今回、guarded apply / adopt-apply を反映の前提にしてはならない。

`engineer-arima` に委任されているのは MCP 便の実行で、世代公開・OpenClaw 便・guarded apply は委任されていない。公開と IAM / project 更新は持ち主（AIIAdev + MFA）または明示的に権限を付与された管理者が行う。既定 `default` プロファイルは root のため使わない。AWS に接続していないため、現在の実権限・追加 deny・live への反映済み状態は未確認。

## ミラー読み取り IAM

リポジトリ上の変更前の権限は次のとおり。S3 上にファイルを置くだけでは利用できない。

| CodeBuild role 定義 | `s3:GetObject` on `build-tools/*` | evidence KMS 鍵の復号 |
|---|---|---|
| `aws_iam_role.image_attestor` | 不足。既存 GetObject は buildspec exact key / source 証跡などに限定 | 既存あり |
| `aws_iam_role.image_promoter` | 不足。同上 | 既存あり |
| `aws_iam_role.tiktok_codebuild` | 不足。buildspec exact key / TikTok source manifest に限定 | 既存あり |
| `aws_iam_role.openclaw_codebuild` | 不足。別の `openclaw_build_evidence/source-manifests/*` のみ | **不足**。既存の復号対象は別の OpenClaw evidence 鍵 |

`infra/terraform/codebuild.tf` に今回記録した差分を持ち主が IAM に反映する。4 ロールに `s3:GetObject` を `arn:aws:s3:::teamagent-dev-image-release-evidence/build-tools/*` だけ許可し、OpenClaw には当該 evidence 鍵の `kms:Decrypt` を追加する。ミラー取得は exact object の `GetObject` なので `build-tools/` の一覧権限・書き込み権限は不要。bucket policy の Delete deny、TLS 要求、既存の署名権限分離は維持する。

ミラーを作る管理者には exact prefix の `s3:GetObject`（HeadObject にも必要）、`s3:PutObject`、Object Lock 設定に必要な `s3:PutObjectRetention`、KMS の `kms:GenerateDataKey` などが必要。404 と 403 を判別するため、実行スクリプト側の caller は必要な `s3:ListBucket` も持つ。権限不足の 403 を「未作成」として処理しない。ツール用の GetObject を許可するために CodeBuild へミラー書き込み権限を渡してはならない。

## ミラーを満たす手順

`scripts/aws/release/mirror_build_tools.sh` は同階層の Python 実装を呼び、4 buildspec と OpenClaw が参照する tooling lock から固定版・URL・SHA を読んで重複をまとめる。別の版一覧を手書きで維持しない。

```bash
# 後日、持ち主が計画を確認する。既定はネットワークに接続しない計画出力。
AWS_PROFILE=aiiadev-mfa scripts/aws/release/mirror_build_tools.sh

# 後日、持ち主が MFA と権限を確認して実行する。
AWS_PROFILE=aiiadev-mfa scripts/aws/release/mirror_build_tools.sh --apply
```

bucket / owner / region は上記の固定値。KMS ARN は `infra/deploy/buildspec_generation_inputs.json` の `kms_keys.image_release_evidence` から読む。apply 時だけ S3 の存在を確認し、既存 object は何もせずスキップ、404 のみ GitHub から取得する。403 やその他の AWS エラーは停止する。ダウンロードを SHA256 検証してから、SSE-KMS / GOVERNANCE（10 年）で content-addressed key に追加する。`If-None-Match: *` を付け、並行実行でも既存キーを上書きしない。このスクリプトは buildspec 公開・UpdateProject・IAM 更新を行わない。

## 戻し方と残るリスク

- attestor / promoter は保存した旧 immutable key に project の source / 環境変数 / exact read IAM を一緒に戻す。repo の manifest / mapping / 台帳も対応する本文へ整合させる。新世代を S3 から削除する必要はない。
- OpenClaw は保存した旧 inline 本文へ戻す。TikTok は旧 VersionId の本文を取り出し、固定キーの**新 version**として再公開する。Object Lock された version の削除はしない。
- ミラー IAM を戻す場合は今回追加した権限だけを外す。ミラー object は保持し、S3 fallback により GitHub retry を使う。旧 buildspec に戻すと今回の retry は失われる。
- リトライは数十分〜1 時間の GitHub 障害を解消しない。retry の予算は 600 秒、各試行の上限は 90 秒（最後の試行が予算を超えて終わる場合がある）。複数ツールでは待ちが積み上がる。継続障害時はミラーの事前充填が必要。
- ミラー未充填 / IAM 不足 / KMS 不足は GitHub fallback となり、ミラーの効果が出ない。fallback のログを運用で確認する。SHA256 固定が内容の取り違えを検出するが、GitHub 停止と S3 停止の同時発生は防げない。
- 既存 object はスキップする。破損したミラーは build 時の SHA 検証で停止するので、管理者が VersionId と本文を調べてから訂正する。Object Lock は同一キーへの新 version 作成まで禁じるものではないため、conditional put と公開者の権限分離を維持する。
- ツール本体以外の依存（例: Trivy DB、ベース image、パッケージレジストリ）の障害は今回の対象外。
- この作業環境では共有 `.git` が書き込み許可範囲外にあり、`origin/dev` への fast-forward は拒否された。作業開始 HEAD は `c9699`、ローカル `origin/dev` は `27d`（6 commits 先）。持ち主が最新 dev に変更を載せ直し、世代を最終 tree から再導出してから公開する。

関連テストは 424 件を確認し、初回の失敗修正後の再実行 332 件もすべて成功。変更した Python の ruff、4 buildspec の YAML 読み取り / 全 phase の `bash -n`、新 shell の `bash -n`、`git diff --check` は通過。ミラーのテストは関数と模擬クライアントだけを使い、CLI は実行していない。shellcheck は未インストール。`terraform fmt -check codebuild.tf` は変更前から存在する 421 行付近の 2 行の整形差分で失敗し、範囲外として維持した。pytest 前の他プロセス数は `ps` / `pgrep` が sandbox に拒否され未確認（対象テストは 1 プロセスで順次実行）。

AWS の変更、ミラーの dry-run / apply、実配信、commit / push はこの作業では実施していない。
