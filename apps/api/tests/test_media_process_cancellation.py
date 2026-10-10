import asyncio
import os
import subprocess
import sys
import threading
import time
import pytest
from lumen_core.capacity_leases import CapacityLeaseLost
from app.media_process import cancellable_media_thread, run_media_process
from app.services.poster_styles.capacity import RedisCapacityLease


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["false", "error", "hang"])
async def test_lease_loss_reaps_real_media_process_before_slot_release(
    monkeypatch, mode
):
    processes, released = [], []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("app.media_process.subprocess.Popen", popen)

    class Redis:
        renewals = 0

        async def set(self, *_args, **_kwargs):
            return True

        async def eval(self, script, _count, *_args):
            if "DEL" in script:
                assert processes and processes[0].poll() is not None
                released.append(True)
                return 1
            self.renewals += 1
            if self.renewals == 1:
                return 1
            if mode == "error":
                raise ConnectionError("isolated simulated transport failure")
            if mode == "hang":
                await asyncio.Event().wait()
            return 0

    redis = Redis()
    capacity = RedisCapacityLease(redis, limit=1, ttl_seconds=1)

    async def work():
        async with capacity.hold():
            await cancellable_media_thread(
                run_media_process,
                [sys.executable, "-c", "import time; time.sleep(20)"],
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(work(), timeout=3)
    assert time.monotonic() - started < 1.2
    assert len(processes) == 1 and released
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)
    # A subsequent slot may be acquired only after the old OS child is reaped.
    assert await capacity.try_acquire(owner_token="next-owner") is not None


@pytest.mark.asyncio
async def test_external_cancellation_reaps_process():
    started = threading.Event()

    def work(*, cancel_event):
        started.set()
        return run_media_process(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            cancel_event=cancel_event,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    task = asyncio.create_task(cancellable_media_thread(work))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)


def test_pre_cancel_does_not_start_process(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(
        "app.media_process.subprocess.Popen",
        lambda *_a, **_k: pytest.fail("process started after cancellation"),
    )
    with pytest.raises(CapacityLeaseLost):
        run_media_process(["unused"], cancel_event=cancelled, timeout=1)
