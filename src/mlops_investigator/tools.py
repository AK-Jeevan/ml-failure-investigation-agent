"""Allowlisted read-only tools over the controlled simulation."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from .models import Evidence
from .simulation import SCENARIOS, get_scenario_data


class ToolError(RuntimeError):
    """A registered evidence tool could not complete its read operation."""


class ReadOnlyToolRegistry:
    def __init__(
        self,
        timeout_seconds: float = 3.0,
        service_url: str | None = None,
        use_base_fixtures: bool = False,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        if service_url:
            parsed = urlsplit(service_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Simulation API URL must be an HTTP(S) URL without embedded credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("Simulation API URL must not include a query string or fragment")
            try:
                parsed.port  # Force validation of malformed port values.
            except ValueError as exc:
                raise ValueError("Simulation API URL has an invalid port") from exc
        self.service_url = service_url.rstrip("/") if service_url else None
        self.use_base_fixtures = use_base_fixtures
        self._tools: dict[str, Callable[[str], Evidence]] = {
            "service_metrics": self.service_metrics,
            "model_metadata": self.model_metadata,
            "feature_statistics": self.feature_statistics,
            "pipeline_status": self.pipeline_status,
            "deployment_history": self.deployment_history,
            "api_health": self.api_health,
            "data_drift": self.data_drift,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def execute(self, name: str, scenario: str) -> Evidence:
        if name not in self._tools:
            raise ToolError(f"Tool {name!r} is not allowlisted")
        # The HTTP adapter enforces its timeout at the transport boundary.
        return self._tools[name](scenario)

    def _scenario_data(self, scenario: str) -> dict[str, Any]:
        if self.use_base_fixtures:
            try:
                return SCENARIOS[scenario]
            except KeyError as exc:
                raise ToolError(f"Scenario {scenario!r} is not in the base fixture set") from exc
        return get_scenario_data(scenario)

    def _read(self, scenario: str, payload_key: str, tool_name: str, summary: str) -> Evidence:
        payload: dict[str, Any] = self._scenario_data(scenario)[payload_key]
        return Evidence(tool_name=tool_name, summary=summary, payload=payload)

    def _remote(self, scenario: str, path: str, tool_name: str, summary: str) -> Evidence:
        if not self.service_url:
            raise ToolError("Simulation API URL is not configured")
        try:
            query = urlencode({"scenario": scenario})
            with urlopen(f"{self.service_url}{path}?{query}", timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, ValueError) as exc:
            raise ToolError(f"Simulation API request failed: {type(exc).__name__}") from exc
        if result.get("scenario") != scenario:
            raise ToolError("Simulation API scenario does not match the investigation scenario")
        payload = result.get("evidence")
        if payload is None:
            payload = {key: value for key, value in result.items() if key not in {"scenario", "service", "source"}}
        if not isinstance(payload, dict):
            raise ToolError("Simulation API returned an invalid evidence payload")
        return Evidence(tool_name=tool_name, summary=summary, payload=payload)

    def service_metrics(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/metrics", "service_metrics", "Compared current quality and error rate with baseline.")
        return self._read(scenario, "metrics", "service_metrics", "Compared current quality and error rate with baseline.")

    def model_metadata(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/evidence/model", "model_metadata", "Compared deployed model version with the previous version.")
        return self._read(scenario, "model", "model_metadata", "Compared deployed model version with the previous version.")

    def feature_statistics(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/evidence/features", "feature_statistics", "Compared feature null rates and schema state with baseline.")
        return self._read(scenario, "features", "feature_statistics", "Compared feature null rates and schema state with baseline.")

    def pipeline_status(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/evidence/pipeline", "pipeline_status", "Checked pipeline outcome and record counts.")
        return self._read(scenario, "pipeline", "pipeline_status", "Checked pipeline outcome and record counts.")

    def deployment_history(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/evidence/deployment", "deployment_history", "Checked the most relevant deployment/change record.")
        return self._read(scenario, "deployment", "deployment_history", "Checked the most relevant deployment/change record.")

    def api_health(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/health", "api_health", "Checked API health, latency, and error rate.")
        return self._read(scenario, "api", "api_health", "Checked API health, latency, and error rate.")

    def data_drift(self, scenario: str) -> Evidence:
        if self.service_url:
            return self._remote(scenario, "/evidence/drift", "data_drift", "Compared current feature distribution with baseline.")
        data = self._scenario_data(scenario)
        payload = data.get("drift", {"psi": 0.02, "threshold": 0.20, "feature": None, "expected_change": False})
        return Evidence(tool_name="data_drift", summary="Compared current feature distribution with baseline.", payload=payload)
