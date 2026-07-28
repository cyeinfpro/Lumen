from __future__ import annotations

import pytest

from lumen_core.generation_resources import (
    ResourceDemand,
    generation_resource_demand,
)

from app.tasks.generation_parts import admission


class PermitRedis:
    def __init__(self) -> None:
        self.strings = {"generation:image_queue:lock": "lock-owner"}
        self.permits: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.user_units: dict[str, int] = {}

    async def eval(
        self,
        script: str,
        _key_count: int,
        *args: str,
    ) -> int:
        if script == admission.RESERVE_WEIGHTED_PERMIT_LUA:
            return self._reserve(*args)
        if script == admission.RELEASE_WEIGHTED_PERMIT_LUA:
            return self._release(*args)
        if script == admission.RENEW_WEIGHTED_PERMIT_LUA:
            return self._renew(*args)
        raise AssertionError("unexpected admission script")

    def _reserve(self, *args: str) -> int:
        (
            _permits_key,
            _expiry_key,
            used_key,
            external_key,
            _users_key,
            lock_key,
            owner,
            now_raw,
            task_id,
            user_id,
            attempt,
            revision,
            total_raw,
            external_raw,
            global_budget_raw,
            external_budget_raw,
            user_budget_raw,
            identity,
            stored_owner,
            expiry_raw,
        ) = args
        if self.strings.get(lock_key) != owner:
            return -1
        now = float(now_raw)
        for expired_task in [
            task for task, expiry in self.expiry.items() if expiry <= now
        ]:
            self._drop(expired_task, used_key, external_key)
        if task_id in self.permits:
            return 0
        total = int(total_raw)
        external = int(external_raw)
        global_used = int(self.strings.get(used_key, "0"))
        external_used = int(self.strings.get(external_key, "0"))
        user_used = self.user_units.get(user_id, 0)
        if (
            global_used + total > int(global_budget_raw)
            or external_used + external > int(external_budget_raw)
            or user_used + total > int(user_budget_raw)
        ):
            return 0
        self.strings[used_key] = str(global_used + total)
        self.strings[external_key] = str(external_used + external)
        self.user_units[user_id] = user_used + total
        self.permits[task_id] = (
            f"{identity}|{total}|{external}|{user_id}|{stored_owner}"
        )
        self.expiry[task_id] = float(expiry_raw)
        assert self.permits[task_id].startswith(f"{attempt}|{revision}|")
        return 1

    def _release(self, *args: str) -> int:
        (
            _permits_key,
            _expiry_key,
            used_key,
            external_key,
            _users_key,
            task_id,
            attempt,
            revision,
        ) = args
        payload = self.permits.get(task_id)
        if payload is None or not payload.startswith(f"{attempt}|{revision}|"):
            return 0
        self._drop(task_id, used_key, external_key)
        return 1

    def _renew(self, *args: str) -> int:
        (
            _permits_key,
            _expiry_key,
            task_id,
            attempt,
            revision,
            expiry,
        ) = args
        payload = self.permits.get(task_id)
        if payload is None or not payload.startswith(f"{attempt}|{revision}|"):
            return 0
        self.expiry[task_id] = float(expiry)
        return 1

    def _drop(self, task_id: str, used_key: str, external_key: str) -> None:
        payload = self.permits.pop(task_id)
        _attempt, _revision, total, external, user_id, _owner = payload.split(
            "|",
            5,
        )
        self.strings[used_key] = str(
            max(0, int(self.strings.get(used_key, "0")) - int(total))
        )
        self.strings[external_key] = str(
            max(0, int(self.strings.get(external_key, "0")) - int(external))
        )
        remaining = self.user_units.get(user_id, 0) - int(total)
        if remaining > 0:
            self.user_units[user_id] = remaining
        else:
            self.user_units.pop(user_id, None)
        self.expiry.pop(task_id, None)


