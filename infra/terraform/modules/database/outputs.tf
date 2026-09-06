output "endpoint" { value = aws_db_instance.airflow.address }
output "db_name" { value = aws_db_instance.airflow.db_name }
output "username" { value = aws_db_instance.airflow.username }
output "password" {
  value     = random_password.airflow_db.result
  sensitive = true
}
