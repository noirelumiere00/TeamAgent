# ============================================================
# budgets.tf — 予算ガード（$599 コスト超過の再発防止）
# ============================================================
# 2026-06末の $599 コスト超過は「予算アラート/異常検知が不在」で事後の手動検知・手動全停止
# だったのが根本原因。本ファイルは AWS Budgets（月次上限＋閾値通知）と Cost Anomaly Detection
# （急増検知）を追加し、既存 aws_sns_topic.alarms（→メール通知）へ配信する。
# 方針: 通知のみ・自動停止はしない（誤検知での自動全断を避ける）。閾値超過を早期に人へ知らせる。
# ⚠️ VPCエンドポイント1AZ化・Fargate right-size は別トラックで対応中のため本ファイルでは触れない。
# ============================================================

variable "monthly_budget_usd" {
  description = "月次コスト予算（USD）。80%/100% 実績・120% 予測で通知。$599 事件の再発検知ライン。"
  type        = number
  default     = 250
}

variable "cost_anomaly_impact_usd" {
  description = "コスト異常検知の通知閾値（USD）。1 異常あたりの絶対インパクトがこの額以上で通知。"
  type        = number
  default     = 50
}

# Cost Explorer / Budgets / Cost Anomaly は us-east-1 バックエンドのグローバルサービス。
# 既定 provider(ap-northeast-1) とは別に us-east-1 alias を用意して CE リソースに使う。
provider "aws" {
  alias               = "us_east_1"
  region              = "us-east-1"
  allowed_account_ids = ["718959508629"]
}

# ---------- SNS トピックポリシー（Budgets/Cost Anomaly の発行を許可） ----------
# 既存 aws_sns_topic.alarms(cloudwatch.tf) はメール購読済み。そこへ Budgets/CostAnomaly が
# Publish できるよう最小権限のトピックポリシーを付与する。
data "aws_iam_policy_document" "alarms_cost_publish" {
  statement {
    sid       = "AllowBudgetsAndCostAnomalyPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com", "costalerts.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "alarms_cost" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms_cost_publish.json
}

# ---------- 月次コスト予算（3閾値で SNS→メール通知） ----------
resource "aws_budgets_budget" "monthly_cost" {
  name         = "${var.project_name}-${var.environment}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # 実績 80%：早期警告
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  # 実績 100%：予算到達
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  # 予測 120%：月末に超過見込み（先回り検知）
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 120
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  depends_on = [aws_sns_topic_policy.alarms_cost]
}

# ---------- コスト異常検知（サービス別の急増を日次で検知） ----------
resource "aws_ce_anomaly_monitor" "service" {
  provider          = aws.us_east_1
  name              = "${var.project_name}-${var.environment}-anomaly-monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "service" {
  provider = aws.us_east_1
  name     = "${var.project_name}-${var.environment}-anomaly-sub"
  # SNS subscriber は IMMEDIATE のみ対応（DAILY/WEEKLY は Email 限定・2026-07-11 apply 実測:
  # ValidationException "Daily or weekly frequencies only support Email subscriptions"）。
  # 通知粒度は threshold_expression（インパクト閾値 USD）でノイズ抑制する。
  frequency        = "IMMEDIATE"
  monitor_arn_list = [aws_ce_anomaly_monitor.service.arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.alarms.arn
  }

  # 1 異常あたりの絶対インパクトが閾値(USD)以上のときだけ通知（ノイズ抑制）。
  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      values        = [tostring(var.cost_anomaly_impact_usd)]
      match_options = ["GREATER_THAN_OR_EQUAL"]
    }
  }

  depends_on = [aws_sns_topic_policy.alarms_cost]
}
