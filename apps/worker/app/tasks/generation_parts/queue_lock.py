from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from redis.exceptions import WatchError

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

RENEW_IMAGE_QUEUE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return -1
end
return redis.call('DEL', KEYS[1])
"""

SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return -1
end
return redis.call('SET', KEYS[1], ARGV[2], 'PX', tonumber(ARGV[3]))
"""

FALLBACK_RETRIES = 3


def _unavailable(message: str) -> Any:
    return _g.UpstreamError(
        message,
        error_code=_g.EC.LOCAL_QUEUE_FULL.value,
        status_code=None,
        payload={"retry_after": _g._IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S},
    )


def _ttl_seconds() -> float:
    try:
        return max(0.001, float(_g._IMAGE_QUEUE_LOCK_TTL_S))
    except (TypeError, ValueError):
        return 1.0


def _ttl_ms() -> int:
    return max(1, int(round(_ttl_seconds() * 1000)))


def _renew_interval() -> float:
    return max(0.01, _ttl_seconds() / 3.0)


async def _reset(pipe: Any) -> None:
    reset = getattr(pipe, "reset", None)
    if not callable(reset):
        return
    with suppress(Exception):
        result = reset()
        if inspect.isawaitable(result):
            await result


async def _renew_watch(redis: Any, token: str) -> bool:
    pipeline = getattr(redis, "pipeline", None)
    if not callable(pipeline):
        raise _unavailable(
            "image queue lock heartbeat requires Redis EVAL or WATCH transaction"
        )
    for attempt in range(FALLBACK_RETRIES):
        pipe: Any | None = None
        try:
            pipe = pipeline(transaction=True)
            await pipe.watch(_g._IMAGE_QUEUE_LOCK_KEY)
            if _g._redis_text(await pipe.get(_g._IMAGE_QUEUE_LOCK_KEY)) != token:
                return False
            pipe.multi()
            pexpire = getattr(pipe, "pexpire", None)
            if callable(pexpire):
                pexpire(_g._IMAGE_QUEUE_LOCK_KEY, _ttl_ms())
            else:
                expire = getattr(pipe, "expire", None)
                if not callable(expire):
                    raise _unavailable(
                        "image queue lock heartbeat WATCH fallback lacks EXPIRE"
                    )
                expire(_g._IMAGE_QUEUE_LOCK_KEY, max(1, int(_ttl_seconds())))
            results = await pipe.execute()
            return bool(results and int(results[0] or 0))
        except WatchError as exc:
            if attempt + 1 >= FALLBACK_RETRIES:
                raise _unavailable(
                    "image queue lock heartbeat conflicted repeatedly"
                ) from exc
        finally:
            if pipe is not None:
                await _reset(pipe)
    return False


async def _release_watch(redis: Any, token: str) -> bool:
    pipeline = getattr(redis, "pipeline", None)
    if not callable(pipeline):
        raise _unavailable(
            "image queue lock release requires Redis EVAL or WATCH transaction"
        )
    for attempt in range(FALLBACK_RETRIES):
        pipe: Any | None = None
        try:
            pipe = pipeline(transaction=True)
            await pipe.watch(_g._IMAGE_QUEUE_LOCK_KEY)
            if _g._redis_text(await pipe.get(_g._IMAGE_QUEUE_LOCK_KEY)) != token:
                return False
            pipe.multi()
            pipe.delete(_g._IMAGE_QUEUE_LOCK_KEY)
            results = await pipe.execute()
            if not results or int(results[0] or 0) != 1:
                raise RuntimeError("image queue lock transaction did not delete owner")
            return True
        except WatchError as exc:
            if attempt + 1 >= FALLBACK_RETRIES:
                raise _unavailable(
                    "image queue lock release transaction conflicted repeatedly"
                ) from exc
        except _g.UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(
                "image queue lock release transaction unavailable"
            ) from exc
        finally:
            if pipe is not None:
                await _reset(pipe)
    raise _unavailable("image queue lock release transaction unavailable")


async def _renew(redis: Any, token: str) -> bool:
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        result = await eval_fn(
            RENEW_IMAGE_QUEUE_LOCK_LUA,
            1,
            _g._IMAGE_QUEUE_LOCK_KEY,
            token,
            _ttl_ms(),
        )
        return int(result or 0) == 1
    return await _renew_watch(redis, token)


class ImageQueueLockLost(BaseException):
    """The image queue lock expired, changed owner, or could not be renewed."""


