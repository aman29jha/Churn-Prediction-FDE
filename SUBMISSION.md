# Submission Notes

Read this first — it covers the two things a reviewer would otherwise have to discover on their own.

## Which AWS account this was deployed to

Built and deployed against AWS account `784004375291` (`ap-south-1`) — this is the account being used for this interview, so the evidence in `docs/evidence/` (the live ALB, the real Airflow DAG runs, the real Spark jobs, the ingress/pod status) is from the same account this submission is reviewed against, not a separate throwaway sandbox. Build started before the account was confirmed as the interview account, so earlier docs/commit messages in this repo's history refer to it as a "personal sandbox" — that framing is now out of date; this is the real deployment.

## Known gap: the live scoring API doesn't yet read from DynamoDB

`docs/architecture/04-serving.md` describes a DynamoDB fast-lookup table as the production serving layer, with Athena for analytical queries. That DynamoDB table **is** provisioned by Terraform with the correct IRSA grants (`infra/terraform/modules/storage/main.tf`, `modules/irsa/main.tf`) — but the deployed `api-service` does not actually read from it yet. It serves `/score/{customer_id}` from a static JSON snapshot (`models/customer_scores.json`, synced from the S3 model registry at pod startup). That snapshot is now refreshed with the real Gold Spark job's full output (1,280 customers — the synthetic set plus the real 80-customer sample, previously only 1,200 synthetic-only), so every customer the pipeline has ever actually scored is servable — but it is still a point-in-time snapshot, not a live per-request DynamoDB read.

Practical implications:
- A `customer_id` that exists in the snapshot at pod-startup time scores correctly, with real SHAP explanations (verified for both a synthetic and a real-sample customer_id).
- A genuinely novel customer_id with no Gold-layer row at all (a true cold start) returns a `404`, not an on-the-fly computed score — there's no live feature-computation fallback in the request path.

This is a real, acknowledged gap between the target design and the current implementation, not an oversight discovered after the fact — wiring the DynamoDB read (and a genuine cold-start fallback that computes RFM features from raw events on demand) is the next concrete step if this goes further, and the infrastructure to do it is already in place.

## Fixed since the review: the Bronze/Silver/Gold "tables" are now real Iceberg tables

An earlier version of this deployment claimed Iceberg tables on Glue Catalog (per `docs/architecture/01-data-platform.md`) but the actual Silver/Gold write path only ever wrote plain Parquet to a raw S3 path — `aws glue get-tables` returned zero tables, and there was no `metadata/` folder or Avro manifest anywhere in S3 (the standard Iceberg-vs-plain-Parquet tell). This has been fixed: `scripts/spark_job_entrypoint.py` now writes each output as a real Iceberg table via a named `glue_catalog` Spark catalog (`org.apache.iceberg.spark.SparkCatalog` + `GlueCatalog` + `S3FileIO`), alongside the existing plain-Parquet write (kept so nothing already depending on the plain S3 paths broke). Verified live: 5 real tables now registered in Glue (`silver_events`, `rfm_features`, `churn_scores`, `rfm_segments`, `kpi_daily`), queried directly via Athena with real SQL — see `docs/evidence/athena_iceberg_verification.txt`. Row counts match exactly what was independently verified via direct S3 reads (58,738 silver events; 1,280 customers across rfm_features/churn_scores/rfm_segments).

Also implemented for real in the same pass: the `analytics_dag` pipeline, which had never actually been run (its SparkApplication spec passed a "gold" placeholder argument that would have crashed on first trigger — confirmed it never had been). Now runs a real `kpi_daily` Iceberg table (DAU, revenue, push-open/campaign-click rates by day), unit-tested and verified via Athena. The other two documented analytics tables (`segment_migration`, `cohort_retention`) remain not implemented — both need Gold's tables to be append-only/partitioned by run_date first, a separate, larger change not made here; stated as a real limitation, not glossed over.

## Everything else

`docs/` is the source of truth for architecture, modeling, evaluation, explainability, and fairness — all with real computed numbers from the actual synthetic dataset, not placeholders. `docs/evidence/README.md` has the full deployment verification trail, including a detailed account of every real infrastructure bug found and fixed by actually running the pipeline end-to-end (not just `terraform plan`).
