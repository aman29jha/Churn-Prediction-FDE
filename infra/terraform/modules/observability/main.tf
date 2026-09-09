# Logging, dashboard, alarms per docs/architecture/05-observability.md.
# Every log group gets EXPLICIT retention — CloudWatch defaults to
# never-expire, a real, easy-to-miss cost leak on a strict-budget account.

locals {
  log_group_names = [
    "/eks/${var.project}-${var.environment}/api-service",
    "/eks/${var.project}-${var.environment}/console",
    "/eks/${var.project}-${var.environment}/spark-silver",
    "/eks/${var.project}-${var.environment}/spark-gold",
    "/eks/${var.project}-${var.environment}/spark-compaction",
    "/eks/${var.project}-${var.environment}/spark-analytics",
    "/eks/${var.project}-${var.environment}/live-simulator",
    "/eks/${var.project}-${var.environment}/airflow",
  ]
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(local.log_group_names)
  name              = each.key
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alarms" {
  name = "${var.project}-${var.environment}-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Budget alarm — the very first thing set up in this project (see the
# conversation history), now codified in Terraform rather than left as a
# console-only click-ops step.
resource "aws_budgets_budget" "monthly_cost" {
  name         = "${var.project}-${var.environment}-monthly-budget"
  budget_type  = "COST"
  limit_amount = "75"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alarm_email != "" ? [var.alarm_email] : []
  }
}

# --- Dashboard: 4 named panel groups mapping to the assignment's own
# production-readiness pillars (auth, rate limiting, observability,
# failure modes) — see docs/architecture/05-observability.md.
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project}-${var.environment}-production-readiness"
  dashboard_body = jsonencode({
    widgets = [
      {
        type       = "text", x = 0, y = 0, width = 24, height = 1,
        properties = { markdown = "# Production Readiness — Auth / Rate Limiting / Observability / Failure Modes" }
      },
      {
        type = "metric", x = 0, y = 1, width = 12, height = 6,
        properties = {
          title  = "Auth: failed vs successful requests"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["ChurnService", "AuthSuccess", { "stat" : "Sum" }],
            ["ChurnService", "AuthFailure", { "stat" : "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 1, width = 12, height = 6,
        properties = {
          title  = "Rate limiting: allowed vs throttled (429)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["ChurnService", "RequestsAllowed", { "stat" : "Sum" }],
            ["ChurnService", "RequestsThrottled", { "stat" : "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 7, width = 12, height = 6,
        properties = {
          title  = "Observability: latency / error rate / throughput"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["ChurnService", "Latency", { "stat" : "p99" }],
            ["ChurnService", "ErrorRate", { "stat" : "Average" }],
            ["ChurnService", "Throughput", { "stat" : "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 7, width = 12, height = 6,
        properties = {
          title  = "Failure modes: DynamoDB throttles, DLQ depth, Spark job failures"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/DynamoDB", "ThrottledRequests", "TableName", var.customer_scores_table_name, { "stat" : "Sum" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${var.project}-${var.environment}-bronze-data-arrived-dlq", { "stat" : "Maximum" }],
            # Real gap this closes: the panel title has always promised
            # "Spark job failures" but nothing ever emitted that metric —
            # scripts/spark_job_entrypoint.py now does, per job type.
            ["ChurnService", "SparkJobFailure", "JobType", "silver", { "stat" : "Sum" }],
            ["ChurnService", "SparkJobFailure", "JobType", "gold", { "stat" : "Sum" }],
            ["ChurnService", "SparkJobFailure", "JobType", "analytics", { "stat" : "Sum" }],
            # compaction runs through the same entrypoint and emits the same
            # metric on failure — it just hadn't been added to this panel's
            # series list, a real gap found auditing dashboard coverage.
            ["ChurnService", "SparkJobFailure", "JobType", "compaction", { "stat" : "Sum" }]
          ]
        }
      },
      {
        type       = "text", x = 0, y = 13, width = 24, height = 1,
        properties = { markdown = "## Trigger chain: the S3->SNS->SQS->Lambda->Airflow hop (2 real bugs found here this project — bearer-vs-basic auth, then a urllib/proxy-detection bug)" }
      },
      {
        type = "metric", x = 0, y = 14, width = 12, height = 6,
        properties = {
          title  = "Trigger Lambda: errors / throttles"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", var.trigger_lambda_function_name, { "stat" : "Sum" }],
            ["AWS/Lambda", "Throttles", "FunctionName", var.trigger_lambda_function_name, { "stat" : "Sum" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 14, width = 12, height = 6,
        properties = {
          title  = "Trigger Lambda: invocations / duration (p99)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", var.trigger_lambda_function_name, { "stat" : "Sum" }],
            ["AWS/Lambda", "Duration", "FunctionName", var.trigger_lambda_function_name, { "stat" : "p99" }]
          ]
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "api_error_rate" {
  alarm_name          = "${var.project}-${var.environment}-api-error-rate-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ErrorRate"
  namespace           = "ChurnService"
  period              = 300
  statistic           = "Average"
  threshold           = 0.05 # >5% error rate for 2 consecutive periods
  alarm_actions       = [aws_sns_topic.alarms.arn]
  treat_missing_data  = "notBreaching"
}
