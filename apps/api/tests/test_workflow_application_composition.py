from __future__ import annotations

from starlette.requests import Request

from app.main import build_app
from app.workflows.transport.http.dependencies import get_workflow_application


def test_build_app_owns_distinct_workflow_application_instances() -> None:
    first = build_app()
    second = build_app()

    first_application = first.state.workflow_application
    second_application = second.state.workflow_application

    assert first_application is not second_application
    assert first_application.http is not second_application.http
    assert first_application.runtime is not second_application.runtime


def test_workflow_dependency_reads_the_request_application_state() -> None:
    app = build_app()
    request = Request({"type": "http", "app": app})

    assert get_workflow_application(request) is app.state.workflow_application
