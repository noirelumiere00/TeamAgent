# ファイル

- [コンテナイメージとビルド](container-images-and-build.md) - Aico の 5 種のコンテナイメージ（MCP core・media worker・OpenClaw・TikTok・Hermes）の Dockerfile 構成と core/media runtime contract、CodeBuild による quarantine → attestor → promoter の署名付きビルド鎖、ECR scan 例外ゲートとローカル Trivy ゲート、immutable tag・digest 参照・provenance の決まり。
- [HMAC 鍵束とローテーション](hmac-keyring-and-rotation.md) - 朝ダイジェストのボタン（下書き・予定登録・確認済み）とレポート短縮リンクに使う HMAC 署名鍵を、hmac_keyring と hmac_durable_state が用途別・世代別に管理する仕組み。verifier-first ローテーションの固定期限、DynamoDB の耐久状態、Terraform の前提条件と hmac_rollout_gate の段階遷移をまとめる。
- [観測・利用記録・コスト管理](observability-and-cost.md) - structlog の JSON 出力と request_id、Sentry のスクラブ、usage_events（1 リクエスト 1 行の利用記録）と runtime_metrics、管理画面（ローカル dashboard と connect-web の /admin）、CloudWatch の metric filter・アラーム・ダッシュボード、合成カナリア、AWS Budgets / Cost Anomaly、ログの PII スキャンをまとめた運用ページ。
- [リリースゲートとデプロイ](release-gates-and-deploy.md) - 署名済みイメージを ECS へ反映する唯一の経路 terraform_runtime_guard.sh（saved plan・one-use intent・共有ロック・apply supervisor・post-apply probe）と、image release gate・buildspec 世代 publish・activation freeze の仕組み、tfvars スイッチや直接 taskdef 登録などの地雷。
- [Terraform 構成（ECS・スケジュール・Lambda）](terraform-layout.md) - infra/terraform が管理する ECS サービス（mcp・openclaw・connect-web）、EventBridge で起動するタスク（morning_digest・ingest・canary）と SQS 起動のワーカー（x_buzz・media）、Lambda（dispatch・janitor・reminder）、enable_*/use_*/*_rule_enabled スイッチ変数、VPC・RDS・踏み台の構成と、変更が runtime guard を通る理由。
