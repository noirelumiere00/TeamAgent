# One-shot command override on the existing ingest task; no service or schedule.
variable "enable_slack_docs_to_drive" {
  description = "Allow the ingest task to run the separately gated Slack → Drive migration"
  type        = bool
  default     = false
}

variable "slack_docs_to_drive_google_oauth_secret_arn" {
  description = "Dedicated migration OAuth secret ARN (client_id/client_secret/refresh_token); never a secret value"
  type        = string
  default     = ""
}

resource "aws_iam_role_policy" "slack_docs_to_drive" {
  count = var.enable_slack_docs_to_drive && var.enable_ingest_schedule ? 1 : 0
  name  = "slack-docs-to-drive"
  role  = aws_iam_role.ingest_task[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.slack_docs_to_drive_google_oauth_secret_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.raw_files.arn}/slack2drive/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.raw_files.arn}/slack2drive/progress/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = ["${aws_s3_bucket.raw_files.arn}/slack2drive/progress/transfer.lock"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.raw_files.arn
        Condition = {
          StringLike = { "s3:prefix" = ["slack2drive/progress/*"] }
        }
      }
    ]
  })

  lifecycle {
    precondition {
      condition     = can(regex("^arn:aws:secretsmanager:[^:]+:[0-9]+:secret:", var.slack_docs_to_drive_google_oauth_secret_arn))
      error_message = "A dedicated migration OAuth secret ARN is required."
    }
  }
}