class ImageQueueLockLease:
    def __init__(self, redis: Any, token: str) -> None:
        self.redis = redis
        self.token = token
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()
        self._stopping = False
        self._closed = False

    @property
    def lost(self) -> asyncio.Event:
        return self._lost

    def start(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    def _mark_lost(self, reason: str) -> None:
        if self._lost.is_set():
            return
        self._lost.set()
        _g.logger.warning(
            "image queue lock lost token=%s reason=%s", self.token, reason
        )

    def _raise_lost(self, reason: str) -> None:
        self._mark_lost(reason)
        raise ImageQueueLockLost(reason)

    def require_atomic_writes(self) -> None:
        if not callable(getattr(self.redis, "eval", None)):
            raise _unavailable(
                "image queue reservation requires Redis EVAL; "
                "WATCH fallback cannot fence reservation writes"
            )

    async def assert_owner(self) -> None:
        if self._lost.is_set():
            raise ImageQueueLockLost("image queue lock owner already lost")
        try:
            renewed = await _renew(self.redis, self.token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._raise_lost(f"heartbeat failed: {exc}")
        if not renewed:
            self._raise_lost("lock owner changed or TTL expired")

    async def eval_fenced(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
        lost_result: int | None = None,
        lose_on_error: bool = True,
    ) -> Any:
        await self.assert_owner()
        eval_fn = getattr(self.redis, "eval", None)
        if not callable(eval_fn):
            raise _unavailable("image queue fenced write requires Redis EVAL")
        try:
            result = await eval_fn(script, numkeys, *keys_and_args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if lose_on_error:
                self._raise_lost(f"fenced write failed: {exc}")
            raise
        if lost_result is not None:
            try:
                lost = int(result or 0) == lost_result
            except (TypeError, ValueError):
                lost = False
            if lost:
                self._raise_lost("fenced write observed a different owner")
        return result

    async def delete_if_owner(self, key: str) -> bool:
        result = await self.eval_fenced(
            DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA,
            2,
            key,
            _g._IMAGE_QUEUE_LOCK_KEY,
            self.token,
            lost_result=-1,
        )
        return int(result or 0) == 1

    async def set_if_owner(self, key: str, value: str, ttl_seconds: float) -> bool:
        result = await self.eval_fenced(
            SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA,
            2,
            key,
            _g._IMAGE_QUEUE_LOCK_KEY,
            self.token,
            value,
            max(1, int(round(float(ttl_seconds) * 1000))),
            lost_result=-1,
        )
        return bool(result)

    async def _heartbeat(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(_renew_interval())
                if self._stopping:
                    return
                try:
                    renewed = await _renew(self.redis, self.token)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._mark_lost(f"heartbeat failed: {exc}")
                    return
                if not renewed:
                    self._mark_lost("lock owner changed or TTL expired")
                    return
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stopping = True
        heartbeat = self._heartbeat_task
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
        try:
            eval_fn = getattr(self.redis, "eval", None)
            released = (
                await eval_fn(
                    _g._RELEASE_LEASE_LUA,
                    1,
                    _g._IMAGE_QUEUE_LOCK_KEY,
                    self.token,
                )
                if callable(eval_fn)
                else await _release_watch(self.redis, self.token)
            )
            if int(released or 0) != 1:
                _g.logger.info("image queue lock release skipped after owner changed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _g.logger.error(
                "image queue lock owner-CAS release failed; "
                "preserving critical-section result and relying on lock TTL",
                exc_info=True,
            )


@asynccontextmanager
async def image_queue_lock(redis: Any) -> AsyncIterator[ImageQueueLockLease]:
    eval_fn = getattr(redis, "eval", None)
    if not callable(eval_fn) and not callable(getattr(redis, "pipeline", None)):
        _g.logger.error(
            "image queue lock acquisition refused without atomic release support"
        )
        raise _unavailable("image queue lock requires Redis EVAL or WATCH transaction")

    token = _g.new_uuid7()
    deadline = asyncio.get_event_loop().time() + _g._IMAGE_QUEUE_LOCK_WAIT_S
    while True:
        ttl = _ttl_seconds()
        kwargs: dict[str, Any] = {"nx": True}
        kwargs["px" if ttl < 1.0 else "ex"] = (
            _ttl_ms() if ttl < 1.0 else max(1, int(ttl))
        )
        try:
            got = await redis.set(_g._IMAGE_QUEUE_LOCK_KEY, token, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _g.logger.error("image queue lock acquisition failed", exc_info=True)
            raise _unavailable("image queue lock acquisition unavailable") from exc
        if got:
            break
        if asyncio.get_event_loop().time() >= deadline:
            raise _g.UpstreamError(
                "image queue scheduler busy",
                error_code=_g.EC.LOCAL_QUEUE_FULL.value,
                status_code=None,
            )
        await asyncio.sleep(0.05)

    lease = ImageQueueLockLease(redis, token)
    try:
        lease.start()
        try:
            yield lease
            if lease.lost.is_set():
                raise ImageQueueLockLost("image queue lock owner lost before exit")
        except ImageQueueLockLost as exc:
            raise _unavailable("image queue lock ownership lost") from exc
    finally:
        await lease.close()
