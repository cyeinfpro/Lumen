from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import byok_runtime, upstream
from app.upstream_parts import client_lifecycle, errors, transport
from lumen_core.url_security import PublicHttpTarget


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


def test_curl_sse_decoder_preserves_multiline_data_and_event_type() -> None:
    event = transport._decode_curl_sse_event(
        "response.completed",
        ['{"response":', '{"id":"response-1"}}'],
    )

    assert event == {
        "type": "response.completed",
        "response": {"id": "response-1"},
    }
    assert transport._decode_curl_sse_event(None, ["[DONE]"]) is None


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


def test_direct_pinned_client_uses_validated_transport_without_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    pinned_transport = object()
    target = PublicHttpTarget(
        "https://byok.example/v1",
        ("203.0.113.10",),
    )

    class FakeTrackedClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(upstream, "_TrackedAsyncClient", FakeTrackedClient)
    monkeypatch.setattr(
        upstream,
        "pinned_async_http_transport",
        lambda current: pinned_transport if current is target else None,
    )

    client = upstream._build_client(
        upstream._TimeoutConfig(connect=3.0, read=40.0, write=5.0),
        pinned_target=target,
    )

    assert isinstance(client, FakeTrackedClient)
    assert calls[0]["transport"] is pinned_transport
    assert calls[0]["proxy"] is None
    assert calls[0]["follow_redirects"] is False
    assert calls[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_explicit_byok_target_pins_direct_images_but_not_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = PublicHttpTarget(
        "https://byok.example/v1",
        ("203.0.113.10",),
    )
    timeout_config = upstream._TimeoutConfig(connect=3.0, read=40.0, write=5.0)
    built: list[dict[str, Any]] = []

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return timeout_config

    def fake_build_images_client(
        _timeout_config: upstream._TimeoutConfig,
        *,
        proxy_url: str | None = None,
        pinned_target: Any | None = None,
    ) -> object:
        client = object()
        built.append(
            {
                "client": client,
                "proxy_url": proxy_url,
                "pinned_target": pinned_target,
            }
        )
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, "_build_images_client", fake_build_images_client)
    monkeypatch.setattr(upstream, "_proxied_images_clients", OrderedDict())
    client_lifecycle._pinned_images_clients.clear()
    try:
        direct_client = await upstream._get_images_client(pinned_target=target)
        proxy_client = await upstream._get_images_client(
            "http://proxy-user:proxy-pass@proxy.example:8080"
        )
    finally:
        client_lifecycle._pinned_images_clients.clear()

    assert built[0] == {
        "client": direct_client,
        "proxy_url": None,
        "pinned_target": target,
    }
    assert built[1] == {
        "client": proxy_client,
        "proxy_url": "http://proxy-user:proxy-pass@proxy.example:8080",
        "pinned_target": None,
    }


@pytest.mark.asyncio
async def test_ambient_byok_target_does_not_pollute_later_unpinned_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = PublicHttpTarget(
        "https://byok.example/v1",
        ("203.0.113.10",),
    )
    timeout_config = upstream._TimeoutConfig(connect=3.0, read=40.0, write=5.0)
    built: list[dict[str, Any]] = []

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return timeout_config

    def fake_build_images_client(
        _timeout_config: upstream._TimeoutConfig,
        *,
        proxy_url: str | None = None,
        pinned_target: Any | None = None,
    ) -> object:
        client = object()
        built.append(
            {
                "client": client,
                "proxy_url": proxy_url,
                "pinned_target": pinned_target,
            }
        )
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, "_build_images_client", fake_build_images_client)
    monkeypatch.setattr(upstream, "_images_client", None)
    monkeypatch.setattr(upstream, "_images_client_timeout_config", None)
    client_lifecycle._pinned_images_clients.clear()
    token = byok_runtime.bind_byok_http_target(target)
    try:
        byok_client = await upstream._get_images_client(pinned_target=target)
        internal_client = await upstream._get_images_client()
    finally:
        byok_runtime.reset_byok_http_target(token)
        client_lifecycle._pinned_images_clients.clear()

    assert byok_client is not internal_client
    assert [entry["pinned_target"] for entry in built] == [target, None]


@pytest.mark.asyncio
async def test_concurrent_client_selection_keeps_byok_targets_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = (
        PublicHttpTarget("https://a.example/v1", ("203.0.113.10",)),
        PublicHttpTarget("https://b.example/v1", ("203.0.113.11",)),
    )
    timeout_config = upstream._TimeoutConfig(connect=3.0, read=40.0, write=5.0)
    built: list[dict[str, Any]] = []

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        await asyncio.sleep(0)
        return timeout_config

    def fake_build_images_client(
        _timeout_config: upstream._TimeoutConfig,
        *,
        proxy_url: str | None = None,
        pinned_target: Any | None = None,
    ) -> object:
        client = object()
        built.append(
            {
                "client": client,
                "proxy_url": proxy_url,
                "pinned_target": pinned_target,
            }
        )
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, "_build_images_client", fake_build_images_client)
    monkeypatch.setattr(upstream, "_images_client", None)
    monkeypatch.setattr(upstream, "_images_client_timeout_config", None)
    client_lifecycle._pinned_images_clients.clear()

    async def select_clients(
        target: PublicHttpTarget,
    ) -> tuple[object, object, PublicHttpTarget | None]:
        token = byok_runtime.bind_byok_http_target(target)
        try:
            pinned_client = await upstream._get_images_client(pinned_target=target)
            unpinned_client = await upstream._get_images_client()
            observed_target = byok_runtime.current_byok_http_target()
        finally:
            byok_runtime.reset_byok_http_target(token)
        return pinned_client, unpinned_client, observed_target

    try:
        results = await asyncio.gather(*(select_clients(target) for target in targets))
    finally:
        client_lifecycle._pinned_images_clients.clear()

    assert results[0][0] is not results[1][0]
    assert results[0][1] is results[1][1]
    assert [result[2] for result in results] == list(targets)
    assert byok_runtime.current_byok_http_target() is None
    assert {entry["pinned_target"] for entry in built if entry["pinned_target"]} == set(
        targets
    )
    assert sum(entry["pinned_target"] is None for entry in built) == 1


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
