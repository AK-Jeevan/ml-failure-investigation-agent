"""Deterministic simulated production evidence for the first MVP scenarios."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any
from pathlib import Path


SCENARIOS: dict[str, dict[str, Any]] = {
    "healthy": {
        "metrics": {"baseline_quality": 0.91, "current_quality": 0.90, "error_rate": 0.003},
        "model": {"baseline_version": "model-17", "current_version": "model-17", "changed": False},
        "features": {"risk_band_null_rate_baseline": 0.003, "risk_band_null_rate_current": 0.003, "schema_changed": False},
        "pipeline": {"status": "succeeded", "records_expected": 10000, "records_written": 10000},
        "deployment": {"commit": "d4e520a", "changed_at": "16:10Z", "component": "feature-transform", "summary": "Restored compatible risk_band contract"},
        "api": {"p95_latency_ms": 96, "error_rate": 0.003, "healthy": True},
        "drift": {"psi": 0.03, "threshold": 0.20, "feature": "transaction_amount", "expected_change": False},
    },
    "feature_schema_change": {
        "metrics": {"baseline_quality": 0.91, "current_quality": 0.73, "error_rate": 0.012},
        "model": {"baseline_version": "model-17", "current_version": "model-17", "changed": False},
        "features": {"risk_band_null_rate_baseline": 0.003, "risk_band_null_rate_current": 0.41, "schema_changed": True},
        "pipeline": {"status": "succeeded", "records_expected": 10000, "records_written": 10000},
        "deployment": {"commit": "a13fc02", "changed_at": "14:27Z", "component": "feature-transform", "summary": "Renamed risk_band output field"},
        "api": {"p95_latency_ms": 94, "error_rate": 0.012, "healthy": True},
    },
    "data_drift": {
        "metrics": {"baseline_quality": 0.90, "current_quality": 0.75, "error_rate": 0.004},
        "model": {"baseline_version": "model-22", "current_version": "model-22", "changed": False},
        "features": {"risk_band_null_rate_baseline": 0.002, "risk_band_null_rate_current": 0.002, "schema_changed": False},
        "pipeline": {"status": "succeeded", "records_expected": 12000, "records_written": 12000},
        "deployment": {"commit": "b45aa31", "changed_at": "yesterday", "component": "docs", "summary": "Updated service documentation"},
        "api": {"p95_latency_ms": 101, "error_rate": 0.004, "healthy": True},
        "drift": {"psi": 0.38, "threshold": 0.20, "feature": "transaction_amount", "expected_change": False},
    },
    "api_regression": {
        "metrics": {"baseline_quality": 0.90, "current_quality": 0.89, "error_rate": 0.16},
        "model": {"baseline_version": "model-11", "current_version": "model-11", "changed": False},
        "features": {"risk_band_null_rate_baseline": 0.002, "risk_band_null_rate_current": 0.002, "schema_changed": False},
        "pipeline": {"status": "succeeded", "records_expected": 9000, "records_written": 9000},
        "deployment": {"commit": "c981de4", "changed_at": "10:06Z", "component": "api-runtime", "summary": "Changed request serialization dependency"},
        "api": {"p95_latency_ms": 840, "error_rate": 0.16, "healthy": False},
    },
}


def get_scenario_data(scenario: str) -> dict[str, Any]:
    try:
        base = copy.deepcopy(SCENARIOS[scenario])
    except KeyError as exc:
        choices = ", ".join(sorted(SCENARIOS))
        raise ValueError(f"Unknown scenario {scenario!r}. Choose one of: {choices}") from exc
    state = _read_simulation_state()
    return copy.deepcopy(state.get("scenarios", {}).get(scenario, base))


def apply_simulated_remediation(scenario: str, hypothesis_id: str) -> dict[str, Any]:
    """Apply one hard-coded recovery mutation to local simulated state only."""
    state = _read_simulation_state()
    data = get_scenario_data(scenario)
    action_descriptions = {
        "feature_change": "Restored the simulated feature schema and baseline null rate.",
        "data_drift": "Restored the simulated input distribution and baseline quality metric.",
        "model_change": "Restored the simulated previously serving model version.",
        "pipeline_failure": "Restored simulated pipeline success and expected record count.",
        "api_regression": "Restored simulated API health, latency, and error rate.",
    }
    if hypothesis_id not in action_descriptions:
        raise ValueError(f"No simulated remediation exists for {hypothesis_id!r}")
    if scenario == "healthy":
        raise ValueError("The healthy scenario is already the recovery fixture; select the original failed scenario")

    if hypothesis_id == "feature_change":
        features = data["features"]
        features["schema_changed"] = False
        features["risk_band_null_rate_current"] = features["risk_band_null_rate_baseline"]
        data["metrics"]["current_quality"] = data["metrics"]["baseline_quality"] * 0.99
    elif hypothesis_id == "data_drift":
        data["drift"]["psi"] = data["drift"]["threshold"] * 0.5
        data["metrics"]["current_quality"] = data["metrics"]["baseline_quality"] * 0.99
    elif hypothesis_id == "model_change":
        data["model"]["current_version"] = data["model"]["baseline_version"]
        data["model"]["changed"] = False
        data["metrics"]["current_quality"] = data["metrics"]["baseline_quality"] * 0.99
    elif hypothesis_id == "pipeline_failure":
        data["pipeline"]["status"] = "succeeded"
        data["pipeline"]["records_written"] = data["pipeline"]["records_expected"]
        data["metrics"]["current_quality"] = data["metrics"]["baseline_quality"] * 0.99
    elif hypothesis_id == "api_regression":
        data["api"].update({"healthy": True, "p95_latency_ms": 110, "error_rate": 0.003})
        data["metrics"]["error_rate"] = 0.003

    state.setdefault("scenarios", {})[scenario] = data
    _write_simulation_state(state)
    return {"scenario": scenario, "hypothesis_id": hypothesis_id, "description": action_descriptions[hypothesis_id]}


def reset_simulation_state(scenario: str | None = None) -> None:
    """Remove simulated recovery overrides so a failure can be replayed."""
    state = _read_simulation_state()
    scenarios = state.setdefault("scenarios", {})
    if scenario is None:
        state["scenarios"] = {}
    else:
        if scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}")
        scenarios.pop(scenario, None)
    _write_simulation_state(state)


def _state_path() -> Path:
    return Path("data/simulation_state.json")


def _read_simulation_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"scenarios": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read local simulation state") from exc


def _write_simulation_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            json.dump(state, temporary, ensure_ascii=False, indent=2)
            temporary_path = temporary.name
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        _remove_temporary_state(temporary_path)
        raise RuntimeError("Could not write local simulation state") from exc
    except Exception:
        _remove_temporary_state(temporary_path)
        raise


def _remove_temporary_state(temporary_path: str | None) -> None:
    if temporary_path and os.path.exists(temporary_path):
        os.unlink(temporary_path)
