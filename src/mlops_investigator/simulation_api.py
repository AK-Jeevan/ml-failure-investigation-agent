"""Local FastAPI model-service simulator for controlled incident scenarios.

This service is intentionally a local test fixture. It is not suitable for
production traffic and exposes scenario evidence to make investigations repeatable.
"""

from __future__ import annotations

import math
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .simulation import SCENARIOS, get_scenario_data

app = FastAPI(
    title="Simulated ML Production Service",
    version="0.1.0",
    description="Local-only service for reproducible ML incident investigation scenarios.",
)


def current_scenario(requested: str | None = None) -> str:
    scenario = requested or os.getenv("SIMULATION_SCENARIO", "feature_schema_change")
    if scenario not in SCENARIOS:
        raise RuntimeError(f"Unknown SIMULATION_SCENARIO: {scenario}")
    return scenario


class PredictionRequest(BaseModel):
    transaction_amount: float = Field(ge=0, le=1_000_000)
    account_age_days: int = Field(ge=0, le=50_000)
    # Old clients send risk_category; the feature schema scenario simulates a
    # producer/consumer mismatch against the new risk_band contract.
    risk_category: str | None = Field(default=None, max_length=32)
    risk_band: str | None = Field(default=None, max_length=32)


@app.get("/health")
def health(scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario = current_scenario(scenario)
    api = get_scenario_data(scenario)["api"]
    return {"service": "fraud-score-api", "scenario": scenario, **api}


@app.get("/metrics")
def metrics(scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario = current_scenario(scenario)
    return {"service": "fraud-score-api", "scenario": scenario, **get_scenario_data(scenario)["metrics"]}


@app.get("/evidence/{source}")
def evidence(source: str, scenario: str | None = Query(default=None)) -> dict[str, Any]:
    """Expose explicit read-only evidence to a local investigation adapter."""
    scenario = current_scenario(scenario)
    data = get_scenario_data(scenario)
    aliases = {"model": "model", "features": "features", "pipeline": "pipeline", "deployment": "deployment", "drift": "drift", "api": "api"}
    if source not in aliases:
        raise HTTPException(status_code=404, detail="Unknown evidence source")
    result = data.get(aliases[source])
    if result is None and source == "drift":
        result = {"psi": 0.02, "threshold": 0.20, "feature": None, "expected_change": False}
    return {"scenario": scenario, "source": source, "evidence": result}


@app.post("/predict")
def predict(request: PredictionRequest, scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario = current_scenario(scenario)
    data = get_scenario_data(scenario)
    if not data["api"].get("healthy", True):
        raise HTTPException(status_code=503, detail="Simulated model runtime unavailable after deployment")

    if data["features"].get("schema_changed", False):
        # The producer still sends risk_category; preprocessing now reads risk_band.
        resolved_risk_band = request.risk_band
    else:
        resolved_risk_band = request.risk_band or request.risk_category

    amount = request.transaction_amount
    if data.get("drift", {}).get("psi", 0) > data.get("drift", {}).get("threshold", 1):
        amount *= 1.25
    risk_component = {"low": 0.08, "medium": 0.35, "high": 0.72}.get(resolved_risk_band or "", 0.5)
    linear_score = (amount / 10_000) + risk_component - (request.account_age_days / 100_000)
    probability = 1 / (1 + math.exp(-max(-20, min(20, linear_score - 0.8))))
    return {
        "prediction": int(probability >= 0.5),
        "risk_probability": round(probability, 6),
        "model_version": data["model"]["current_version"],
        "scenario": scenario,
        "resolved_risk_band": resolved_risk_band,
    }
