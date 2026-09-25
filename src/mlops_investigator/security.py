"""Deterministic input and boundary checks for investigation requests."""

from __future__ import annotations

import json

from .models import Evidence, Incident
from .simulation import SCENARIOS


MAX_INCIDENT_DESCRIPTION_CHARS = 4_000
MAX_SERVICE_NAME_CHARS = 200
MAX_EVIDENCE_JSON_CHARS = 100_000


def validate_incident_input(incident: Incident) -> None:
    """Reject malformed or oversized input before it reaches tools or the LLM.

    Incident text is treated as untrusted data. It is not filtered for phrases
    that resemble prompts because such filtering is unreliable; tool access and
    remediation authorization are enforced by deterministic allowlists instead.
    """
    if not isinstance(incident.description, str) or not incident.description.strip():
        raise ValueError("Incident description must be a non-empty string")
    if len(incident.description) > MAX_INCIDENT_DESCRIPTION_CHARS:
        raise ValueError(f"Incident description exceeds {MAX_INCIDENT_DESCRIPTION_CHARS} characters")
    if "\x00" in incident.description:
        raise ValueError("Incident description contains a null character")
    if not isinstance(incident.service, str) or not incident.service.strip():
        raise ValueError("Service name must be a non-empty string")
    if len(incident.service) > MAX_SERVICE_NAME_CHARS:
        raise ValueError(f"Service name exceeds {MAX_SERVICE_NAME_CHARS} characters")
    if not isinstance(incident.scenario, str) or incident.scenario not in SCENARIOS:
        raise ValueError("Incident scenario is not in the simulation allowlist")


def validate_evidence(evidence: Evidence, expected_tool: str) -> None:
    """Validate adapter output before storing it or exposing it to the model."""
    if not isinstance(evidence, Evidence):
        raise ValueError("Evidence adapter returned an unsupported result type")
    if evidence.tool_name != expected_tool:
        raise ValueError("Evidence source does not match the requested allowlisted tool")
    if not isinstance(evidence.evidence_id, str) or not evidence.evidence_id or len(evidence.evidence_id) > 128:
        raise ValueError("Evidence identifier is invalid")
    if not isinstance(evidence.collected_at, str) or len(evidence.collected_at) > 80:
        raise ValueError("Evidence timestamp is invalid")
    if not isinstance(evidence.summary, str) or len(evidence.summary) > 2_000:
        raise ValueError("Evidence summary is invalid or too large")
    if not isinstance(evidence.payload, dict):
        raise ValueError("Evidence payload must be a JSON object")
    try:
        encoded = json.dumps(evidence.payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Evidence payload is not valid finite JSON data") from exc
    if len(encoded) > MAX_EVIDENCE_JSON_CHARS:
        raise ValueError("Evidence payload exceeds the configured size limit")
