# ============================================================
# ECR — OpenClaw外殻 overlay / TeamAgent-MCP バックエンドのイメージ置き場（§H / M2）
# ============================================================
# build/push は本人操作（承認後）。digest pin で task def から参照する。

resource "aws_ecr_repository" "openclaw" {
  name                 = "${var.project_name}-openclaw"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

# OpenClaw core/media candidates are built, scanned, provenance-verified, and
# signed here. No production task definition is allowed to pull these repos.
resource "aws_ecr_repository" "openclaw_quarantine" {
  name                 = "${var.project_name}-openclaw-quarantine"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "openclaw_verified_candidates" {
  name                 = "${var.project_name}-openclaw-verified-candidates"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "openclaw_media" {
  name                 = "${var.project_name}-openclaw-media"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "openclaw_media_quarantine" {
  name                 = "${var.project_name}-openclaw-media-quarantine"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "openclaw_media_verified_candidates" {
  name                 = "${var.project_name}-openclaw-media-verified-candidates"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "mcp" {
  name                 = "${var.project_name}-mcp"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Build and scan candidates here. Nothing in ECS/EventBridge references this
# repository; only a digest that passed every gate is copied to aws_ecr_repository.mcp.
resource "aws_ecr_repository" "mcp_quarantine" {
  name                 = "${var.project_name}-mcp-quarantine"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "mcp_verified_candidates" {
  name                 = "${var.project_name}-mcp-verified-candidates"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

# TikTok release ECR remains declared with its task stack, but candidate
# storage and both lifecycle policies stay in this ECR-only file.
resource "aws_ecr_repository" "tiktok_acquire_quarantine" {
  count                = local.tk_enabled
  name                 = "${local.tk_name}-quarantine"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "tiktok_acquire_verified_candidates" {
  count                = local.tk_enabled
  name                 = "${local.tk_name}-verified-candidates"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

locals {
  # Existing production release repositories receive only active-/rollback-
  # tags. Verified candidates live in physically separate repositories so a
  # lifecycle rule can never expire a digest that also carries a protected
  # production tag.
  ecr_release_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire only untagged release artifacts after 365 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 365
      }
      action = { type = "expire" }
    }]
  })
  ecr_verified_candidate_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire verified candidates after 30 days"
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
  ecr_quarantine_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire all quarantined candidates after 2 days"
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 2
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "openclaw" {
  repository = aws_ecr_repository.openclaw.name
  policy     = local.ecr_release_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "openclaw_quarantine" {
  repository = aws_ecr_repository.openclaw_quarantine.name
  policy     = local.ecr_quarantine_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "openclaw_verified_candidates" {
  repository = aws_ecr_repository.openclaw_verified_candidates.name
  policy     = local.ecr_verified_candidate_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "openclaw_media" {
  repository = aws_ecr_repository.openclaw_media.name
  policy     = local.ecr_release_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "openclaw_media_quarantine" {
  repository = aws_ecr_repository.openclaw_media_quarantine.name
  policy     = local.ecr_quarantine_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "openclaw_media_verified_candidates" {
  repository = aws_ecr_repository.openclaw_media_verified_candidates.name
  policy     = local.ecr_verified_candidate_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "mcp" {
  repository = aws_ecr_repository.mcp.name
  policy     = local.ecr_release_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "mcp_quarantine" {
  repository = aws_ecr_repository.mcp_quarantine.name
  policy     = local.ecr_quarantine_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "mcp_verified_candidates" {
  repository = aws_ecr_repository.mcp_verified_candidates.name
  policy     = local.ecr_verified_candidate_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "tiktok_acquire" {
  count      = local.tk_enabled
  repository = aws_ecr_repository.tiktok_acquire[0].name
  policy     = local.ecr_release_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "tiktok_acquire_quarantine" {
  count      = local.tk_enabled
  repository = aws_ecr_repository.tiktok_acquire_quarantine[0].name
  policy     = local.ecr_quarantine_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "tiktok_acquire_verified_candidates" {
  count      = local.tk_enabled
  repository = aws_ecr_repository.tiktok_acquire_verified_candidates[0].name
  policy     = local.ecr_verified_candidate_lifecycle_policy
}

# The AWS-managed ECS execution policy permits pulling from any ECR repository.
# Attach this explicit deny to every production execution role so a retained
# rejected candidate cannot become a runtime pull path even if referenced by a
# task definition created outside this module.
data "aws_iam_policy_document" "deny_quarantine_runtime_pull" {
  statement {
    sid    = "DenyQuarantineRuntimePull"
    effect = "Deny"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.mcp_quarantine.arn,
      aws_ecr_repository.mcp_verified_candidates.arn,
      aws_ecr_repository.openclaw_quarantine.arn,
      aws_ecr_repository.openclaw_verified_candidates.arn,
      aws_ecr_repository.openclaw_media_quarantine.arn,
      aws_ecr_repository.openclaw_media_verified_candidates.arn,
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-dev-tiktok-acquire-quarantine",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-dev-tiktok-acquire-verified-candidates",
    ]
  }
}

resource "aws_iam_policy" "deny_quarantine_runtime_pull" {
  name   = "teamagent-dev-deny-quarantine-runtime-pull"
  policy = data.aws_iam_policy_document.deny_quarantine_runtime_pull.json
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_openclaw" {
  role       = aws_iam_role.ecs_execution_openclaw.name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_mcp" {
  role       = aws_iam_role.ecs_execution_mcp.name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_connect_web" {
  count      = var.enable_connect_web ? 1 : 0
  role       = aws_iam_role.ecs_execution_connect_web[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_ingest" {
  count      = var.enable_ingest_schedule ? 1 : 0
  role       = aws_iam_role.ecs_execution_ingest[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_canary" {
  count      = var.enable_canary_health ? 1 : 0
  role       = aws_iam_role.ecs_execution_canary[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_morning_digest" {
  count      = var.enable_morning_digest ? 1 : 0
  role       = aws_iam_role.ecs_execution_morning_digest[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_x_buzz" {
  count      = local.xr_enabled
  role       = aws_iam_role.x_buzz_exec[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

resource "aws_iam_role_policy_attachment" "deny_quarantine_pull_tiktok" {
  count      = local.tk_enabled
  role       = aws_iam_role.tiktok_exec[0].name
  policy_arn = aws_iam_policy.deny_quarantine_runtime_pull.arn
}

output "ecr_openclaw_url" {
  description = "OpenClaw overlay イメージの push 先"
  value       = aws_ecr_repository.openclaw.repository_url
}

output "ecr_openclaw_quarantine_url" {
  description = "OpenClaw core build/scan quarantine; never reference from a task definition"
  value       = aws_ecr_repository.openclaw_quarantine.repository_url
}

output "ecr_openclaw_verified_candidates_url" {
  description = "OpenClaw verified candidates; never reference from a task definition"
  value       = aws_ecr_repository.openclaw_verified_candidates.repository_url
}

output "ecr_openclaw_media_url" {
  description = "OpenClaw media release repository"
  value       = aws_ecr_repository.openclaw_media.repository_url
}

output "ecr_openclaw_media_quarantine_url" {
  description = "OpenClaw media build/scan quarantine; never reference from a task definition"
  value       = aws_ecr_repository.openclaw_media_quarantine.repository_url
}

output "ecr_openclaw_media_verified_candidates_url" {
  description = "OpenClaw media verified candidates; never reference from a task definition"
  value       = aws_ecr_repository.openclaw_media_verified_candidates.repository_url
}

output "ecr_mcp_url" {
  description = "TeamAgent-MCP イメージの push 先"
  value       = aws_ecr_repository.mcp.repository_url
}

output "ecr_mcp_quarantine_url" {
  description = "TeamAgent-MCP build/scan quarantine; never reference from a task definition"
  value       = aws_ecr_repository.mcp_quarantine.repository_url
}

output "ecr_mcp_verified_candidates_url" {
  description = "TeamAgent MCP verified candidates; never reference from a task definition"
  value       = aws_ecr_repository.mcp_verified_candidates.repository_url
}

output "ecr_tiktok_acquire_quarantine_url" {
  description = "TikTok build/scan quarantine; never reference from a task definition"
  value = (
    local.tk_enabled == 1 ? aws_ecr_repository.tiktok_acquire_quarantine[0].repository_url : null
  )
}

output "ecr_tiktok_acquire_verified_candidates_url" {
  description = "TikTok verified candidates; never reference from a task definition"
  value = (
    local.tk_enabled == 1
    ? aws_ecr_repository.tiktok_acquire_verified_candidates[0].repository_url
    : null
  )
}
