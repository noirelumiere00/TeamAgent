# ============================================================
# Live runtime parity guard
# ============================================================
# ECS の image/env は CLI 直登録でも更新されるため、Terraform state / tfvars が
# live より古いままになり得る。runtime plan は
# infra/deploy/terraform_runtime_guard.sh で検証し、同スクリプトが read-only で取得した
# live 値を runtime_guard_live に一時注入する。共有deployment lockがないため同scriptは
# apply機能を持たない。
#
# ここでの precondition は直接planと古いtfvarsを止める事故防止の第1防壁であり、
# 自己申告objectを認証するIAM境界ではない。保存planのcanonical比較はvalidatorが行う。

variable "runtime_guard_live" {
  description = "terraform_runtime_guard.sh が live から生成して -var で一時注入する照合値。tfvars へ保存しない。null のまま主要runtime resourceをplanするとfail-closed。"
  type = object({
    project_name                       = string
    environment                        = string
    aws_region                         = string
    account_id                         = string
    mode                               = string
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
  })
  default  = null
  nullable = true
}

locals {
  runtime_guard_verified = var.runtime_guard_live == null ? false : (
    var.project_name == var.runtime_guard_live.project_name &&
    var.environment == var.runtime_guard_live.environment &&
    var.aws_region == var.runtime_guard_live.aws_region &&
    data.aws_caller_identity.current.account_id == var.runtime_guard_live.account_id &&
    contains(["sync", "rollout"], var.runtime_guard_live.mode) &&
    (
      var.runtime_guard_live.mode == "sync" ?
      (
        var.runtime_guard_live.desired_mcp_image == var.runtime_guard_live.live_mcp_image &&
        var.runtime_guard_live.desired_x_image == var.runtime_guard_live.live_x_image
      ) :
      (
        var.runtime_guard_live.desired_mcp_image != var.runtime_guard_live.live_mcp_image &&
        var.runtime_guard_live.desired_x_image == var.runtime_guard_live.desired_mcp_image
      )
    ) &&
    var.runtime_guard_live.desired_tiktok_image == var.runtime_guard_live.live_tiktok_image &&
    var.mcp_image == var.runtime_guard_live.desired_mcp_image &&
    var.mcp_image == var.runtime_guard_live.desired_x_image &&
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
    (
      var.enable_tiktok_acquire ?
      (
        local.tk_dispatch_static_environment == var.runtime_guard_live.tiktok_dispatch_static_environment &&
        data.archive_file.tiktok_dispatch[0].output_base64sha256 == var.runtime_guard_live.tiktok_dispatch_code_sha256
      ) : true
    ) &&
    (
      var.enable_x_research ?
      (
        local.xr_dispatch_static_environment == var.runtime_guard_live.x_dispatch_static_environment &&
        data.archive_file.x_dispatch[0].output_base64sha256 == var.runtime_guard_live.x_dispatch_code_sha256
      ) : true
    )
  )

  runtime_guard_error = <<-EOT
    live runtime と desired tfvars/rollout image が一致していないため停止しました。
    直接 terraform plan/apply せず、infra/deploy/terraform_runtime_guard.sh snapshot で
    non-secretのlive値を確認・tfvarsへ反映後、read-only validatorのplanを使用してください。
  EOT
}
