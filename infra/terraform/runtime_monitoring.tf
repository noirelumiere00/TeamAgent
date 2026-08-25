# ============================================================
# Production-path availability and saturation alarms
# ============================================================
# All notifications use the existing TeamAgent alarm topic. Missing service
# task-count telemetry is itself a failure; sparse request/error metrics are
# not.

locals {
  monitored_ecs_services = {
    mcp         = "${var.project_name}-${var.environment}-mcp"
    connect_web = "${var.project_name}-${var.environment}-connect-web"
    openclaw    = "${var.project_name}-${var.environment}-openclaw"
  }
  monitored_lambda_functions = {
    reminders = "${var.project_name}-${var.environment}-reminders-notify"
    tiktok    = "${var.project_name}-${var.environment}-tiktok-acquire-dispatch"
    x_buzz    = "${var.project_name}-${var.environment}-x-buzz-dispatch"
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_running_tasks" {
  for_each = local.monitored_ecs_services

  alarm_name          = "${var.project_name}-${var.environment}-${replace(each.key, "_", "-")}-running-task-missing"
  alarm_description   = "${each.value} has fewer than one running task for two consecutive minutes"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

# タスク停止の検知は上の running-task-missing が担う（RunningTaskCount<1・breaching）。
# ここは「停止する前」＝メモリ逼迫（OOM で killed になる直前）を捕まえる。OOM kill 後は
# コンテナのログが残らないため、アプリログの metric filter では原理的に取れない。
# AWS/ECS MemoryUtilization は task definition の memory に対する % なので、
# taskdef の memory を変えた時に閾値の意味が変わらない（絶対値で書かない理由）。
resource "aws_cloudwatch_metric_alarm" "ecs_service_memory_high" {
  for_each = local.monitored_ecs_services

  alarm_name          = "${var.project_name}-${var.environment}-${replace(each.key, "_", "-")}-memory-high"
  alarm_description   = "${each.value} sustained memory utilisation above the OOM safety margin"
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  # 85%。瞬間的なピークではなく「10分以上張り付いている」状態だけを通知する。
  threshold           = 85
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # 欠測は「サービスが居ない」＝running-task-missing の担当。ここで二重に鳴らさない。
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "connect_api_5xx" {
  alarm_name          = "${var.project_name}-${var.environment}-connect-api-5xx"
  alarm_description   = "Connect HTTP API returned one or more 5xx responses in five minutes"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    ApiId = local.connect_http_api_id
    Stage = "$default"
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.monitored_lambda_functions

  alarm_name          = "${var.project_name}-${var.environment}-${replace(each.key, "_", "-")}-lambda-errors"
  alarm_description   = "${each.value} emitted one or more errors in five minutes"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    FunctionName = each.value
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each = local.monitored_lambda_functions

  alarm_name          = "${var.project_name}-${var.environment}-${replace(each.key, "_", "-")}-lambda-throttles"
  alarm_description   = "${each.value} was throttled one or more times in five minutes"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    FunctionName = each.value
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_database_connections_high" {
  alarm_name          = "${var.project_name}-${var.environment}-rds-database-connections-high"
  alarm_description   = "RDS connections are approaching the configured instance limit"
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  # db.t4g.microの接続枯渇より前に通知。instance class変更時はreviewed migrationで再裁定する。
  threshold           = 80
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

# db.t4g.micro は burstable＝CPU クレジットを使い切ると baseline(10%) まで絞られる。
# その時 CPUUtilization は「下がって」見えるので、CPU 系の閾値監視では原理的に検知できない。
# 検索/ingest が体感で遅くなるだけの無音劣化になるため、残高そのものを見る。
resource "aws_cloudwatch_metric_alarm" "rds_cpu_credit_balance_low" {
  alarm_name          = "${var.project_name}-${var.environment}-rds-cpu-credit-balance-low"
  alarm_description   = "RDS burstable CPU credits are close to exhaustion (throttling to baseline)"
  namespace           = "AWS/RDS"
  metric_name         = "CPUCreditBalance"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  # t4g.micro は 12 credit/h 獲得・上限 288。30 は「約 2.5 時間ぶんしか残っていない」水準。
  threshold           = 30
  comparison_operator = "LessThanThreshold"
  # 非 burstable クラス（db.r7g 等）へ変更すると本メトリクスは発行されなくなる。
  # そこで永久 ALARM にしないため notBreaching。instance class 変更時は本 alarm を再裁定する。
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_freeable_memory_low" {
  alarm_name          = "${var.project_name}-${var.environment}-rds-freeable-memory-low"
  alarm_description   = "RDS freeable memory stayed below the practical safety floor"
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  # 512 MiB。swap/oomに入る前の運用余裕を確保する。
  threshold           = 536870912
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}
