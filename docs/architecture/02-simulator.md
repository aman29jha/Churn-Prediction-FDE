# Live Simulator

![Simulator architecture](diagrams/02-simulator.svg)

## Purpose

A static one-time synthetic file doesn't demo well — reviewers would just see a frozen CSV. The live simulator makes the end-to-end pipeline demonstrably real: new events show up, flow through ingest, get processed, and update scores, all while a reviewer watches.

## Two generators, two jobs

### 1. Bootstrap generator (one-time, run locally before anything else)
Generates the full ~1,200-customer historical dataset (2 years of backdated events per our archetype design — see [../modeling.md](../modeling.md)), writes it as the initial Bronze Iceberg load, and writes `S3: sim-state/customer_registry.json` (customer_id + assigned archetype for all ~1,200 customers) so the live simulator has a consistent population to keep generating events for.

### 2. Live trickle generator (recurring, Airflow DAG)
Runs as its own **`live_simulator_dag`** in Airflow (see [03-orchestration.md](03-orchestration.md)), scheduled every ~10 minutes, using a `KubernetesPodOperator` — not a raw Kubernetes CronJob. Each run:
1. Reads the customer registry from S3.
2. Picks 5-20 customers, weighted by their archetype's activity rate (high-engagement customers show up more often).
3. Generates 1-3 new events per selected customer, using the same fitted distributions as the bootstrap generator, **timestamped to current wall-clock time** (not backdated) — this is what makes it feel live.
4. POSTs the batch to the `/events/ingest` API endpoint, authenticated with a bearer token stored in a Kubernetes Secret.

## Ingest path

`/events/ingest` (on the same FastAPI service as scoring) validates the request's auth token and payload schema, applies a rate limit, then appends the batch directly into the **Bronze Iceberg table** via a lightweight Python Iceberg writer — no Spark needed for a simple append.

That write creates a new S3 data file, which is what feeds the event-driven processing trigger (S3 Event Notification -> SNS -> SQS -> Lambda -> Airflow) described in [03-orchestration.md](03-orchestration.md) — the simulator doesn't need to know or care how downstream processing gets kicked off.

## How the SQS batching actually works

`03-orchestration.md` already covers the AWS-level config (`BatchSize=20`, `MaximumBatchingWindowInSeconds=300`); this is what that config actually does in practice, since it matters for reading the trigger Lambda's own logs live.

**The batching itself needs no code.** Lambda's **event source mapping** — a background poller AWS manages for you, not something the handler implements — long-polls the SQS queue continuously and accumulates messages until *either* 20 have arrived *or* 5 minutes have passed since the first one in the batch, whichever comes first. Only then does it invoke the Lambda **once**, with every accumulated message bundled into `event["Records"]`. With `live_simulator_dag` firing roughly every 10 minutes and producing one SQS message per run, batches in this deployment are almost always time-boundary batches of 1 message, not count-boundary batches of 20 — worth knowing if a reviewer asks "so it waits for 20 messages?" (no — whichever limit hits first, and in practice here it's almost always the 5-minute window with a single message in it, since 20 simulator runs would take over 3 hours to accumulate).

**What the handler does with that batch** (`infra/terraform/modules/messaging/lambda_src/handler.py`) is deliberately not about the messages' *contents* at all:

```python
def handler(event, context):
    message_count = len(event.get("Records", []))  # only used for logging

    if _has_active_run():
        print(f"... already has a queued/running run — skipping trigger for this batch "
              f"({message_count} SQS message(s)); the pending run will include this data too.")
        return {"triggered": False, ...}

    print(f"Triggering {DAG_ID}: {message_count} SQS message(s) in this batch")
    status, _ = _with_retries("POST", f"{_url.path}/dags/{DAG_ID}/dagRuns", json.dumps({}))
```

The handler never parses individual S3 keys out of the SQS/SNS message bodies — it doesn't need to, because `silver_transform` re-scans the *entire* Bronze prefix every run regardless of which file triggered it. So the Lambda's only real job is deciding **whether to trigger at all**, not what to do with the batch's contents: it asks Airflow (`GET .../dagRuns`) whether `medallion_pipeline_dag` already has a queued or running instance, and skips triggering if so — a pending run will pick up this batch's data too once it actually executes, so triggering again would only grow a redundant backlog (a real bug found and fixed earlier in this project: an 8-deep queue after a few hours of unchecked triggers).

One deliberate reliability choice worth knowing: `_has_active_run()` **fails open** — if the Airflow status check itself errors for any reason, it triggers anyway rather than silently skipping. Reasoning in the code's own words: "a duplicate queued run is harmless, a silently-dropped real trigger isn't." That asymmetry (tolerate a harmless duplicate over risk a silent miss) is a good one to be able to state out loud if asked why the check isn't stricter.

## Start/pause control

Because it's an Airflow DAG rather than a raw CronJob, control happens through **Airflow's native per-DAG pause/unpause toggle** in the UI — the same switch every other DAG has, no `kubectl` needed:
- **Pause**: flip `live_simulator_dag` off in the Airflow UI (or `airflow dags pause live_simulator_dag`) — stops new scheduled runs immediately, any in-flight run finishes.
- **Resume**: flip it back on, or use the native **"Trigger DAG"** button for an immediate one-off run outside the schedule.

This is a genuine improvement over a `kubectl patch cronjob ... suspend` toggle: one consistent control plane (the Airflow UI) for every recurring/on-demand process in the system, and it can be flipped live in front of reviewers on screen — pause it beforehand, then unpause (or manually trigger) it live so the reviewer watches new data flow through ingest → Bronze → Silver → Gold → the API in real time, without leaving the browser.
