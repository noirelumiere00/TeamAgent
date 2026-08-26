# ============================================================
# Purpose-separated HMAC keyrings and fail-closed rollout contract
# ============================================================
#
# Purpose-specific secret containers are pre-created and supplied as exact ARNs through the
# canonical hmac_rotation.tf contract. Terraform owns only non-secret rollout metadata and gates;
# secret values never enter Terraform. ECS references are pinned to exact VersionIds.
#
# rollout phases (independent per domain):
#   blocked            - default; any task that needs the domain fails its precondition
#   legacy_migration   - dedicated primary + pinned database-url previous + fixed T0
#   dedicated_rotation - new dedicated primary version + prior dedicated version + fixed T0
#   steady             - dedicated primary only; previous removal is allowed only after deadline

variable "mail_action_hmac_rollout_phase" {
  description = "MAIL_ACTION HMAC phase: blocked, bootstrap_pin, legacy_migration, dedicated_rotation, or steady."
  type        = string
  default     = "blocked"

  # bootstrap_pin は移行専用の一時 phase。selector も鍵 material も変えず、
  # live の曖昧な AWSCURRENT 参照を exact VersionId へ固定するためだけに使う。
  # canonical 化が完了したらこの phase 値と exact legacy selector 許可を撤去する。
  validation {
    condition = contains(
      ["blocked", "bootstrap_pin", "legacy_migration", "dedicated_rotation", "steady"],
      var.mail_action_hmac_rollout_phase,
    )
    error_message = "mail_action_hmac_rollout_phase is invalid."
  }
}

variable "report_link_hmac_rollout_phase" {
  description = "REPORT_LINK HMAC phase: blocked, bootstrap_pin, legacy_migration, dedicated_rotation, or steady."
  type        = string
  default     = "blocked"

  # bootstrap_pin は移行専用の一時 phase。selector も鍵 material も変えず、
  # live の曖昧な AWSCURRENT 参照を exact VersionId へ固定するためだけに使う。
  # canonical 化が完了したらこの phase 値と exact legacy selector 許可を撤去する。
  validation {
    condition = contains(
      ["blocked", "bootstrap_pin", "legacy_migration", "dedicated_rotation", "steady"],
      var.report_link_hmac_rollout_phase,
    )
    error_message = "report_link_hmac_rollout_phase is invalid."
  }
}

variable "mail_action_hmac_primary_version_id" {
  description = "Non-secret Secrets Manager VersionId for the dedicated MAIL_ACTION primary."
  type        = string
  default     = ""
}

variable "report_link_hmac_primary_version_id" {
  description = "Non-secret Secrets Manager VersionId for the dedicated REPORT_LINK primary."
  type        = string
  default     = ""
}

variable "mail_action_hmac_previous_version_id" {
  description = "Prior dedicated MAIL_ACTION VersionId; required only for dedicated_rotation."
  type        = string
  default     = ""
}

variable "report_link_hmac_previous_version_id" {
  description = "Prior dedicated REPORT_LINK VersionId; required only for dedicated_rotation."
  type        = string
  default     = ""
}

variable "hmac_legacy_database_url_version_id" {
  description = "Pinned non-secret VersionId of the live database-url generation used only as migration previous."
  type        = string
  default     = ""
}

variable "hmac_legacy_slack_bot_version_id" {
  description = "Pinned non-secret VersionId of the historical Slack fallback key, used only for bounded MAIL_ACTION v1 verification."
  type        = string
  default     = ""
}

variable "mail_action_hmac_rotation_started_at" {
  description = "Fixed Unix T0 for the proposed MAIL_ACTION previous generation; never recompute on restart."
  type        = string
  default     = ""
}

variable "report_link_hmac_rotation_started_at" {
  description = "Fixed Unix T0 for the proposed REPORT_LINK previous generation; never recompute on restart."
  type        = string
  default     = ""
}

variable "hmac_preflight_epoch_s" {
  description = "Fixed operator-recorded Unix time for the reviewed plan; non-secret and required for transitions."
  type        = string
  default     = ""
}

variable "hmac_rotation_epoch" {
  description = "Stable non-secret rollout epoch shared by the durable HMAC state and every task."
  type        = string
  default     = ""

  validation {
    condition = (
      var.hmac_rotation_epoch == ""
      || can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.hmac_rotation_epoch))
    )
    error_message = "hmac_rotation_epoch must be empty or a stable bounded identifier."
  }
}

variable "hmac_live_manifest_path" {
  description = "Local path to the reviewed secret-free transition manifest used by the apply-time live gate."
  type        = string
  default     = ""
}

variable "hmac_rollout_control_path" {
  description = "Local path to the secret-free live service/rollback control manifest."
  type        = string
  default     = ""
}

variable "hmac_gate_python" {
  description = "Python interpreter used by the apply-time live HMAC gate."
  type        = string
  default     = "../../.venv/bin/python"
}

variable "hmac_gate_mode" {
  description = "Apply-time HMAC gate mode: candidate during issuer cutover, cleanup after prepare-cleanup CAS, or exact approved rollback."
  type        = string
  default     = "candidate"

  validation {
    condition     = contains(["candidate", "cleanup", "rollback"], var.hmac_gate_mode)
    error_message = "hmac_gate_mode must be candidate, cleanup, or rollback."
  }
}

