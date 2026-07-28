from __future__ import annotations

import asyncio
import os
import queue
import time


STAGE_WRITER_QUEUE_BYTES = 2 * 1024 * 1024
_STAGE_WRITER_STOP = object()


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while staging artifact")
        view = view[written:]


class StageFileWriter:
    def __init__(
        self,
        fd: int,
        *,
        queue_bytes: int = STAGE_WRITER_QUEUE_BYTES,
    ) -> None:
        self._fd = fd
        self._queue_bytes = max(1, queue_bytes)
        self._queue: queue.Queue[bytes | object] = queue.Queue()
        self._queued_bytes = 0
        self._space_available = asyncio.Event()
        self._space_available.set()
        self._closed = False
        self.queue_wait_seconds = 0.0
        self.started_at = time.monotonic()
        self.duration_seconds = 0.0
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.ensure_future(asyncio.to_thread(self._run_sync))
        self._task.add_done_callback(lambda _task: self._space_available.set())

    def _chunk_consumed(self, size: int) -> None:
        self._queued_bytes = max(0, self._queued_bytes - size)
        self._space_available.set()

    def _run_sync(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STAGE_WRITER_STOP:
                    break
                assert isinstance(item, bytes)
                try:
                    write_all(self._fd, item)
                finally:
                    self._loop.call_soon_threadsafe(
                        self._chunk_consumed,
                        len(item),
                    )
            os.fsync(self._fd)
        finally:
            os.close(self._fd)

    async def write(self, chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("staged artifact writer is closed")
        while (
            self._queued_bytes > 0
            and self._queued_bytes + len(chunk) > self._queue_bytes
        ):
            if self._task.done():
                await self._task
            self._space_available.clear()
            started = self._loop.time()
            await self._space_available.wait()
            self.queue_wait_seconds += self._loop.time() - started
        if self._task.done():
            await self._task
        self._queued_bytes += len(chunk)
        self._queue.put_nowait(chunk)

    async def finish(self) -> None:
        if self._closed:
            await self._task
            return
        self._closed = True
        self._queue.put_nowait(_STAGE_WRITER_STOP)
        await self._task
        self.duration_seconds = time.monotonic() - self.started_at

    async def abort(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_STAGE_WRITER_STOP)
        await asyncio.gather(self._task, return_exceptions=True)
