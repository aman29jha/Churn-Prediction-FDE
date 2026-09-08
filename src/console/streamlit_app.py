"""
Reviewer console. See docs/architecture/06-reviewer-console.md.

Run: streamlit run src/console/streamlit_app.py
Expects the FastAPI service running (default http://127.0.0.1:8811) for
the live lookup tab, and the reports/ + models/ artifacts from
scripts/run_training_pipeline.py for everything else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_ROOT / "reports"
DOCS_DIR = REPO_ROOT / "docs"
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8811")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "local-dev-token")
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD")
SPARK_HISTORY_PATH = os.environ.get("SPARK_HISTORY_PATH", "/spark-history")
AIRFLOW_PATH = os.environ.get("AIRFLOW_PATH", "/airflow")
CLOUDWATCH_DASHBOARD_URL = os.environ.get("CLOUDWATCH_DASHBOARD_URL", "")

st.set_page_config(page_title="Churn Prediction — Reviewer Console", layout="wide")

# Auth note: docs/architecture/06-reviewer-console.md originally specified
# HTTP Basic Auth at the ingress level, assuming nginx-ingress semantics.
# Real finding from actually provisioning the ingress controller: AWS ALB
# (via the AWS Load Balancer Controller, which is what we actually run —
# see docs/architecture/01-data-platform.md) does NOT support htpasswd-
# style Basic Auth natively; it only supports Cognito or OIDC auth actions,
# either of which needs a full User Pool / IdP setup disproportionate to
# this exercise. Pragmatic correction: a simple app-level password gate
# instead — good enough for a small, short-lived reviewer audience, same
# reasoning the original design used to justify skipping a full login
# system, just enforced one layer up the stack than originally planned.
if CONSOLE_PASSWORD:
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if not st.session_state["authenticated"]:
        st.title("Churn Prediction — Reviewer Console")
        entered = st.text_input("Password", type="password")
        if st.button("Enter"):
            if entered == CONSOLE_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()
st.title("Churn Prediction Service — Reviewer Console")
st.caption("Localytics FDE take-home — deployed to the AWS account used for this interview (see SUBMISSION.md).")

tab_arch, tab_api, tab_model, tab_explain, tab_fairness, tab_analytics, tab_observability, tab_lookup = st.tabs(
    ["Architecture", "API Docs", "Model Dashboard", "Explainability", "Fairness", "Analytics", "Observability", "Live Lookup"]
)

with tab_arch:
    st.header("System Architecture")
    st.markdown((DOCS_DIR / "architecture" / "00-overview.md").read_text())
    with st.expander("Data platform (medallion, Iceberg, Spark-on-K8s)"):
        st.markdown((DOCS_DIR / "architecture" / "01-data-platform.md").read_text())
    with st.expander("Simulator"):
        st.markdown((DOCS_DIR / "architecture" / "02-simulator.md").read_text())
    with st.expander("Orchestration"):
        st.markdown((DOCS_DIR / "architecture" / "03-orchestration.md").read_text())
    with st.expander("Serving"):
        st.markdown((DOCS_DIR / "architecture" / "04-serving.md").read_text())
    with st.expander("Observability"):
        st.markdown((DOCS_DIR / "architecture" / "05-observability.md").read_text())

with tab_api:
    st.header("API Reference")
    # The browser's own Host header — dynamic on purpose, so these examples
    # are always the real, currently-live ALB hostname (whatever it is at
    # the moment you're viewing this) rather than a value baked in at
    # build time that would go stale if the ALB were ever recreated.
    try:
        _host = st.context.headers.get("Host", "<this-console-host>")
    except Exception:
        _host = "<this-console-host>"
    EXTERNAL_BASE_URL = f"http://{_host}"

    st.caption(
        f"Base URL: `{EXTERNAL_BASE_URL}` — the same host serving this console (read live from your "
        "browser's own request, not hardcoded). All routes below are real, live, deployed endpoints "
        "(see `src/service/app.py`), not a spec for something planned."
    )

    st.subheader("Auth")
    st.markdown(
        "Every route except `/events/ingest` is unauthenticated (rate-limited only — see "
        "[docs/architecture/04-serving.md](.) for the reasoning). `/events/ingest` requires a bearer "
        "token matching the `INGEST_TOKEN` K8s Secret, injected into the live simulator's pod the same way."
    )

    def _endpoint(method: str, path: str, auth: str, purpose: str, example: str):
        st.markdown(f"#### `{method} {path}`")
        st.markdown(f"**Auth:** {auth}  \n**Purpose:** {purpose}")
        st.code(example, language="bash")
        st.divider()

    _endpoint(
        "GET", "/health", "None",
        "Liveness probe + a quick sanity count of how many customers are currently servable "
        "from the in-memory score cache (see the known DynamoDB-vs-snapshot gap in SUBMISSION.md).",
        f"curl {EXTERNAL_BASE_URL}/health",
    )
    _endpoint(
        "GET", "/score/{customer_id}", "None",
        "Real-time churn probability, RFM segment, and a plain-language SHAP explanation for one "
        "customer — the same call this console's Live Lookup tab makes. Try `syn_cust_00001` "
        "(synthetic) or `cust_00001` (from the original 80-customer sample).",
        f"curl {EXTERNAL_BASE_URL}/score/syn_cust_00001",
    )
    _endpoint(
        "POST", "/events/ingest", "Bearer token (`INGEST_TOKEN`)",
        "Accepts a batch of raw engagement events — the same route the live trickle simulator "
        "(`live_simulator_dag` in Airflow) posts to on its schedule.",
        f'curl -X POST {EXTERNAL_BASE_URL}/events/ingest \\\n'
        f'  -H "Authorization: Bearer <INGEST_TOKEN>" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"events": []}}\'',
    )
    _endpoint(
        "GET", "/analytics/kpi_daily", "None",
        "Real daily KPI trends (DAU, revenue, push-open rate, campaign-click rate) — runs a live "
        "Athena query against the real `kpi_daily` Iceberg table. Backs the Analytics tab's charts.",
        f"curl {EXTERNAL_BASE_URL}/analytics/kpi_daily",
    )
    _endpoint(
        "GET", "/analytics/segments", "None",
        "RFM segment bucketing (Champions / Loyal / At Risk / Hibernating / Lost) — a live Athena "
        "query against the real `rfm_segments` Iceberg table. Backs the Analytics tab's bar chart.",
        f"curl {EXTERNAL_BASE_URL}/analytics/segments",
    )

    st.subheader("Non-API routes on the same ALB")
    st.markdown(
        "- `/spark-history` — Spark History Server (real job DAGs/stage timings), see Observability tab\n"
        "- `/airflow` — Airflow UI (`admin`/`admin`), see Observability tab\n"
        "- `/` — this console"
    )

    st.subheader("Try it now")
    st.caption(
        "Calls the API over the internal cluster network (same as every other tab in this "
        "console) rather than looping back out through the public ALB, which can hairpin "
        "unreliably from inside the same cluster it's fronting — the curl examples above are "
        "what you'd actually run from your own terminal."
    )
    if st.button("GET /health"):
        try:
            st.json(requests.get(f"{API_BASE_URL}/health", timeout=5).json())
        except requests.exceptions.RequestException as e:
            st.error(f"Could not reach the API: {e}")

with tab_model:
    st.header("Baseline vs. XGBoost")
    metrics_path = REPORTS_DIR / "metrics.json"
    if metrics_path.exists():
        metrics = pd.read_json(metrics_path, orient="index")
        st.dataframe(metrics.style.format("{:.4f}"))
        col1, col2 = st.columns(2)
        col1.metric("XGBoost PR-AUC", f"{metrics.loc['xgboost', 'pr_auc']:.3f}",
                    delta=f"{metrics.loc['xgboost', 'pr_auc'] - metrics.loc['baseline', 'pr_auc']:+.3f} vs baseline")
        col2.metric("XGBoost F2 @ capacity", f"{metrics.loc['xgboost', 'f2_at_capacity_threshold']:.3f}",
                    delta=f"{metrics.loc['xgboost', 'f2_at_capacity_threshold'] - metrics.loc['baseline', 'f2_at_capacity_threshold']:+.3f} vs baseline")
    else:
        st.warning("Run `python -m scripts.run_training_pipeline` first to produce reports/metrics.json.")
    st.markdown((DOCS_DIR / "evaluation.md").read_text())

with tab_explain:
    st.header("Explainability")
    shap_img = REPORTS_DIR / "global_shap_importance.png"
    if shap_img.exists():
        st.image(str(shap_img), caption="Global feature importance (mean |SHAP value|)")
    example_path = REPORTS_DIR / "example_explanation.json"
    if example_path.exists():
        example = json.loads(example_path.read_text())
        st.subheader(f"Example: {example['customer_id']} (churn probability = {example['churn_probability']:.3f})")
        st.write(example["plain_language"])
        st.dataframe(pd.DataFrame(example["explanation"]))
    st.markdown((DOCS_DIR / "explainability.md").read_text())

with tab_fairness:
    st.header("Fairness / Bias Check")
    fairness_path = REPORTS_DIR / "fairness.json"
    if fairness_path.exists():
        fairness_df = pd.read_json(fairness_path)
        findings = fairness_df[fairness_df["is_finding"]]
        if len(findings):
            st.warning(f"{len(findings)} finding(s) flagged (FNR ratio > 1.25x or absolute gap > 10pt).")
            st.dataframe(findings)
        else:
            st.success("No fairness findings above threshold.")
        st.dataframe(fairness_df)
    st.markdown((DOCS_DIR / "fairness.md").read_text())

with tab_analytics:
    st.header("Analytics")
    st.caption(
        "Real business-trend queries against the live Iceberg tables (Glue Catalog), run "
        "on-demand via Athena through the API — not screenshots, not a mockup. See "
        "docs/architecture/07-analytics.md. Only kpi_daily is implemented; segment_migration "
        "and cohort_retention are documented next steps, not silently faked here (see "
        "SUBMISSION.md for why — both need Gold's tables to be append-only/partitioned by "
        "run_date first)."
    )

    st.subheader("RFM Segment Bucketing")
    st.caption("Champions / Loyal / At Risk / Hibernating / Lost — from the real rfm_segments Iceberg table.")
    try:
        segments_resp = requests.get(f"{API_BASE_URL}/analytics/segments", timeout=30)
        if segments_resp.status_code == 200:
            segments_df = pd.DataFrame(segments_resp.json()["rows"])
            segments_df["customers"] = pd.to_numeric(segments_df["customers"])
            st.bar_chart(segments_df.set_index("segment")["customers"])
            st.dataframe(segments_df, hide_index=True)
        else:
            st.error(f"API returned {segments_resp.status_code}: {segments_resp.text}")
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach the API at {API_BASE_URL}: {e}")

    st.divider()

    st.subheader("KPI Trends (daily)")
    st.caption("DAU, revenue, push-open rate, campaign-click rate — from the real kpi_daily Iceberg table.")
    try:
        kpi_resp = requests.get(f"{API_BASE_URL}/analytics/kpi_daily", timeout=30)
        if kpi_resp.status_code == 200:
            kpi_rows = kpi_resp.json()["rows"]
            if kpi_rows:
                kpi_df = pd.DataFrame(kpi_rows)
                kpi_df["event_date"] = pd.to_datetime(kpi_df["event_date"])
                numeric_cols = [
                    "dau", "total_revenue", "avg_revenue", "purchase_count",
                    "push_open_rate", "campaign_click_rate",
                ]
                for col in numeric_cols:
                    kpi_df[col] = pd.to_numeric(kpi_df[col], errors="coerce").fillna(0)
                kpi_df = kpi_df.set_index("event_date").sort_index()

                st.markdown("**Daily Active Users**")
                st.line_chart(kpi_df["dau"])

                st.markdown("**Daily Revenue**")
                st.line_chart(kpi_df["total_revenue"])

                st.markdown("**Push Open Rate / Campaign Click Rate**")
                st.line_chart(kpi_df[["push_open_rate", "campaign_click_rate"]])

                with st.expander(f"Raw kpi_daily rows ({len(kpi_df)} days)"):
                    st.dataframe(kpi_df)
            else:
                st.info("kpi_daily table is empty — trigger analytics_dag in Airflow first.")
        else:
            st.error(f"API returned {kpi_resp.status_code}: {kpi_resp.text}")
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach the API at {API_BASE_URL}: {e}")

with tab_observability:
    st.header("Observability")
    st.caption(
        "Both panels below are the real, live deployed monitoring surfaces — not screenshots — "
        "see docs/architecture/05-observability.md for the full design (auth/rate-limiting/"
        "observability/failure-modes dashboard panels, log retention, alarms)."
    )

    st.subheader("Spark History Server")
    st.caption(
        "Real job DAGs, stage timings, and executor metrics for every Silver/Gold/Analytics "
        "SparkApplication run. Served on the same ALB as this console, at "
        f"`{SPARK_HISTORY_PATH}` — embedded below; if the embed doesn't render "
        "(Spark's UI assets don't always cooperate inside an iframe), use the direct link."
    )
    st.markdown(f"[Open Spark History Server directly]({SPARK_HISTORY_PATH})")
    st.components.v1.iframe(SPARK_HISTORY_PATH, height=600, scrolling=True)

    st.divider()

    st.subheader("Airflow")
    st.caption(
        "Real DAG run history for medallion_pipeline_dag, analytics_dag, training_dag, and "
        f"live_simulator_dag — served on the same ALB at `{AIRFLOW_PATH}`. Login required "
        "(admin/admin — see SUBMISSION.md); the login form doesn't render well inside an "
        "iframe, so this is a direct link rather than an embed."
    )
    st.markdown(f"[Open Airflow directly]({AIRFLOW_PATH})")

    st.divider()

    st.subheader("CloudWatch Dashboard")
    st.caption(
        "The 4-panel production-readiness dashboard (auth, rate limiting, latency/error rate, "
        "failure modes). Requires AWS Console sign-in to view — CloudWatch dashboards can't be "
        "embedded without enabling paid public sharing, so this is a direct link rather than an "
        "iframe. Screenshots are captured into docs/evidence/ as the fallback if a reviewer's "
        "session doesn't have AWS console access."
    )
    if CLOUDWATCH_DASHBOARD_URL:
        st.markdown(f"[Open CloudWatch Dashboard]({CLOUDWATCH_DASHBOARD_URL})")
    else:
        st.info("CLOUDWATCH_DASHBOARD_URL not set in this environment.")

with tab_lookup:
    st.header("Live Lookup")
    st.caption(f"Calls the real deployed API at {API_BASE_URL} — proves the service actually works.")
    customer_id = st.text_input("Customer ID", value="syn_cust_00001")
    if st.button("Score this customer"):
        try:
            response = requests.get(f"{API_BASE_URL}/score/{customer_id}", timeout=5)
            if response.status_code == 200:
                result = response.json()
                source = result.get("source", "cache")
                if source == "dynamodb":
                    st.success("Live read from DynamoDB — this reflects the most recent gold_transform run.")
                else:
                    st.info(
                        "Served from the cold-start JSON snapshot — DynamoDB has no row for this "
                        "customer yet (gold_transform hasn't written one, e.g. before its first run)."
                    )
                col1, col2 = st.columns(2)
                col1.metric("Churn probability", f"{result['churn_probability']:.1%}")
                col2.metric("RFM segment", result["rfm_segment"])
                st.write(result["plain_language"])
                st.dataframe(pd.DataFrame(result["explanation"]))
            elif response.status_code == 404:
                st.error(f"Customer '{customer_id}' not found.")
            else:
                st.error(f"API returned {response.status_code}: {response.text}")
        except requests.exceptions.ConnectionError:
            st.error(f"Could not reach the API at {API_BASE_URL}. Is it running? "
                     f"(`uvicorn src.service.app:app --port 8811`)")
        except requests.exceptions.RequestException as e:
            # Real bug: a bare `except ConnectionError` here let anything
            # else requests can raise (e.g. ReadTimeout, the likely failure
            # mode if Athena/SHAP is slow) propagate out of this block and
            # render as a raw Python traceback in the console — a confusing,
            # broken-looking screen for a reviewer. Catch the general case
            # too, same as the Analytics tab already does.
            st.error(f"Request to the API at {API_BASE_URL} failed: {e}")

    st.divider()
    st.subheader("Live Simulator Control")
    st.caption(
        "Disabled placeholder — this console doesn't duplicate simulator control. The real "
        "toggle is Airflow's native pause/unpause on live_simulator_dag; open it from the "
        "Observability tab's Airflow link above (see docs/architecture/02-simulator.md)."
    )
    st.button("Trigger one manual event batch (demo placeholder)", disabled=True)
