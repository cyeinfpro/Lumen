"""Bounded media subprocesses that stop before their capacity hold exits."""

import asyncio
import subprocess
import threading
import time
from lumen_core.capacity_leases import CapacityLeaseLost


def stop_media_process(process, *, force=False):
    if process.poll() is not None:
        return
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)


def run_media_process(command, *, cancel_event=None, timeout, check=False, **kwargs):
    if cancel_event is None:
        return subprocess.run(command, timeout=timeout, check=check, **kwargs)
    if cancel_event.is_set():
        raise CapacityLeaseLost("media operation was cancelled")
    process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event.is_set():
                raise CapacityLeaseLost("media operation lost its capacity lease")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        if check:
            result.check_returncode()
        return result
    finally:
        # Lease loss cannot spend a grace period past the confirmed TTL.
        stop_media_process(process, force=cancel_event.is_set())


async def cancellable_media_thread(function, *args, **kwargs):
    cancel_event = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(function, *args, cancel_event=cancel_event, **kwargs)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel_event.set()
        # Never release the distributed hold while the OS process still runs.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
