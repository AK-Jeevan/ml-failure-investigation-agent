"""Evidence-based termination policy for the investigation loop."""

from __future__ import annotations

from dataclasses import dataclass

from .hypothesis_engine import HypothesisEngine
from .models import Investigation


@dataclass(frozen=True, slots=True)
class StopDecision:
    should_stop: bool
    reason: str


class StopPolicy:
    """Stop after corroboration and targeted falsification of close alternatives."""

    # These are required evidence sources for closing the first controlled cases.
    # For quality degradation, API checks are required only when the symptom
    # metrics indicate elevated API errors; this avoids irrelevant calls.
    _required_tools = {
        "feature_change": {"service_metrics", "feature_statistics", "data_drift", "pipeline_status", "model_metadata", "deployment_history"},
        "data_drift": {"service_metrics", "feature_statistics", "data_drift", "pipeline_status", "model_metadata"},
        "api_regression": {"service_metrics", "model_metadata", "deployment_history", "api_health"},
    }
    _required_alternatives = {
        "feature_change": {"data_drift", "pipeline_failure", "model_change"},
        "data_drift": {"feature_change", "pipeline_failure", "model_change"},
        "api_regression": {"model_change"},
    }

    def __init__(self, hypothesis_engine: HypothesisEngine | None = None) -> None:
        self.hypothesis_engine = hypothesis_engine or HypothesisEngine()

    def decide(self, state: Investigation) -> StopDecision:
        cause = self.hypothesis_engine.probable_cause(state)
        if cause is None:
            return StopDecision(False, "no hypothesis is corroborated yet")

        observed_tools = {item.tool_name for item in state.evidence}
        required = self._required_tools.get(cause.hypothesis_id)
        if required is None:
            return StopDecision(False, "no early-stop profile exists for the leading hypothesis")

        metric = next((item.payload for item in state.evidence if item.tool_name == "service_metrics"), {})
        if cause.hypothesis_id in {"feature_change", "data_drift"} and metric.get("error_rate", 0) > 0.05:
            required = required | {"api_health"}
        missing = sorted(required - observed_tools)
        if missing:
            return StopDecision(False, f"corroborated cause still needs checks: {', '.join(missing)}")

        by_id = {item.hypothesis_id: item for item in state.hypotheses}
        unresolved = [
            hypothesis_id
            for hypothesis_id in self._required_alternatives.get(cause.hypothesis_id, set())
            if hypothesis_id in by_id and by_id[hypothesis_id].status != "weakened"
        ]
        if unresolved:
            return StopDecision(False, f"competing hypotheses are not ruled down: {', '.join(sorted(unresolved))}")

        return StopDecision(True, f"{cause.hypothesis_id} is corroborated and required alternatives were weakened")
