"""Pydantic schemas for the scoring/ingest API. See docs/architecture/04-serving.md."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EVENT_TYPES = Literal[
    "session", "purchase", "push_sent", "push_open", "campaign_click", "in_app_event", "support_ticket"
]


class RawEvent(BaseModel):
    event_id: str
    customer_id: str
    event_type: EVENT_TYPES
    timestamp: str
    properties: dict = Field(default_factory=dict)


class IngestBatch(BaseModel):
    events: list[RawEvent]


class IngestResponse(BaseModel):
    accepted: int


class ExplanationItem(BaseModel):
    feature: str
    value: float | None
    shap_value: float
    direction: Literal["increases_risk", "decreases_risk"]


class ScoreResponse(BaseModel):
    customer_id: str
    churn_probability: float
    rfm_segment: str
    source: Literal["cache", "computed_on_demand"]
    explanation: list[ExplanationItem] | None = None
    plain_language: str | None = None
