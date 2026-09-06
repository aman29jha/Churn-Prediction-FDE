# One repo per image built under docker/ — see docs/architecture/01-data-platform.md.
locals {
  repo_names = ["spark-jobs", "api-service", "console"]
}

resource "aws_ecr_repository" "this" {
  for_each             = toset(local.repo_names)
  name                 = "${var.project}-${var.environment}-${each.key}"
  image_tag_mutability = "IMMUTABLE" # forces a new tag per build — no silent "latest" drift for what's actually running

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days — avoids silent storage cost growth from failed/superseded builds."
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })
}
