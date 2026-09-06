# RDS Postgres for Airflow's metadata database ONLY (not application data
# — that all lives in the Iceberg/S3/DynamoDB layers described in
# docs/architecture/01-data-platform.md and 04-serving.md).
#
# Real finding from actually deploying this stack, not a design choice
# made in advance: the Airflow Helm chart's built-in postgresql subchart
# uses a StatefulSet + PersistentVolumeClaim backed by EBS (gp2/gp3),
# which Fargate cannot attach — the pod sat permanently Pending. RDS
# sidesteps this entirely by moving Postgres outside Kubernetes, which is
# also the more production-credible answer anyway (this is what MWAA
# itself does under the hood). See docs/architecture/08-infrastructure.md.

resource "random_password" "airflow_db" {
  length  = 24
  special = false # avoids characters that need escaping in a connection URI
}

resource "aws_db_subnet_group" "airflow" {
  name       = "${var.project}-${var.environment}-airflow-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "airflow_db" {
  name_prefix = "${var.project}-${var.environment}-airflow-db-"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.eks_cluster_security_group_id]
    description     = "Allow Airflow scheduler/webserver pods to reach Postgres"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "airflow" {
  identifier = "${var.project}-${var.environment}-airflow-db"
  engine     = "postgres"
  # Pinned to a specific available version rather than left unset, so a
  # later `apply` doesn't surprise-upgrade the metadata DB — 16.4 (this
  # module's first choice) turned out not to exist in ap-south-1 at
  # deploy time ("Cannot find version 16.4 for postgres"); confirmed
  # 16.9-16.15 are actually available via `aws rds describe-db-engine-versions`
  # before picking this one.
  engine_version = "16.15"
  instance_class = "db.t4g.micro" # smallest viable — Airflow's own metadata only, not application data

  allocated_storage = 20 # RDS minimum for gp3
  storage_type      = "gp3"

  db_name  = "airflow"
  username = "airflow"
  password = random_password.airflow_db.result

  db_subnet_group_name   = aws_db_subnet_group.airflow.name
  vpc_security_group_ids = [aws_security_group.airflow_db.id]

  multi_az                = false # single-AZ — a demo/exercise instance, not a production HA requirement
  publicly_accessible     = false
  skip_final_snapshot     = true # throwaway sandbox; a final snapshot would survive `terraform destroy` and cost money silently
  deletion_protection     = false
  backup_retention_period = 0
}
