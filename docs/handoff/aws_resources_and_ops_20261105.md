# Aico（TeamAgent）の AWS リソースと運用の決まりごと（2026-11-05 時点の控え）

持ち主の作業メモ（Claude Code のメモリ）から、引き継ぎに要る部分だけを写した。日付付きの記述はその時点の事実で、今は変わっているものがある。**正本は `infra/terraform/` と `docs/v3.2/ops/`**。値が食い違ったら Terraform と AWS コンソールを信じる。

## アカウント
- AWS アカウント `718959508629`、メインリージョン `ap-northeast-1`。
- 持ち主の `default` プロファイルは root（使わない）。作業は IAM ユーザー `AIIAdev`＋MFA（`aiiadev` / `aiiadev-mfa` プロファイル、セッション 36 時間）。
- 社内 Mac からは会社プロキシの CA が要る: `AWS_CA_BUNDLE` / `SSL_CERT_FILE` に certifi の `cacert.pem` などを指定。
- `session-manager-plugin` は sudo 不可のため `~/.local/bin` に手で置いている。

## Bedrock
- 利用モデル: Claude Sonnet 4.6 / Claude Haiku 4.5。東京は `jp.anthropic.claude-sonnet-4-6` / `jp.anthropic.claude-haiku-4-5-20251001-v1:0`、バージニアは `us.anthropic.*`。
- Anthropic の Use Case フォーム未提出だと `ResourceNotFoundException: Model use case details have not been submitted`。アカウント単位で提出済みのはずだが、新しい IAM で初回に確認する。
- 予算: `TeamAgent-Bedrock-Monthly`（Bedrock、月 50 USD）と `TeamAgent-Server-Monthly`（RDS/EC2/S3 ほか、月 267 USD）。50/80/100% で通知。

## RDS PostgreSQL（東京）
- `teamagent-dev`（db.t4g.micro / 20GB / PostgreSQL 16 / pgvector 0.8.2）。DB 名・ユーザーとも `teamagent`。公開アクセス無し。
- パスワードは Secrets Manager `teamagent/dev/db_password`。
- 接続は踏み台 EC2（SSM Session Manager 専用、SSH 22 は閉）経由のポートフォワードで手元の psql から。**踏み台の中では Secrets Manager に届かない**（VPC エンドポイント／NAT 無し）ので、secret は手元で引いて `AWS-StartPortForwardingSessionToRemoteHost` で RDS を localhost に出す。migration の当て方は `scripts/aws/release/apply_00NN_*.sh` が実例。

## S3 / DynamoDB
- `teamagent-dev-raw-files`: 生ファイル・動画アーカイブのサムネ（`video-library/thumbs/`）。ライフサイクル設定あり。
- `teamagent-tfstate-718959508629`: Terraform state（versioning + KMS + public block）。AIチェッカーの state もここの `vct/` に移す計画。
- `teamagent-tflock`: tfstate ロック（PAY_PER_REQUEST）。
- `teamagent-dev-cost-usage`: Aico の費用台帳（Apify などの利用記録。11/02 時点で Apify の請求と合っていない）。

## ECS Fargate（クラスタ `teamagent-dev`）
- サービス: `openclaw`（Slack 受け口）、`mcp`（MCP Gateway :8787）、`connect-web`（connect.newstv.co.jp）、`ingest`（毎日 18:00 のシート同期→動画アーカイブ同期→サムネ→事例推定→URL 取り込み）、`morning-digest`、`canary`、`media`（動画処理のジョブ）。
- image の反映は便（`scripts/aws/release_mcp.sh` → `apply_mcp_tds.sh`、`scripts/aws/release/` に実例）。**タスク定義を CLI で直接登録するのは禁止**（CLAUDE.md B9。`tests/scripts/test_legacy_image_helpers.py` などの契約テストが `infra/` 配下のそういうシェルを弾く）。env の変更は Terraform の guarded apply（`-var-file`）。
- Terraform apply は guarded apply（`infra/deploy/terraform_runtime_guard.sh`）で、必ず `-var-file`。plain apply は過去にドリフトで事故。
- CodeBuild の起動は `require-teamagent-codebuild-launcher-role` で Aico 専用ロールに縛られている（統制側の設定）。

