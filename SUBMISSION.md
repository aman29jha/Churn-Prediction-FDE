# Submission Notes

Read this first — it covers the two things a reviewer would otherwise have to discover on their own.

## Which AWS account this was deployed to

The official Localytics interview-account invite had not arrived by the time this was built, so — per the assignment's own 4-day clock starting at invite time — this was built and deployed against a **personal AWS sandbox account** (`784004375291`, `ap-south-1`) instead of waiting idle. Every piece of evidence in `docs/evidence/` (the live ALB, the real Airflow DAG runs, the real Spark jobs, the ingress/pod status) is from that personal sandbox, not from a Localytics-provisioned account.

The Terraform is written to be region/account-agnostic (see `infra/terraform/README.md` for the two-phase apply and remote-state setup) — the same configuration will be re-applied unchanged to the official interview account the moment that invite arrives, and fresh evidence recaptured there. If you're reading this after that has happened, this note should have been updated to say so; if it still reads like this, the invite hadn't arrived as of the last commit.

## Known gap: the live scoring API doesn't yet read from DynamoDB

`docs/architecture/04-serving.md` describes a DynamoDB fast-lookup table as the production serving layer, with Athena for analytical queries. That DynamoDB table **is** provisioned by Terraform with the correct IRSA grants (`infra/terraform/modules/storage/main.tf`, `modules/irsa/main.tf`) — but the deployed `api-service` does not actually read from it yet. It currently serves `/score/{customer_id}` from a static JSON snapshot (`models/customer_scores.json`, synced from the S3 model registry at pod startup), which is refreshed from the real Gold Spark job's output but is still a point-in-time snapshot, not a live per-request DynamoDB read.

Practical implications:
- A `customer_id` that exists in the snapshot at pod-startup time scores correctly, with real SHAP explanations.
- A genuinely novel customer_id with no Gold-layer row at all (a true cold start) returns a `404`, not an on-the-fly computed score — there's no live feature-computation fallback in the request path.

This is a real, acknowledged gap between the target design and the current implementation, not an oversight discovered after the fact — wiring the DynamoDB read (and a genuine cold-start fallback that computes RFM features from raw events on demand) is the next concrete step if this goes further, and the infrastructure to do it is already in place.

## Everything else

`docs/` is the source of truth for architecture, modeling, evaluation, explainability, and fairness — all with real computed numbers from the actual synthetic dataset, not placeholders. `docs/evidence/README.md` has the full deployment verification trail, including a detailed account of every real infrastructure bug found and fixed by actually running the pipeline end-to-end (not just `terraform plan`).