variable "hmac_cleanup_domain" {
  description = "Exact domain being removed in cleanup mode; blank outside cleanup. The other expired domain may remain unchanged only after the live gate proves durable retirement."
  type        = string
  default     = ""

  validation {
    condition = (
      (contains(["candidate", "rollback"], var.hmac_gate_mode) && var.hmac_cleanup_domain == "")
      || (
        var.hmac_gate_mode == "cleanup"
        && contains(["mail_action", "report_link"], var.hmac_cleanup_domain)
      )
    )
    error_message = "hmac_cleanup_domain must be blank outside cleanup and exactly mail_action or report_link in cleanup mode."
  }
}

variable "hmac_runtime_promotion_tasks" {
  description = "Exact ECS/EventBridge workloads this saved plan may mutate. Staged rollouts select only the task valid at the current durable stage."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for task in var.hmac_runtime_promotion_tasks :
      contains(["mcp", "connect_web", "morning_digest"], task)
    ])
    error_message = "hmac_runtime_promotion_tasks accepts only mcp, connect_web, and morning_digest."
  }
}

variable "worker_hmac_artifact_sha256" {
  description = "Reviewed SHA-256 of the exact worker archive bound into worker HMAC provenance."
  type        = string
  default     = ""

  validation {
    condition = (
      var.worker_hmac_artifact_sha256 == ""
      || can(regex("^[a-f0-9]{64}$", var.worker_hmac_artifact_sha256))
    )
    error_message = "worker_hmac_artifact_sha256 must be empty or a lowercase SHA-256."
  }
}

variable "mail_action_hmac_deployed_primary_generation" {
  description = "Observed deployed MAIL_ACTION primary generation (secret ARN@VersionId), never a secret value."
  type        = string
  default     = ""
}

variable "mail_action_hmac_deployed_previous_generation" {
  description = "Observed deployed MAIL_ACTION previous generation, or empty when absent."
  type        = string
  default     = ""
}

variable "mail_action_hmac_deployed_rotation_started_at" {
  description = "Observed deployed MAIL_ACTION T0, or empty when previous is absent."
  type        = string
  default     = ""
}

variable "report_link_hmac_deployed_primary_generation" {
  description = "Observed deployed REPORT_LINK primary generation (secret ARN@VersionId), never a secret value."
  type        = string
  default     = ""
}

variable "report_link_hmac_deployed_previous_generation" {
  description = "Observed deployed REPORT_LINK previous generation, or empty when absent."
  type        = string
  default     = ""
}

variable "report_link_hmac_deployed_rotation_started_at" {
  description = "Observed deployed REPORT_LINK T0, or empty when previous is absent."
  type        = string
  default     = ""
}

variable "mail_action_hmac_ttl_s" {
  description = "MAIL_ACTION issuance TTL; bounded by the true draft/event maximum of 24 hours."
  type        = number
  default     = 86400

  validation {
    condition = (
      floor(var.mail_action_hmac_ttl_s) == var.mail_action_hmac_ttl_s
      && var.mail_action_hmac_ttl_s >= 1
      && var.mail_action_hmac_ttl_s <= 86400
    )
    error_message = "mail_action_hmac_ttl_s must be an integer in 1..86400."
  }
}

variable "report_link_hmac_ttl_s" {
  description = "REPORT_LINK issuance TTL; bounded by the true report-link maximum of 7 days."
  type        = number
  default     = 604800

  validation {
    condition = (
      floor(var.report_link_hmac_ttl_s) == var.report_link_hmac_ttl_s
      && var.report_link_hmac_ttl_s >= 1
      && var.report_link_hmac_ttl_s <= 604800
    )
    error_message = "report_link_hmac_ttl_s must be an integer in 1..604800."
  }
}

resource "aws_dynamodb_table" "hmac_state" {
  name         = "${var.project_name}-${var.environment}-hmac-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "scope"
  range_key    = "record"

  attribute {
    name = "scope"
    type = "S"
  }

  attribute {
    name = "record"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = true

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-hmac-state"
    DataClass   = "secret-free-hmac-control-metadata"
    Environment = var.environment
  }
}

data "aws_iam_policy_document" "hmac_rollout_gate" {
  statement {
    sid = "ReadLiveEcsMetadata"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
    ]
    resources = ["*"]
  }

  statement {
    sid = "InspectAndTransactionallyRestoreScheduledTarget"
    actions = [
      "events:DescribeRule",
      "events:DisableRule",
      "events:ListTargetsByRule",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
    ]
    resources = ["arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${var.project_name}-${var.environment}-*"]
  }

  statement {
    sid     = "ReadHmacGenerationMetadataOnly"
    actions = ["secretsmanager:ListSecretVersionIds"]
    resources = distinct(concat([
      data.aws_secretsmanager_secret.database_url.arn,
      data.aws_secretsmanager_secret.slack_bot.arn,
    ], local.hmac_secret_iam_arns))
  }

  statement {
    sid = "CasHmacControlMetadata"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.hmac_state.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = [local.hmac_state_scope]
    }
  }

  statement {
    sid = "ReconcileCleanupAcrossExactLedgers"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [
      aws_dynamodb_table.hmac_state.arn,
      aws_dynamodb_table.image_deployment_intents.arn,
    ]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        local.hmac_state_scope,
        "intent#*",
      ]
    }
  }

  statement {
    sid       = "VerifyExactWorkerProvenanceKey"
    actions   = ["kms:Verify"]
    resources = [aws_kms_key.mcp_source_publisher_signing.arn]
  }
}

