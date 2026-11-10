# mcp 便の道具

Mac / WSL の bash 3.2 以上で使う。Git、GitHub CLI（`gh auth login` 済み）、AWS CLI v2、Python 3.11 以上（certifi）、uv、jq が必要。Docker は任意（無ければ APK の実ビルド確認を省略し、索引確認は行う）。読み取り・MFA 付き書き込みの AWS プロファイルを設定しておく。

repo ルートで実行する。プロファイル名は自分の設定に合わせる。CA は未指定なら `python3` の certifi を使い、社内 CA が必要ならその証明書バンドルを指定する。

```bash
export AWS_PROFILE=aiiadev                  # 読み取り（外部実行者は自分の MFA 付きプロファイルを指定）
export RELEASE_PROFILE=aiiadev-mfa          # 書き込み・MFA セッションの保存先
export MFA_BASE_PROFILE=engineer-arima      # 長期キーを設定したプロファイル名
export MFA_SERIAL=arn:aws:iam::718959508629:mfa/iCloudOTP
# export AWS_CA_BUNDLE=/path/to/ca-bundle.pem
# export RELEASE_ACTOR_EMAIL=...            # 未指定なら git config user.email
# export RELEASE_WORK_DIR="$HOME/.teamagent-release"
```

1. PR をレビューして dev にマージする。担当者間で今から撃つ便を共有する。
2. `bash scripts/aws/release/mfa_refresh.sh` で6桁の MFA を入力する（既存スクリプトを共用。コードをファイルに残さない）。読み取りプロファイルにも MFA が必要なら、更新先の `RELEASE_PROFILE` と同じ名前を `AWS_PROFILE` に指定する。
3. `bash scripts/aws/release/kit/deploy_mcp_gate.sh <PR> -- rNNN "<説明>" <確認するファイル> <確認する文字列>` を実行する。複数 PR は空白区切り。ファイルは repo 相対パス、文字列は固定文字列として検査する。
4. 発射 → 承認 → 候補と本番の答え試験比較 → mcp・consumer・media の差し替えが完了したら、DM で実機確認する。mcp の再起動に伴う OpenClaw の再デプロイも現行台本の一部。

発射台は `$RELEASE_WORK_DIR/teamagent-build` に dev を clone して作る。入口が uv で必要な依存を準備する（既存の発射台なら `RELEASE_PAD` で指定可）。ログ・便 JSON・承認用ファイルは `$RELEASE_WORK_DIR/rNNN/`、一時ファイルは同 `tmp/`。このディレクトリには本番の設定値が入るため共有・コミットしない。

開始時と発射直前に CodeBuild 5段（approval-publisher / mcp-source-publisher / image-builder / image-attestor / image-promoter）の `IN_PROGRESS` と、mcp / openclaw / connect-web の PRIMARY 以外の deployment を読む。ビルド ID と initiator、または差し替え中のサービスを表示して止まる。照会失敗も停止する。ECS だけが動いている場合は実行者を判別できないため担当者に確認する。読み取り確認にはロックがなく、確認後に同時開始する競合は防げない。担当者同士の連絡は必要。

停止時は便ディレクトリの `fire.log` / `gate.log` / `apply.log` の `FATAL` と末尾を見る。既存の便ディレクトリがあれば再発射しない。同じコミットの撃ち直しは昇格タグが衝突する。答え試験の失敗を無視して apply しない。世代不一致・APK 在庫不一致・途中再開は持ち主へログを渡して確認する。単独取込が動いている場合も、勝手に停止せず完了を待つ。

`apply_mcp_tds.sh` とその補助台本は、持ち主が運用している現行の経路。CLI でタスク定義を登録・差し替える。Terraform に正本が無く apply 禁止のため、規則 B9 の検査（`tests/scripts/test_legacy_image_helpers.py`）はこの kit の 5 本だけを名指しで例外にしている。

`releases/template.json` は r99 の確認項目・digest・版固定を残した雛形。履歴上の前提確認を落とさないため固定値を保持し、便名・説明は空にした。入口が便名・説明・現行 `OLD_CORE`・今回の確認を設定する。JSON はコメントを許さないので固定値の理由はここに記載する。consumer manifest の雛形も現行台本から移し、個人情報の env 値は現行 family から実行時に読む。免除 gate の期限は **2026-12-22 UTC**。期限・buildspec 世代・ベース digest が変わったら、持ち主に雛形の更新を依頼する。

やってはいけないこと: `terraform apply`、OpenClaw 便、buildspec 世代の公開（いずれも持ち主だけ）。Sonnet は使わない。
