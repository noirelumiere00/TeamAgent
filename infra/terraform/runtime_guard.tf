# ============================================================
# Always-present live runtime parity guard
# ============================================================
# runtime_guard_live is an ephemeral value emitted by terraform_runtime_guard.sh.
# It must never be persisted in tfvars. Every protected runtime depends on the
# always-present terraform_data resource below, so targeted plans cannot evade it.

variable "runtime_guard_live" {
  description = "Guard scriptがlive/preflight/migration manifestから生成する一時照合値。tfvarsへ保存しない。"
  type = object({
    project_name                       = string
    environment                        = string
    aws_region                         = string
    account_id                         = string
    mode                               = string
    migration_id                       = string
    preflight_receipt_sha256           = string
    live_openclaw_image                = string
    desired_openclaw_image             = string
    live_mcp_image                     = string
    desired_mcp_image                  = string
    live_x_image                       = string
    desired_x_image                    = string
    live_tiktok_image                  = string
    desired_tiktok_image               = string
    enable_connect_web                 = bool
    enable_ingest_schedule             = bool
    enable_morning_digest              = bool
    enable_canary_health               = bool
    enable_x_research                  = bool
    enable_tiktok_acquire              = bool
    enable_scrape_tools                = bool
    enable_reminders                   = bool
    enable_report_shorturl             = bool
    enable_research_persist            = bool
    enable_kaiwai_classify             = bool
    use_calendar_event_tool            = bool
    use_schedule_propose_tool          = bool
    enable_progress_notify             = bool
    use_entity_tags                    = bool
    use_ailavault_deeplinks            = bool
    use_payload_offload                = bool
    video_quota_enabled                = bool
    use_analysis_cache                 = bool
    morning_digest_slack_unread        = bool
    morning_digest_schedule_button     = bool
    morning_digest_calendar_button     = bool
    morning_digest_compact             = bool
    morning_digest_reminders           = bool
    shared_company_domains             = string
    slack_team_id                      = string
    ingest_rule_enabled                = bool
    morning_digest_rule_enabled        = bool
    canary_rule_enabled                = bool
    tiktok_dispatch_static_environment = map(string)
    x_dispatch_static_environment      = map(string)
    tiktok_dispatch_code_sha256        = string
    x_dispatch_code_sha256             = string
    monitoring = object({
      container_insights = string
    })
    alarm_delivery = object({
      canonical_topic_arn                 = string
      canonical_topic_exists              = bool
      confirmed_email_endpoint_sha256     = list(string)
      attached_chatbot_configuration_arns = list(string)
      legacy_topic_arn                    = string
      legacy_topic_exists                 = bool
      legacy_action_reference_count       = number
    })
    api_gateway = object({
      api_id                       = string
      name                         = string
      protocol_type                = string
      disable_execute_api_endpoint = bool
      default_stage = object({
        stage_name                 = string
        auto_deploy                = bool
        access_log_enabled         = bool
        access_log_destination_arn = string
        access_log_format          = string
        detailed_metrics_enabled   = bool
      })
      custom_domain_mappings = list(object({
        domain_name     = string
        api_id          = string
        stage           = string
        api_mapping_key = string
      }))
    })
    connect_app_html = object({
      bucket     = string
      key        = string
      version_id = string
      sha256     = string
    })
    hmac_transition_epoch = number
    deployed_hmac = object({
      mail = object({
        primary_secret_arn  = string
        previous_secret_arn = string
        previous_present    = bool
        rotation_started_at = optional(number)
      })
      report = object({
        primary_secret_arn  = string
        previous_secret_arn = string
        previous_present    = bool
        rotation_started_at = optional(number)
      })
    })
  })
  default  = null
  nullable = true
}

