from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from lumen_core.schemas import (
    ApparelModelLibraryItemOut,
    ApparelModelLibrarySyncOut,
    ApparelModelLibrarySyncStateOut,
)

from app.workflows.adapters.operations.apparel import _SQLAlchemyApparelLibraryAdapter
from app.workflows.application.apparel_library import (
    DeleteApparelModelLibraryItems,
    ListApparelModelLibrary,
    SyncApparelModelLibraryPresets,
)
from app.workflows.application.apparel_workflow_rules import (
    approve_product_analysis_state,
    metadata_model_profile_from_prompt,
    resolve_accessory_plan,
)
from app.workflows.application.errors import WorkflowRequestError


class _ApparelPort:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.private_rows = {"user:one"}
        self.items = {
            "preset:one": {"id": "preset:one", "source": "preset"},
        }
        self.library_rows = [
            {"id": "preset:one", "source": "preset"},
            {"id": "user:unused", "source": "user_upload"},
        ]

    async def combined_items(
        self,
        *,
        user_id: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.events.append(f"combined:{user_id}")
        return self.library_rows, True

    def filter_items(
        self,
        items: Any,
        *,
        source: str,
        age_segment: str,
        appearance: str,
        query: str,
    ) -> list[dict[str, Any]]:
        self.events.append(f"filter:{source}:{age_segment}:{appearance}:{query}")
        return list(items)

    async def usage_counts(
        self,
        *,
        user_id: str,
        item_ids: Any,
    ) -> dict[str, int]:
        self.events.append(f"usage:{user_id}:{','.join(item_ids)}")
        return {"preset:one": 2}

    def item_out(self, item: dict[str, Any]) -> Any:
        return ApparelModelLibraryItemOut(
            id=item["id"],
            source=item["source"],
            visibility_scope=(
                "global_preset" if item["source"] == "preset" else "user_private"
            ),
            title=item["id"],
            age_segment="young_adult",
            image_url=f"/images/{item['id']}",
            usage_count=item.get("usage_count", 0),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def sync_state_out(self, user: Any) -> ApparelModelLibrarySyncStateOut:
        return ApparelModelLibrarySyncStateOut(can_sync=user.role == "admin")

    def can_sync(self, user: Any) -> bool:
        return user.role == "admin"

    def github_contents_url(self) -> str:
        return "https://api.github.com/repos/cyeinfpro/Lumen/contents/presets"

    async def resolve_sync_proxy(self) -> str | None:
        self.events.append("resolve-proxy")
        return None

    async def close_request_transaction(self) -> None:
        self.events.append("rollback")

    async def sync_presets(
        self,
        *,
        contents_url: str,
        sync_lock: Any,
        proxy_url: str | None,
    ) -> ApparelModelLibrarySyncOut:
        self.events.append("network-sync")
        assert contents_url.startswith("https://api.github.com/")
        assert sync_lock is not None
        assert proxy_url is None
        return ApparelModelLibrarySyncOut(status="skipped")

    async def ensure_legacy_migrated(self, *, user_id: str) -> None:
        self.events.append(f"migrate:{user_id}")

    def remove_legacy_private_item(self, *, user_id: str, item_id: str) -> bool:
        return False

    async def delete_private_row(self, *, user_id: str, item_id: str) -> bool:
        if item_id not in self.private_rows:
            return False
        self.private_rows.remove(item_id)
        return True

    async def find_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> dict[str, Any] | None:
        return self.items.get(item_id)

    async def hide_preset(self, *, user_id: str, item_id: str) -> None:
        self.events.append(f"hide:{user_id}:{item_id}")

    async def commit(self) -> None:
        self.events.append("commit")


@pytest.mark.asyncio
async def test_apparel_library_use_cases_own_validation_and_io_order() -> None:
    port = _ApparelPort()
    user = SimpleNamespace(id="user-1", role="admin")

    with pytest.raises(WorkflowRequestError) as excinfo:
        await ListApparelModelLibrary(port).execute(
            user=user,
            source="invalid",
        )
    assert excinfo.value.status_code == 422
    assert port.events == []

    result = await ListApparelModelLibrary(port).execute(user=user)
    assert [item.usage_count for item in result.items] == [2, 0]
    assert port.events[-2:] == [
        "usage:user-1:preset:one,user:unused",
        "commit",
    ]

    port.events.clear()
    sync = await SyncApparelModelLibraryPresets(port).execute(
        user=user,
        sync_lock=asyncio.Lock(),
    )
    assert sync.status == "skipped"
    assert port.events == ["resolve-proxy", "rollback", "network-sync"]


@pytest.mark.asyncio
async def test_apparel_usage_counts_are_user_scoped_and_distinct_per_workflow() -> None:
    class _Result:
        def all(self) -> list[Any]:
            return [
                ("run-1", {"library_item_id": "preset:one"}),
                ("run-1", {"library_item_id": "preset:one"}),
                ("run-2", {"library_item_id": "preset:one"}),
                ("run-3", {"library_item_id": "other"}),
            ]

    class _Db:
        def __init__(self) -> None:
            self.statement: Any = None

        async def execute(self, statement: Any) -> _Result:
            self.statement = statement
            return _Result()

    db = _Db()
    adapter = _SQLAlchemyApparelLibraryAdapter(db)  # type: ignore[arg-type]
    counts = await adapter.usage_counts(
        user_id="user-1",
        item_ids=["preset:one", "unused"],
    )

    assert counts == {"preset:one": 2}
    rendered = str(
        db.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "workflow_runs.user_id = 'user-1'" in rendered
    assert "workflow_runs.deleted_at IS NULL" in rendered


@pytest.mark.asyncio
async def test_apparel_library_delete_dedupes_and_distinguishes_item_ownership() -> (
    None
):
    port = _ApparelPort()
    result = await DeleteApparelModelLibraryItems(port).delete_many(
        user_id="user-1",
        item_ids=["user:one", "user:one", "preset:one", "missing"],
    )

    assert result.deleted == 2
    assert result.not_found == ("missing",)
    assert port.events.count("hide:user-1:preset:one") == 1
    assert port.events[-1] == "commit"


def test_apparel_workflow_rules_preserve_business_transitions() -> None:
    profile = metadata_model_profile_from_prompt("亚洲青年女性，自然通勤")
    assert profile == {
        "age_segment": "young_adult",
        "gender": "female",
        "appearance_direction": "asian",
    }
    accessory_plan = resolve_accessory_plan(
        requested=None,
        model_settings_output=None,
        model_settings_input=None,
        product_analysis={"styling_recommendations": "耳环、手提包; 腰带"},
    )
    assert accessory_plan["items"] == ["耳环", "手提包", "腰带"]

    run = SimpleNamespace(
        user_prompt="clean studio",
        metadata_jsonb={},
        current_step="product_analysis",
        status="running",
    )
    product = SimpleNamespace(
        status="needs_review",
        approved_at=None,
        approved_by=None,
        input_json={},
        output_json={"color": "blue"},
    )
    model_settings = SimpleNamespace(
        status="waiting_input",
        approved_at=None,
        approved_by=None,
        input_json={},
        output_json={},
    )
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    approve_product_analysis_state(
        run=run,
        product_step=product,
        model_settings_step=model_settings,
        corrections={"color": "navy"},
        user_id="user-1",
        confirmed_at=now,
        approved_at=now,
    )

    assert product.output_json["color"] == "navy"
    assert product.output_json["confirmed_at"] == now.isoformat()
    assert model_settings.status == "needs_review"
    assert (run.current_step, run.status) == ("model_settings", "needs_review")
