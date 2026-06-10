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

locals {
  ecr_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire untagged images after 14 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "openclaw" {
  repository = aws_ecr_repository.openclaw.name
  policy     = local.ecr_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "mcp" {
  repository = aws_ecr_repository.mcp.name
  policy     = local.ecr_lifecycle_policy
}

output "ecr_openclaw_url" {
  description = "OpenClaw overlay イメージの push 先"
  value       = aws_ecr_repository.openclaw.repository_url
}

output "ecr_mcp_url" {
  description = "TeamAgent-MCP イメージの push 先"
  value       = aws_ecr_repository.mcp.repository_url
}
