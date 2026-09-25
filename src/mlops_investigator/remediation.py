"""Advisory remediation planning and human approval transitions.

This module intentionally has no production action executor. Approval records
authorization intent for a future reviewed executor; it never performs a change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import utc_now
from .simulation import apply_simulated_remediation

RECOMMENDATIONS = {
    "feature_change": (
        "Compare the feature producer and model-consumer schemas; restore a compatible field contract, "
        "then replay representative data and validate feature null rates before considering deployment."
    ),
    "data_drift": (
        "Validate the population shift with the data owner and inspect affected feature slices; assess model changes only after "
        "checking labels and offline evaluation against the new population."
    ),
    "model_change": (
        "Compare the changed model against the previous version on a representative evaluation set and inspect the release criteria."
    ),
    "pipeline_failure": (
        "Repair the failed or incomplete pipeline run, validate record counts and freshness, then replay downstream feature checks."
    ),
    "api_regression": (
        "Reproduce the API/runtime regression in a non-production environment and prepare a reviewed fix or rollback."
    ),
}

PRODUCTION_CHANGE_HYPOTHESES = {"feature_change", "model_change", "pipeline_failure", "api_regression"}


class ApprovalError(ValueError):
    """An approval request is missing, invalid, or already decided."""


class RemediationPlanner:
    def create(self, hypothesis_id: str) -> dict[str, Any] | None:
        recommendation = RECOMMENDATIONS.get(hypothesis_id)
        if recommendation is None:
            return None
        requires_approval = hypothesis_id in PRODUCTION_CHANGE_HYPOTHESES
        return {
            "plan_id": str(uuid4()),
            "hypothesis_id": hypothesis_id,
            "simulated_action": f"simulate_recovery_{hypothesis_id}",
            "recommendation": recommendation,
            "risk": "high" if requires_approval else "medium",
            "approval_required": requires_approval,
            "approval_status": "pending" if requires_approval else "not_required",
            "approval": None,
            "execution_status": "not_available",
        }


class RemediationApprovalService:
    """Validate and record approve/reject decisions without executing actions."""

    def decide(self, payload: dict[str, Any], decision: str, reviewer: str, reason: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ApprovalError("Decision must be 'approved' or 'rejected'")
        reviewer = reviewer.strip()
        reason = reason.strip()
        if not reviewer or len(reviewer) > 100:
            raise ApprovalError("Reviewer is required and must be at most 100 characters")
        if len(reason) < 5 or len(reason) > 1000:
            raise ApprovalError("Decision reason must be between 5 and 1000 characters")

        plan = payload.get("remediation")
        if not isinstance(plan, dict):
            raise ApprovalError("Investigation has no remediation plan")
        if not plan.get("approval_required"):
            raise ApprovalError("This remediation plan does not require approval")
        if plan.get("approval_status") != "pending":
            raise ApprovalError(f"Remediation is not pending approval (state: {plan.get('approval_status')})")

        timestamp = datetime.now(timezone.utc).isoformat()
        approval_record = {
            "decision": decision,
            "reviewer": reviewer,
            "reason": reason,
            "decided_at": timestamp,
        }
        plan["approval_status"] = "approved_not_executed" if decision == "approved" else "rejected"
        plan["approval"] = approval_record

        event = {
            "kind": "remediation_approval",
            "message": f"Remediation recommendation {decision}; no action was executed",
            "details": {"plan_id": plan["plan_id"], **approval_record},
            "created_at": timestamp,
        }
        payload.setdefault("trajectory", []).append(event)

        report = payload.get("report")
        if isinstance(report, dict):
            report["remediation_approval"] = {"status": plan["approval_status"], **approval_record}
            approval_section = report.get("required_human_approval")
            if isinstance(approval_section, dict):
                approval_section["status"] = plan["approval_status"]
                approval_section["reviewer"] = reviewer
            for section in ("timeline", "trajectory"):
                if isinstance(report.get(section), list):
                    report[section].append(event)
        return payload


class SimulationRemediationExecutor:
    """Execute only a mapped local fixture mutation after its approval gate."""

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = payload.get("remediation")
        if not isinstance(plan, dict):
            raise ApprovalError("Investigation has no remediation plan")
        if plan.get("approval_required"):
            if plan.get("approval_status") != "approved_not_executed":
                raise ApprovalError("A high-risk simulated action requires recorded approval first")
            approval = plan.get("approval")
            if (
                not isinstance(approval, dict)
                or approval.get("decision") != "approved"
                or not isinstance(approval.get("reviewer"), str)
                or not approval.get("reviewer", "").strip()
                or not isinstance(approval.get("reason"), str)
                or len(approval.get("reason", "").strip()) < 5
                or not isinstance(approval.get("decided_at"), str)
            ):
                raise ApprovalError("Recorded approval details are missing or invalid")
        if plan.get("execution_status") != "not_available":
            raise ApprovalError(f"Remediation is not executable in its current state: {plan.get('execution_status')}")

        hypothesis_id = plan.get("hypothesis_id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise ApprovalError("Remediation plan is missing a valid hypothesis id")
        expected_action = f"simulate_recovery_{hypothesis_id}"
        if plan.get("simulated_action") != expected_action:
            raise ApprovalError("Remediation action does not match the registered simulation allowlist")
        incident = payload.get("incident") or {}
        scenario = incident.get("scenario")
        if not isinstance(scenario, str):
            raise ApprovalError("Investigation is missing its simulation scenario")
        expected_scenario = {
            "feature_change": "feature_schema_change",
            "data_drift": "data_drift",
            "api_regression": "api_regression",
        }.get(hypothesis_id)
        if scenario != expected_scenario:
            raise ApprovalError("This remediation action is not registered for the investigation scenario")

        result = apply_simulated_remediation(scenario, hypothesis_id)
        action = {
            "action_id": plan["simulated_action"],
            "scope": "local_simulation_only",
            "scenario": scenario,
            "hypothesis_id": hypothesis_id,
            "description": result["description"],
            "executed_at": utc_now(),
            "production_changed": False,
        }
        plan["approval_status"] = "approved" if plan.get("approval_required") else "not_required"
        plan["execution_status"] = "simulated_executed"
        payload.setdefault("actions_performed", []).append(action)

        event = {
            "kind": "simulated_remediation_executed",
            "message": "Allowlisted local simulation recovery applied; no production system was changed",
            "details": action,
            "created_at": action["executed_at"],
        }
        payload.setdefault("trajectory", []).append(event)
        report = payload.get("report")
        if isinstance(report, dict):
            report.setdefault("actions_performed", []).append(action)
            report["remediation_execution_status"] = "simulated_executed"
            approval_section = report.get("required_human_approval")
            if isinstance(approval_section, dict):
                approval_section["status"] = plan["approval_status"]
            report.setdefault("trajectory", []).append(event)
            report.setdefault("timeline", []).append(event)
        return payload
