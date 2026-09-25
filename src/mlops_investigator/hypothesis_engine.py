"""Deterministic hypothesis assessment and probable-cause selection."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Evidence, EvidencePolarity, Hypothesis, Investigation


@dataclass(frozen=True, slots=True)
class Assessment:
    polarity: EvidencePolarity
    points: int
    rationale: str


class HypothesisEngine:
    """Scores observable evidence without asking the model to invent facts.

    A direct signal can make a hypothesis a candidate. A probable cause requires
    support from at least two distinct tool sources and no unresolved tie for the
    highest score. Absence of evidence is never silently converted to a failure.
    """

    def assess(self, state: Investigation) -> None:
        for evidence in state.evidence:
            for hypothesis in state.hypotheses:
                if evidence.evidence_id in hypothesis.evidence_ids:
                    continue
                result = self._assess_one(hypothesis.hypothesis_id, evidence, state.evidence)
                if result is None or result.polarity == EvidencePolarity.NEUTRAL:
                    continue
                hypothesis.evidence_ids.append(evidence.evidence_id)
                hypothesis.score += result.points
                if result.polarity == EvidencePolarity.SUPPORTS:
                    hypothesis.supporting_evidence_ids.append(evidence.evidence_id)
                else:
                    hypothesis.contradicting_evidence_ids.append(evidence.evidence_id)
                sources = self._sources(state, hypothesis.supporting_evidence_ids)
                contradictions = len(hypothesis.contradicting_evidence_ids)
                if len(sources) >= 2 and hypothesis.score >= 3 and contradictions == 0:
                    hypothesis.status = "corroborated"
                    hypothesis.confidence = "high"
                elif hypothesis.supporting_evidence_ids:
                    hypothesis.status = "candidate"
                    hypothesis.confidence = "medium" if len(sources) >= 2 else "low"
                elif contradictions:
                    hypothesis.status = "weakened"
                    hypothesis.confidence = "low"
                state.add_event(
                    "hypothesis_assessed",
                    result.rationale,
                    hypothesis_id=hypothesis.hypothesis_id,
                    polarity=result.polarity.value,
                    evidence_id=evidence.evidence_id,
                    score=hypothesis.score,
                    confidence=hypothesis.confidence,
                )

    def probable_cause(self, state: Investigation) -> Hypothesis | None:
        ranked = sorted(state.hypotheses, key=lambda item: item.score, reverse=True)
        if not ranked:
            return None
        leading = ranked[0]
        sources = self._sources(state, leading.supporting_evidence_ids)
        if len(sources) < 2 or leading.score < 3 or leading.contradicting_evidence_ids:
            return None
        if len(ranked) > 1 and ranked[1].score == leading.score:
            return None
        return leading

    @staticmethod
    def _sources(state: Investigation, ids: list[str]) -> set[str]:
        wanted = set(ids)
        return {item.tool_name for item in state.evidence if item.evidence_id in wanted}

    @staticmethod
    def _signal(evidence: Evidence, all_evidence: list[Evidence], tool: str) -> dict:
        return next((item.payload for item in all_evidence if item.tool_name == tool), {})

    def _assess_one(
        self, hypothesis_id: str, evidence: Evidence, all_evidence: list[Evidence]
    ) -> Assessment | None:
        payload = evidence.payload
        if hypothesis_id == "feature_change":
            if evidence.tool_name == "feature_statistics":
                changed = payload.get("schema_changed", False) or payload.get("risk_band_null_rate_current", 0) > 0.05
                return self._result(changed, "Feature schema or null-rate check found an abnormality", "Feature schema and null-rate checks were near baseline")
            if evidence.tool_name == "deployment_history":
                feature_signal = self._signal(evidence, all_evidence, "feature_statistics")
                matches = payload.get("component") == "feature-transform" and (
                    feature_signal.get("schema_changed") or feature_signal.get("risk_band_null_rate_current", 0) > 0.05
                )
                return Assessment(EvidencePolarity.SUPPORTS, 1, "Feature transformation deployment matches the observed feature abnormality") if matches else None

        if hypothesis_id == "data_drift":
            if evidence.tool_name == "data_drift":
                changed = payload.get("psi", 0) > payload.get("threshold", 1)
                return self._result(changed, "Measured input drift exceeded its configured threshold", "Measured input drift remained below its configured threshold")
            if evidence.tool_name == "service_metrics":
                drift = self._signal(evidence, all_evidence, "data_drift")
                quality_drop = payload.get("current_quality", 1) < payload.get("baseline_quality", 0)
                if quality_drop and drift.get("psi", 0) > drift.get("threshold", 1):
                    return Assessment(EvidencePolarity.SUPPORTS, 1, "Quality degradation overlaps the measured drift signal")

        if hypothesis_id == "model_change":
            if evidence.tool_name == "model_metadata":
                changed = payload.get("changed", False)
                return self._result(changed, "Deployed model version changed during the incident window", "Deployed model version did not change")
            if evidence.tool_name == "deployment_history":
                model_signal = self._signal(evidence, all_evidence, "model_metadata")
                if payload.get("component") in {"model", "model-serving"} and model_signal.get("changed"):
                    return Assessment(EvidencePolarity.SUPPORTS, 1, "Model-serving deployment matches the changed model version")

        if hypothesis_id == "pipeline_failure":
            if evidence.tool_name == "pipeline_status":
                failed = payload.get("status") != "succeeded" or payload.get("records_written", 0) < payload.get("records_expected", 0)
                return self._result(failed, "Pipeline status or record counts show an incomplete run", "Pipeline succeeded and record counts match")
            if evidence.tool_name == "deployment_history":
                pipeline = self._signal(evidence, all_evidence, "pipeline_status")
                failed = pipeline.get("status") != "succeeded" or pipeline.get("records_written", 0) < pipeline.get("records_expected", 0)
                if payload.get("component") in {"pipeline", "data-pipeline"} and failed:
                    return Assessment(EvidencePolarity.SUPPORTS, 1, "Pipeline deployment matches the observed pipeline failure")

        if hypothesis_id == "api_regression":
            if evidence.tool_name == "api_health":
                failed = not payload.get("healthy", True) or payload.get("error_rate", 0) > 0.05 or payload.get("p95_latency_ms", 0) > 500
                return self._result(failed, "API health, latency, or error rate crossed its failure condition", "API health, latency, and error rate remained within bounds")
            if evidence.tool_name == "service_metrics":
                if payload.get("error_rate", 0) > 0.05:
                    return Assessment(EvidencePolarity.SUPPORTS, 1, "Service error rate is elevated in monitoring metrics")
            if evidence.tool_name == "deployment_history":
                api = self._signal(evidence, all_evidence, "api_health")
                failed = not api.get("healthy", True) or api.get("error_rate", 0) > 0.05 or api.get("p95_latency_ms", 0) > 500
                if payload.get("component") in {"api-runtime", "api", "inference-service"} and failed:
                    return Assessment(EvidencePolarity.SUPPORTS, 1, "API runtime deployment matches the observed API failure")

        return None

    @staticmethod
    def _result(condition: bool, support_reason: str, contradiction_reason: str) -> Assessment:
        if condition:
            return Assessment(EvidencePolarity.SUPPORTS, 2, support_reason)
        return Assessment(EvidencePolarity.CONTRADICTS, -1, contradiction_reason)
