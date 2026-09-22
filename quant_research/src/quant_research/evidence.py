"""Strict offline evidence bridge; ratings retain exposure intent, not probabilities."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset_id: str
    source: str
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    generated_at: datetime
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: str
    prompt_version: str
    kind: Literal["fact", "inference"]
    rating: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    parse_status: Literal["validated"]
    validity_hours: int = Field(gt=0, le=168)

    @model_validator(mode="after")
    def times(self):
        values = (self.published_at, self.available_at, self.ingested_at, self.generated_at)
        if any(v.tzinfo is None or v.utcoffset() is None for v in values):
            raise ValueError("all evidence timestamps must include a timezone")
        if self.available_at < self.published_at or self.generated_at < self.ingested_at:
            raise ValueError("invalid evidence time ordering")
        return self


def accept_evidence(payload, asset_id, decision_at, forward=True):
    item = Evidence.model_validate(payload)
    if decision_at.tzinfo is None:
        raise ValueError("decision_at must be timezone aware")
    if item.asset_id != asset_id:
        raise ValueError("asset mismatch")
    # An LLM inference does not exist before its actual generation, including
    # historical reconstruction. Never backdate a present-day model answer.
    times = [item.available_at]
    if forward or item.kind == "inference":
        times += [item.ingested_at, item.generated_at]
    if max(times) > decision_at:
        raise ValueError("late evidence")
    if decision_at > item.available_at + timedelta(hours=item.validity_hours):
        raise ValueError("expired evidence")
    return {"evidence": item.model_dump(mode="json"), "probability": None,
            "overlay_enabled": False, "historical_knowledge_contamination_possible": not forward}
