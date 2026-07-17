# ============================================================
# Purpose-separated HMAC secret metadata and rotation contract
# ============================================================
# Secret values never enter Terraform. Primary ARNs identify purpose-specific
# Secrets Manager objects. Previous ARNs must include an exact version ID
# (`BASE_ARN:::VERSION_ID`) so the verification generation cannot mutate under
# an unchanged ARN. T0 is non-secret metadata and is persisted in tfvars/state.

variable "mail_action_hmac_secret_arn" {
  description = "MAIL_ACTION_HMAC_SECRETのpurpose専用Secrets Manager exact ARN。"
  type        = string
  default     = ""

  validation {
    condition = var.mail_action_hmac_secret_arn == "" || can(regex(
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/hmac/mail-action-[A-Za-z0-9]{6}$",
      var.mail_action_hmac_secret_arn,
    ))
    error_message = "mail_action_hmac_secret_arnは東京/dev accountのmail-action purpose exact ARNに限定します。"
  }
}

variable "report_link_hmac_secret_arn" {
  description = "REPORT_LINK_HMAC_SECRETのpurpose専用Secrets Manager exact ARN。"
  type        = string
  default     = ""

  validation {
    condition = var.report_link_hmac_secret_arn == "" || can(regex(
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/hmac/report-link-[A-Za-z0-9]{6}$",
      var.report_link_hmac_secret_arn,
    ))
    error_message = "report_link_hmac_secret_arnは東京/dev accountのreport-link purpose exact ARNに限定します。"
  }
}

variable "mail_action_hmac_previous_secret_arn" {
  description = "MAIL_ACTION_HMAC_PREVIOUS_SECRET。exact version IDを含むvalueFrom ARN。空はprevious無し。"
  type        = string
  default     = ""

  validation {
    condition = var.mail_action_hmac_previous_secret_arn == "" || can(regex(
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/(database-url|hmac/mail-action)-[A-Za-z0-9]{6}:::[A-Za-z0-9-]{32,64}$",
      var.mail_action_hmac_previous_secret_arn,
    ))
    error_message = "mail previousは同account/regionのlegacy database-urlまたはmail-action secretをexact version pinしてください。"
  }
}

variable "report_link_hmac_previous_secret_arn" {
  description = "REPORT_LINK_HMAC_PREVIOUS_SECRET。exact version IDを含むvalueFrom ARN。空はprevious無し。"
  type        = string
  default     = ""

  validation {
    condition = var.report_link_hmac_previous_secret_arn == "" || can(regex(
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/(database-url|hmac/report-link)-[A-Za-z0-9]{6}:::[A-Za-z0-9-]{32,64}$",
      var.report_link_hmac_previous_secret_arn,
    ))
    error_message = "report previousは同account/regionのlegacy database-urlまたはreport-link secretをexact version pinしてください。"
  }
}

variable "mail_action_hmac_previous_rotation_started_at" {
  description = "Mail previous generationの永続T0 (Unix epoch)。previous ARNと同一revisionで追加削除する。"
  type        = number
  default     = null
  nullable    = true

  validation {
    condition = (
      var.mail_action_hmac_previous_rotation_started_at == null ||
      (
        floor(var.mail_action_hmac_previous_rotation_started_at) ==
        var.mail_action_hmac_previous_rotation_started_at &&
        var.mail_action_hmac_previous_rotation_started_at >= 0 &&
        var.mail_action_hmac_previous_rotation_started_at <= 9999999999
      )
    )
    error_message = "mail rotation T0はbounded Unix epoch整数にしてください。"
  }
}

variable "report_link_hmac_previous_rotation_started_at" {
  description = "Report previous generationの永続T0 (Unix epoch)。previous ARNと同一revisionで追加削除する。"
  type        = number
  default     = null
  nullable    = true

  validation {
    condition = (
      var.report_link_hmac_previous_rotation_started_at == null ||
      (
        floor(var.report_link_hmac_previous_rotation_started_at) ==
        var.report_link_hmac_previous_rotation_started_at &&
        var.report_link_hmac_previous_rotation_started_at >= 0 &&
        var.report_link_hmac_previous_rotation_started_at <= 9999999999
      )
    )
    error_message = "report rotation T0はbounded Unix epoch整数にしてください。"
  }
}

