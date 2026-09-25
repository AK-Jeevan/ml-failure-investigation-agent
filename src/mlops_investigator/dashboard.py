"""Gradio investigation dashboard backed by the FastAPI service."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import gradio as gr

from .config import load_local_environment


def _api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base_url = os.getenv("AGENT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.getenv("API_ACCESS_TOKEN", "")
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "Request rejected")
        except (ValueError, AttributeError):
            detail = "Request rejected"
        raise RuntimeError(f"Agent API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not reach the Agent API ({type(exc).__name__})") from exc


def _blank_view(message: str) -> tuple[str, str, Any, Any, Any, Any, Any]:
    return message, "", None, None, None, None, None


def _load_view(investigation_id: str) -> tuple[str, str, Any, Any, Any, Any, Any]:
    if not investigation_id:
        return _blank_view("Start an investigation or enter an investigation ID.")
    try:
        payload = _api_request("GET", f"/investigations/{investigation_id}")
        status_payload = _api_request("GET", f"/investigations/{investigation_id}/status")
        report = payload.get("report") or {}
        evidence = payload.get("evidence", [])
        hypotheses = payload.get("hypotheses", [])
        trajectory = payload.get("trajectory", [])
        status_text = (
            f"**Status:** `{status_payload.get('status', 'unknown')}`  \n"
            f"**Updated:** {status_payload.get('updated_at', 'unknown')}  \n"
            f"**Conclusion:** {payload.get('conclusion') or 'Not finalized yet'}"
        )
        return status_text, investigation_id, payload, evidence, hypotheses, trajectory, report
    except RuntimeError as exc:
        return _blank_view(str(exc))


def _start_investigation(description: str, service: str, scenario: str):
    try:
        accepted = _api_request("POST", "/investigate", {
            "description": description,
            "service": service,
            "scenario": scenario,
        })
        investigation_id = accepted["investigation_id"]
        return (investigation_id, *_load_view(investigation_id))
    except (RuntimeError, KeyError, TypeError) as exc:
        return ("", *_blank_view(str(exc)))


def _approve(investigation_id: str, reviewer: str, reason: str) -> str:
    if not investigation_id:
        return "Start or select an investigation first."
    try:
        _api_request("POST", f"/investigations/{investigation_id}/approve", {
            "reviewer": reviewer,
            "reason": reason,
        })
        return "Approval recorded. No remediation was executed by this dashboard."
    except RuntimeError as exc:
        return str(exc)


def _reject(investigation_id: str, reviewer: str, reason: str) -> str:
    if not investigation_id:
        return "Start or select an investigation first."
    try:
        _api_request("POST", f"/investigations/{investigation_id}/reject", {
            "reviewer": reviewer,
            "reason": reason,
        })
        return "Rejection recorded. No remediation was executed."
    except RuntimeError as exc:
        return str(exc)


def _cancel(investigation_id: str) -> str:
    if not investigation_id:
        return "Start or select an investigation first."
    try:
        result = _api_request("POST", f"/investigations/{investigation_id}/cancel")
        return f"Cancellation status: {result.get('status', 'unknown')}"
    except RuntimeError as exc:
        return str(exc)


def create_dashboard() -> gr.Blocks:
    """Build a workflow-focused dashboard (not a chat interface)."""
    with gr.Blocks(title="ML Production Investigation Dashboard") as demo:
        gr.Markdown(
            "# ML Production Investigation\n"
            "Start a bounded evidence collection run, inspect the agent’s reasoning trail, "
            "and review remediation recommendations before recording a decision."
        )
        investigation_state = gr.State("")

        with gr.Row():
            with gr.Column(scale=2):
                description = gr.Textbox(
                    label="Incident description",
                    placeholder="Prediction quality dropped after the latest feature deployment…",
                    lines=3,
                    max_lines=8,
                )
            with gr.Column(scale=1):
                service = gr.Textbox(label="Affected service", value="fraud-score-api", max_length=200)
                scenario = gr.Dropdown(
                    choices=["feature_schema_change", "data_drift", "api_regression", "healthy"],
                    value="feature_schema_change",
                    label="Controlled scenario",
                )
        with gr.Row():
            start_button = gr.Button("Start investigation", variant="primary")
            investigation_id = gr.Textbox(label="Investigation ID", interactive=True)
            refresh_button = gr.Button("Refresh")
            cancel_button = gr.Button("Cancel investigation", variant="stop")

        status_view = gr.Markdown("Start an investigation to view its status and report.")
        notice = gr.Markdown()

        with gr.Tabs():
            with gr.Tab("Overview"):
                full_payload = gr.JSON(label="Investigation overview")
                report_view = gr.JSON(label="Final investigation report")
            with gr.Tab("Evidence"):
                evidence_view = gr.JSON(label="Evidence collected")
            with gr.Tab("Hypotheses"):
                hypothesis_view = gr.JSON(label="Hypotheses and evidence assessment")
            with gr.Tab("Trajectory"):
                trajectory_view = gr.JSON(label="Investigation steps")
            with gr.Tab("Human review"):
                gr.Markdown("Approval records a reviewer decision. It does not execute a remediation.")
                reviewer = gr.Textbox(label="Reviewer", max_length=100)
                reason = gr.Textbox(label="Decision reason", lines=2, max_length=1_000)
                with gr.Row():
                    approve_button = gr.Button("Approve recommendation", variant="primary")
                    reject_button = gr.Button("Reject recommendation", variant="stop")

        view_outputs = [status_view, investigation_id, full_payload, evidence_view, hypothesis_view, trajectory_view, report_view]
        start_button.click(
            _start_investigation,
            inputs=[description, service, scenario],
            outputs=[investigation_state, *view_outputs],
        )
        refresh_button.click(
            _load_view,
            inputs=[investigation_id],
            outputs=view_outputs,
        ).then(lambda value: value, inputs=[investigation_id], outputs=[investigation_state])
        approve_button.click(
            _approve,
            inputs=[investigation_id, reviewer, reason],
            outputs=[notice],
        ).then(_load_view, inputs=[investigation_id], outputs=view_outputs)
        reject_button.click(
            _reject,
            inputs=[investigation_id, reviewer, reason],
            outputs=[notice],
        ).then(_load_view, inputs=[investigation_id], outputs=view_outputs)
        cancel_button.click(_cancel, inputs=[investigation_id], outputs=[notice])
    return demo


def main() -> None:
    load_local_environment()
    app = create_dashboard()
    username = os.getenv("GRADIO_AUTH_USERNAME", "")
    password = os.getenv("GRADIO_AUTH_PASSWORD", "")
    server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    if (username or password) and not (username and password):
        raise RuntimeError("Set both GRADIO_AUTH_USERNAME and GRADIO_AUTH_PASSWORD")
    if server_name not in {"127.0.0.1", "localhost", "::1"} and not (username and password):
        raise RuntimeError("Dashboard authentication is required when binding beyond localhost")
    app.launch(
        server_name=server_name,
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        theme=gr.themes.Soft(),
        auth=(username, password) if username and password else None,
        share=False,
    )


if __name__ == "__main__":
    main()
