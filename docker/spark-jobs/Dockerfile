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
# hadoop-aws + aws-java-sdk-bundle: REQUIRED separately from Iceberg's own
# AWS bundle above — Iceberg's bundle only wires up S3 access for the
# Iceberg catalog/table format itself. Any plain `s3a://` FileSystem
# access (Hadoop's generic API — used by the Spark History Server's
# spark.history.fs.logDirectory, and by our Silver/Gold jobs' own
# spark.read/write against s3a:// paths) goes through Hadoop's
# FileSystem implementation, which needs its own connector jar. Missing
# this caused a real, fully reproducible crash: `ClassNotFoundException:
# Class org.apache.hadoop.fs.s3a.S3AFileSystem not found` — caught by
# actually running the Spark History Server against real S3, which
# would have equally blocked the real Silver/Gold pipeline runs (they
# also read/write s3a:// paths) had this not been caught here first.
# Versions matched to Spark 3.4.0's bundled Hadoop 3.3.4.
ARG HADOOP_AWS_VERSION=3.3.4
ARG AWS_SDK_BUNDLE_VERSION=1.12.262
RUN curl -fsSL -o /opt/spark/jars/iceberg-spark-runtime-3.4_2.12-${ICEBERG_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.4_2.12/${ICEBERG_VERSION}/iceberg-spark-runtime-3.4_2.12-${ICEBERG_VERSION}.jar" \
 && curl -fsSL -o /opt/spark/jars/iceberg-aws-bundle-${ICEBERG_AWS_BUNDLE_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_AWS_BUNDLE_VERSION}/iceberg-aws-bundle-${ICEBERG_AWS_BUNDLE_VERSION}.jar" \
 && curl -fsSL -o /opt/spark/jars/hadoop-aws-${HADOOP_AWS_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar" \
 && curl -fsSL -o /opt/spark/jars/aws-java-sdk-bundle-${AWS_SDK_BUNDLE_VERSION}.jar \
      "https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK_BUNDLE_VERSION}/aws-java-sdk-bundle-${AWS_SDK_BUNDLE_VERSION}.jar"

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
