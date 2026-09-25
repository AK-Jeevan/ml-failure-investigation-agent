"""Command line entry point for a local simulated investigation."""

from __future__ import annotations

import argparse
import json
import os

from .evaluation import run_evaluation
from .engine import InvestigationEngine
from .config import load_local_environment
from .llm import NvidiaProvider
from .models import Incident
from .remediation import ApprovalError, RemediationApprovalService, SimulationRemediationExecutor
from .simulation import reset_simulation_state
from .verification import VerificationEngine, VerificationError
from .storage import InvestigationStore
from .tools import ReadOnlyToolRegistry


def main() -> None:
    load_local_environment()
    parser = argparse.ArgumentParser(description="Investigate a simulated ML production incident")
    parser.add_argument("--scenario", default=os.getenv("SIMULATION_SCENARIO", "feature_schema_change"), choices=["feature_schema_change", "data_drift", "api_regression", "healthy"])
    parser.add_argument("--incident", default="Production ML service is behaving abnormally.")
    parser.add_argument("--database", default=os.getenv("INVESTIGATION_DB", "data/investigations.sqlite3"))
    parser.add_argument("--offline", action="store_true", help="Do not use the configured NVIDIA model")
    parser.add_argument("--fixtures", action="store_true", help="Read scenario evidence in-process instead of from the FastAPI simulator")
    parser.add_argument("--evaluate", action="store_true", help="Run the offline scenario and trajectory evaluation suite")
    parser.add_argument("--evaluation-cases", help="JSON evaluation dataset path (used with --evaluate)")
    decision_group = parser.add_mutually_exclusive_group()
    decision_group.add_argument("--approve", metavar="INVESTIGATION_ID", help="Approve a pending remediation recommendation; does not execute it")
    decision_group.add_argument("--reject", metavar="INVESTIGATION_ID", help="Reject a pending remediation recommendation")
    decision_group.add_argument("--verify", metavar="INVESTIGATION_ID", help="Verify recovery using the selected current simulation scenario")
    decision_group.add_argument("--execute-simulation-remediation", metavar="INVESTIGATION_ID", help="Apply an allowlisted action to local simulation state after approval")
    decision_group.add_argument("--reset-simulation", action="store_true", help="Reset local simulated state for the selected scenario")
    parser.add_argument("--reviewer", help="Reviewer identity to record with an approval decision")
    parser.add_argument("--reason", help="Required explanation for an approval decision")
    args = parser.parse_args()

    if args.evaluate:
        try:
            result = run_evaluation(args.evaluation_cases)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["summary"]["failed"]:
            raise SystemExit(1)
        return

    store = InvestigationStore(args.database)
    if args.reset_simulation:
        reset_simulation_state(args.scenario)
        print(json.dumps({"status": "reset", "scenario": args.scenario, "scope": "local_simulation_only"}, indent=2))
        return
    if args.execute_simulation_remediation:
        investigation_id = args.execute_simulation_remediation
        payload = store.load_payload(investigation_id)
        if payload is None:
            parser.error(f"Investigation {investigation_id!r} was not found in {args.database}")
        try:
            updated = SimulationRemediationExecutor().execute(payload)
            store.save_payload(investigation_id, updated)
        except (ApprovalError, KeyError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        print(json.dumps(updated["actions_performed"][-1], indent=2, ensure_ascii=False))
        return
    if args.verify:
        original = store.load_payload(args.verify)
        if original is None:
            parser.error(f"Investigation {args.verify!r} was not found in {args.database}")
        tools = ReadOnlyToolRegistry(service_url=None if args.fixtures else os.getenv("SIMULATION_API_URL"))
        try:
            result = VerificationEngine(tools).verify(original, args.scenario)
            original.setdefault("trajectory", []).append(result["trajectory_event"])
            if isinstance(original.get("report"), dict):
                original["report"]["verification"] = result["verification"]
                uncertainty = original["report"].get("uncertainty", [])
                if isinstance(uncertainty, list):
                    original["report"]["uncertainty"] = [
                        item for item in uncertainty if item != "No remediation was executed, so recovery has not been verified."
                    ]
                    original["report"]["uncertainty"].append(
                        "A local simulation remediation was executed; verification reflects deterministic checks against the current observed scenario. No production system was changed."
                    )
                original["report"].setdefault("trajectory", []).append(result["trajectory_event"])
                original["report"].setdefault("timeline", []).append(result["trajectory_event"])
            original["verification"] = result["verification"]
            store.save_payload(args.verify, original)
        except (VerificationError, KeyError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result["verification"], indent=2, ensure_ascii=False))
        return
    if args.approve or args.reject:
        if not args.reviewer or not args.reason:
            parser.error("--reviewer and --reason are required with --approve or --reject")
        investigation_id = args.approve or args.reject
        payload = store.load_payload(investigation_id)
        if payload is None:
            parser.error(f"Investigation {investigation_id!r} was not found in {args.database}")
        try:
            updated = RemediationApprovalService().decide(
                payload,
                "approved" if args.approve else "rejected",
                args.reviewer,
                args.reason,
            )
            store.save_payload(investigation_id, updated)
        except (ApprovalError, KeyError) as exc:
            parser.error(str(exc))
        print(json.dumps(updated, indent=2, ensure_ascii=False))
        return

    provider = None
    if not args.offline and os.environ.get("NVIDIA_API_KEY"):
        provider = NvidiaProvider(
            api_key=os.environ.get("NVIDIA_API_KEY", ""),
            model=os.environ.get("NVIDIA_MODEL", ""),
            base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        )
    tools = ReadOnlyToolRegistry(service_url=None if args.fixtures else os.getenv("SIMULATION_API_URL"))
    engine = InvestigationEngine(tools=tools, store=store, provider=provider)
    try:
        state = engine.investigate(Incident(description=args.incident, scenario=args.scenario))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(state.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