resource "aws_iam_policy" "hmac_rollout_gate" {
  name        = "${var.project_name}-${var.environment}-hmac-rollout-gate"
  description = "Secret-free live metadata reads and CAS-only HMAC rollout ledger transitions."
  policy      = data.aws_iam_policy_document.hmac_rollout_gate.json
}

locals {
  hmac_version_id_pattern = "^[A-Za-z0-9_-]{32,64}$"
  hmac_generation_pattern = "^[!-~]{1,2048}$"
  hmac_t0_pattern         = "^[0-9]{1,10}$"
  hmac_max_epoch_s        = 9999999999
  hmac_image_digest_valid = can(regex("^[^[:space:]@]+@sha256:[a-f0-9]{64}$", var.mcp_image))
  hmac_state_scope        = "${var.project_name}/${var.environment}"
  hmac_rotation_epoch_valid = can(
    regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.hmac_rotation_epoch)
  )

  hmac_preflight_epoch = try(tonumber(var.hmac_preflight_epoch_s), -1)
  hmac_preflight_epoch_valid = (
    can(regex(local.hmac_t0_pattern, var.hmac_preflight_epoch_s))
    && local.hmac_preflight_epoch >= 0
    && local.hmac_preflight_epoch <= local.hmac_max_epoch_s
  )

  hmac_legacy_database_generation = (
    can(regex(local.hmac_version_id_pattern, var.hmac_legacy_database_url_version_id))
    ? "${data.aws_secretsmanager_secret.database_url.arn}@${var.hmac_legacy_database_url_version_id}"
    : ""
  )
  hmac_legacy_database_value_from = (
    can(regex(local.hmac_version_id_pattern, var.hmac_legacy_database_url_version_id))
    ? "${data.aws_secretsmanager_secret.database_url.arn}:::${var.hmac_legacy_database_url_version_id}"
    : ""
  )
  hmac_legacy_worker_generation = (
    can(regex(local.hmac_version_id_pattern, var.hmac_legacy_slack_bot_version_id))
    ? "${data.aws_secretsmanager_secret.slack_bot.arn}@${var.hmac_legacy_slack_bot_version_id}"
    : ""
  )
  hmac_legacy_worker_value_from = (
    can(regex(local.hmac_version_id_pattern, var.hmac_legacy_slack_bot_version_id))
    ? "${data.aws_secretsmanager_secret.slack_bot.arn}:::${var.hmac_legacy_slack_bot_version_id}"
    : ""
  )

  mail_action_hmac_primary_generation = (
    can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_primary_version_id))
    ? "${var.mail_action_hmac_secret_arn}@${var.mail_action_hmac_primary_version_id}"
    : ""
  )
  report_link_hmac_primary_generation = (
    can(regex(local.hmac_version_id_pattern, var.report_link_hmac_primary_version_id))
    ? "${var.report_link_hmac_secret_arn}@${var.report_link_hmac_primary_version_id}"
    : ""
  )
  mail_action_hmac_primary_value_from = (
    can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_primary_version_id))
    ? "${var.mail_action_hmac_secret_arn}:::${var.mail_action_hmac_primary_version_id}"
    : ""
  )
  report_link_hmac_primary_value_from = (
    can(regex(local.hmac_version_id_pattern, var.report_link_hmac_primary_version_id))
    ? "${var.report_link_hmac_secret_arn}:::${var.report_link_hmac_primary_version_id}"
    : ""
  )

  mail_action_hmac_rotation_active = contains(
    ["legacy_migration", "dedicated_rotation"],
    var.mail_action_hmac_rollout_phase,
  )
  report_link_hmac_rotation_active = contains(
    ["legacy_migration", "dedicated_rotation"],
    var.report_link_hmac_rollout_phase,
  )

  mail_action_hmac_previous_generation = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_generation
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_previous_version_id))
      ? "${var.mail_action_hmac_secret_arn}@${var.mail_action_hmac_previous_version_id}"
      : ""
    )
  )
  report_link_hmac_previous_generation = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_generation
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.report_link_hmac_previous_version_id))
      ? "${var.report_link_hmac_secret_arn}@${var.report_link_hmac_previous_version_id}"
      : ""
    )
  )
  mail_action_hmac_previous_value_from = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_value_from
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_previous_version_id))
      ? "${var.mail_action_hmac_secret_arn}:::${var.mail_action_hmac_previous_version_id}"
      : ""
    )
  )
  report_link_hmac_previous_value_from = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_value_from
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.report_link_hmac_previous_version_id))
      ? "${var.report_link_hmac_secret_arn}:::${var.report_link_hmac_previous_version_id}"
      : ""
    )
  )
  mail_action_hmac_previous_secret_name = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? data.aws_secretsmanager_secret.database_url.name
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      ? var.mail_action_hmac_secret_arn
      : ""
    )
  )
  report_link_hmac_previous_secret_name = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? data.aws_secretsmanager_secret.database_url.name
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      ? var.report_link_hmac_secret_arn
      : ""
    )
  )
  mail_action_hmac_previous_version_id = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? var.hmac_legacy_database_url_version_id
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      ? var.mail_action_hmac_previous_version_id
      : ""
    )
  )
  report_link_hmac_previous_version_id = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? var.hmac_legacy_database_url_version_id
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      ? var.report_link_hmac_previous_version_id
      : ""
    )
  )

  mail_action_hmac_deployed_primary_valid = can(
    regex(local.hmac_generation_pattern, var.mail_action_hmac_deployed_primary_generation)
  )
  report_link_hmac_deployed_primary_valid = can(
    regex(local.hmac_generation_pattern, var.report_link_hmac_deployed_primary_generation)
  )
  mail_action_hmac_deployed_previous_valid = (
    var.mail_action_hmac_deployed_previous_generation == ""
    || can(regex(
      local.hmac_generation_pattern,
      var.mail_action_hmac_deployed_previous_generation,
    ))
  )
  report_link_hmac_deployed_previous_valid = (
    var.report_link_hmac_deployed_previous_generation == ""
    || can(regex(
      local.hmac_generation_pattern,
      var.report_link_hmac_deployed_previous_generation,
    ))
  )
  mail_action_hmac_deployed_pair_valid = (
    (
      var.mail_action_hmac_deployed_previous_generation == ""
      && var.mail_action_hmac_deployed_rotation_started_at == ""
    )
    || (
      var.mail_action_hmac_deployed_previous_generation != ""
      && can(regex(
        local.hmac_t0_pattern,
        var.mail_action_hmac_deployed_rotation_started_at,
      ))
      && local.mail_action_hmac_deployed_t0 <= local.hmac_max_epoch_s - 900 - 86400
    )
  )
  report_link_hmac_deployed_pair_valid = (
    (
      var.report_link_hmac_deployed_previous_generation == ""
      && var.report_link_hmac_deployed_rotation_started_at == ""
    )
    || (
      var.report_link_hmac_deployed_previous_generation != ""
      && can(regex(
        local.hmac_t0_pattern,
        var.report_link_hmac_deployed_rotation_started_at,
      ))
      && local.report_link_hmac_deployed_t0 <= local.hmac_max_epoch_s - 900 - 604800
    )
  )

  mail_action_hmac_deployed_active = (
    var.mail_action_hmac_deployed_previous_generation != ""
    && var.mail_action_hmac_deployed_rotation_started_at != ""
  )
  report_link_hmac_deployed_active = (
    var.report_link_hmac_deployed_previous_generation != ""
    && var.report_link_hmac_deployed_rotation_started_at != ""
  )
  mail_action_hmac_proposed_t0_valid = (
    !local.mail_action_hmac_rotation_active
    || (
      can(regex(local.hmac_t0_pattern, var.mail_action_hmac_rotation_started_at))
      && local.mail_action_hmac_proposed_t0 <= local.hmac_max_epoch_s - 900 - 86400
    )
  )
  report_link_hmac_proposed_t0_valid = (
    !local.report_link_hmac_rotation_active
    || (
      can(regex(local.hmac_t0_pattern, var.report_link_hmac_rotation_started_at))
      && local.report_link_hmac_proposed_t0 <= local.hmac_max_epoch_s - 900 - 604800
    )
  )

  mail_action_hmac_proposed_t0 = try(tonumber(var.mail_action_hmac_rotation_started_at), -1)
  report_link_hmac_proposed_t0 = try(tonumber(var.report_link_hmac_rotation_started_at), -1)
  mail_action_hmac_deployed_t0 = try(
    tonumber(var.mail_action_hmac_deployed_rotation_started_at),
    -1,
  )
  report_link_hmac_deployed_t0 = try(
    tonumber(var.report_link_hmac_deployed_rotation_started_at),
    -1,
  )

  # worker HMAC deploy は独立した feature。worker を使わない移行
  # （bootstrap_pin / canonical rotation）でも artifact SHA を要求すると、
  # worker と無関係な作業まで全部止まる（2026-08-26 実測: 承認済み worker archive の
  # 所在が repo・AWS・ローカルのいずれからも特定できなかった）。
  #
  # 検査を **消す** のではなく、fail-closed の位置を
  # 「共通 HMAC readiness」から「worker readiness」へ移す。
  # worker deploy を有効化する時点で hard blocker として復活する。
  hmac_worker_in_scope = var.enable_hmac_worker_deploy
  hmac_worker_artifact_ready = (
    !local.hmac_worker_in_scope
    || can(regex("^[a-f0-9]{64}$", var.worker_hmac_artifact_sha256))
  )

  mail_action_hmac_config_ready = (
    var.mail_action_hmac_rollout_phase != "blocked"
    && local.hmac_rotation_epoch_valid
    && var.hmac_live_manifest_path != ""
    && var.hmac_rollout_control_path != ""
    && local.hmac_worker_artifact_ready
    && local.hmac_image_digest_valid
    && local.mail_action_hmac_primary_generation != ""
    && local.mail_action_hmac_primary_value_from != ""
    && local.mail_action_hmac_proposed_t0_valid
    && (
      var.mail_action_hmac_rollout_phase == "legacy_migration"
      ? (
        var.mail_action_hmac_previous_version_id == ""
        && local.hmac_legacy_worker_generation != ""
        && local.hmac_legacy_worker_value_from != ""
      )
      : (
        var.mail_action_hmac_rollout_phase == "dedicated_rotation"
        ? can(regex(
          local.hmac_version_id_pattern,
          var.mail_action_hmac_previous_version_id,
        ))
        : (
          var.mail_action_hmac_previous_version_id == ""
          && var.mail_action_hmac_rotation_started_at == ""
        )
      )
    )
    && (
      !local.mail_action_hmac_rotation_active
      || (
        local.mail_action_hmac_previous_generation != ""
        && local.mail_action_hmac_previous_value_from != ""
        && local.mail_action_hmac_previous_generation != local.mail_action_hmac_primary_generation
      )
    )
  )
  report_link_hmac_config_ready = (
    var.report_link_hmac_rollout_phase != "blocked"
    && local.hmac_rotation_epoch_valid
    && var.hmac_live_manifest_path != ""
    && var.hmac_rollout_control_path != ""
    && local.hmac_worker_artifact_ready
    && local.hmac_image_digest_valid
    && local.report_link_hmac_primary_generation != ""
    && local.report_link_hmac_primary_value_from != ""
    && local.report_link_hmac_proposed_t0_valid
    && (
      var.report_link_hmac_rollout_phase == "legacy_migration"
      ? var.report_link_hmac_previous_version_id == ""
      : (
        var.report_link_hmac_rollout_phase == "dedicated_rotation"
        ? can(regex(
          local.hmac_version_id_pattern,
          var.report_link_hmac_previous_version_id,
        ))
        : (
          var.report_link_hmac_previous_version_id == ""
          && var.report_link_hmac_rotation_started_at == ""
        )
      )
    )
    && (
      !local.report_link_hmac_rotation_active
      || (
        local.report_link_hmac_previous_generation != ""
        && local.report_link_hmac_previous_value_from != ""
        && local.report_link_hmac_previous_generation != local.report_link_hmac_primary_generation
      )
    )
  )

  # Mirrors validate_hmac_rotation_transition. Each task definition also has a resource-level
  # precondition, so `terraform apply -target=aws_ecs_task_definition.*` cannot skip this gate.
  mail_action_hmac_transition_valid = (
    local.mail_action_hmac_config_ready
    && local.hmac_preflight_epoch_valid
    && local.mail_action_hmac_deployed_primary_valid
    && local.mail_action_hmac_deployed_previous_valid
    && local.mail_action_hmac_deployed_pair_valid
    && (
      var.mail_action_hmac_deployed_previous_generation == ""
      || var.mail_action_hmac_deployed_previous_generation
      != var.mail_action_hmac_deployed_primary_generation
    )
    && (
      (
        !local.mail_action_hmac_deployed_active
        && local.mail_action_hmac_rotation_active
        && local.mail_action_hmac_primary_generation
        != var.mail_action_hmac_deployed_primary_generation
        && local.mail_action_hmac_previous_generation
        == var.mail_action_hmac_deployed_primary_generation
        && local.hmac_preflight_epoch >= 0
        && local.mail_action_hmac_proposed_t0
        <= local.hmac_preflight_epoch + 300
        && local.hmac_preflight_epoch
        < local.mail_action_hmac_proposed_t0 + 900 + 86400
      )
      || (
        local.mail_action_hmac_deployed_active
        && local.mail_action_hmac_rotation_active
        && local.mail_action_hmac_primary_generation
        == var.mail_action_hmac_deployed_primary_generation
        && local.mail_action_hmac_previous_generation
        == var.mail_action_hmac_deployed_previous_generation
        && var.mail_action_hmac_rotation_started_at
        == var.mail_action_hmac_deployed_rotation_started_at
        && local.hmac_preflight_epoch >= 0
        && local.mail_action_hmac_proposed_t0
        <= local.hmac_preflight_epoch + 300
        && local.hmac_preflight_epoch
        < local.mail_action_hmac_deployed_t0 + 900 + 86400
      )
      || (
        var.hmac_gate_mode == "cleanup"
        && var.hmac_cleanup_domain == "report_link"
        && local.mail_action_hmac_deployed_active
        && local.mail_action_hmac_rotation_active
        && local.mail_action_hmac_primary_generation
        == var.mail_action_hmac_deployed_primary_generation
        && local.mail_action_hmac_previous_generation
        == var.mail_action_hmac_deployed_previous_generation
        && var.mail_action_hmac_rotation_started_at
        == var.mail_action_hmac_deployed_rotation_started_at
        && local.hmac_preflight_epoch
        >= local.mail_action_hmac_deployed_t0 + 900 + 86400
      )
      || (
        local.mail_action_hmac_deployed_active
        && !local.mail_action_hmac_rotation_active
        && local.mail_action_hmac_primary_generation
        == var.mail_action_hmac_deployed_primary_generation
        && local.hmac_preflight_epoch
        >= local.mail_action_hmac_deployed_t0 + 900 + 86400
      )
      || (
        !local.mail_action_hmac_deployed_active
        && !local.mail_action_hmac_rotation_active
        && local.mail_action_hmac_primary_generation
        == var.mail_action_hmac_deployed_primary_generation
      )
    )
  )
  report_link_hmac_transition_valid = (
    local.report_link_hmac_config_ready
    && local.hmac_preflight_epoch_valid
    && local.report_link_hmac_deployed_primary_valid
    && local.report_link_hmac_deployed_previous_valid
    && local.report_link_hmac_deployed_pair_valid
    && (
      var.report_link_hmac_deployed_previous_generation == ""
      || var.report_link_hmac_deployed_previous_generation
      != var.report_link_hmac_deployed_primary_generation
    )
    && (
      (
        !local.report_link_hmac_deployed_active
        && local.report_link_hmac_rotation_active
        && local.report_link_hmac_primary_generation
        != var.report_link_hmac_deployed_primary_generation
        && local.report_link_hmac_previous_generation
        == var.report_link_hmac_deployed_primary_generation
        && local.hmac_preflight_epoch >= 0
        && local.report_link_hmac_proposed_t0
        <= local.hmac_preflight_epoch + 300
        && local.hmac_preflight_epoch
        < local.report_link_hmac_proposed_t0 + 900 + 604800
      )
      || (
        local.report_link_hmac_deployed_active
        && local.report_link_hmac_rotation_active
        && local.report_link_hmac_primary_generation
        == var.report_link_hmac_deployed_primary_generation
        && local.report_link_hmac_previous_generation
        == var.report_link_hmac_deployed_previous_generation
        && var.report_link_hmac_rotation_started_at
        == var.report_link_hmac_deployed_rotation_started_at
        && local.hmac_preflight_epoch >= 0
        && local.report_link_hmac_proposed_t0
        <= local.hmac_preflight_epoch + 300
        && local.hmac_preflight_epoch
        < local.report_link_hmac_deployed_t0 + 900 + 604800
      )
      || (
        var.hmac_gate_mode == "cleanup"
        && var.hmac_cleanup_domain == "mail_action"
        && local.report_link_hmac_deployed_active
        && local.report_link_hmac_rotation_active
        && local.report_link_hmac_primary_generation
        == var.report_link_hmac_deployed_primary_generation
        && local.report_link_hmac_previous_generation
        == var.report_link_hmac_deployed_previous_generation
        && var.report_link_hmac_rotation_started_at
        == var.report_link_hmac_deployed_rotation_started_at
        && local.hmac_preflight_epoch
        >= local.report_link_hmac_deployed_t0 + 900 + 604800
      )
      || (
        local.report_link_hmac_deployed_active
        && !local.report_link_hmac_rotation_active
        && local.report_link_hmac_primary_generation
        == var.report_link_hmac_deployed_primary_generation
        && local.hmac_preflight_epoch
        >= local.report_link_hmac_deployed_t0 + 900 + 604800
      )
      || (
        !local.report_link_hmac_deployed_active
        && !local.report_link_hmac_rotation_active
        && local.report_link_hmac_primary_generation
        == var.report_link_hmac_deployed_primary_generation
      )
    )
  )

  hmac_runtime_base_environment = [
    {
      name  = "TEAMAGENT_HMAC_STATE_REQUIRED"
      value = "1"
    },
    {
      name  = "TEAMAGENT_HMAC_STATE_TABLE"
      value = aws_dynamodb_table.hmac_state.name
    },
    {
      name  = "TEAMAGENT_HMAC_STATE_SCOPE"
      value = local.hmac_state_scope
    },
    {
      name  = "TEAMAGENT_HMAC_ROTATION_EPOCH"
      value = var.hmac_rotation_epoch
    },
  ]
  mcp_hmac_provenance = sha256(jsonencode({
    workload        = "mcp"
    image           = var.mcp_image
    rotation_epoch  = var.hmac_rotation_epoch
    mail_primary    = local.mail_action_hmac_primary_generation
    mail_previous   = local.mail_action_hmac_previous_generation
    mail_t0         = local.mail_action_hmac_rotation_active ? var.mail_action_hmac_rotation_started_at : ""
    report_primary  = local.report_link_hmac_primary_generation
    report_previous = local.report_link_hmac_previous_generation
    report_t0       = local.report_link_hmac_rotation_active ? var.report_link_hmac_rotation_started_at : ""
    legacy_worker   = var.mail_action_hmac_rollout_phase == "legacy_migration" ? local.hmac_legacy_worker_generation : ""
  }))
  connect_web_hmac_provenance = sha256(jsonencode({
    workload        = "connect_web"
    image           = var.mcp_image
    rotation_epoch  = var.hmac_rotation_epoch
    report_primary  = local.report_link_hmac_primary_generation
    report_previous = local.report_link_hmac_previous_generation
    report_t0       = local.report_link_hmac_rotation_active ? var.report_link_hmac_rotation_started_at : ""
  }))
  morning_digest_hmac_provenance = sha256(jsonencode({
    workload       = "morning_digest"
    image          = var.mcp_image
    rotation_epoch = var.hmac_rotation_epoch
    mail_primary   = local.mail_action_hmac_primary_generation
    mail_previous  = local.mail_action_hmac_previous_generation
    mail_t0        = local.mail_action_hmac_rotation_active ? var.mail_action_hmac_rotation_started_at : ""
    legacy_worker  = var.mail_action_hmac_rollout_phase == "legacy_migration" ? local.hmac_legacy_worker_generation : ""
  }))
  worker_hmac_provenance = sha256(jsonencode({
    workload        = "worker"
    artifact        = var.worker_hmac_artifact_sha256
    rotation_epoch  = var.hmac_rotation_epoch
    mail_primary    = local.mail_action_hmac_primary_generation
    mail_previous   = local.mail_action_hmac_previous_generation
    mail_t0         = local.mail_action_hmac_rotation_active ? var.mail_action_hmac_rotation_started_at : ""
    report_primary  = local.report_link_hmac_primary_generation
    report_previous = local.report_link_hmac_previous_generation
    report_t0       = local.report_link_hmac_rotation_active ? var.report_link_hmac_rotation_started_at : ""
    legacy_worker   = var.mail_action_hmac_rollout_phase == "legacy_migration" ? local.hmac_legacy_worker_generation : ""
  }))
  mcp_hmac_runtime_environment = concat(local.hmac_runtime_base_environment, [
    {
      name  = "TEAMAGENT_HMAC_PROVENANCE"
      value = local.mcp_hmac_provenance
    },
  ])
  connect_web_hmac_runtime_environment = concat(local.hmac_runtime_base_environment, [
    {
      name  = "TEAMAGENT_HMAC_PROVENANCE"
      value = local.connect_web_hmac_provenance
    },
  ])
  morning_digest_hmac_runtime_environment = concat(local.hmac_runtime_base_environment, [
    {
      name  = "TEAMAGENT_HMAC_PROVENANCE"
      value = local.morning_digest_hmac_provenance
    },
  ])

  mail_action_hmac_environment = concat(
    [
      {
        name  = "MAIL_ACTION_HMAC_PRIMARY_GENERATION"
        value = local.mail_action_hmac_primary_generation
      },
      {
        name  = "MAIL_ACTION_TTL_S"
        value = tostring(var.mail_action_hmac_ttl_s)
      },
    ],
    local.mail_action_hmac_rotation_active ? [
      {
        name  = "MAIL_ACTION_HMAC_PREVIOUS_GENERATION"
        value = local.mail_action_hmac_previous_generation
      },
      {
        name  = "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT"
        value = var.mail_action_hmac_rotation_started_at
      },
    ] : [],
    var.mail_action_hmac_rollout_phase == "legacy_migration" ? [
      {
        name  = "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY"
        value = "1"
      },
      {
        name  = "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION"
        value = local.hmac_legacy_worker_generation
      },
    ] : [],
  )
  report_link_hmac_environment = concat(
    [
      {
        name  = "REPORT_LINK_HMAC_PRIMARY_GENERATION"
        value = local.report_link_hmac_primary_generation
      },
      {
        name  = "REPORT_LINK_TTL_S"
        value = tostring(var.report_link_hmac_ttl_s)
      },
    ],
    local.report_link_hmac_rotation_active ? [
      {
        name  = "REPORT_LINK_HMAC_PREVIOUS_GENERATION"
        value = local.report_link_hmac_previous_generation
      },
      {
        name  = "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"
        value = var.report_link_hmac_rotation_started_at
      },
    ] : [],
    var.report_link_hmac_rollout_phase == "legacy_migration" ? [
      {
        name  = "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY"
        value = "1"
      },
    ] : [],
  )
  mail_action_hmac_secrets = concat(
    [
      {
        name      = "MAIL_ACTION_HMAC_SECRET"
        valueFrom = local.mail_action_hmac_primary_value_from
      },
    ],
    local.mail_action_hmac_rotation_active ? [
      {
        name      = "MAIL_ACTION_HMAC_PREVIOUS_SECRET"
        valueFrom = local.mail_action_hmac_previous_value_from
      },
    ] : [],
    var.mail_action_hmac_rollout_phase == "legacy_migration" ? [
      {
        name      = "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET"
        valueFrom = local.hmac_legacy_worker_value_from
      },
    ] : [],
  )
  report_link_hmac_secrets = concat(
    [
      {
        name      = "REPORT_LINK_HMAC_SECRET"
        valueFrom = local.report_link_hmac_primary_value_from
      },
    ],
    local.report_link_hmac_rotation_active ? [
      {
        name      = "REPORT_LINK_HMAC_PREVIOUS_SECRET"
        valueFrom = local.report_link_hmac_previous_value_from
      },
    ] : [],
  )

  hmac_live_gate_task_addresses = {
    mcp            = "aws_ecs_task_definition.mcp"
    connect_web    = "aws_ecs_task_definition.connect_web[0]"
    morning_digest = "aws_ecs_task_definition.morning_digest[0]"
  }
  hmac_rollout_control = (
    var.hmac_rollout_control_path != ""
    && fileexists(var.hmac_rollout_control_path)
    ? jsondecode(file(var.hmac_rollout_control_path))
    : {}
  )
  hmac_rollback_task_definition_arns = {
    mcp = try(
      local.hmac_rollout_control.services.mcp.rollback_task_definition,
      "",
    )
    connect_web = try(
      local.hmac_rollout_control.services.connect_web.rollback_task_definition,
      "",
    )
    morning_digest = try(
      local.hmac_rollout_control.morning_digest.rollback_task_definition,
      "",
    )
  }
  hmac_rollback_control_ready = alltrue([
    for arn in values(local.hmac_rollback_task_definition_arns) :
    can(regex(
      "^arn:aws:ecs:${var.aws_region}:${local.account_id}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$",
      arn,
    ))
  ])
  hmac_rollback_gate_ready = (
    var.hmac_gate_mode == "rollback"
    && local.hmac_rollback_control_ready
    && var.hmac_rotation_epoch != ""
    && local.hmac_rotation_epoch_valid
    && var.hmac_live_manifest_path != ""
    && fileexists(var.hmac_live_manifest_path)
    && var.hmac_rollout_control_path != ""
    && fileexists(var.hmac_rollout_control_path)
  )
  hmac_live_gate_enabled = {
    mcp = (
      var.hmac_gate_mode == "rollback"
      ? local.hmac_rollback_gate_ready
      : local.mail_action_hmac_config_ready && local.report_link_hmac_config_ready
    )
    connect_web = (
      var.hmac_gate_mode == "rollback"
      ? local.hmac_rollback_gate_ready
      : local.report_link_hmac_config_ready
    )
    morning_digest = (
      var.hmac_gate_mode == "rollback"
      ? local.hmac_rollback_gate_ready
      : local.mail_action_hmac_config_ready
    )
  }
  hmac_release_intent_bindings = {
    rotation_epoch         = var.hmac_rotation_epoch
    gate_mode              = var.hmac_gate_mode
    cleanup_domain         = var.hmac_cleanup_domain
    manifest_sha256        = var.hmac_live_manifest_path != "" ? filesha256(var.hmac_live_manifest_path) : ""
    rollout_control_sha256 = var.hmac_rollout_control_path != "" ? filesha256(var.hmac_rollout_control_path) : ""
    worker_enabled         = var.enable_hmac_worker_deploy
    worker_mode            = var.hmac_worker_deploy_mode
    worker_artifacts = (
      var.enable_hmac_worker_deploy
      ? local.hmac_worker_deploy_hashes
      : {}
    )
    worker_provenance_key_arn = (
      var.enable_hmac_worker_deploy
      ? aws_kms_key.mcp_source_publisher_signing.arn
      : ""
    )
  }
}

