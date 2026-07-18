"""Shared fixtures for Volcano asset worker tests."""

from __future__ import annotations

import json
from typing import Any

from lumen_core.video_providers import (
    VideoProviderDefinition,
    video_provider_binding_fingerprint,
)
from lumen_core.volcano_assets import volcano_asset_operation_key


def provider() -> VideoProviderDefinition:
    return VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://generation.example/v1",
        api_key="generation-key",
        access_key_id="AKLTEXAMPLE",
        secret_access_key="secret-example",
        project_name="project-a",
        region="cn-beijing",
        enabled=True,
        priority=100,
        models={"seedance:reference": "doubao-seedance-ref"},
    )


def operation() -> dict[str, Any]:
    selected = provider()
    return {
        "id": "operation-1",
        "action": "create_asset",
        "status": "queued",
        "progress_stage": "queued",
        "attempt": 1,
        "retryable": False,
        "user_id": "user-1",
        "actor_email_hash": "email-hash",
        "actor_ip_hash": "ip-hash",
        "model": "seedance",
        "provider_name": "volcano-main",
        "provider_binding": video_provider_binding_fingerprint(selected),
        "project_name": "project-a",
        "region": "cn-beijing",
        "group_id": "group-1",
        "name": "User Display Name",
        "asset_type": "Video",
        "local_source_id": "video-1",
        "public_base_url": "https://lumen.example",
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:00+00:00",
        "completed_at": None,
        "result": None,
        "error": None,
    }


def management_operation(
    action: str,
    **changes: Any,
) -> dict[str, Any]:
    payload = operation()
    payload.update(
        {
            "action": action,
            "description": "Private portrait references",
            "asset_id": "asset-1",
            **changes,
        }
    )
    return payload


class Redis:
    def __init__(
        self,
        operation_payload: dict[str, Any] | list[dict[str, Any]],
    ) -> None:
        payloads = (
            operation_payload
            if isinstance(operation_payload, list)
            else [operation_payload]
        )
        self.values = {
            volcano_asset_operation_key(str(payload["id"])): json.dumps(payload)
            for payload in payloads
        }
        self.enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.enqueue_error: Exception | None = None
        self.fail_success_sets = 0
        self.renew_result = 1

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        if (
            key == volcano_asset_operation_key("operation-1")
            and self.fail_success_sets > 0
            and json.loads(value).get("status") == "succeeded"
        ):
            self.fail_success_sets -= 1
            raise ConnectionError("success state unavailable")
        self.values[key] = value
        return True

    async def eval(
        self,
        script: str,
        numkeys: int,
        *parts: Any,
    ) -> int:
        keys = [str(item) for item in parts[:numkeys]]
        args = list(parts[numkeys:])
        if "volcano-operation-fence-allocate" in script:
            lock_key, fencing_key = keys
            token = str(args[0])
            if self.values.get(lock_key) != token:
                return 0
            fencing = int(self.values.get(fencing_key) or 0) + 1
            self.values[fencing_key] = str(fencing)
            return fencing
        if "volcano-operation-fence-confirm" in script:
            lock_key, fencing_key, operation_key = keys
            token, fencing, attempt = map(str, args[:3])
            if self.values.get(lock_key) != token:
                return -1
            if str(self.values.get(fencing_key) or "") != fencing:
                return -2
            if self.renew_result != 1:
                return 0
            if attempt:
                operation = json.loads(self.values[operation_key])
                if int(operation.get("attempt") or 1) != int(attempt):
                    return -5
            return 1
        if "volcano-operation-fence-set" in script:
            lock_key, fencing_key, operation_key = keys
            token, fencing, attempt, payload = map(str, args[:4])
            if self.values.get(lock_key) != token:
                return -1
            if str(self.values.get(fencing_key) or "") != fencing:
                return -2
            current = json.loads(self.values[operation_key])
            if int(current.get("attempt") or 1) != int(attempt):
                return -5
            candidate = json.loads(payload)
            if self.fail_success_sets > 0 and candidate.get("status") == "succeeded":
                self.fail_success_sets -= 1
                raise ConnectionError("success state unavailable")
            self.values[operation_key] = payload
            return 1
        key = keys[0]
        token = str(args[0])
        if self.values.get(key) != token:
            return 0
        if "EXPIRE" in script:
            assert args[1:]
            return self.renew_result
        self.values.pop(key, None)
        return 1

    async def enqueue_job(
        self,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.enqueued.append((name, args, kwargs))
        return object()

    def operation(self, operation_id: str = "operation-1") -> dict[str, Any]:
        return json.loads(self.values[volcano_asset_operation_key(operation_id)])


__all__ = ["Redis", "management_operation", "operation", "provider"]
