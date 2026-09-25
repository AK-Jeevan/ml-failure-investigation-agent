"""Build an evidence-linked report without turning inference into fact."""

from __future__ import annotations

from .hypothesis_engine import HypothesisEngine
from .models import Evidence, Investigation


class InvestigationReportBuilder:
    def __init__(self, hypothesis_engine: HypothesisEngine | None = None) -> None:
        self.hypothesis_engine = hypothesis_engine or HypothesisEngine()

    def build(self, state: Investigation) -> dict:
        evidence_by_id = {item.evidence_id: item for item in state.evidence}
        cause = self.hypothesis_engine.probable_cause(state) if state.status.value != "cancelled" else None
        facts = [self._fact(item) for item in state.evidence]
        inferences = []
        if cause:
            inferences.append(
                {
                    "label": "AGENT INFERENCE",
                    "statement": cause.statement,
                    "confidence": cause.confidence,
                    "evidence_ids": list(cause.supporting_evidence_ids),
                    "method": "Deterministic signal checks plus corroboration from distinct registered tool sources.",
                }
            )
        else:
            inferences.append(
                {
                    "label": "AGENT INFERENCE",
                    "statement": (
                        "The investigation was cancelled before a root cause was finalized."
                        if state.status.value == "cancelled"
                        else "The available evidence does not support a probable root cause."
                    ),
                    "confidence": "low",
                    "evidence_ids": [],
                    "method": "No hypothesis met the configured corroboration and contradiction checks.",
                }
            )

        considered = []
        for hypothesis in state.hypotheses:
            considered.append(
                {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "statement": hypothesis.statement,
                    "status": hypothesis.status,
                    "score": hypothesis.score,
                    "confidence": hypothesis.confidence,
                    "supporting_evidence": [self._evidence_ref(evidence_by_id[item]) for item in hypothesis.supporting_evidence_ids if item in evidence_by_id],
                    "contradicting_evidence": [self._evidence_ref(evidence_by_id[item]) for item in hypothesis.contradicting_evidence_ids if item in evidence_by_id],
                }
            )

        rejected = [item for item in considered if item["status"] == "weakened"]
        remediation = state.remediation
        recommendation = remediation.get("recommendation") if remediation else None
        approval_required = bool(remediation and remediation.get("approval_required"))

        uncertainties = [
            "The environment is simulated; these results do not establish the cause of any real production incident.",
            "No remediation was executed, so recovery has not been verified.",
        ]
        if state.status == "inconclusive":
            uncertainties.append("At least one material explanation remains unresolved or lacks independent corroboration.")
        if state.status.value == "cancelled":
            uncertainties.append("The investigation was cancelled before finalizing its root-cause assessment.")
        if any(event.kind in {"tool_failed", "model_planning_failed", "summary_unavailable"} for event in state.trajectory):
            uncertainties.append("One or more tools or optional model calls failed; review the trajectory for missing evidence.")

        timeline = [
            {
                "time": event.created_at,
                "event": event.kind,
                "description": event.message,
                "details": event.details,
            }
            for event in state.trajectory
            if event.kind in {"incident_received", "tool_started", "evidence_collected", "hypothesis_assessed", "stop_condition_met", "investigation_stopped"}
        ]
        return {
            "investigation_id": state.incident.incident_id,
            "status": state.status.value,
            "incident_summary": state.incident.description,
            "affected_system": state.incident.service,
            "observed_symptoms": facts,
            "timeline": timeline,
            "evidence_collected": facts,
            "hypotheses_considered": considered,
            "hypotheses_rejected_or_weakened": rejected,
            "probable_root_cause": inferences[0] if cause else None,
            "contributing_factors": [],
            "detection_gaps": ["No automated source-level causal tracing is present in this local simulation."],
            "recommended_remediation": recommendation,
            "required_human_approval": {
                "required": approval_required,
                "status": remediation.get("approval_status") if remediation else "not_required",
                "reason": "Any production-changing remediation requires explicit human review." if approval_required else "No production action is proposed for automatic execution.",
            },
            "remediation_approval": remediation.get("approval") if remediation else None,
            "remediation_execution_status": remediation.get("execution_status") if remediation else "not_available",
            "actions_performed": [],
            "verification": {"status": "not_performed", "detail": "No remediation was executed; recovery is not verified."},
            "facts": facts,
            "agent_inferences": inferences,
            "uncertainty": uncertainties,
            "stop_reason": state.stop_reason,
            "trajectory": timeline,
        }

    @staticmethod
    def _fact(evidence: Evidence) -> dict:
        return {
            "label": "FACT",
            "source": evidence.tool_name,
            "summary": evidence.summary,
            "observed_at": evidence.collected_at,
            "evidence_id": evidence.evidence_id,
            "data": evidence.payload,
        }

    @staticmethod
    def _evidence_ref(evidence: Evidence) -> dict:
        return {"evidence_id": evidence.evidence_id, "source": evidence.tool_name, "summary": evidence.summary}
