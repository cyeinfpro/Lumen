"""Stable uvicorn entrypoint."""

from image_job.app_factory import create_app

app = create_app()

__all__ = ["app", "create_app"]
