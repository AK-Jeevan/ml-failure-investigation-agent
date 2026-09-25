"""Deterministic closed-loop verification for manually remediated scenarios."""

from __future__ import annotations

from typing import Any

from .models import Evidence, TrajectoryEvent
from .tools import ReadOnlyToolRegistry


class VerificationError(ValueError):
    """Verification cannot proceed under the current approval state."""


class VerificationEngine:
    def __init__(self, tools: ReadOnlyToolRegistry) -> None:
        self.tools = tools

    def verify(self, original: dict[str, Any], current_scenario: str) -> dict[str, Any]:
        remediation = original.get("remediation") or {}
        if remediation.get("approval_required") and remediation.get("approval_status") != "approved":
            if remediation.get("approval_status") == "approved_not_executed":
                raise VerificationError("The recommendation is approved; execute its local simulation action before verification")
            raise VerificationError("Approve the remediation recommendation before recovery verification")
        if remediation.get("execution_status") != "simulated_executed":
            raise VerificationError("Execute the approved local simulation remediation before recovery verification")
        hypothesis_id = remediation.get("hypothesis_id")
        if not hypothesis_id:
            raise VerificationError("The investigation has no probable cause to verify")

        baseline_evidence = original.get("evidence", [])
        baseline_metrics = self._payload(baseline_evidence, "service_metrics")
        baseline_features = self._payload(baseline_evidence, "feature_statistics")
        checks: list[dict[str, Any]] = []
        observed: list[Evidence] = []

        try:
            if hypothesis_id in {"feature_change", "data_drift"}:
                metrics = self._collect("service_metrics", current_scenario)
                observed.append(metrics)
                quality_floor = float(baseline_metrics.get("baseline_quality", 0)) * 0.95
                checks.append(self._check(
                    "prediction_quality",
                    float(metrics.payload.get("current_quality", 0)) >= quality_floor,
                    f"Current quality must recover to at least 95% of baseline ({quality_floor:.4f}).",
                    metrics,
                ))

            if hypothesis_id == "feature_change":
                features = self._collect("feature_statistics", current_scenario)
                observed.append(features)
                original_null_rate = float(baseline_features.get("risk_band_null_rate_baseline", 0.05))
                null_limit = max(0.05, original_null_rate * 2)
                feature_ok = not features.payload.get("schema_changed", True) and float(features.payload.get("risk_band_null_rate_current", 1)) <= null_limit
                checks.append(self._check(
                    "feature_contract",
                    feature_ok,
                    f"Schema must match and risk_band null rate must be at most {null_limit:.4f}.",
                    features,
                ))

            elif hypothesis_id == "data_drift":
                drift = self._collect("data_drift", current_scenario)
                observed.append(drift)
                drift_ok = float(drift.payload.get("psi", 1)) <= float(drift.payload.get("threshold", 0))
                checks.append(self._check("input_drift", drift_ok, "PSI must be at or below the configured threshold.", drift))

            elif hypothesis_id == "api_regression":
                api = self._collect("api_health", current_scenario)
                observed.append(api)
                api_ok = (
                    bool(api.payload.get("healthy"))
                    and float(api.payload.get("error_rate", 1)) <= 0.05
                    and float(api.payload.get("p95_latency_ms", float("inf"))) <= 500
                )
                checks.append(self._check("api_health", api_ok, "API must be healthy, error rate ≤ 5%, and p95 latency ≤ 500 ms.", api))

            elif hypothesis_id == "pipeline_failure":
                pipeline = self._collect("pipeline_status", current_scenario)
                observed.append(pipeline)
                pipeline_ok = (
                    pipeline.payload.get("status") == "succeeded"
                    and int(pipeline.payload.get("records_written", 0)) >= int(pipeline.payload.get("records_expected", 1))
                )
                checks.append(self._check("pipeline_recovery", pipeline_ok, "Pipeline must succeed and write at least the expected record count.", pipeline))

            elif hypothesis_id == "model_change":
                model = self._collect("model_metadata", current_scenario)
                observed.append(model)
                baseline_model = self._payload(baseline_evidence, "model_metadata")
                model_ok = bool(baseline_model) and model.payload.get("current_version") == baseline_model.get("baseline_version")
                checks.append(self._check("model_version", model_ok, "Deployed model version must match the previously serving baseline.", model))
            else:
                raise VerificationError(f"No deterministic verification criteria exist for {hypothesis_id!r}")

        except Exception as exc:
            if isinstance(exc, VerificationError):
                raise
            checks.append({"name": "evidence_collection", "passed": False, "detail": f"Evidence collection failed: {type(exc).__name__}"})

        if not checks:
            status = "inconclusive"
        elif all(item["passed"] for item in checks):
            status = "passed"
        elif any(item["passed"] for item in checks):
            status = "partial"
        else:
            status = "failed"

        verification = {
            "status": status,
            "scenario_checked": current_scenario,
            "hypothesis_id": hypothesis_id,
            "checks": checks,
            "evidence": [
                {"evidence_id": item.evidence_id, "source": item.tool_name, "observed_at": item.collected_at, "data": item.payload}
                for item in observed
            ],
            "actions_performed": list(original.get("actions_performed", [])),
            "detail": "Recovery checks are deterministic. The recorded action changed only local simulation state; no production action was executed.",
        }
        event = TrajectoryEvent(
            kind="verification_completed",
            message=f"Recovery verification {status}",
            details={"scenario": current_scenario, "hypothesis_id": hypothesis_id, "checks_passed": sum(item["passed"] for item in checks), "checks_total": len(checks)},
        )
        return {"verification": verification, "trajectory_event": {
            "kind": event.kind,
            "message": event.message,
            "details": event.details,
            "created_at": event.created_at,
        }}

    def _collect(self, tool_name: str, scenario: str) -> Evidence:
        return self.tools.execute(tool_name, scenario)

    @staticmethod
    def _payload(evidence: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
        return next((item.get("payload", {}) for item in evidence if item.get("tool_name") == tool_name), {})

    @staticmethod
    def _check(name: str, passed: bool, criteria: str, evidence: Evidence) -> dict[str, Any]:
        return {
            "name": name,
            "passed": passed,
            "criteria": criteria,
            "evidence_id": evidence.evidence_id,
            "source": evidence.tool_name,
        }
