output "sns_topic_arn" { value = aws_sns_topic.bronze_data_arrived.arn }
output "sqs_queue_arn" { value = aws_sqs_queue.bronze_data_arrived.arn }
output "dlq_arn" { value = aws_sqs_queue.bronze_data_arrived_dlq.arn }
