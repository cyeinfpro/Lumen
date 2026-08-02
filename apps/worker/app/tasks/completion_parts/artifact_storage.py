"""Completion-owned storage transaction helpers for tool-generated images."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ...storage import storage


async def write_completion_image_files(files: list[tuple[str, bytes]]) -> list[str]:
    created_keys: list[str] = []
    try:
        for key, data in files:
            result = await asyncio.to_thread(storage.put_bytes_result, key, data)
            if result.created:
                created_keys.append(key)
    except BaseException:
        await asyncio.gather(
            *(asyncio.to_thread(storage.delete, key) for key in created_keys),
            return_exceptions=True,
        )
        raise
    return created_keys


async def delete_completion_image_files(keys: list[str]) -> None:
    await asyncio.gather(
        *(asyncio.to_thread(storage.delete, key) for key in dict.fromkeys(keys)),
        return_exceptions=True,
    )


@asynccontextmanager
async def cleanup_completion_image_files_on_error(
    keys: list[str],
) -> AsyncIterator[None]:
    try:
        yield
    except BaseException:
        await delete_completion_image_files(keys)
        raise