resource "terraform_data" "hmac_live_task_gate" {
  for_each = local.hmac_live_gate_task_addresses

  input = {
    action                 = "pre-register"
    workload               = each.key
    task_address           = each.value
    mode                   = var.hmac_gate_mode
    rotation_epoch         = var.hmac_rotation_epoch
    cleanup_domain         = var.hmac_cleanup_domain
    manifest_sha256        = var.hmac_live_manifest_path != "" ? filesha256(var.hmac_live_manifest_path) : ""
    rollout_control_sha256 = var.hmac_rollout_control_path != "" ? filesha256(var.hmac_rollout_control_path) : ""
  }

  # A selected workload gets one deterministic replacement per reviewed release
  # intent. Unselected gates remain stable instead of churning on wall-clock time.
  triggers_replace = (
    local.hmac_live_gate_enabled[each.key]
    && contains(var.hmac_runtime_promotion_tasks, each.key)
    ? [sha256(jsonencode({
      action                 = "pre-register"
      workload               = each.key
      task_address           = each.value
      mode                   = var.hmac_gate_mode
      rotation_epoch         = var.hmac_rotation_epoch
      cleanup_domain         = var.hmac_cleanup_domain
      manifest_sha256        = filesha256(var.hmac_live_manifest_path)
      rollout_control_sha256 = filesha256(var.hmac_rollout_control_path)
      deployment_intent_id   = var.image_deployment_intent_id
    }))]
    : ["inactive"]
  )

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [terraform_data.production_image_release_gate]

  provisioner "local-exec" {
    command     = "\"$HMAC_GATE_PYTHON\" \"$HMAC_GATE_SCRIPT\""
    interpreter = ["/usr/bin/env", "bash", "-c"]
    working_dir = path.root
    environment = {
      HMAC_GATE_ENABLED       = local.hmac_live_gate_enabled[each.key] && contains(var.hmac_runtime_promotion_tasks, each.key) ? "true" : "false"
      HMAC_GATE_PYTHON        = var.hmac_gate_python
      HMAC_GATE_SCRIPT        = abspath("${path.module}/../../scripts/terraform_hmac_gate.py")
      HMAC_GATE_TASK          = each.key
      HMAC_GATE_MODE          = var.hmac_gate_mode
      HMAC_CLEANUP_DOMAIN     = var.hmac_cleanup_domain
      HMAC_GATE_TASK_ADDRESS  = each.value
      HMAC_PREFLIGHT_MANIFEST = var.hmac_live_manifest_path
      HMAC_ROLLOUT_CONTROL    = var.hmac_rollout_control_path
    }
  }
}
