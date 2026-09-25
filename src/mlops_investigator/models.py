"""Typed records shared by the investigation engine and its tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvestigationStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvidencePolarity(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


@dataclass(slots=True)
class Incident:
    description: str
    service: str = "fraud-score-api"
    scenario: str = "feature_schema_change"
    incident_id: str = field(default_factory=lambda: str(uuid4()))
    observed_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Evidence:
    tool_name: str
    summary: str
    payload: dict[str, Any]
    collected_at: str = field(default_factory=utc_now)
    evidence_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(slots=True)
class Hypothesis:
    hypothesis_id: str
    statement: str
    expected_signal: str
    score: int = 0
    status: str = "untested"
    evidence_ids: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    confidence: str = "unknown"


@dataclass(slots=True)
class TrajectoryEvent:
    kind: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Investigation:
    incident: Incident
    status: InvestigationStatus = InvestigationStatus.RUNNING
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    trajectory: list[TrajectoryEvent] = field(default_factory=list)
    tool_calls: int = 0
    model_calls: int = 0
    conclusion: str | None = None
    stop_reason: str | None = None
    remediation: dict[str, Any] | None = None
    report: dict[str, Any] | None = None

    def add_event(self, kind: str, message: str, **details: Any) -> None:
        self.trajectory.append(TrajectoryEvent(kind, message, details))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
