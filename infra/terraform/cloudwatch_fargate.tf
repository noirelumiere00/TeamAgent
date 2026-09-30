# ============================================================
# CloudWatch — Fargate(OpenClaw/MCP) 観測・アラーム・ダッシュボード（§I / N1・M4）
# ============================================================
# 既存 cloudwatch.tf の SNS topic(aws_sns_topic.alarms)・namespace(local.metric_namespace)・
# パターンをそのまま流用。対象 log group は fargate.tf の /teamagent/<env>/{openclaw,teamagent-mcp}。
# 構造化ログ(structlog JSON Lines) の event 名: identity_spoof_rejected / mcp_tool_error / cost_usd。

# ---------- metric filters（MCP バックエンドログ） ----------
resource "aws_cloudwatch_log_metric_filter" "mcp_spoof_rejected" {
  name           = "${var.project_name}-${var.environment}-mcp-spoof-rejected"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.event = \"identity_spoof_rejected\" }"

  metric_transformation {
    name          = "McpIdentitySpoofRejected"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "mcp_tool_error" {
  name           = "${var.project_name}-${var.environment}-mcp-tool-error"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.event = \"mcp_tool_error\" || $.level = \"error\" }"

  metric_transformation {
    name          = "McpToolError"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "mcp_cost_usd" {
  name           = "${var.project_name}-${var.environment}-mcp-cost-usd"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.cost_usd = * }"

  metric_transformation {
    name          = "McpCostUSD"
    namespace     = local.metric_namespace
    value         = "$.cost_usd"
    default_value = "0"
    unit          = "None"
  }
}

# 検索#17: 引用忠実性 KPI（最終回答の引用が、ツールが返した根拠に裏付けられている割合 0..1）。
# orchestrator(L2 run_agent)/検索経路が JSON ログに citation_validity を吐くと束ねる。
# JSON 値抽出のため datapoint が出るのは当該フィールドを emit する版がデプロイされてから。
resource "aws_cloudwatch_log_metric_filter" "mcp_citation_validity" {
  name           = "${var.project_name}-${var.environment}-mcp-citation-validity"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.citation_validity = * }"

  metric_transformation {
    name          = "McpCitationValidity"
    namespace     = local.metric_namespace
    value         = "$.citation_validity"
    default_value = "0"
    unit          = "None"
  }
}

# OpenClaw 起動時の全fail-closed経路を拾う。旧config validatorの
# openclaw_config_invariant_violation と、現行Node entrypointの
# openclaw_entrypoint_error のどちらでも同じ起動失敗alarmへ送る。
resource "aws_cloudwatch_log_metric_filter" "openclaw_config_violation" {
  name           = "${var.project_name}-${var.environment}-openclaw-startup-failure"
  log_group_name = aws_cloudwatch_log_group.openclaw.name
  pattern        = "{ $.event = \"openclaw_config_invariant_violation\" || $.event = \"openclaw_entrypoint_error\" }"

  metric_transformation {
    name          = "OpenClawStartupFailure"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# 柱2: 連携(oauth_connect)の失敗を拾う。本人未解決 fail-closed(oauth_connect_fail_closed)＋
# URL生成失敗(oauth_connect_url_failed)。「連携が機能していない」直近シグナル＝低閾値で通知。
resource "aws_cloudwatch_log_metric_filter" "oauth_connect_failed" {
  name           = "${var.project_name}-${var.environment}-oauth-connect-failed"
  log_group_name = aws_cloudwatch_log_group.mcp.name
  pattern        = "{ $.event = \"oauth_connect_fail_closed\" || $.event = \"oauth_connect_url_failed\" }"

  metric_transformation {
    name          = "OAuthConnectFailed"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# DM の context overflow（2026-09-30・石田さんの DM が 09-29 から毎回失敗した件の再発検知）。
# 上流 openclaw@2026.7.1 の回復失敗の行と、plugin が英文を日本語の案内へ差し替えた行を数える。
# 案内が日本語になったので、利用者が黙って諦めると管理者には見えない＝ここで拾う。
# パターンは aws logs test-metric-filter で本番の実物の行に 1 件だけ一致することを確認済み
# （tests/scripts/test_openclaw_runtime_contract.py の test_context_overflow_alarms_match_exact_log_lines）。
resource "aws_cloudwatch_log_metric_filter" "openclaw_context_overflow_exhausted" {
  name           = "${var.project_name}-${var.environment}-openclaw-context-overflow-exhausted"
  log_group_name = aws_cloudwatch_log_group.openclaw.name
  pattern        = "\"exhausted provider overflow recovery\""

  metric_transformation {
    name          = "OpenClawContextOverflowExhausted"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "openclaw_context_overflow_replaced" {
  name           = "${var.project_name}-${var.environment}-openclaw-context-overflow-replaced"
  log_group_name = aws_cloudwatch_log_group.openclaw.name
  pattern        = "\"context overflow reply replaced\""

  metric_transformation {
    name          = "OpenClawContextOverflowReplaced"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# ---------- alarms ----------
# なりすまし拒否は「攻撃 or バグの早期検知」シグナル＝5分窓で1件でも通知。
resource "aws_cloudwatch_metric_alarm" "mcp_spoof_rejected" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-identity-spoof-rejected"
  alarm_description   = "MCP境界で identity 詐称/不整合を拒否（攻撃 or 結線バグの兆候）"
  namespace           = local.metric_namespace
  metric_name         = "McpIdentitySpoofRejected"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# 設定矛盾で OpenClaw が起動時 fail-loud＝即通知（無音で動き続けるより遥かにマシ）。
resource "aws_cloudwatch_metric_alarm" "openclaw_config_violation" {
  alarm_name          = "${var.project_name}-${var.environment}-openclaw-startup-failure"
  alarm_description   = "OpenClaw entrypoint/config invariantの起動失敗を即時検知"
  namespace           = local.metric_namespace
  metric_name         = "OpenClawStartupFailure"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# 連携失敗は「営業がAiLaを使い始められない」直結 シグナル＝5分窓で1件でも通知。
resource "aws_cloudwatch_metric_alarm" "oauth_connect_failed" {
  alarm_name          = "${var.project_name}-${var.environment}-oauth-connect-failed"
  alarm_description   = "oauth_connect が失敗（本人未解決 fail-closed or URL生成失敗）＝連携が機能していない兆候"
  namespace           = local.metric_namespace
  metric_name         = "OAuthConnectFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "mcp_tool_error_spike" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-tool-error-spike"
  alarm_description   = "MCP tool 実行エラーが 5 分窓で ${var.error_count_threshold} 件以上"
  namespace           = local.metric_namespace
  metric_name         = "McpToolError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.error_count_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "mcp_daily_cost_high" {
  alarm_name          = "${var.project_name}-${var.environment}-mcp-daily-cost-high"
  alarm_description   = "MCP 経由 Bedrock 日次コストが ${var.daily_cost_threshold_usd} USD を超過"
  namespace           = local.metric_namespace
  metric_name         = "McpCostUSD"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.daily_cost_threshold_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# overflow の再発＝5 分窓で 1 件でも通知（段2＝固定部分の削減の期限を測る材料にもなる）。
resource "aws_cloudwatch_metric_alarm" "openclaw_context_overflow" {
  alarm_name          = "${var.project_name}-${var.environment}-openclaw-context-overflow"
  alarm_description   = "Aico の会話が上限（200k tokens）を超えて依頼が失敗した（DM・チャンネル）"
  namespace           = local.metric_namespace
  metric_name         = "OpenClawContextOverflowExhausted"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# 回復失敗があるのに日本語の案内への差し替えが無い＝文面の照合が外れた（上流の版上げ等）か、
# 保証経路の抑止で案内が出なかった。1 時間窓で「失敗 − 差し替え」が 1 以上なら通知。
resource "aws_cloudwatch_metric_alarm" "openclaw_context_overflow_unreplaced" {
  alarm_name          = "${var.project_name}-${var.environment}-openclaw-context-overflow-unreplaced"
  alarm_description   = "overflow の英文が日本語の案内へ差し替わっていない疑い（plugin の文面照合を確認）"
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  metric_query {
    id          = "unreplaced"
    expression  = "exhausted - replaced"
    label       = "overflow without Japanese notice"
    return_data = true
  }

  metric_query {
    id = "exhausted"
    metric {
      namespace   = local.metric_namespace
      metric_name = "OpenClawContextOverflowExhausted"
      period      = 3600
      stat        = "Sum"
    }
  }

  metric_query {
    id = "replaced"
    metric {
      namespace   = local.metric_namespace
      metric_name = "OpenClawContextOverflowReplaced"
      period      = 3600
      stat        = "Sum"
    }
  }
}

# ---------- dashboard ----------
resource "aws_cloudwatch_dashboard" "fargate" {
  dashboard_name = "${var.project_name}-${var.environment}-openclaw-pilot"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "MCP cost (USD, sum/5m) & spoof-rejected / tool-error"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            [local.metric_namespace, "McpCostUSD", { stat = "Sum", label = "cost_usd" }],
            [local.metric_namespace, "McpIdentitySpoofRejected", { stat = "Sum", label = "spoof_rejected" }],
            [local.metric_namespace, "McpToolError", { stat = "Sum", label = "tool_error" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "ECS CPU/Memory utilized (Container Insights)"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            ["ECS/ContainerInsights", "CpuUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-openclaw", { stat = "Average", label = "openclaw cpu" }],
            ["ECS/ContainerInsights", "CpuUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-mcp", { stat = "Average", label = "mcp cpu" }],
            ["ECS/ContainerInsights", "MemoryUtilized", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "${var.project_name}-${var.environment}-mcp", { stat = "Average", label = "mcp mem" }],
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 6
        width  = 24
        height = 6
        properties = {
          title  = "MCP recent identity & errors"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.mcp.name}' | fields @timestamp, event, tool, reason, source, domain | filter event in ['identity_resolved','caller_claim_rejected','identity_spoof_rejected','mcp_tool_error'] | sort @timestamp desc | limit 50"
        }
      },
    ]
  })
}

# ---------- ingest / 検索品質ダッシュボード（監視#19・インフラ#19・検索#17）----------
# 取り込んだナレッジが検索で実際に引かれ、引用が忠実か（citation KPI）を 1 枚で見る。
# ⚠️ 既知ギャップ: ingest 本体は worker EC2(journald) で動き CloudWatch 未到達のため、
#    取り込み件数等の ingest 内部メトリクスはここに出ない（EC2 CloudWatch agent 導入は別タスク）。
#    本ダッシュボードは MCP 経路（検索の消費側）に出る品質シグナルを束ねる。
resource "aws_cloudwatch_dashboard" "ingest" {
  dashboard_name = "${var.project_name}-${var.environment}-ingest"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Citation faithfulness KPI (avg, 検索#17)"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            [local.metric_namespace, "McpCitationValidity", { stat = "Average", label = "citation_validity" }],
          ]
          yAxis = { left = { min = 0, max = 1 } }
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "検索イベント（取り込んだナレッジが引かれているか）"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.mcp.name}' | fields @timestamp, event, latency_ms, cost_usd | filter event in ['bedrock_converse','search_skill_done','agent_result'] | sort @timestamp desc | limit 50"
        }
      },
      {
        type   = "text"
        x      = 0
        y      = 6
        width  = 24
        height = 3
        properties = {
          markdown = "### ingest 内部メトリクスの所在\ningest は worker EC2(journald) で実行され CloudWatch へ未到達のため、取り込み件数/失敗率はここに出ません。一次ソースは `connector_state`/`ingest_jobs`/`audit_log`（RDS・migration 0005/0012/0014）と `scripts/pilot_health.py`。EC2 CloudWatch agent 導入は別タスク（`docs/v3.2/bundled_deploy_2026-06-16.md` の既知ギャップ）。"
        }
      },
    ]
  })
}