## 監視
- CloudWatch アラーム（RDS 空き容量・CPU、ワーカー CPU など）→ SNS `teamagent-dev-alarms`（持ち主と管理者メーリングリスト宛）。
- Sentry: `vectorinc.sentry.io` にプロジェクトがあり DSN は Secrets Manager `teamagent/dev/sentry_dsn` に入っているが、**11/05 時点の mcp / openclaw のタスク定義に `SENTRY_DSN` は入っていない**（実質未使用）。

## Slack App
- `TeamAgent Ver.2`（Socket Mode、Vector ワークスペース）。Bot token / App token は Secrets Manager `teamagent/dev/slack/bot_token` / `app_token`。
- 初期構築時に token がチャットに露出した記録があり、ローテーションは未実施（Reinstall App で再発行）。
- App の所有者は持ち主個人。引き継ぎ時に Collaborator を追加する。

## Google
- GCP プロジェクト `ntv-ai`（vectorinc.co.jp の Workspace）。OAuth クライアント「TeamAgent OAuth」（Drive + Gmail、`gmail.modify` は adapter 層で破壊的操作を拒否）。
- secret は Secrets Manager `teamagent/dev/google_oauth`。再取得は `scripts/get_google_refresh_token.py`、確認は `scripts/verify_google_oauth.py`。
- OAuth 同意画面は Internal（vectorinc.co.jp）なので社外アカウントではログイン試験ができない。

## 古い情報（今は使っていない）
- 2026-06 の常駐ワーカー EC2（systemd で Bot を動かしていた時代）と internal ALB `teamagent-connectweb-alb` は Fargate 化で役目を終えている。残骸が課金されていないか `aws ec2 describe-instances` と ALB 一覧で確認する価値あり。

## 追記 2026-11-09: MCP 便の実行者に engineer-arima を追加（AWS で直接変更・Terraform 未反映）

持ち主（小俣さん）の判断で、外部エンジニアの IAM ユーザー `engineer-arima` にも MCP 便を任せた。Aico の Terraform は正本が無く apply 禁止のため、以下は **AWS を直接変更**しており、`infra/terraform/` とずれている。正本を作り直すときに取り込むこと。

| 対象 | 変更 | Terraform 上の定義 |
|---|---|---|
| role `teamagent-dev-approval-caller` の信頼ポリシー | Sid `EngineerArimaMfaSession` を追加（MFA 必須・セッション名 `teamagent-approval-caller`） | `infra/terraform/mcp_approval.tf` |
| role `teamagent-dev-codebuild-launcher` の信頼ポリシー | Sid `EngineerArimaMfaSession` を追加（MFA 必須・セッション名 `teamagent-build-launcher`） | `infra/terraform/codebuild.tf`（buildspec 世代の入力。変更すると世代の再生成が要る） |
| user `engineer-arima` | 管理ポリシー `engineer-arima-stage1`（読み取り・Bedrock・踏み台・開発用秘密 2 件）と `engineer-arima-release`（タスク定義の登録、mcp/openclaw/connect-web の更新、定期実行 3 ルール、dispatch Lambda 2 つ、13 ロールの PassRole、証跡鍵の復号）。いずれも MFA 無しは全拒否 | 無し（Terraform 管理外） |

- 実行者の切り替えは `RELEASE_PROFILE` / `RELEASE_MCP_MFA_ARN` / `MFA_BASE_PROFILE` / `MFA_SERIAL` ほか（`scripts/aws/release/` の各スクリプト冒頭）。既定値は持ち主のまま。
- 任せていないもの: OpenClaw 便、buildspec 世代の公開、`terraform_runtime_guard.sh` による適用。
- **既知の抜け**: activation freeze の AWS 強制（現在 `declaration_only`）を戻すときは、強制の対象（role 9 + user 1）に `engineer-arima` も加えること。今のままだと強制を戻しても engineer-arima は対象外になる。