def permit(
    task_id: str,
    *,
    attempt: int = 1,
    revision: int = 1,
    user_id: str = "user-a",
    demand: ResourceDemand | None = None,
) -> admission.WeightedPermit:
    return admission.WeightedPermit(
        task_id=task_id,
        attempt=attempt,
        revision=revision,
        demand=demand
        or ResourceDemand(
            pixel_units=1,
            reference_units=0,
            postprocess_units=0,
            external_lane_units=1,
            output_units=1,
        ),
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_weighted_permit_enforces_global_external_and_user_budgets() -> None:
    redis = PermitRedis()
    dual = permit(
        "dual",
        demand=ResourceDemand(1, 0, 0, 2, 1),
    )
    large = permit(
        "large",
        user_id="user-b",
        demand=ResourceDemand(4, 0, 0, 1, 1),
    )

    assert await admission.reserve_weighted_permit(
        redis,
        permit=dual,
        owner="lock-owner",
        now=10,
        expiry=70,
        global_budget=8,
        external_budget=2,
        user_budget=6,
        lock_key="generation:image_queue:lock",
    )
    assert not await admission.reserve_weighted_permit(
        redis,
        permit=large,
        owner="lock-owner",
        now=10,
        expiry=70,
        global_budget=8,
        external_budget=2,
        user_budget=6,
        lock_key="generation:image_queue:lock",
    )
    assert int(redis.strings[admission.RESOURCE_USED_KEY]) == dual.demand.total
    assert int(redis.strings[admission.RESOURCE_EXTERNAL_USED_KEY]) == 2


@pytest.mark.asyncio
async def test_release_is_revision_fenced_and_idempotent() -> None:
    redis = PermitRedis()
    current = permit("gen", attempt=2, revision=4)
    assert await admission.reserve_weighted_permit(
        redis,
        permit=current,
        owner="lock-owner",
        now=10,
        expiry=70,
        global_budget=14,
        external_budget=4,
        user_budget=10,
        lock_key="generation:image_queue:lock",
    )

    assert not await admission.release_weighted_permit(
        redis,
        permit=permit("gen", attempt=2, revision=3),
    )
    assert await admission.release_weighted_permit(redis, permit=current)
    assert not await admission.release_weighted_permit(redis, permit=current)
    assert int(redis.strings[admission.RESOURCE_USED_KEY]) == 0
    assert redis.user_units == {}


@pytest.mark.asyncio
async def test_expired_permit_is_reclaimed_on_next_admission() -> None:
    redis = PermitRedis()
    expired = permit("expired", demand=ResourceDemand(4, 0, 0, 1, 1))
    replacement = permit("replacement", user_id="user-b")
    assert await admission.reserve_weighted_permit(
        redis,
        permit=expired,
        owner="lock-owner",
        now=10,
        expiry=20,
        global_budget=6,
        external_budget=2,
        user_budget=6,
        lock_key="generation:image_queue:lock",
    )
    assert await admission.reserve_weighted_permit(
        redis,
        permit=replacement,
        owner="lock-owner",
        now=21,
        expiry=80,
        global_budget=6,
        external_budget=2,
        user_budget=6,
        lock_key="generation:image_queue:lock",
    )
    assert set(redis.permits) == {"replacement"}


@pytest.mark.asyncio
async def test_renew_rejects_superseded_revision() -> None:
    redis = PermitRedis()
    current = permit("renew", revision=2)
    assert await admission.reserve_weighted_permit(
        redis,
        permit=current,
        owner="lock-owner",
        now=10,
        expiry=20,
        global_budget=14,
        external_budget=4,
        user_budget=10,
        lock_key="generation:image_queue:lock",
    )

    assert not await admission.renew_weighted_permit(
        redis,
        permit=permit("renew", revision=1),
        expiry=90,
    )
    assert await admission.renew_weighted_permit(
        redis,
        permit=current,
        expiry=90,
    )
    assert redis.expiry["renew"] == 90


def test_resource_demand_accounts_for_4k_references_edit_and_dual_race() -> None:
    demand = generation_resource_demand(
        pixel_count=3840 * 2160,
        reference_count=3,
        action="edit",
        has_mask=True,
        transparent=True,
        output_count=2,
        dual_race=True,
    )

    assert demand == ResourceDemand(
        pixel_units=4,
        reference_units=3,
        postprocess_units=2,
        external_lane_units=2,
        output_units=2,
    )
    assert demand.total == 13
