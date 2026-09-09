# Observability

![Observability architecture](diagrams/05-observability.svg)

This is the direct answer to the assignment's production-readiness ask: "how do you expose observability metrics (latency, error rate) back to the centralized platform dashboard."

## Logging

Every component (API, Spark jobs, live simulator) is meant to ship stdout to **CloudWatch Logs**, one log group per component (`/eks/churn-fde-sandbox/api-service`, `/eks/churn-fde-sandbox/spark-gold`, etc.) — queryable via CloudWatch Logs Insights when something fails.

**Real gap found mid-project, not by reading this doc but by actually checking log group *contents*, not just that `modules/observability` provisioned the groups**: all 8 groups had zero log streams the entire time. Root cause: this doc's original assumption — "the standard EKS logging path (Fluent Bit DaemonSet)" — doesn't hold here, because a DaemonSet can't schedule onto Fargate at all (no persistent node to run on), and nearly every pod in this cluster is Fargate-scheduled (see [01-data-platform.md](01-data-platform.md) / the `apps`/`system` Fargate profiles). Fargate ships logs via its own built-in log router instead, activated by an `aws-observability` namespace + `aws-logging` ConfigMap (neither existed) plus CloudWatch Logs permissions on the Fargate pod execution role (also missing). Added all three via Terraform (`modules/eks`, `modules/k8s-addons`) — a `kubernetes` filter + `rewrite_tag` rules route each pod to its existing per-component log group by pod-name pattern (e.g. anything matching `silver-transform` → `spark-silver`), not a single undifferentiated catch-all.

**Honest status**: deployed safely (verified zero disruption to already-running demo pods — `modules/eks`'s IAM policy and `modules/k8s-addons`'s new namespace/ConfigMap are purely additive; Fargate only applies new logging config to pods *created after* the ConfigMap exists, never retroactively to pods already running). Not yet confirmed shipping real log content as of this pass — three real test runs across ~25 minutes after applying still showed zero log streams in every group, and no unexpected log group was created elsewhere in the account either (ruling out simple misrouting). The mechanism is deployed correctly per AWS's documented EKS Fargate logging feature; the most likely remaining culprit is the `Parser crio` line in `filters.conf` (Fargate's actual container log format may not match), worth removing/adjusting as the next debugging step — not yet done here to avoid further live-system risk mid-verification.

## Metrics

- **Custom app metrics** via **CloudWatch EMF** (Embedded Metric Format), using the `aws-embedded-metrics` Python library in the FastAPI service — request latency/error rate/throughput on `/score` and `/events/ingest`, logged as structured JSON that CloudWatch automatically extracts into real metrics. No Prometheus/Grafana stack needed.
- **Spark job metrics** — success/failure and duration emitted as a metric at job completion.
- **EKS Container Insights** — pod CPU/memory/restarts across the cluster.

## Tracing

**AWS X-Ray**, end-to-end (ingress -> API -> DynamoDB/S3 -> response) — a real per-hop latency breakdown for a given request, not just an aggregate number.

## Dashboard — organized around the assignment's own 4 production-readiness pillars

Rather than one undifferentiated wall of metrics, the CloudWatch Dashboard has four named panel groups, each answering one specific question from the assignment's production-readiness section:

| Panel group | Answers | Metrics |
|---|---|---|
| **Auth** | Is the service-to-service auth actually working, and is anyone trying to break it? | Successful vs. failed auth attempts (401/403 rate) on `/score` and `/events/ingest`, broken out by caller (simulator vs. console vs. other) |
| **Rate limiting** | Is the limiter doing its job, and against whom? | Requests allowed vs. throttled (429 rate) per route, top offending callers |
| **Observability (latency/errors)** | Is the service healthy right now? | P50/P90/P99 latency, error rate, throughput on `/score` and `/events/ingest` (via EMF), plus X-Ray trace map |
| **Failure modes** | When something breaks, what actually broke, and did we degrade gracefully? | DynamoDB throttle/error rate, S3 read/write errors, model-load failures triggering the cold-path fallback, Spark job failure count (from Airflow/SparkApplication status), Spark job success/failure + duration, EKS Container Insights (pod restarts/OOMs) |

This structure is deliberate: it means the dashboard itself is direct evidence for the "how does this handle auth/rate limiting/observability/failure modes" question, not just a claim in the write-up. Linked from the reviewer console.

## Alarms

CloudWatch Alarms on API error-rate spikes and Spark job failures, alongside the billing alarm already set on the AWS account — ties to the "operate this at 3am" mindset the assignment explicitly asks about, not just an unwatched dashboard.

## Spark History Server

Every Spark job runs with `spark.eventLog.enabled=true`, writing event logs to `s3://<bucket>/spark-events/`. A small always-on Deployment (Fargate) serves the History Server UI behind the ingress — reviewers can browse real job DAGs, stage timings, and executor metrics live, not just take the architecture diagram's word for it.

## Cost gotcha we deliberately avoid

CloudWatch Log Groups default to **never-expire retention** — on a strict-budget shared account, that's a silent, easy-to-miss cost leak. Terraform sets explicit 7-14 day retention on every log group we create.
