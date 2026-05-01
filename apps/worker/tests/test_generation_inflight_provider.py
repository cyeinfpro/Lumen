"""单测：worker 写入 image_inflight 快照（admin 请求事件面板的 in-flight 数据源）。"""

from __future__ import annotations

import pytest

from app.tasks import generation


def test_classify_inflight_lane_image2_routes() -> None:
    assert generation._classify_inflight_lane("image2", "images/generations") == "lane_a"
    assert generation._classify_inflight_lane("image2_direct", None) == "lane_a"


def test_classify_inflight_lane_responses_routes() -> None:
    assert generation._classify_inflight_lane("responses", None) == "lane_b"
    assert generation._classify_inflight_lane("responses_fallback", None) == "lane_b"


def test_classify_inflight_lane_image_jobs_endpoint_split() -> None:
    assert (
        generation._classify_inflight_lane("image_jobs", "image-jobs:generations")
        == "lane_a"
    )
    assert (
        generation._classify_inflight_lane("image_jobs", "image-jobs:responses")
        == "lane_b"
    )
    # 没 endpoint 信息时退化到 lane_a，不让事件丢
    assert generation._classify_inflight_lane("image_jobs", None) == "lane_a"


def test_classify_inflight_lane_unknown_route_falls_back_to_lane_a() -> None:
    assert generation._classify_inflight_lane(None, None) == "lane_a"
    assert generation._classify_inflight_lane("", "") == "lane_a"
    assert generation._classify_inflight_lane("weird", None) == "lane_a"


class _HashRedis:
    """最小 Redis 模拟，只覆盖 inflight 用到的 hset/hgetall/expire/delete。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}
        self.deletes: list[str] = []

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        bucket = self.hashes.setdefault(key, {})
        for k, v in mapping.items():
            bucket[k] = v
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        self.deletes.append(key)
        existed = key in self.hashes
        self.hashes.pop(key, None)
        return 1 if existed else 0


@pytest.mark.asyncio
async def test_inflight_set_fields_writes_hash_with_ttl() -> None:
    redis = _HashRedis()
    await generation._inflight_set_fields(
        redis, "task-1", {"mode": "single", "provider": "alpha"}
    )
    snap = await redis.hgetall(generation._image_inflight_key("task-1"))
    assert snap["mode"] == "single"
    assert snap["provider"] == "alpha"
    # updated_at 是字符串的整数毫秒
    assert snap["updated_at"].isdigit()
    assert redis.expires[generation._image_inflight_key("task-1")] >= 60


@pytest.mark.asyncio
async def test_inflight_set_fields_skips_empty_payload() -> None:
    redis = _HashRedis()
    await generation._inflight_set_fields(redis, "task-1", {})
    await generation._inflight_set_fields(redis, "task-1", {"x": "", "y": None})
    assert redis.hashes == {}


@pytest.mark.asyncio
async def test_inflight_set_fields_dual_race_lane_round_trip() -> None:
    """worker 写两条 lane → admin 端 _build_live_lanes_from_snapshot 能还原。"""
    redis = _HashRedis()
    await generation._inflight_set_fields(
        redis,
        "task-1",
        {"mode": "dual_race", "lane_a_provider": "alpha", "lane_b_provider": "beta"},
    )
    raw = await redis.hgetall(generation._image_inflight_key("task-1"))
    # 不直接 import admin（避免引入 api 依赖到 worker 测试），手工 assert 字段。
    assert raw["mode"] == "dual_race"
    assert raw["lane_a_provider"] == "alpha"
    assert raw["lane_b_provider"] == "beta"


@pytest.mark.asyncio
async def test_inflight_clear_deletes_key() -> None:
    redis = _HashRedis()
    await generation._inflight_set_fields(redis, "task-1", {"provider": "alpha"})
    await generation._inflight_clear(redis, "task-1")
    assert generation._image_inflight_key("task-1") in redis.deletes
    assert redis.hashes == {}
