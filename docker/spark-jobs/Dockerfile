# Silver/Gold/Compaction Spark jobs, submitted as SparkApplication /
# ScheduledSparkApplication CRs (Spark Operator) — see
# docs/architecture/{01-data-platform,03-orchestration}.md.
#
# Iceberg + AWS jars are added for the real deployment (Glue Catalog, S3
# reads/writes) — not exercised by the local[*] validation in
# tests/test_spark_jobs.py, which runs directly against plain
# Parquet/JSON, but required once this image runs against real Iceberg
# tables. Versions pinned to match the base image's Spark/Scala build.
FROM apache/spark-py:v3.4.0

USER root
WORKDIR /app

# Iceberg runtime (Spark 3.4, Scala 2.12) + AWS bundle (S3A + Glue Catalog client).
ARG ICEBERG_VERSION=1.5.2
ARG ICEBERG_AWS_BUNDLE_VERSION=1.5.2
RUN curl -fsSL -o /opt/spark/jars/iceberg-spark-runtime-3.4_2.12-${ICEBERG_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.4_2.12/${ICEBERG_VERSION}/iceberg-spark-runtime-3.4_2.12-${ICEBERG_VERSION}.jar" \
 && curl -fsSL -o /opt/spark/jars/iceberg-aws-bundle-${ICEBERG_AWS_BUNDLE_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_AWS_BUNDLE_VERSION}/iceberg-aws-bundle-${ICEBERG_AWS_BUNDLE_VERSION}.jar"

COPY docker/spark-jobs/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY scripts/spark_job_entrypoint.py scripts/spark_job_entrypoint.py

# spark.sql.session.timeZone=UTC is critical — see docs/architecture/01-data-platform.md
# ("local[*] validation before this ever touches a cluster") for the bug
# this caught during local testing.
ENV PYTHONPATH=/app
ENTRYPOINT ["/opt/spark/bin/spark-submit", \
  "--conf", "spark.sql.session.timeZone=UTC", \
  "--conf", "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions", \
  "/app/scripts/spark_job_entrypoint.py"]
