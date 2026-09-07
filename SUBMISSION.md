# Submission Notes

Read this first — it covers the two things a reviewer would otherwise have to discover on their own.

## Which AWS account this was deployed to

Built and deployed against AWS account `784004375291` (`ap-south-1`) — this is the account being used for this interview, so the evidence in `docs/evidence/` (the live ALB, the real Airflow DAG runs, the real Spark jobs, the ingress/pod status) is from the same account this submission is reviewed against, not a separate throwaway sandbox. Build started before the account was confirmed as the interview account, so earlier docs/commit messages in this repo's history refer to it as a "personal sandbox" — that framing is now out of date; this is the real deployment.

## Known gap: the live scoring API doesn't yet read from DynamoDB

`docs/architecture/04-serving.md` describes a DynamoDB fast-lookup table as the production serving layer, with Athena for analytical queries. That DynamoDB table **is** provisioned by Terraform with the correct IRSA grants (`infra/terraform/modules/storage/main.tf`, `modules/irsa/main.tf`) — but the deployed `api-service` does not actually read from it yet. It currently serves `/score/{customer_id}` from a static JSON snapshot (`models/customer_scores.json`, synced from the S3 model registry at pod startup), which is refreshed from the real Gold Spark job's output but is still a point-in-time snapshot, not a live per-request DynamoDB read.

Practical implications:
- A `customer_id` that exists in the snapshot at pod-startup time scores correctly, with real SHAP explanations.
- A genuinely novel customer_id with no Gold-layer row at all (a true cold start) returns a `404`, not an on-the-fly computed score — there's no live feature-computation fallback in the request path.

This is a real, acknowledged gap between the target design and the current implementation, not an oversight discovered after the fact — wiring the DynamoDB read (and a genuine cold-start fallback that computes RFM features from raw events on demand) is the next concrete step if this goes further, and the infrastructure to do it is already in place.

## Everything else

`docs/` is the source of truth for architecture, modeling, evaluation, explainability, and fairness — all with real computed numbers from the actual synthetic dataset, not placeholders. `docs/evidence/README.md` has the full deployment verification trail, including a detailed account of every real infrastructure bug found and fixed by actually running the pipeline end-to-end (not just `terraform plan`).
