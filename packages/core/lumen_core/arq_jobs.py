"""Helpers for stable arq job identifiers shared by API and workers."""

from __future__ import annotations


def arq_job_id(kind: str, task_id: str, outbox_id: str | None) -> str:
    return f"lumen:{kind}:{task_id}:outbox:{outbox_id or 'direct'}"


__all__ = ["arq_job_id"]
