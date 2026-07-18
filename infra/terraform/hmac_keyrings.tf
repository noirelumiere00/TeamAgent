# ============================================================
# Purpose-separated HMAC keyrings and fail-closed rollout contract
# ============================================================
#
# Terraform owns secret containers and non-secret generation metadata only. Secret payloads are
# created through an approved Secrets Manager write path and are never accepted as variables,
# resources, outputs, or command arguments here. ECS references are pinned to exact VersionIds.
#
# rollout phases (independent per domain):
#   blocked            - default; any task that needs the domain fails its precondition
#   legacy_migration   - dedicated primary + pinned database-url previous + fixed T0
#   dedicated_rotation - new dedicated primary version + prior dedicated version + fixed T0
#   steady             - dedicated primary only; previous removal is allowed only after deadline

variable "mail_action_hmac_rollout_phase" {
  description = "MAIL_ACTION HMAC phase: blocked, legacy_migration, dedicated_rotation, or steady."
  type        = string
  default     = "blocked"

  validation {
    condition = contains(
      ["blocked", "legacy_migration", "dedicated_rotation", "steady"],
      var.mail_action_hmac_rollout_phase,
    )
    error_message = "mail_action_hmac_rollout_phase is invalid."
  }
}

variable "report_link_hmac_rollout_phase" {
  description = "REPORT_LINK HMAC phase: blocked, legacy_migration, dedicated_rotation, or steady."
  type        = string
  default     = "blocked"

  validation {
    condition = contains(
      ["blocked", "legacy_migration", "dedicated_rotation", "steady"],
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

resource "aws_secretsmanager_secret" "mail_action_hmac" {
  name                    = "${var.project_name}/${var.environment}/hmac/mail-action"
  description             = "Dedicated TeamAgent MAIL_ACTION HMAC keyring; values managed outside Terraform."
  recovery_window_in_days = 30

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "report_link_hmac" {
  name                    = "${var.project_name}/${var.environment}/hmac/report-link"
  description             = "Dedicated TeamAgent REPORT_LINK HMAC keyring; values managed outside Terraform."
  recovery_window_in_days = 30

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  hmac_version_id_pattern = "^[A-Za-z0-9_-]{32,64}$"
  hmac_generation_pattern = "^[!-~]{1,2048}$"
  hmac_t0_pattern         = "^[0-9]{1,10}$"
  hmac_max_epoch_s        = 9999999999

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

  mail_action_hmac_primary_generation = (
    can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_primary_version_id))
    ? "${aws_secretsmanager_secret.mail_action_hmac.arn}@${var.mail_action_hmac_primary_version_id}"
    : ""
  )
  report_link_hmac_primary_generation = (
    can(regex(local.hmac_version_id_pattern, var.report_link_hmac_primary_version_id))
    ? "${aws_secretsmanager_secret.report_link_hmac.arn}@${var.report_link_hmac_primary_version_id}"
    : ""
  )
  mail_action_hmac_primary_value_from = (
    can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_primary_version_id))
    ? "${aws_secretsmanager_secret.mail_action_hmac.arn}:::${var.mail_action_hmac_primary_version_id}"
    : ""
  )
  report_link_hmac_primary_value_from = (
    can(regex(local.hmac_version_id_pattern, var.report_link_hmac_primary_version_id))
    ? "${aws_secretsmanager_secret.report_link_hmac.arn}:::${var.report_link_hmac_primary_version_id}"
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
      ? "${aws_secretsmanager_secret.mail_action_hmac.arn}@${var.mail_action_hmac_previous_version_id}"
      : ""
    )
  )
  report_link_hmac_previous_generation = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_generation
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.report_link_hmac_previous_version_id))
      ? "${aws_secretsmanager_secret.report_link_hmac.arn}@${var.report_link_hmac_previous_version_id}"
      : ""
    )
  )
  mail_action_hmac_previous_value_from = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_value_from
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.mail_action_hmac_previous_version_id))
      ? "${aws_secretsmanager_secret.mail_action_hmac.arn}:::${var.mail_action_hmac_previous_version_id}"
      : ""
    )
  )
  report_link_hmac_previous_value_from = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? local.hmac_legacy_database_value_from
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      && can(regex(local.hmac_version_id_pattern, var.report_link_hmac_previous_version_id))
      ? "${aws_secretsmanager_secret.report_link_hmac.arn}:::${var.report_link_hmac_previous_version_id}"
      : ""
    )
  )
  mail_action_hmac_previous_secret_name = (
    var.mail_action_hmac_rollout_phase == "legacy_migration"
    ? data.aws_secretsmanager_secret.database_url.name
    : (
      var.mail_action_hmac_rollout_phase == "dedicated_rotation"
      ? aws_secretsmanager_secret.mail_action_hmac.name
      : ""
    )
  )
  report_link_hmac_previous_secret_name = (
    var.report_link_hmac_rollout_phase == "legacy_migration"
    ? data.aws_secretsmanager_secret.database_url.name
    : (
      var.report_link_hmac_rollout_phase == "dedicated_rotation"
      ? aws_secretsmanager_secret.report_link_hmac.name
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

  mail_action_hmac_config_ready = (
    var.mail_action_hmac_rollout_phase != "blocked"
    && local.mail_action_hmac_primary_generation != ""
    && local.mail_action_hmac_primary_value_from != ""
    && local.mail_action_hmac_proposed_t0_valid
    && (
      var.mail_action_hmac_rollout_phase == "legacy_migration"
      ? var.mail_action_hmac_previous_version_id == ""
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
}
