"""B18/B19: native client deadlines and cleanup, with no paid upstream calls."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx

from app.agent_runtime_client import (
    AgentRuntimeClient,
    AgentRuntimeClientError,
    _next_stream_chunk,
    _open_runtime_stream,
)


class AgentRuntimeAuditRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_cancel_reaps_chunk_reader(self) -> None:
        started = asyncio.Event()
        closed = asyncio.Event()

        async def source():
            try:
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

        before = asyncio.all_tasks()
        task = asyncio.create_task(
            _next_stream_chunk(
                source().__aiter__(),
                cancel_requested=asyncio.Event(),
                timeout_seconds=5,
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertFalse([t for t in asyncio.all_tasks() - before if not t.done()])

    async def test_idle_timeout_reaps_reader(self) -> None:
        closed = asyncio.Event()

        async def source():
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

        before = asyncio.all_tasks()
        with self.assertRaises(AgentRuntimeClientError) as error:
            await _next_stream_chunk(
                source().__aiter__(),
                cancel_requested=asyncio.Event(),
                timeout_seconds=0.02,
            )
        self.assertEqual(error.exception.code, "agent_runtime_event_timeout")
        self.assertTrue(closed.is_set())
        self.assertFalse([t for t in asyncio.all_tasks() - before if not t.done()])

    async def test_normal_chunk_and_eof(self) -> None:
        async def source():
            yield b"hello"

        iterator = source().__aiter__()
        self.assertEqual(
            await _next_stream_chunk(iterator, cancel_requested=None, timeout_seconds=1),
            b"hello",
        )
        with self.assertRaises(StopAsyncIteration):
            await _next_stream_chunk(iterator, cancel_requested=None, timeout_seconds=1)

    async def test_header_cancel_race_closes_acquired_response(self) -> None:
        cancel = asyncio.Event()

        class Context:
            closed = False

            async def __aenter__(self):
                cancel.set()
                return httpx.Response(200)

            async def __aexit__(self, *_args: Any) -> None:
                self.closed = True

        context = Context()
        with self.assertRaises(AgentRuntimeClientError) as error:
            async with _open_runtime_stream(
                context, cancel_requested=cancel, timeout_seconds=1
            ):
                self.fail("Cancelled response must not be yielded")
        self.assertEqual(error.exception.code, "agent_cancelled")
        self.assertEqual(error.exception.delivery, "unknown")
        self.assertTrue(context.closed)

    async def test_pre_cancel_does_not_open_request(self) -> None:
        cancel = asyncio.Event()
        cancel.set()

        class Context:
            async def __aenter__(self):
                raise AssertionError("Must not send a pre-cancelled request")

        with self.assertRaises(AgentRuntimeClientError) as error:
            async with _open_runtime_stream(
                Context(), cancel_requested=cancel, timeout_seconds=1
            ):
                self.fail("Must not enter a pre-cancelled request")
        self.assertEqual(error.exception.delivery, "proven_absent")

    async def test_header_timeout_stops_opening_without_retry(self) -> None:
        calls = 0
        released = asyncio.Event()

        class Context:
            async def __aenter__(self):
                nonlocal calls
                calls += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    released.set()

            async def __aexit__(self, *_args: Any) -> None:
                raise AssertionError("An unacquired context must not be exited")

        with self.assertRaises(AgentRuntimeClientError) as error:
            async with _open_runtime_stream(
                Context(), cancel_requested=asyncio.Event(), timeout_seconds=0.02
            ):
                self.fail("Must not yield without response headers")
        self.assertEqual(error.exception.code, "agent_runtime_header_timeout")
        self.assertEqual(error.exception.delivery, "unknown")
        self.assertEqual(calls, 1)
        self.assertTrue(released.is_set())

    async def test_health_request_has_finite_total_deadline(self) -> None:
        async def handler(_request):
            await asyncio.Event().wait()

        client = AgentRuntimeClient(
            "http://runtime.test", "test-secret-" * 4, health_timeout_seconds=0.02
        )
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=client.base_url
        )
        try:
            with self.assertRaises(AgentRuntimeClientError) as error:
                await asyncio.wait_for(client.verify_contract(), 1)
            self.assertEqual(error.exception.code, "agent_runtime_health_timeout")
            self.assertEqual(error.exception.delivery, "proven_absent")
        finally:
            await client.close()