locals {
  hmac_mail_previous_present   = var.mail_action_hmac_previous_secret_arn != ""
  hmac_report_previous_present = var.report_link_hmac_previous_secret_arn != ""
  hmac_rotation_pairs_valid = (
    local.hmac_mail_previous_present ==
    (var.mail_action_hmac_previous_rotation_started_at != null) &&
    local.hmac_report_previous_present ==
    (var.report_link_hmac_previous_rotation_started_at != null)
  )
  hmac_primary_contract_valid = (
    var.mail_action_hmac_secret_arn != "" &&
    var.report_link_hmac_secret_arn != "" &&
    var.mail_action_hmac_secret_arn != var.report_link_hmac_secret_arn
  )

  hmac_mail_environment = local.hmac_mail_previous_present ? [
    {
      name  = "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT"
      value = tostring(var.mail_action_hmac_previous_rotation_started_at)
    },
  ] : []
  hmac_report_environment = local.hmac_report_previous_present ? [
    {
      name  = "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT"
      value = tostring(var.report_link_hmac_previous_rotation_started_at)
    },
  ] : []

  hmac_mail_secrets = concat(
    [{ name = "MAIL_ACTION_HMAC_SECRET", valueFrom = var.mail_action_hmac_secret_arn }],
    local.hmac_mail_previous_present ? [{
      name      = "MAIL_ACTION_HMAC_PREVIOUS_SECRET"
      valueFrom = var.mail_action_hmac_previous_secret_arn
    }] : [],
  )
  hmac_report_secrets = concat(
    [{ name = "REPORT_LINK_HMAC_SECRET", valueFrom = var.report_link_hmac_secret_arn }],
    local.hmac_report_previous_present ? [{
      name      = "REPORT_LINK_HMAC_PREVIOUS_SECRET"
      valueFrom = var.report_link_hmac_previous_secret_arn
    }] : [],
  )

  hmac_mcp_environment     = concat(local.hmac_mail_environment, local.hmac_report_environment)
  hmac_connect_environment = concat(local.hmac_mail_environment, local.hmac_report_environment)
  hmac_morning_environment = local.hmac_mail_environment
  hmac_mcp_secrets         = concat(local.hmac_mail_secrets, local.hmac_report_secrets)
  hmac_connect_secrets     = concat(local.hmac_mail_secrets, local.hmac_report_secrets)
  hmac_morning_secrets     = local.hmac_mail_secrets

  hmac_mail_secret_iam_arns = compact([
    var.mail_action_hmac_secret_arn,
    local.hmac_mail_previous_present ? split(":::", var.mail_action_hmac_previous_secret_arn)[0] : "",
  ])
  hmac_report_secret_iam_arns = compact([
    var.report_link_hmac_secret_arn,
    local.hmac_report_previous_present ? split(":::", var.report_link_hmac_previous_secret_arn)[0] : "",
  ])
  hmac_secret_iam_arns = distinct(concat(
    local.hmac_mail_secret_iam_arns,
    local.hmac_report_secret_iam_arns,
  ))

  hmac_proposed = {
    mail = {
      primary_secret_arn  = var.mail_action_hmac_secret_arn
      previous_secret_arn = var.mail_action_hmac_previous_secret_arn
      previous_present    = local.hmac_mail_previous_present
      rotation_started_at = var.mail_action_hmac_previous_rotation_started_at
    }
    report = {
      primary_secret_arn  = var.report_link_hmac_secret_arn
      previous_secret_arn = var.report_link_hmac_previous_secret_arn
      previous_present    = local.hmac_report_previous_present
      rotation_started_at = var.report_link_hmac_previous_rotation_started_at
    }
  }
}
