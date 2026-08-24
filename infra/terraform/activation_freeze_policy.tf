# ============================================================
# Activation Freeze v2: persistent explicit-deny による mutation 経路の封鎖
# ============================================================
# 2026-08-24 ユーザー裁定。generation publisher freeze と production deployment
# freeze は口頭合意だけを hard safety control にしていたため 3 度破られた:
#
#   1. 2026-08-20 10:48Z  buildspec 4 objects publish（vulkan ドリフト対応）
#   2. 2026-08-21 07:16Z  buildspec 4 objects publish + CodeBuild UpdateProject ×7
#                         （openssl CVE 対応）
#   3. 2026-08-21 08:45-08:54Z  RegisterTaskDefinition ×10 + UpdateService ×4 +
#                         PutTargets ×4 + Lambda env 更新 → **state rebind 完了後に
#                         B3（ECS state drift）を作り直した**
#
# repo 側の機械強制（infra/deploy/activation_freeze.json + CI）は repo 経由の変更しか
# 止められない。AWS へ直接叩く経路（admin 手動 / 他セッション / 長期 credential）は
# 素通りする。よって **実際に mutation できる principal 側へ persistent な explicit
# Deny** を置く。session policy は当該 session にしか効かないため hard control に
# しない（同裁定で禁止）。
#
# ── enable/disable ──────────────────────────────────────────────────────────
# var.activation_freeze_enabled = true のときだけ policy と attachment が生える。
# 適用は AIIAdev による **saved targeted plan** 経路のみ（guard の boundary が
# iam:PutRolePolicy / AttachUserPolicy を自己拒否するため guard では適用できない）。
# production mutation なので apply 直前に human gate で停止する。
#
# ── 対象 principal（census 由来。推測で広げない）────────────────────────────
# CloudTrail（2026-07-01 以降）と repo 内 policy document の 2 面 census:
#   user/AIIAdev … UpdateProject / RegisterTaskDefinition / UpdateService /
#                  PutTargets / PutRule / DeregisterTaskDefinition を実際に実行。
#                  simulate でも 5/6 が allowed（StartBuild だけ既存 Deny で封鎖済み）。
#                  generation prefix への s3:PutObject も allowed
#   runtime_automation … manage-a/b が ECS/events/lambda/codebuild の mutation を許可
#   launcher / caller 系 … StartBuild を実際に実行した経路（build 経由の publish）
#
# ⚠️ **root は identity policy の Deny で止められない**（root は identity-based
#    policy と permissions boundary をバイパスする）。CloudTrail では root が
#    PutTargets ×23 / DeregisterTaskDefinition ×40 を実行した実績がある。
#    root の封鎖には SCP が必要で、本 policy の射程外（別 human gate）。
#    root 静的キーの無効化も別タスクとして未了。
#
# ── resource scope の根拠 ───────────────────────────────────────────────────
# repo 内の Allow 実績で resource-level が使えると分かっている action:
#   codebuild:StartBuild / UpdateProject … exact project ARN で付与済み
#   events:PutRule / PutTargets          … exact rule ARN で付与済み
# repo 内で "*" しか実績が無い action:
#   ecs:RegisterTaskDefinition / DeregisterTaskDefinition / UpdateService /
#   lambda:UpdateFunctionConfiguration
# 本 policy は **Deny** であり、広い方が安全側（Allow の拡大ではない）。freeze の
# 意図も account 全体なので、これらは "*" で止める。ただし s3:PutObject は
# prefix scope を必須にする（"*" にすると Terraform state の書き込みまで止まり、
# state rebind / adopt が実行不能になる）。

variable "activation_freeze_enabled" {
  description = <<-EOT
    Activation Freeze v2 の persistent explicit-deny を有効化する。
    true にすると generation publish と workload deployment の mutation 経路が
    censused principal 上で封鎖される。activation 完了まで true を維持し、
    解除は human gate を経る。
  EOT
  type        = bool
  default     = false
}

locals {
  activation_freeze_count = var.activation_freeze_enabled ? 1 : 0

  # attachment 対象の role。名前は live の実体（census で mutation 到達性を確認済み）。
  # tiktok_build_launcher は count 付きなので、無効時は対象に入れない
  # （ReadExactDeploymentSubjectGraph と同じ条件付き concat 形）。
  activation_freeze_role_names = var.activation_freeze_enabled ? concat(
    [
      aws_iam_role.runtime_automation.name,
      aws_iam_role.codebuild_launcher.name,
      aws_iam_role.approval_caller.name,
      aws_iam_role.openclaw_publisher.name,
      aws_iam_role.release_launcher.name,
      aws_iam_role.release_control_updater.name,
      aws_iam_role.image_deployment_gate.name,
      aws_iam_role.media_cutover_attestor.name,
    ],
    local.tk_enabled == 1 ? [aws_iam_role.tiktok_build_launcher[0].name] : [],
  ) : []
}

data "aws_iam_policy_document" "activation_freeze" {
  # 1) workload deployment: B3 を作り直した経路そのもの
  statement {
    sid    = "DenyWorkloadDeploymentDuringActivationFreeze"
    effect = "Deny"
    actions = [
      "ecs:DeregisterTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = ["*"]
  }

  # 2) generation publisher: buildspec 世代を差し替える経路
  statement {
    sid    = "DenyGenerationPublisherDuringActivationFreeze"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:UpdateProject",
    ]
    resources = ["*"]
  }

  # 3) buildspec generation object への書き込み。**prefix scope 必須**
  #    （"*" にすると Terraform state 書き込みまで止まり state 操作が不能になる）
  statement {
    sid    = "DenyBuildspecGenerationWritesDuringActivationFreeze"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "arn:aws:s3:::${local.release_evidence_bucket}/codebuild-buildspecs/*",
    ]
  }
}

resource "aws_iam_policy" "activation_freeze" {
  count       = local.activation_freeze_count
  name        = "${var.project_name}-${var.environment}-activation-freeze"
  description = "PR2-A0.x activation freeze: generation publish と workload deployment の一時封鎖"
  policy      = data.aws_iam_policy_document.activation_freeze.json
}

resource "aws_iam_user_policy_attachment" "activation_freeze_aiia_dev" {
  count      = local.activation_freeze_count
  user       = data.aws_iam_user.aiia_dev.user_name
  policy_arn = aws_iam_policy.activation_freeze[0].arn
}

resource "aws_iam_role_policy_attachment" "activation_freeze" {
  for_each   = toset(local.activation_freeze_role_names)
  role       = each.value
  policy_arn = aws_iam_policy.activation_freeze[0].arn
}
