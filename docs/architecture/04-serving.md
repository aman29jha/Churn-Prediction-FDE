# Serving — Real-Time API + Operational/Analytical Split

![Serving architecture](diagrams/04-serving.svg)

## Current implementation status (read this before the design below)

The DynamoDB fast path described below is now real: `src/service/app.py`'s
`/score/{customer_id}` reads live, per-request, from the `customer_scores`
DynamoDB table (`src/service/dynamodb_client.py`). Every `gold_transform`
run writes each customer's current feature vector + `churn_probability` +
segment there (`scripts/spark_job_entrypoint.py --dynamodb-table`), so a
score reflects the latest Gold run immediately — no pod restart needed,
closing the gap this section used to describe.

One piece of the original target design is still not built: true
on-the-fly cold-start compute (deriving RFM features from raw Silver
events for a customer with no Gold row at all, e.g. one added between Gold
runs). What exists instead is a two-tier lookup — DynamoDB first, falling
back to the static JSON snapshot (`models/customer_scores.json`, synced
from S3 at pod startup) only if DynamoDB has no row either. A `customer_id`
missing from *both* still returns a `404`, not a computed score. Stated
honestly as the one remaining gap, not glossed over.

## Two stores, two purposes

A standard, deliberate pattern rather than one store trying to do both jobs:

- **Athena / Iceberg Gold tables** — the **analytical** layer. Ad-hoc marketing queries ("give me everyone in the Hibernating segment"), dashboards, the reviewer console's aggregate views. Query latency of 1-2+ seconds is fine here.
- **DynamoDB `customer_scores`** (keyed by `customer_id`, written by the Gold Spark job after each run) — the **operational** layer. Fast, O(1) point lookups for the real-time API. Querying Athena per API request would be far too slow for live scoring.

## `/score/{customer_id}`

1. Request hits the ingress (TLS termination) -> auth middleware -> rate limiter -> FastAPI.
2. **Fast path** (real, deployed): look up the latest precomputed score in DynamoDB — written by the last `gold_transform` run, which now scores customers using a rolling `as_of=now()` (`--live-scoring`, see [01-data-platform.md](01-data-platform.md)) rather than a fixed historical cutoff, so this genuinely reflects current data.
3. **Cold-start fallback** (DynamoDB has no row — e.g. before Gold has ever run): the static JSON snapshot synced from S3 at pod startup. Still a gap: true on-the-fly compute from raw Silver events for a customer in neither store isn't built — see "Current implementation status" above.
4. Response includes the probability, the RFM segment, and (for the console) a SHAP-based explanation — see [../modeling.md](../modeling.md).

## `/events/ingest`

Same service, different route — see [02-simulator.md](02-simulator.md) for what calls it and [03-orchestration.md](03-orchestration.md) for what happens after it writes to Bronze.

## Auth, rate limiting, tracing

- **Auth**: bearer token validated at the API layer; the live simulator's token lives in a Kubernetes Secret, injected as an env var — this is our service-to-service auth story.
- **Rate limiting**: a token-bucket limiter in front of both routes, protecting against a runaway simulator misconfiguration or an abusive caller.
- **Tracing**: AWS X-Ray end-to-end (ingress -> API -> DynamoDB/S3 -> response) — a real per-hop latency breakdown, not just an aggregate number. See [05-observability.md](05-observability.md).

## Ingress

ALB or nginx ingress (Terraform-managed), fronting both the scoring/ingest API and the Streamlit reviewer console (separate route, separate auth — see [06-reviewer-console.md](06-reviewer-console.md)) — this is the concrete answer to the assignment's "how does this integrate with the platform's existing ingress/gateway stack" requirement.
