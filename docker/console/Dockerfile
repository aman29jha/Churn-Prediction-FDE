# Streamlit reviewer console. See docs/architecture/06-reviewer-console.md.
# Auth: ALB doesn't support htpasswd-style Basic Auth (only Cognito/OIDC,
# disproportionate to this exercise), so the password gate is app-level,
# inside streamlit_app.py itself (CONSOLE_PASSWORD env var) — not applied
# at the ingress. See the "Auth note" comment at the top of
# src/console/streamlit_app.py for the full reasoning.
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY docker/console/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/console/ src/console/
COPY src/__init__.py src/__init__.py
COPY docs/ docs/
COPY reports/ reports/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8501
ENV API_BASE_URL=http://api-service:8000
CMD ["streamlit", "run", "src/console/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
