# Lightweight FastAPI scoring + ingest service. No JVM — see
# docker/spark-jobs for the heavier Spark image. Model artifacts are
# NOT baked into this image (they're retrained independently of service
# code) — mounted at /app/models locally, pulled from the S3 model
# registry by an init step in production (see docs/architecture/04-serving.md).
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY docker/api-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/service/ src/service/
COPY src/modeling/ src/modeling/
COPY src/features/ src/features/
COPY src/__init__.py src/__init__.py

RUN mkdir -p /app/models /app/data/local_bronze && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "src.service.app:app", "--host", "0.0.0.0", "--port", "8000"]
