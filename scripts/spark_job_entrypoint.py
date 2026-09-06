"""
Standalone entrypoint so silver_transform/gold_transform/compaction can
run via `spark-submit` (locally, or as a SparkApplication CR in
production — see docs/architecture/03-orchestration.md). The functions
themselves take/return DataFrames and are unit-tested directly
(tests/test_spark_jobs.py); this script just wires them to file I/O.

Usage:
  spark-submit scripts/spark_job_entrypoint.py silver --input /data/bronze --output /data/silver
  spark-submit scripts/spark_job_entrypoint.py gold --input /data/silver --output /data/gold \
      --model /models/xgboost_model.json --baseline /models/baseline.pkl --as-of 2024-06-01T12:00:00Z
  spark-submit scripts/spark_job_entrypoint.py compaction --input /data/bronze --output /data/bronze_compacted
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

# spark-submit runs this file directly (not via `python -m`), so the repo
# root needs to be on sys.path explicitly for `from src....` imports to work.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.spark_jobs.compaction import compact_small_files
from src.spark_jobs.gold_transform import run_gold_transform
from src.spark_jobs.silver_transform import build_local_spark_session, run_silver_transform


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["silver", "gold", "compaction"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--as-of", default="2024-06-01T12:00:00Z")
    args = parser.parse_args()

    spark = build_local_spark_session(f"{args.job}-job")

    if args.job == "silver":
        # multiLine handles a pretty-printed JSON array (like
        # data/synthetic/synthetic_events.json); real Bronze in production
        # is one-JSON-object-per-line append files, which read fine either way.
        bronze = spark.read.option("multiLine", "true").json(args.input)
        silver = run_silver_transform(bronze)
        silver.write.mode("overwrite").parquet(args.output)
        print(f"[silver] wrote {silver.count()} rows to {args.output}")

    elif args.job == "gold":
        if not args.model or not args.baseline:
            raise SystemExit("gold job requires --model and --baseline")
        silver = spark.read.parquet(args.input)
        with open(args.baseline, "rb") as f:
            baseline = pickle.load(f)
        result = run_gold_transform(
            spark, silver, as_of=pd.Timestamp(args.as_of),
            model_path=Path(args.model), baseline=baseline,
        )
        for name, df in result.items():
            out_path = f"{args.output}/{name}"
            df.write.mode("overwrite").parquet(out_path)
            print(f"[gold] wrote {df.count()} rows to {out_path}")

    elif args.job == "compaction":
        df = spark.read.parquet(args.input)
        before = df.rdd.getNumPartitions()
        compacted = compact_small_files(df)
        compacted.write.mode("overwrite").parquet(args.output)
        print(f"[compaction] {before} -> {compacted.rdd.getNumPartitions()} partitions, wrote to {args.output}")

    spark.stop()


if __name__ == "__main__":
    main()
