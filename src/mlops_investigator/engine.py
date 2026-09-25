"""Bounded evidence-driven investigation loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .llm import DisabledProvider, ReasoningProvider
from .hypothesis_engine import HypothesisEngine
from .models import Hypothesis, Incident, Investigation, InvestigationStatus
from .reporting import InvestigationReportBuilder
from .remediation import RemediationPlanner
from .security import validate_evidence, validate_incident_input
from .stop_policy import StopPolicy
from .storage import InvestigationStore
from .tools import ReadOnlyToolRegistry


@dataclass(frozen=True, slots=True)
class InvestigationLimits:
    max_steps: int = 8
    max_tool_calls: int = 8
    max_model_calls: int = 4


class InvestigationEngine:
    """Runs a bounded planner with deterministic validation and fallback."""

    def __init__(
        self,
        tools: ReadOnlyToolRegistry | None = None,
        store: InvestigationStore | None = None,
        provider: ReasoningProvider | None = None,
        limits: InvestigationLimits | None = None,
    ) -> None:
        self.tools = tools or ReadOnlyToolRegistry()
        self.store = store or InvestigationStore()
        self.provider = provider or DisabledProvider()
        self.limits = limits or InvestigationLimits()
        self.hypothesis_engine = HypothesisEngine()
        self.stop_policy = StopPolicy(self.hypothesis_engine)
        self.report_builder = InvestigationReportBuilder(self.hypothesis_engine)
        self.remediation_planner = RemediationPlanner()

    def investigate(
        self,
        incident: Incident,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> Investigation:
        validate_incident_input(incident)
        state = Investigation(incident=incident)
        state.add_event("incident_received", "Incident accepted for read-only investigation", scenario=incident.scenario)
        self._seed_hypotheses(state)
        self.store.save(state)

        planned = ["service_metrics", "model_metadata", "feature_statistics", "pipeline_status", "deployment_history", "api_health", "data_drift"]
        step = 0
        while step < self.limits.max_steps and state.tool_calls < self.limits.max_tool_calls:
            if cancellation_requested and cancellation_requested():
                state.status = InvestigationStatus.CANCELLED
                state.stop_reason = "cancellation requested by caller"
                state.conclusion = "Investigation cancelled before a probable root cause was finalized."
                state.add_event("investigation_cancelled", "Investigation stopped after a cancellation request")
                self.store.save(state)
                break
            tool_name = self._next_tool(planned, state)
            if tool_name is None:
                break
            step += 1
            state.tool_calls += 1
            state.add_event("tool_started", f"Collecting evidence with {tool_name}", tool=tool_name, step=step)
            try:
                evidence = self.tools.execute(tool_name, incident.scenario)
                validate_evidence(evidence, tool_name)
            except Exception as exc:  # tool boundary: record the failure and continue safely
                state.add_event("tool_failed", f"Tool {tool_name} failed", error_type=type(exc).__name__)
                self.store.save(state)
                continue
            state.evidence.append(evidence)
            state.add_event("evidence_collected", evidence.summary, tool=tool_name, evidence_id=evidence.evidence_id)
            self.hypothesis_engine.assess(state)
            self.store.save(state)
            decision = self.stop_policy.decide(state)
            if decision.should_stop:
                state.stop_reason = decision.reason
                state.add_event("stop_condition_met", decision.reason)
                self.store.save(state)
                break

        if state.stop_reason:
            pass
        elif state.tool_calls >= self.limits.max_tool_calls or step >= self.limits.max_steps:
            state.stop_reason = "investigation budget exhausted"
        else:
            state.stop_reason = "all relevant read-only checks completed"

        leading = self.hypothesis_engine.probable_cause(state)
        if state.status == InvestigationStatus.CANCELLED:
            state.remediation = None
        elif leading is not None:
            state.status = InvestigationStatus.COMPLETE
            state.conclusion = leading.statement
            state.remediation = self.remediation_planner.create(leading.hypothesis_id)
            if state.remediation:
                state.add_event(
                    "remediation_planned",
                    "Advisory remediation prepared; no action executor is enabled",
                    plan_id=state.remediation["plan_id"],
                    approval_required=state.remediation["approval_required"],
                )
        else:
            state.status = InvestigationStatus.INCONCLUSIVE
            state.conclusion = "No hypothesis has enough supporting evidence for a probable root cause."

        summaries = [f"{item.tool_name}: {item.summary} {item.payload}" for item in state.evidence]
        if not getattr(self.provider, "enabled", True):
            state.add_event("summary_disabled", "NVIDIA provider is not configured; deterministic report retained")
        elif self.limits.max_model_calls > state.model_calls:
            state.model_calls += 1
            try:
                state.add_event("summary", self.provider.summarize(incident.description, summaries), model_calls=state.model_calls)
            except Exception as exc:
                state.add_event("summary_unavailable", "Optional LLM summary unavailable; deterministic report retained", error_type=type(exc).__name__)
        else:
            state.add_event("summary_skipped", "Model-call budget exhausted; deterministic report retained")
        state.add_event("investigation_stopped", state.stop_reason or "stopped", conclusion=state.conclusion)
        state.report = self.report_builder.build(state)
        self.store.save(state)
        return state

    def _seed_hypotheses(self, state: Investigation) -> None:
        state.hypotheses = [
            Hypothesis("feature_change", "A feature schema or transformation change caused model degradation.", "feature null rate or schema changed"),
            Hypothesis("data_drift", "An unexpected input distribution shift caused model degradation.", "drift statistic exceeds threshold"),
            Hypothesis("model_change", "A newly deployed model version caused model degradation.", "deployed model version changed"),
            Hypothesis("pipeline_failure", "A data pipeline failure caused incomplete or stale inputs.", "pipeline failed or record count is short"),
            Hypothesis("api_regression", "An API or deployment regression caused the service symptoms.", "API unhealthy or latency/error rate elevated"),
        ]
        for hypothesis in state.hypotheses:
            state.add_event("hypothesis_created", hypothesis.statement, hypothesis_id=hypothesis.hypothesis_id)

    def _next_tool(self, planned: list[str], state: Investigation) -> str | None:
        observed = {item.tool_name for item in state.evidence}
        available = [name for name in self.tools.names if name not in observed]
        # Always establish a deterministic baseline before asking the model to steer.
        if not observed and "service_metrics" in available:
            return "service_metrics"

        if getattr(self.provider, "enabled", True) and self.limits.max_model_calls > state.model_calls and available:
            evidence_context = [
                {"id": item.evidence_id, "tool": item.tool_name, "summary": item.summary, "payload": item.payload}
                for item in state.evidence
            ]
            hypothesis_context = [
                {
                    "id": item.hypothesis_id,
                    "statement": item.statement,
                    "status": item.status,
                    "score": item.score,
                    "confidence": item.confidence,
                    "supporting_evidence": item.supporting_evidence_ids,
                    "contradicting_evidence": item.contradicting_evidence_ids,
                }
                for item in state.hypotheses
            ]
            state.model_calls += 1
            try:
                proposal = self.provider.plan_next_tool(
                    state.incident.description, evidence_context, hypothesis_context, available
                )
                if proposal and proposal.get("tool") in available:
                    state.add_event(
                        "model_tool_proposal",
                        "LLM proposed an allowlisted read-only check; deterministic validation accepted it",
                        tool=proposal["tool"],
                        reason=proposal.get("reason", ""),
                        model_call=state.model_calls,
                    )
                    return proposal["tool"]
                if proposal:
                    state.add_event(
                        "model_tool_rejected",
                        "LLM proposed a tool that is unavailable or already collected",
                        proposed_tool=proposal.get("tool"),
                    )
            except Exception as exc:
                state.add_event(
                    "model_planning_failed",
                    "LLM planning failed; deterministic investigation order will be used",
                    error_type=type(exc).__name__,
                    model_call=state.model_calls,
                )

        # Evidence can alter priority: after a quality drop, feature checks precede
        # less diagnostic checks; an API error signal prioritizes deployment history.
        metrics = next((item.payload for item in state.evidence if item.tool_name == "service_metrics"), {})
        api = next((item.payload for item in state.evidence if item.tool_name == "api_health"), {})
        if metrics.get("current_quality", 1) < metrics.get("baseline_quality", 0):
            priority = ["feature_statistics", "data_drift", "pipeline_status", "deployment_history", "model_metadata", "api_health"]
        elif api.get("healthy") is False:
            priority = ["deployment_history", "api_health", "model_metadata", "feature_statistics", "pipeline_status", "data_drift"]
        else:
            priority = planned
        choice = next((name for name in priority if name in available), None)
        if choice:
            state.add_event("deterministic_plan", "Selected next check using deterministic priority", tool=choice)
        return choice