locals {
  runtime_migration_manifest = jsondecode(file("${path.module}/../deploy/terraform_runtime_migrations.json"))
  runtime_selected_migration = var.runtime_guard_live == null ? null : try(
    local.runtime_migration_manifest.migrations[var.runtime_guard_live.migration_id],
    null,
  )

  runtime_image_contracts_valid = (
    can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-openclaw@sha256:[0-9a-f]{64}$",
      var.openclaw_image,
    )) &&
    can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-mcp@sha256:[0-9a-f]{64}$",
      var.mcp_image,
    )) &&
    (!var.enable_x_research || can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-mcp@sha256:[0-9a-f]{64}$",
      var.x_buzz_image,
    ))) &&
    (!var.enable_tiktok_acquire || can(regex(
      "^718959508629\\.dkr\\.ecr\\.ap-northeast-1\\.amazonaws\\.com/teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$",
      var.tiktok_acquire_image,
    )))
  )

  runtime_guard_hmac_deployed = var.runtime_guard_live == null ? {
    mail = {
      primary_secret_arn  = ""
      previous_secret_arn = ""
      previous_present    = false
      rotation_started_at = null
    }
    report = {
      primary_secret_arn  = ""
      previous_secret_arn = ""
      previous_present    = false
      rotation_started_at = null
    }
  } : var.runtime_guard_live.deployed_hmac

  hmac_previous_deadline_offsets = {
    mail   = 87300  # issuer cutover 900 + max mail TTL 86400
    report = 605700 # issuer cutover 900 + max report TTL 604800
  }
  hmac_proposed_previous_base = {
    for purpose, proposed in local.hmac_proposed :
    purpose => proposed.previous_present ? split(":::", proposed.previous_secret_arn)[0] : ""
  }
  hmac_deployed_t0 = {
    for purpose, deployed in local.runtime_guard_hmac_deployed :
    purpose => coalesce(deployed.rotation_started_at, 0)
  }
  hmac_proposed_t0 = {
    for purpose, proposed in local.hmac_proposed :
    purpose => coalesce(proposed.rotation_started_at, 0)
  }
  hmac_transition_checks = {
    for purpose, deadline_offset in local.hmac_previous_deadline_offsets :
    purpose => (
      local.runtime_guard_hmac_deployed[purpose].previous_present ==
      (local.runtime_guard_hmac_deployed[purpose].rotation_started_at != null) &&
      local.hmac_proposed[purpose].previous_present ==
      (local.hmac_proposed[purpose].rotation_started_at != null) &&
      (
        var.runtime_guard_live == null ? false :
        var.runtime_guard_live.mode == "sync" ?
        (
          local.hmac_proposed[purpose].primary_secret_arn ==
          local.runtime_guard_hmac_deployed[purpose].primary_secret_arn &&
          local.hmac_proposed[purpose].previous_secret_arn ==
          local.runtime_guard_hmac_deployed[purpose].previous_secret_arn &&
          local.hmac_proposed[purpose].previous_present ==
          local.runtime_guard_hmac_deployed[purpose].previous_present &&
          local.hmac_proposed_t0[purpose] == local.hmac_deployed_t0[purpose]
        ) :
        (
          # A configured generation is immutable. A second generation cannot
          # start until the old previous/T0 pair has been removed.
          (
            !local.runtime_guard_hmac_deployed[purpose].previous_present ||
            (
              local.hmac_proposed[purpose].primary_secret_arn ==
              local.runtime_guard_hmac_deployed[purpose].primary_secret_arn &&
              (
                !local.hmac_proposed[purpose].previous_present ||
                (
                  local.hmac_proposed[purpose].previous_secret_arn ==
                  local.runtime_guard_hmac_deployed[purpose].previous_secret_arn &&
                  local.hmac_proposed_t0[purpose] == local.hmac_deployed_t0[purpose]
                )
              )
            )
          ) &&
          # Removal is permitted only at/after the exclusive purpose deadline.
          (
            !local.runtime_guard_hmac_deployed[purpose].previous_present ||
            local.hmac_proposed[purpose].previous_present ||
            var.runtime_guard_live.hmac_transition_epoch >=
            local.hmac_deployed_t0[purpose] + deadline_offset
          ) &&
          # A proposed previous generation must not already be expired and T0
          # may be at most 300 seconds in the future.
          (
            !local.hmac_proposed[purpose].previous_present ||
            (
              var.runtime_guard_live.hmac_transition_epoch <
              local.hmac_proposed_t0[purpose] + deadline_offset &&
              local.hmac_proposed_t0[purpose] <=
              var.runtime_guard_live.hmac_transition_epoch + 300
            )
          ) &&
          # An issuer-key change is accepted only with the exact deployed key
          # version as previous and no later than T0+900.
          (
            local.hmac_proposed[purpose].primary_secret_arn ==
            local.runtime_guard_hmac_deployed[purpose].primary_secret_arn ?
            true :
            (
              !local.runtime_guard_hmac_deployed[purpose].previous_present &&
              local.hmac_proposed[purpose].previous_present &&
              local.hmac_proposed_previous_base[purpose] ==
              local.runtime_guard_hmac_deployed[purpose].primary_secret_arn &&
              var.runtime_guard_live.hmac_transition_epoch <=
              local.hmac_proposed_t0[purpose] + 900
            )
          )
        )
      )
    )
  }
  hmac_transition_contract_valid = (
    local.hmac_primary_contract_valid &&
    local.hmac_rotation_pairs_valid &&
    alltrue(values(local.hmac_transition_checks))
  )
  runtime_api_access_log_contract = {
    requestId          = "$context.requestId"
    routeKey           = "$context.routeKey"
    status             = "$context.status"
    responseLength     = "$context.responseLength"
    integrationStatus  = "$context.integration.status"
    integrationLatency = "$context.integrationLatency"
    responseType       = "$context.error.responseType"
  }
  runtime_api_gateway_contract_valid = var.runtime_guard_live == null ? false : (
    var.runtime_guard_live.mode == "migration" &&
    try(local.runtime_selected_migration.kind, "") == "runtime" ?
    (
      !var.runtime_guard_live.api_gateway.disable_execute_api_endpoint &&
      !var.runtime_guard_live.api_gateway.default_stage.access_log_enabled &&
      var.runtime_guard_live.api_gateway.default_stage.access_log_destination_arn == "" &&
      var.runtime_guard_live.api_gateway.default_stage.access_log_format == ""
    ) :
    (
      var.runtime_guard_live.api_gateway.disable_execute_api_endpoint &&
      var.runtime_guard_live.api_gateway.default_stage.access_log_enabled &&
      var.runtime_guard_live.api_gateway.default_stage.access_log_destination_arn ==
      "arn:aws:logs:ap-northeast-1:718959508629:log-group:/aws/apigateway/teamagent-dev-connect-web" &&
      try(
        jsondecode(var.runtime_guard_live.api_gateway.default_stage.access_log_format),
        {},
      ) == local.runtime_api_access_log_contract
    )
  )
  runtime_connect_app_html_contract_valid = var.runtime_guard_live == null ? false : (
    var.runtime_guard_live.connect_app_html.bucket == "teamagent-dev-raw-files" &&
    var.runtime_guard_live.connect_app_html.key == "codebuild/connect-web-app.html" &&
    can(regex(
      "^[A-Za-z0-9._-]{1,1024}$",
      var.runtime_guard_live.connect_app_html.version_id,
    )) &&
    can(regex("^[0-9a-f]{64}$", var.runtime_guard_live.connect_app_html.sha256)) &&
    (
      var.runtime_guard_live.mode == "migration" ?
      (
        var.runtime_guard_live.connect_app_html ==
        try(local.runtime_selected_migration.from.connect_app_html, null)
      ) :
      true
    )
  )
  canonical_alarm_topic_arn = "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms"
  legacy_alarm_topic_arn    = "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms"
  configured_alarm_email_sha256 = sort([
    for endpoint in var.alarm_email_endpoints :
    sha256(lower(trimspace(endpoint)))
  ])
  configured_alarm_chatbot_arns = sort(var.alarm_chatbot_configuration_arns)
  alarm_delivery_configuration_valid = (
    var.require_alarm_delivery &&
    (
      (
        length(local.configured_alarm_email_sha256) == 1 &&
        length(local.configured_alarm_chatbot_arns) == 0
      ) ||
      (
        length(local.configured_alarm_email_sha256) == 0 &&
        length(local.configured_alarm_chatbot_arns) > 0
      )
    )
  )
  runtime_is_initial_alarm_delivery_migration = (
    var.runtime_guard_live != null &&
    var.runtime_guard_live.mode == "migration" &&
    try(local.runtime_selected_migration.kind, "") == "runtime"
  )
  runtime_alarm_delivery_contract_valid = var.runtime_guard_live == null ? false : (
    var.runtime_guard_live.alarm_delivery.canonical_topic_arn ==
    local.canonical_alarm_topic_arn &&
    var.runtime_guard_live.alarm_delivery.canonical_topic_exists &&
    var.runtime_guard_live.alarm_delivery.legacy_topic_arn ==
    local.legacy_alarm_topic_arn &&
    sort(var.runtime_guard_live.alarm_delivery.confirmed_email_endpoint_sha256) ==
    local.configured_alarm_email_sha256 &&
    sort(var.runtime_guard_live.alarm_delivery.attached_chatbot_configuration_arns) ==
    local.configured_alarm_chatbot_arns &&
    (
      length(var.runtime_guard_live.alarm_delivery.confirmed_email_endpoint_sha256) +
      length(var.runtime_guard_live.alarm_delivery.attached_chatbot_configuration_arns)
    ) > 0 &&
    (
      local.runtime_is_initial_alarm_delivery_migration ?
      (
        sort(var.runtime_guard_live.alarm_delivery.confirmed_email_endpoint_sha256) ==
        sort(try(
          local.runtime_selected_migration.from.alarm_delivery.confirmed_email_endpoint_sha256,
          [],
        )) &&
        sort(var.runtime_guard_live.alarm_delivery.attached_chatbot_configuration_arns) ==
        sort(try(
          local.runtime_selected_migration.from.alarm_delivery.attached_chatbot_configuration_arns,
          [],
        ))
      ) :
      (
        !var.runtime_guard_live.alarm_delivery.legacy_topic_exists &&
        var.runtime_guard_live.alarm_delivery.legacy_action_reference_count == 0
      )
    )
  )

  runtime_guard_verified = var.runtime_guard_live == null ? false : (
    var.project_name == var.runtime_guard_live.project_name &&
    var.environment == var.runtime_guard_live.environment &&
    var.aws_region == var.runtime_guard_live.aws_region &&
    data.aws_caller_identity.current.account_id == var.runtime_guard_live.account_id &&
    contains(["sync", "migration"], var.runtime_guard_live.mode) &&
    (
      var.runtime_guard_live.mode == "sync" ?
      (
        var.runtime_guard_live.migration_id == "" &&
        var.runtime_guard_live.preflight_receipt_sha256 == "" &&
        var.runtime_guard_live.desired_openclaw_image == var.runtime_guard_live.live_openclaw_image &&
        var.runtime_guard_live.desired_mcp_image == var.runtime_guard_live.live_mcp_image &&
        var.runtime_guard_live.desired_x_image == var.runtime_guard_live.live_x_image &&
        var.runtime_guard_live.desired_tiktok_image == var.runtime_guard_live.live_tiktok_image
      ) :
      (
        local.runtime_migration_manifest.schema_version == 1 &&
        try(local.runtime_selected_migration.enabled, false) &&
        can(regex("^[0-9a-f]{64}$", var.runtime_guard_live.preflight_receipt_sha256)) &&
        var.runtime_guard_live.ingest_rule_enabled ==
        (try(local.runtime_selected_migration.to.rule_states.ingest, "") == "ENABLED") &&
        var.runtime_guard_live.morning_digest_rule_enabled ==
        (try(local.runtime_selected_migration.to.rule_states.morning, "") == "ENABLED") &&
        var.runtime_guard_live.canary_rule_enabled ==
        (try(local.runtime_selected_migration.to.rule_states.canary, "") == "ENABLED") &&
        (
          try(local.runtime_selected_migration.kind, "") == "runtime" ?
          (
            try(local.runtime_selected_migration.to.openclaw_image, null) == var.runtime_guard_live.desired_openclaw_image &&
            try(local.runtime_selected_migration.to.mcp_image, null) == var.runtime_guard_live.desired_mcp_image &&
            try(local.runtime_selected_migration.to.x_buzz_image, null) == var.runtime_guard_live.desired_x_image &&
            try(local.runtime_selected_migration.to.tiktok_image, null) == var.runtime_guard_live.desired_tiktok_image
          ) :
          (
            try(local.runtime_selected_migration.kind, "") == "activation" &&
            var.runtime_guard_live.desired_openclaw_image == var.runtime_guard_live.live_openclaw_image &&
            var.runtime_guard_live.desired_mcp_image == var.runtime_guard_live.live_mcp_image &&
            var.runtime_guard_live.desired_x_image == var.runtime_guard_live.live_x_image &&
            var.runtime_guard_live.desired_tiktok_image == var.runtime_guard_live.live_tiktok_image
          )
        )
      )
    ) &&
    var.openclaw_image == var.runtime_guard_live.desired_openclaw_image &&
    var.mcp_image == var.runtime_guard_live.desired_mcp_image &&
    var.x_buzz_image == var.runtime_guard_live.desired_x_image &&
    var.tiktok_acquire_image == var.runtime_guard_live.desired_tiktok_image &&
    var.enable_connect_web == var.runtime_guard_live.enable_connect_web &&
    var.enable_ingest_schedule == var.runtime_guard_live.enable_ingest_schedule &&
    var.enable_morning_digest == var.runtime_guard_live.enable_morning_digest &&
    var.enable_canary_health == var.runtime_guard_live.enable_canary_health &&
    var.enable_x_research == var.runtime_guard_live.enable_x_research &&
    var.enable_tiktok_acquire == var.runtime_guard_live.enable_tiktok_acquire &&
    var.enable_scrape_tools == var.runtime_guard_live.enable_scrape_tools &&
    var.enable_reminders == var.runtime_guard_live.enable_reminders &&
    var.enable_report_shorturl == var.runtime_guard_live.enable_report_shorturl &&
    var.enable_research_persist == var.runtime_guard_live.enable_research_persist &&
    var.enable_kaiwai_classify == var.runtime_guard_live.enable_kaiwai_classify &&
    var.use_calendar_event_tool == var.runtime_guard_live.use_calendar_event_tool &&
    var.use_schedule_propose_tool == var.runtime_guard_live.use_schedule_propose_tool &&
    var.enable_progress_notify == var.runtime_guard_live.enable_progress_notify &&
    var.use_entity_tags == var.runtime_guard_live.use_entity_tags &&
    var.use_ailavault_deeplinks == var.runtime_guard_live.use_ailavault_deeplinks &&
    var.use_payload_offload == var.runtime_guard_live.use_payload_offload &&
    var.video_quota_enabled == var.runtime_guard_live.video_quota_enabled &&
    var.use_analysis_cache == var.runtime_guard_live.use_analysis_cache &&
    var.morning_digest_slack_unread == var.runtime_guard_live.morning_digest_slack_unread &&
    var.morning_digest_schedule_button == var.runtime_guard_live.morning_digest_schedule_button &&
    var.morning_digest_calendar_button == var.runtime_guard_live.morning_digest_calendar_button &&
    var.morning_digest_compact == var.runtime_guard_live.morning_digest_compact &&
    var.morning_digest_reminders == var.runtime_guard_live.morning_digest_reminders &&
    var.shared_company_domains == var.runtime_guard_live.shared_company_domains &&
    var.slack_team_id == var.runtime_guard_live.slack_team_id &&
    var.ingest_rule_enabled == var.runtime_guard_live.ingest_rule_enabled &&
    var.morning_digest_rule_enabled == var.runtime_guard_live.morning_digest_rule_enabled &&
    var.canary_rule_enabled == var.runtime_guard_live.canary_rule_enabled &&
    var.runtime_guard_live.api_gateway.api_id == "esk97z9grh" &&
    var.runtime_guard_live.api_gateway.name == "teamagent-connectweb-api" &&
    var.runtime_guard_live.api_gateway.protocol_type == "HTTP" &&
    var.runtime_guard_live.api_gateway.default_stage.stage_name == "$default" &&
    var.runtime_guard_live.api_gateway.default_stage.auto_deploy &&
    !var.runtime_guard_live.api_gateway.default_stage.detailed_metrics_enabled &&
    var.runtime_guard_live.api_gateway.custom_domain_mappings == [{
      domain_name     = "connect.newstv.co.jp"
      api_id          = "esk97z9grh"
      stage           = "$default"
      api_mapping_key = ""
    }] &&
    var.runtime_guard_live.monitoring.container_insights == (
      var.runtime_guard_live.mode == "migration" &&
      try(local.runtime_selected_migration.kind, "") == "runtime" ?
      "disabled" : "enabled"
    ) &&
    local.alarm_delivery_configuration_valid &&
    local.runtime_alarm_delivery_contract_valid &&
    local.runtime_api_gateway_contract_valid &&
    local.runtime_connect_app_html_contract_valid &&
    local.hmac_transition_contract_valid &&
    (
      var.runtime_guard_live.mode == "migration" || !var.enable_tiktok_acquire ||
      (
        try(local.tk_dispatch_static_environment, {}) ==
        var.runtime_guard_live.tiktok_dispatch_static_environment &&
        try(data.archive_file.tiktok_dispatch[0].output_base64sha256, "") ==
        var.runtime_guard_live.tiktok_dispatch_code_sha256
      )
    ) &&
    (
      var.runtime_guard_live.mode == "migration" || !var.enable_x_research ||
      (
        try(local.xr_dispatch_static_environment, {}) ==
        var.runtime_guard_live.x_dispatch_static_environment &&
        try(data.archive_file.x_dispatch[0].output_base64sha256, "") ==
        var.runtime_guard_live.x_dispatch_code_sha256
      )
    )
  )

  runtime_guard_error = <<-EOT
    runtime guardが未注入、liveとstrict equalityでない、または承認済みmigration/preflightに
    束縛されていないため停止しました。plain terraform plan/applyは禁止です。
    infra/deploy/terraform_runtime_guard.sh を使用してください。
  EOT
}

