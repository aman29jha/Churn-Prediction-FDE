"""
Standalone entrypoint so silver_transform/gold_transform/compaction can
run via `spark-submit` (locally, or as a SparkApplication CR in
production — see docs/architecture/03-orchestration.md). The functions
themselves take/return DataFrames and are unit-tested directly
(tests/test_spark_jobs.py); this script just wires them to file I/O.

Usage:
  spark-submit scripts/spark_job_entrypoint.py silver --input /data/bronze --output /data/silver
  # Real production scoring (medallion_pipeline_dag's actual deployed spec): no
  # --as-of, so it defaults to now() — --live-scoring so features use every
  # event known right now, not the offline train/eval path's held-out gap.
  spark-submit scripts/spark_job_entrypoint.py gold --input /data/silver --output /data/gold \
      --model /models/xgboost_model.json --baseline /models/baseline.pkl \
      --live-scoring --dynamodb-table churn-fde-sandbox-customer-scores
  # Reproducible backtest/eval (fixed historical cutoff, matches training's own as-of):
  spark-submit scripts/spark_job_entrypoint.py gold --input /data/silver --output /data/gold \
      --model /models/xgboost_model.json --baseline /models/baseline.pkl --as-of 2024-06-01T12:00:00Z
  spark-submit scripts/spark_job_entrypoint.py compaction --input /data/bronze --output /data/bronze_compacted
  spark-submit scripts/spark_job_entrypoint.py analytics --input /data/silver --output /data/gold/analytics

Add --iceberg-catalog-db (e.g. glue_catalog.churn_fde_sandbox) to ALSO
write each output as a real Iceberg table registered in that catalog's
database, alongside the existing plain-Parquet write (kept as-is so
existing consumers of the plain S3 paths — e.g. the model-registry
customer_scores.json refresh — are unaffected). Requires the SparkSession
to have that catalog configured via sparkConf (spark.sql.catalog.<name>.*)
— see docs/architecture/01-data-platform.md and
airflow/dags/specs/*.yaml's sparkConf block.

The Iceberg write is a real MERGE INTO upsert, not a blind overwrite or
append — see _write_iceberg's docstring for why: an overwrite loses
history on every run (breaks docs/architecture/07-analytics.md's stated
history-retention prerequisite), while a naive append would duplicate
every row that hasn't changed since the last run (real event replay /
same-day re-run both hit this). MERGE INTO, keyed appropriately per
table, updates matching rows in place and only inserts genuinely new
ones — see docs/architecture/01-data-platform.md.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# spark-submit runs this file directly (not via `python -m`), so the repo
# root needs to be on sys.path explicitly for `from src....` imports to work.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from pyspark.sql import DataFrame, SparkSession

from src.spark_jobs.analytics_transform import run_kpi_daily_transform
from src.spark_jobs.compaction import compact_small_files
from src.spark_jobs.gold_transform import run_gold_transform
from src.spark_jobs.silver_transform import build_local_spark_session, run_silver_transform


def _write_iceberg(spark: SparkSession, df: DataFrame, iceberg_table: str, merge_keys: list[str], row_label: str) -> None:
    """Upsert `df` into `iceberg_table`, keyed on `merge_keys`.

    Bootstraps the table on first run (it doesn't exist yet — Iceberg's
    MERGE INTO requires an existing target). Every run after that is a
    real MERGE: rows whose key already exists get UPDATEd in place (an
    event/customer re-processed on a later run doesn't duplicate — the
    exact bug a plain `.append()` would create), rows with a new key get
    INSERTed. This is what makes Gold's tables genuinely
    append-only-per-run_date (history preserved across days) while still
    being idempotent within a single day's re-run.
    """
    try:
        spark.sql(f"DESCRIBE TABLE {iceberg_table}")
        table_exists = True
    except Exception:
        table_exists = False

    if not table_exists:
        df.writeTo(iceberg_table).using("iceberg").create()
        print(f"[{row_label}] bootstrapped new Iceberg table {iceberg_table} with {df.count()} rows")
        return

    # Schema evolution: a column added to this job's output after the
    # target table was first bootstrapped (e.g. this session's run_date
    # addition to Gold) would otherwise fail MERGE with "cannot resolve
    # <col> in MERGE command" — real failure hit live when rfm_features/
    # churn_scores/rfm_segments (bootstrapped before run_date existed)
    # were re-merged. Iceberg supports additive schema evolution; widen
    # the target first so the MERGE below always sees every source column.
    existing_cols = set(spark.table(iceberg_table).columns)
    new_cols = [c for c in df.columns if c not in existing_cols]
    if new_cols:
        add_cols_sql = ", ".join(f"{c} {df.schema[c].dataType.simpleString()}" for c in new_cols)
        spark.sql(f"ALTER TABLE {iceberg_table} ADD COLUMNS ({add_cols_sql})")
        print(f"[{row_label}] evolved {iceberg_table} schema, added columns: {new_cols}")

    temp_view = f"_merge_source_{row_label}_{abs(hash(iceberg_table))}"
    df.createOrReplaceTempView(temp_view)
    match_cond = " AND ".join(f"target.{k} = source.{k}" for k in merge_keys)
    non_key_cols = [c for c in df.columns if c not in merge_keys]
    update_set = ", ".join(f"target.{c} = source.{c}" for c in non_key_cols)
    insert_cols = ", ".join(df.columns)
    insert_vals = ", ".join(f"source.{c}" for c in df.columns)
    merge_sql = f"""
        MERGE INTO {iceberg_table} target
        USING {temp_view} source
        ON {match_cond}
        WHEN MATCHED THEN UPDATE SET {update_set}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
    """
    spark.sql(merge_sql)
    print(f"[{row_label}] merged {df.count()} rows into Iceberg table {iceberg_table} (keys: {merge_keys})")


def _dynamodb_value(v):
    """boto3's DynamoDB serializer rejects raw Python float and doesn't
    recognize numpy scalar types at all — convert to what it accepts."""
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return Decimal(str(v))
    return v


def _write_dynamodb(rows_pd: pd.DataFrame, table_name: str) -> None:
    """Real per-customer fast-lookup write, feeding /score's live
    DynamoDB read (src/service/dynamodb_client.py) — see
    docs/architecture/04-serving.md. ~1,280 rows even at this dataset's
    full scale, so a plain boto3 batch_writer from the driver (no Spark
    connector) is the right-sized tool, same reasoning as gold_transform's
    own toPandas() docstring.
    """
    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    with table.batch_writer(overwrite_by_pkeys=["customer_id"]) as batch:
        for row in rows_pd.to_dict(orient="records"):
            item = {k: _dynamodb_value(v) for k, v in row.items() if pd.notna(v)}
            batch.put_item(Item=item)
    print(f"[gold] wrote {len(rows_pd)} rows to DynamoDB table {table_name}")


def _write(spark: SparkSession, df: DataFrame, plain_path: str, iceberg_table: str | None, merge_keys: list[str], row_label: str) -> None:
    # Plain-Parquet write stays a full overwrite deliberately: it's the
    # "latest snapshot only" path the model-registry customer_scores.json
    # refresh reads directly (see SUBMISSION.md) — history belongs solely
    # in the Iceberg table, keeping the two representations' semantics
    # distinct rather than accidentally duplicating history logic twice.
    df.write.mode("overwrite").parquet(plain_path)
    print(f"[{row_label}] wrote {df.count()} rows to {plain_path}")
    if iceberg_table:
        _write_iceberg(spark, df, iceberg_table, merge_keys, row_label)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["silver", "gold", "compaction", "analytics"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument(
        "--as-of", default=None,
        help="Fixed cutoff for reproducible backtests/training. Omit for the real production gold run — "
             "defaults to the current time, i.e. score customers using everything known right now.",
    )
    parser.add_argument(
        "--live-scoring", action="store_true",
        help="Real scoring semantics: features use every event up to --as-of (normally 'now'), not the "
             "offline train/eval path's held-out 60-day gap — see compute_rfm_features's docstring.",
    )
    parser.add_argument(
        "--dynamodb-table", default=None,
        help="e.g. churn-fde-sandbox-customer-scores — when set, also writes each customer's current "
             "feature vector + churn_probability + segment there for /score's live per-request read.",
    )
    parser.add_argument(
        "--iceberg-catalog-db", default=None,
        help="e.g. glue_catalog.churn_fde_sandbox — when set, also writes each output as a real Iceberg table there.",
    )
    args = parser.parse_args()

    spark = build_local_spark_session(f"{args.job}-job")
    catalog_db = args.iceberg_catalog_db

    try:
        _run_job(args, spark, catalog_db)
    except Exception:
        # Real gap found checking the CloudWatch dashboard's own "Failure
        # modes" panel: its title promised "Spark job failures" but no
        # metric anywhere ever tracked that. This catches real
        # application-level failures (bad input schema, a permissions
        # error mid-run, a genuine bug) — not pod-scheduling-level
        # failures where this Python process never gets to start at all
        # (e.g. the ConfigMap-mount race documented in
        # medallion_pipeline_dag.py), which need Airflow/K8s-level
        # observability instead, a real gap not closed here. Re-raises
        # unconditionally: this is additive telemetry, never swallows
        # the actual failure Airflow/the Spark Operator need to see.
        _put_metric_safe("SparkJobFailure", {"JobType": args.job})
        raise
    finally:
        spark.stop()


def _put_metric_safe(name: str, dimensions: dict[str, str]) -> None:
    try:
        import boto3

        boto3.client("cloudwatch").put_metric_data(
            Namespace="ChurnService",
            MetricData=[{
                "MetricName": name,
                "Value": 1.0,
                "Unit": "Count",
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
            }],
        )
    except Exception as exc:
        print(f"Failed to publish {name} metric (non-fatal): {exc}")


def _run_job(args, spark: SparkSession, catalog_db: str | None) -> None:
    if args.job == "silver":
        # multiLine handles a pretty-printed JSON array (like
        # data/synthetic/synthetic_events.json); real Bronze in production
        # is one-JSON-object-per-line append files, which read fine either way.
        bronze = spark.read.option("multiLine", "true").json(args.input)
        silver = run_silver_transform(bronze)
        # event_id is immutable once written — merging on it means a
        # re-processed/duplicate event updates its own row in place
        # instead of duplicating, whether that's from re-running this job
        # or Bronze legitimately containing the same event twice.
        _write(
            spark, silver, args.output,
            f"{catalog_db}.silver_events" if catalog_db else None,
            ["event_id"], "silver",
        )

    elif args.job == "gold":
        if not args.model or not args.baseline:
            raise SystemExit("gold job requires --model and --baseline")
        silver = spark.read.parquet(args.input)
        with open(args.baseline, "rb") as f:
            baseline = pickle.load(f)
        as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(datetime.now(timezone.utc))
        result = run_gold_transform(
            spark, silver, as_of=as_of,
            model_path=Path(args.model), baseline=baseline,
            live_scoring=args.live_scoring,
        )
        # (customer_id, run_date): a same-day re-run updates each
        # customer's row in place (idempotent); a new day's run appends a
        # new row per customer, preserving history across run_dates.
        for name, df in result.items():
            _write(
                spark, df, f"{args.output}/{name}",
                f"{catalog_db}.{name}" if catalog_db else None,
                ["customer_id", "run_date"], "gold",
            )

        if args.dynamodb_table:
            # Join the three Gold outputs back into one per-customer row
            # (feature vector + churn_probability + segment) — exactly
            # what /score needs for a live lookup + SHAP explanation.
            combined = (
                result["rfm_features"]
                .join(
                    result["churn_scores"].select("customer_id", "run_date", "churn_probability"),
                    ["customer_id", "run_date"],
                )
                .join(
                    result["rfm_segments"].select("customer_id", "run_date", "segment"),
                    ["customer_id", "run_date"],
                )
                .withColumnRenamed("segment", "rfm_segment")
                .drop("run_date")
            )
            _write_dynamodb(combined.toPandas(), args.dynamodb_table)

    elif args.job == "compaction":
        df = spark.read.parquet(args.input)
        before = df.rdd.getNumPartitions()
        compacted = compact_small_files(df)
        compacted.write.mode("overwrite").parquet(args.output)
        print(f"[compaction] {before} -> {compacted.rdd.getNumPartitions()} partitions, wrote to {args.output}")

    elif args.job == "analytics":
        silver = spark.read.parquet(args.input)
        kpi_daily = run_kpi_daily_transform(silver)
        # event_date: re-running analytics for a date range that overlaps
        # a previous run updates those days' KPIs in place rather than
        # duplicating them.
        _write(
            spark, kpi_daily, f"{args.output}/kpi_daily",
            f"{catalog_db}.kpi_daily" if catalog_db else None,
            ["event_date"], "analytics",
        )


if __name__ == "__main__":
    main()
