from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import subprocess
import sys

import pytest
from redis.asyncio import Redis


REDIS_URL = os.environ.get("LUMEN_TEST_REDIS_URL")
ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = ROOT / "apps" / "worker"
_OWNED_LOCK_CHILD = """
import asyncio
import sys

sys.path.insert(0, sys.argv[1])

from redis.asyncio import Redis
from app.locks.owned_redis import owned_redis_lock


async def main() -> None:
    redis = Redis.from_url(sys.argv[2], decode_responses=True)
    try:
        async with owned_redis_lock(
            redis,
            key=sys.argv[3],
            ttl_s=10,
        ) as acquired:
            print("1" if acquired else "0", flush=True)
            if acquired:
                await asyncio.sleep(float(sys.argv[4]))
    finally:
        await redis.aclose()


asyncio.run(main())
"""

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not REDIS_URL,
        reason="LUMEN_TEST_REDIS_URL is required for real Redis contracts",
    ),
]


@pytest.mark.asyncio
async def test_real_redis_owned_lease_time_ttl_and_stream_contracts() -> None:
    assert REDIS_URL is not None
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    namespace = f"lumen:test:redis:{secrets.token_hex(8)}"
    lease_key = f"{namespace}:lease"
    stream_key = f"{namespace}:events"
    owner = secrets.token_hex(16)
    other_owner = secrets.token_hex(16)
    renew_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    end
    return 0
    """
    release_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    try:
        assert await redis.set(lease_key, owner, nx=True, ex=5) is True
        assert await redis.set(lease_key, other_owner, nx=True, ex=5) is None
        assert await redis.eval(renew_script, 1, lease_key, other_owner, 5) == 0
        assert await redis.eval(renew_script, 1, lease_key, owner, 5) == 1
        assert 1 <= await redis.ttl(lease_key) <= 5

        server_time = await redis.time()
        assert len(server_time) == 2
        assert int(server_time[0]) > 0

        stream_id = await redis.xadd(stream_key, {"event": "ready"})
        rows = await redis.xrange(stream_key, min=stream_id, max=stream_id)
        assert rows == [(stream_id, {"event": "ready"})]

        assert await redis.eval(release_script, 1, lease_key, other_owner) == 0
        assert await redis.get(lease_key) == owner
        assert await redis.eval(release_script, 1, lease_key, owner) == 1
        assert await redis.get(lease_key) is None
    finally:
        await redis.delete(lease_key, stream_key)
        await redis.aclose()


@pytest.mark.asyncio
async def test_production_owned_lock_contends_across_processes() -> None:
    assert REDIS_URL is not None
    key = f"lock:lumen:test:multiprocess:{secrets.token_hex(8)}"

    def command(hold_seconds: float) -> list[str]:
        return [
            sys.executable,
            "-c",
            _OWNED_LOCK_CHILD,
            str(WORKER_ROOT),
            REDIS_URL,
            key,
            str(hold_seconds),
        ]

    holder = subprocess.Popen(
        command(5.0),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        acquired = await asyncio.to_thread(holder.stdout.readline)
        if acquired.strip() != "1":
            assert holder.stderr is not None
            stderr = await asyncio.to_thread(holder.stderr.read)
            raise AssertionError(f"holder failed to acquire lock: {stderr}")

        contender = await asyncio.to_thread(
            subprocess.run,
            command(0),
            check=False,
            capture_output=True,
            text=True,
        )
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip() == "0"
        assert await asyncio.to_thread(holder.wait) == 0

        successor = await asyncio.to_thread(
            subprocess.run,
            command(0),
            check=False,
            capture_output=True,
            text=True,
        )
        assert successor.returncode == 0, successor.stderr
        assert successor.stdout.strip() == "1"
    finally:
        if holder.poll() is None:
            holder.terminate()
            await asyncio.to_thread(holder.wait)
        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            await redis.delete(key)
        finally:
            await redis.aclose()
