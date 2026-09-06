# Orchestration — Event-Driven Triggering + Airflow

![Orchestration architecture](diagrams/03-orchestration.svg)

## Why not a fixed cron schedule

Running the Silver/Gold pipeline on a blind fixed interval means either wasting compute on empty runs (nothing new arrived) or missing bursts (trickle rate varies). Instead, processing is **triggered by actual data arrival**, with a time-based safety net so data never sits unprocessed indefinitely.

## Trigger chain: S3 -> SNS -> SQS -> Lambda -> Airflow

1. Every new event batch written to Bronze (from the live simulator via `/events/ingest`, or the bootstrap load) creates a new S3 data file.
2. An **S3 Event Notification** (`s3:ObjectCreated:*`) on that path publishes to an **SNS topic** (`bronze-data-arrived`).
3. SNS fans out to an **SQS queue** (`bronze-data-arrived-queue`) — durable, poll-based, decoupled from the producer.
4. A **Lambda function** is wired to the queue via an **SQS event-source mapping** configured with `BatchSize=20` and `MaximumBatchingWindowInSeconds=300` — AWS's native batching semantics, no custom polling loop needed. Lambda fires when EITHER 20 messages have accumulated OR 5 minutes have passed, whichever comes first.
5. On invocation, the Lambda calls the **Airflow REST API** (`POST /dags/medallion_pipeline_dag/dagRuns`) to trigger a pipeline run. `medallion_pipeline_dag` has `schedule_interval=None` — it only ever runs when triggered, never polled on a timer.

This gives us: prompt processing when there's a real burst of activity, no wasted runs during quiet periods, and a guaranteed maximum staleness (5 minutes) even during a slow trickle.

## Spark Operator CRDs

We installed the **Spark Operator** (a separate controller + CRDs + admission webhook) rather than using plain `spark-submit`, for declarative job specs and built-in status/retry/history:

- **`SparkApplication`** — on-demand, one-shot job. Used for **Silver** and **Gold**, submitted via Airflow's `SparkKubernetesOperator` task, which creates the `SparkApplication` custom resource and lets the Operator's controller handle actually running it. `kubectl get sparkapplications` shows real, live job status — a concrete artifact for reviewers, not just a diagram.
- **`ScheduledSparkApplication`** — the Operator's own native cron scheduling, wrapping a `SparkApplication` template with a `schedule` field. Used for **Compaction**, which is genuinely just "run on a timer, no dependencies" — it doesn't need Airflow's DAG machinery on top, so it runs independently via the Operator's own scheduler.

## Airflow

**Self-hosted on EKS** (Helm chart, `KubernetesExecutor`) rather than managed MWAA — consistent with the same capability-demonstration choice made for Spark, accepted alongside the added setup/ops time within the 4-day window.

**Deliberately minimal/lite configuration** — a single scheduler pod, a lightweight Postgres backend (no HA, no read replicas, sized for a demo not production scale), no worker autoscaling. This directly addresses the setup-time/ops-risk a full production-grade Airflow install would carry, while keeping the real DAG-dependency-chaining/retry/UI story intact.

**Live-demo safety net**: Airflow's UI has a native **"Trigger DAG"** button — if the automated event-driven chain (S3 -> SNS -> SQS -> Lambda) has any hiccup during a live reviewer call, triggering `medallion_pipeline_dag` manually from the UI guarantees the pipeline still runs, with zero extra code needed to build that fallback.

- **`medallion_pipeline_dag`**: `Silver task -> Gold task`, real dependency chaining (Gold only runs if Silver succeeded) and automatic retries — not a timer-based guess at sequencing.
- **`training_dag`**: manual trigger or infrequent (e.g. weekly) schedule, `KubernetesPodOperator` running plain Python/XGBoost (not Spark — see [../modeling.md](../modeling.md) for why). Deliberately decoupled from the main pipeline's cadence: **train rarely, score often** is itself a production-readiness talking point.
- **`analytics_dag`**: daily, time-based schedule (not event-triggered like `medallion_pipeline_dag`) — KPI trends and cohort retention don't need low-latency refresh, and shouldn't couple to or slow down the churn-scoring critical path. See [07-analytics.md](07-analytics.md).
- **`live_simulator_dag`**: the live trickle generator (see [02-simulator.md](02-simulator.md)), running as an Airflow DAG rather than a raw Kubernetes CronJob specifically so it shares the same start/pause control plane as everything else — Airflow's native per-DAG pause/unpause toggle, flippable live during a reviewer demo with no `kubectl` needed.
- **Airflow UI**: DAG run history, retries, logs — linked from the reviewer console.
