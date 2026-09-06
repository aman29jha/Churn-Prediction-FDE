"""
Bronze -> Silver Spark job. In production this runs as a SparkApplication
CR (Spark Operator, on-demand via Airflow — see docs/architecture/03-orchestration.md)
reading/writing Iceberg tables via the Glue Catalog. Validated here in
local[*] mode against a fixture before ever touching a cluster, per the
staff-engineer execution plan's highest-leverage pre-AWS step.

Responsibility: dedup by event_id, validate event_type against the known
enum (drop anything else — quarantining rather than crashing the job),
standardize timestamp to UTC, and parse `properties` into typed top-level
columns per docs/architecture/01-data-platform.md's Silver definition
("rather than a loose JSON blob") — this also happens to be what keeps
the Spark->pandas handoff into Gold clean (a struct column would arrive
in pandas as Row objects, not plain scalars). Genuinely Spark's job
because this operates at raw-event scale, unlike Gold's customer-grained
aggregation (see gold_transform.py for why that reuses pandas instead).
"""
from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

KNOWN_EVENT_TYPES = [
    "session", "purchase", "push_sent", "push_open", "campaign_click", "in_app_event", "support_ticket",
]


def run_silver_transform(bronze_df: DataFrame) -> DataFrame:
    deduped = bronze_df.dropDuplicates(["event_id"])

    valid = deduped.filter(F.col("event_type").isin(KNOWN_EVENT_TYPES))
    dropped_count = deduped.count() - valid.count()
    if dropped_count > 0:
        print(f"[silver_transform] quarantined {dropped_count} event(s) with an unrecognized event_type")

    standardized = valid.withColumn("timestamp", F.to_timestamp(F.col("timestamp")))

    # Flatten properties.* into typed top-level columns.
    property_fields = [f.name for f in standardized.schema["properties"].dataType.fields] if "properties" in standardized.columns else []
    flattened = standardized.select(
        "event_id", "customer_id", "event_type", "timestamp",
        *[F.col(f"properties.{name}").alias(name) for name in property_fields],
    )
    return flattened


def build_local_spark_session(app_name: str = "silver-transform-local") -> SparkSession:
    return (
        SparkSession.builder.appName(app_name)
        .master("local[*]")
        .config("spark.sql.shuffle.partitions", "4")  # small for local dev; production sizes this per cluster
        # Without this, Spark uses the JVM's local timezone when converting
        # TimestampType during toPandas()/read, silently shifting every
        # timestamp by the local UTC offset relative to pandas' UTC-aware
        # values — a real bug this local validation caught (see
        # tests/test_spark_jobs.py::test_gold_matches_pandas_path_exactly).
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
