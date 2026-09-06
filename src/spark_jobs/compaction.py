"""
Compaction job. In production this is a `ScheduledSparkApplication` (the
Spark Operator's own native cron, not an Airflow DAG — see
docs/architecture/03-orchestration.md) running Iceberg's maintenance
procedures directly against the Glue-cataloged tables:

    CALL glue_catalog.system.rewrite_data_files(table => 'bronze.events')
    CALL glue_catalog.system.expire_snapshots(table => 'bronze.events', older_than => ...)

Iceberg's table-maintenance procedures require an actual Iceberg catalog
(Glue in production) — there's nothing meaningful to validate locally
without one. What *is* validated here, against local Parquet, is the
underlying mechanic both procedures rely on: coalescing many small files
into fewer larger ones. This is a stand-in for the real procedure calls,
not a reimplementation of them.
"""
from __future__ import annotations

from pyspark.sql import DataFrame


def compact_small_files(df: DataFrame, target_partitions: int = 4) -> DataFrame:
    """Coalesces a DataFrame that's spread across many small partitions
    (simulating many small files accumulated from the live simulator's
    frequent small ingest batches) into fewer, larger partitions."""
    return df.coalesce(target_partitions)


def count_partitions(df: DataFrame) -> int:
    return df.rdd.getNumPartitions()
