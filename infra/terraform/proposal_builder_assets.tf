# ============================================================
# proposal_builder の資材（統合 FMT・アカウント DB）を暗号化する専用 KMS 鍵
# ============================================================
# 2026-09-15: 提案書生成（Gemini 併用）の点灯にあたり新設。
#
# なぜ専用鍵か:
#   - adapters/proposal_assets.py は S3 オブジェクトが `ServerSideEncryption=aws:kms` かつ
#     `SSEKMSKeyId == PROPOSAL_BUILDER_ASSETS_KMS_KEY_ARN` であることを **完全一致** で要求する
#     （固定 VersionId・SHA-256・サイズと合わせて「改変されていない資材」を fail-closed で担保）。
#   - 置き先バケット（raw-files）の既定は AES256 なので、資材だけ本鍵で SSE-KMS にする
#     （バケットの既定暗号化は変えない。`aws s3 cp --sse aws:kms --sse-kms-key-id <この鍵>`）。
#   - fargate.tf は `var.proposal_builder_assets_kms_key_arn` に対して MCP task role へ
#     kms:Decrypt（kms:ViaService=s3）を付ける。本鍵の ARN を tfvars に貼る。
#
# 運用順（壁47 の教訓「フラグ ON と資材準備を同時にやらない」）:
#   1. 本ファイルを apply（enable_proposal_builder は false のまま）→ 鍵 ARN を得る
#   2. 資材を S3 へ SSE-KMS で配置し VersionId を控える（Artifacts/aico-inputs-20260911/provision_proposal_assets.sh）
#   3. tfvars に資材 8 変数と鍵 ARN を入れ、enable_proposal_builder=true で plan → apply

resource "aws_kms_key" "proposal_builder_assets" {
  description             = "TeamAgent ${var.environment} — proposal_builder assets (integrated FMT / account DB) SSE-KMS"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRoot"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
    ]
  })

  tags = {
    Project     = var.project_name
    Environment = var.environment
    Purpose     = "proposal-builder-assets"
  }
}

resource "aws_kms_alias" "proposal_builder_assets" {
  name          = "alias/${var.project_name}-${var.environment}-proposal-builder-assets"
  target_key_id = aws_kms_key.proposal_builder_assets.key_id
}

output "proposal_builder_assets_kms_key_arn" {
  description = "tfvars の proposal_builder_assets_kms_key_arn に貼る値（資材の SSE-KMS 鍵）"
  value       = aws_kms_key.proposal_builder_assets.arn
}
