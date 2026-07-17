# ============================================================
# OpenClaw single-writer persistent state
# ============================================================
# `/tmp` itself remains a fresh Fargate volume. Only the state subtree is
# persisted through an encrypted EFS access point whose POSIX identity matches
# the image/runtime contract (65532:65532). The service deployment policy keeps
# at most one writer.

resource "aws_efs_file_system" "openclaw_state" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-openclaw-state"
  }
}

resource "aws_efs_access_point" "openclaw_state" {
  file_system_id = aws_efs_file_system.openclaw_state.id

  posix_user {
    uid = 65532
    gid = 65532
  }

  root_directory {
    path = "/teamagent-openclaw/state"

    creation_info {
      owner_uid   = 65532
      owner_gid   = 65532
      permissions = "0700"
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-openclaw-state"
  }
}

resource "aws_security_group" "openclaw_efs" {
  name        = "${var.project_name}-${var.environment}-openclaw-efs-sg"
  description = "EFS NFS ingress from the OpenClaw task SG only"
  vpc_id      = data.aws_vpc.default.id

  depends_on = [terraform_data.runtime_guard]

  ingress {
    description     = "NFS from OpenClaw tasks only"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.openclaw.id]
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-openclaw-efs-sg"
  }
}

# Every subnet used by the service has a mount target. The default subnets are
# one-per-AZ in the live contract; the preflight/plan validator rejects a
# mismatch before task definition migration.
resource "aws_efs_mount_target" "openclaw_state" {
  for_each        = toset(data.aws_subnets.default.ids)
  file_system_id  = aws_efs_file_system.openclaw_state.id
  subnet_id       = each.value
  security_groups = [aws_security_group.openclaw_efs.id]

  depends_on = [
    aws_efs_access_point.openclaw_state,
    terraform_data.runtime_guard,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "openclaw_efs" {
  statement {
    sid       = "MountOpenClawStateAccessPoint"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.openclaw_state.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.openclaw_state.arn]
    }

    condition {
      test     = "Bool"
      variable = "elasticfilesystem:AccessedViaMountTarget"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "openclaw_efs" {
  name   = "${var.project_name}-${var.environment}-openclaw-efs"
  role   = aws_iam_role.openclaw_task.id
  policy = data.aws_iam_policy_document.openclaw_efs.json
}
