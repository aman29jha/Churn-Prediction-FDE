output "data_lake_bucket" { value = aws_s3_bucket.data_lake.bucket }
output "data_lake_bucket_arn" { value = aws_s3_bucket.data_lake.arn }
output "model_registry_bucket" { value = aws_s3_bucket.model_registry.bucket }
output "model_registry_bucket_arn" { value = aws_s3_bucket.model_registry.arn }
output "glue_database_name" { value = aws_glue_catalog_database.churn.name }
output "customer_scores_table_name" { value = aws_dynamodb_table.customer_scores.name }
output "customer_scores_table_arn" { value = aws_dynamodb_table.customer_scores.arn }
