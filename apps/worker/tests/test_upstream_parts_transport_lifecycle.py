from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import upstream
from app.upstream_parts import client_lifecycle, errors, transport


def test_upstream_facade_exports_extracted_contracts_without_state_aliases() -> None:
    assert upstream.UpstreamError is errors.UpstreamError
    assert upstream.UpstreamCancelled is errors.UpstreamCancelled
    assert upstream._get_client is client_lifecycle._get_client
    assert upstream._iter_sse_curl is transport._iter_sse_curl

    for state_name in (
        "_client",
        "_images_client",
        "_proxied_clients",
        "_proxied_images_clients",
        "_retired_client_close_tasks",
        "_retired_clients",
    ):
        assert hasattr(upstream, state_name)
        assert not hasattr(client_lifecycle, state_name)


def test_client_builders_read_late_bound_facade_transport_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeTrackedClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(upstream, "_TrackedAsyncClient", FakeTrackedClient)
    timeout_config = upstream._TimeoutConfig(connect=3.0, read=40.0, write=5.0)

    json_client = upstream._build_client(
        timeout_config,
        proxy_url="socks5://proxy.example:1080",
    )
    images_client = upstream._build_images_client(
        timeout_config,
        proxy_url="http://proxy.example:8080",
    )

    assert isinstance(json_client, FakeTrackedClient)
    assert isinstance(images_client, FakeTrackedClient)
    assert calls[0]["headers"] == {"content-type": "application/json"}
    assert "headers" not in calls[1]
    assert calls[0]["proxy"] == "socks5://proxy.example:1080"
    assert calls[1]["proxy"] == "http://proxy.example:8080"
    assert calls[0]["trust_env"] is False
    assert calls[1]["trust_env"] is False
    assert calls[0]["follow_redirects"] is False
    assert calls[1]["follow_redirects"] is False
    assert isinstance(calls[0]["timeout"], httpx.Timeout)
    assert calls[0]["timeout"].connect == 3.0
    assert calls[0]["timeout"].read == 40.0
    assert calls[0]["timeout"].write == 5.0
    assert calls[0]["timeout"].pool == 3.0


@pytest.mark.asyncio
async def test_client_facade_uses_rebound_proxy_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout_config = upstream._TimeoutConfig(connect=1.0, read=2.0, write=3.0)
    rebound_cache: OrderedDict[tuple[upstream._TimeoutConfig, str], Any] = OrderedDict()
    built: list[object] = []

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return timeout_config

    def fake_build_client(
        _timeout_config: upstream._TimeoutConfig,
        *,
        proxy_url: str | None = None,
    ) -> object:
        assert proxy_url == "http://proxy.example:8080"
        client = object()
        built.append(client)
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, "_build_client", fake_build_client)
    monkeypatch.setattr(upstream, "_proxied_clients", rebound_cache)

    client = await upstream._get_client("http://proxy.example:8080")

    assert client is built[0]
    assert rebound_cache[(timeout_config, "http://proxy.example:8080")] is client


@pytest.mark.asyncio
async def test_sse_cancellation_uses_facade_cleanup_and_unlinks_body_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    read_started = asyncio.Event()
    never_release = asyncio.Event()
    created_paths: list[Path] = []
    terminated: list[object | None] = []
    create_kwargs: dict[str, Any] = {}
    real_mkstemp = upstream.tempfile.mkstemp

    class BlockingStdout:
        async def read(self, _size: int) -> bytes:
            read_started.set()
            await never_release.wait()
            return b""

    class FakeProc:
        pid = 999_999
        returncode: int | None = None
        stdout = BlockingStdout()
        stderr = None

    proc = FakeProc()

    def fake_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        fd, path = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_paths.append(Path(path))
        return fd, path

    async def fake_create_subprocess_exec(
        *_args: Any,
        **kwargs: Any,
    ) -> FakeProc:
        create_kwargs.update(kwargs)
        return proc

    async def fake_terminate(received: object | None) -> None:
        terminated.append(received)

    monkeypatch.setattr(upstream.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(
        upstream.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(upstream, "_terminate_curl_proc_group", fake_terminate)
    monkeypatch.setattr(upstream, "_generate_trace_id", lambda: "trace-test")
    monkeypatch.setattr(upstream, "_log_upstream_call", lambda **_kwargs: None)

    stream = upstream._iter_sse_curl(
        url="https://upstream.example/v1/responses",
        json_body={"stream": True},
        headers={"authorization": "Bearer test"},
        timeout_s=30,
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(read_started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert create_kwargs["start_new_session"] is True
    assert terminated == [proc]
    assert created_paths
    assert all(not path.exists() for path in created_paths)
    await stream.aclose()
