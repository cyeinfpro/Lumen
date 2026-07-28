"""Dependency providers for the production workflow application."""

from __future__ import annotations

from fastapi import Request

from ...composition import WorkflowApplication


def get_workflow_application(request: Request) -> WorkflowApplication:
    application = request.app.state.workflow_application
    if not isinstance(application, WorkflowApplication):
        raise RuntimeError("workflow application is not configured")
    return application


__all__ = ["get_workflow_application"]
