# Event-driven pipeline trigger: S3 -> SNS -> SQS -> Lambda (native
# batching) -> Airflow REST API. See docs/architecture/03-orchestration.md.
#
# Includes a dead-letter queue — flagged as MISSING in an earlier
# principal-engineer review of this design (no documented recovery path
# if the Lambda->Airflow call failed). Messages that fail 3 times land in
# the DLQ and raise a CloudWatch alarm instead of vanishing silently.

resource "aws_sns_topic" "bronze_data_arrived" {
  name = "${var.project}-${var.environment}-bronze-data-arrived"
}

resource "aws_sqs_queue" "bronze_data_arrived_dlq" {
  name                      = "${var.project}-${var.environment}-bronze-data-arrived-dlq"
  message_retention_seconds = 1209600 # 14 days — enough time to notice+investigate before messages age out
}

resource "aws_sqs_queue" "bronze_data_arrived" {
  name                       = "${var.project}-${var.environment}-bronze-data-arrived"
  visibility_timeout_seconds = 60

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.bronze_data_arrived_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "allow_sns" {
  queue_url = aws_sqs_queue.bronze_data_arrived.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.bronze_data_arrived.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.bronze_data_arrived.arn } }
    }]
  })
}

resource "aws_sns_topic_subscription" "sqs" {
  topic_arn = aws_sns_topic.bronze_data_arrived.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.bronze_data_arrived.arn
}

resource "aws_s3_bucket_notification" "bronze_writes" {
  bucket = var.data_lake_bucket
  topic {
    topic_arn     = aws_sns_topic.bronze_data_arrived.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "bronze/"
  }
  depends_on = [aws_sns_topic_policy.allow_s3]
}

resource "aws_sns_topic_policy" "allow_s3" {
  arn = aws_sns_topic.bronze_data_arrived.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.bronze_data_arrived.arn
      Condition = { ArnLike = { "aws:SourceArn" = var.data_lake_bucket_arn } }
    }]
  })
}

# --- Lambda: SQS batch (size OR time, native semantics) -> Airflow trigger ---

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/lambda_src"
  output_path = "${path.module}/lambda_src.zip"
}

resource "aws_iam_role" "lambda" {
  name = "${var.project}-${var.environment}-trigger-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The Lambda needs to run inside the VPC to reach Airflow's internal
# ClusterIP service (airflow_api_url is not internet-routable) — attaching
# to ENIs also requires this managed policy for the ENI lifecycle.
resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_security_group" "lambda" {
  name_prefix = "${var.project}-${var.environment}-trigger-lambda-"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"] # egress-only; reaching the Airflow ClusterIP is permitted by the EKS cluster SG's own ingress rule below, not by this SG
  }
}

# Allow the Lambda's security group to reach the Airflow webserver port on
# the EKS cluster/node security group.
resource "aws_security_group_rule" "eks_allow_lambda_to_airflow" {
  type                     = "ingress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  security_group_id        = var.eks_cluster_security_group_id
  source_security_group_id = aws_security_group.lambda.id
  description              = "Allow the pipeline-trigger Lambda to reach the Airflow webserver API"
}

resource "aws_iam_role_policy" "lambda_sqs" {
  name = "consume-sqs"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.bronze_data_arrived.arn
    }]
  })
}

resource "aws_lambda_function" "trigger_pipeline" {
  function_name    = "${var.project}-${var.environment}-trigger-medallion-pipeline"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      AIRFLOW_API_URL   = var.airflow_api_url
      AIRFLOW_API_TOKEN = var.airflow_api_token
    }
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }
}

# Native SQS batching: fires on 20 messages OR 300s, whichever first —
# see docs/architecture/03-orchestration.md for why this beats a
# hand-rolled polling loop.
resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn                   = aws_sqs_queue.bronze_data_arrived.arn
  function_name                      = aws_lambda_function.trigger_pipeline.arn
  batch_size                         = 20
  maximum_batching_window_in_seconds = 300
}

# Alarm on the DLQ — a message landing here means the trigger chain
# failed 3 times and needs a human, not silent data loss.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.project}-${var.environment}-trigger-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  dimensions          = { QueueName = aws_sqs_queue.bronze_data_arrived_dlq.name }
  alarm_description   = "Pipeline trigger failed 3x and landed in the DLQ — the automated event-driven path is broken; use Airflow's manual Trigger DAG button as the documented fallback."
  alarm_actions       = var.alarm_topic_arn != "" ? [var.alarm_topic_arn] : []
}
