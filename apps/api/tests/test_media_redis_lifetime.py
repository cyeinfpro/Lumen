"""Opt-in real Redis expiry/reassignment and operating-system child fencing."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest
from redis.asyncio import Redis

from app.media_process import cancellable_media_thread, run_media_process
from app.services.poster_styles.capacity import RedisCapacityLease

pytestmark = pytest.mark.skipif(
    os.getenv("LUMEN_LOCAL_REDIS_TEST") != "1",
    reason="explicit disposable local Redis test only",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "hang", "false"])
async def test_real_redis_expiry_never_overlaps_old_media_child(monkeypatch, mode):
    children = []
    real_popen = subprocess.Popen
    with tempfile.TemporaryDirectory(prefix="lumen-lease-") as directory:
        socket = str(Path(directory) / "redis.sock")
        server = real_popen(
            [
                "/opt/homebrew/bin/redis-server",
                "--port",
                "0",
                "--unixsocket",
                socket,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=socket)
        try:
            for _ in range(100):
                try:
                    await client.ping()
                    break
                except (ConnectionError, OSError):
                    await asyncio.sleep(0.02)
                except Exception as exc:
                    if type(exc).__name__ != "ConnectionError":
                        raise
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("disposable Redis did not start")

            class BrokenConnection:
                renewals = 0

                async def set(self, *args, **kwargs):
                    return await client.set(*args, **kwargs)

                async def eval(self, script, count, *args):
                    if "DEL" in script:
                        # Model a disconnected old owner: key must actually expire.
                        return 0
                    self.renewals += 1
                    if self.renewals == 1:
                        return await client.eval(script, count, *args)
                    if mode == "error":
                        raise ConnectionError("isolated old-owner connection failure")
                    if mode == "hang":
                        await asyncio.Event().wait()
                    return 0

            def popen(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child

            monkeypatch.setattr("app.media_process.subprocess.Popen", popen)
            capacity = RedisCapacityLease(
                BrokenConnection(), limit=1, ttl_seconds=1, key_prefix="isolated-test"
            )

            async def old_owner():
                async with capacity.hold():
                    await cancellable_media_thread(
                        run_media_process,
                        [
                            sys.executable,
                            "-c",
                            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)",
                        ],
                        timeout=25,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

            started = time.monotonic()
            task = asyncio.create_task(old_owner())
            while not children:
                await asyncio.sleep(0.005)
            replacement = RedisCapacityLease(
                client, limit=1, ttl_seconds=1, key_prefix="isolated-test"
            )
            assert await replacement.try_acquire(owner_token="new-owner") is None
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert children[0].poll() is not None
            with pytest.raises(ProcessLookupError):
                os.kill(children[0].pid, 0)
            # Real Redis key remains occupied after old child has been reaped.
            assert await client.get("isolated-test:0") is not None
            lease = None
            for _ in range(100):
                lease = await replacement.try_acquire(owner_token="new-owner")
                if lease:
                    break
                await asyncio.sleep(0.02)
            assert lease is not None
            assert time.monotonic() - started >= 0.9
            assert children[0].returncode is not None
            assert await client.get("isolated-test:0") == b"new-owner"
            await lease.release()
            print(
                "REDIS_TTL_REASSIGN_REAPED", mode, round(time.monotonic() - started, 3)
            )
        finally:
            await client.aclose()
            server.terminate()
            server.wait(timeout=3)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