resource "terraform_data" "runtime_guard" {
  input = var.runtime_guard_live == null ? null : {
    mode                     = var.runtime_guard_live.mode
    migration_id             = var.runtime_guard_live.migration_id
    preflight_receipt_sha256 = var.runtime_guard_live.preflight_receipt_sha256
    desired_openclaw_image   = var.runtime_guard_live.desired_openclaw_image
    desired_mcp_image        = var.runtime_guard_live.desired_mcp_image
    desired_x_image          = var.runtime_guard_live.desired_x_image
    desired_tiktok_image     = var.runtime_guard_live.desired_tiktok_image
    hmac_transition_epoch    = var.runtime_guard_live.hmac_transition_epoch
    deployed_hmac            = var.runtime_guard_live.deployed_hmac
    proposed_hmac            = local.hmac_proposed
    monitoring               = var.runtime_guard_live.monitoring
    alarm_delivery           = var.runtime_guard_live.alarm_delivery
    api_gateway              = var.runtime_guard_live.api_gateway
    connect_app_html         = var.runtime_guard_live.connect_app_html
  }

  # The canonical topic already exists. Confirmed delivery is live metadata,
  # validated above before this always-present guard can unblock any runtime.
  depends_on = [aws_sns_topic.alarms]

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.runtime_image_contracts_valid
      error_message = "全runtime imageはconsumerごとの同一account/region/repository完全digest URIが必須です。"
    }

    precondition {
      condition     = local.hmac_transition_contract_valid
      error_message = "HMAC primary/previous/T0 transitionがpurpose別rotation契約を満たしません。"
    }

    precondition {
      condition     = local.alarm_delivery_configuration_valid && local.runtime_alarm_delivery_contract_valid
      error_message = "確認済みalarm deliveryが無い、またはlegacy topicの参照/二重所有が残っています。"
    }

    precondition {
      condition     = local.runtime_connect_app_html_contract_valid
      error_message = "connect /appのexact S3 VersionId/SHA-256がreviewed migrationまたはlive snapshotと一致しません。"
    }

    precondition {
      condition     = local.runtime_guard_verified
      error_message = local.runtime_guard_error
    }
  }
}
