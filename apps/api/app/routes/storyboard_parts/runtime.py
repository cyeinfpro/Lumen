"""Composition contract shared by storyboard route services."""

from __future__ import annotations

from datetime import datetime
from types import ModuleType
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Conversation,
    User,
    WorkflowRun,
    WorkflowStep,
)

from ...services.storyboard.contracts import (
    StoryboardImageTask,
    StoryboardRunListItemOut,
    StoryboardRunOut,
)


class StoryboardRuntime(Protocol):
    """Route-owned dependencies used by storyboard application services."""

    def now(self) -> datetime: ...

    def new_id(self) -> str: ...

    async def get_run(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        run_id: str,
        lock: bool = False,
    ) -> WorkflowRun: ...

    async def load_steps(
        self,
        db: AsyncSession,
        run_id: str,
        *,
        lock: bool = False,
    ) -> list[WorkflowStep]: ...

    async def get_step(
        self,
        db: AsyncSession,
        run: WorkflowRun,
        step_id: str,
        *,
        kind: Literal["asset", "shot"] | None = None,
        lock: bool = False,
    ) -> WorkflowStep: ...

    async def assembly_step(
        self,
        db: AsyncSession,
        run: WorkflowRun,
        *,
        lock: bool = False,
    ) -> WorkflowStep: ...

    async def get_or_create_conversation(
        self,
        db: AsyncSession,
        *,
        user: User,
        run: WorkflowRun,
    ) -> Conversation: ...

    async def sync_outputs(self, db: AsyncSession, run: WorkflowRun) -> None: ...

    async def build_run_out(
        self,
        db: AsyncSession,
        run: WorkflowRun,
    ) -> StoryboardRunOut: ...

    async def list_item_out(
        self,
        db: AsyncSession,
        run: WorkflowRun,
    ) -> StoryboardRunListItemOut: ...

    async def publish_event(
        self,
        user_id: str,
        run_id: str,
        event_name: str,
        data: dict[str, object],
    ) -> None: ...

    async def publish_image_task(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        task: StoryboardImageTask,
    ) -> None: ...

    async def publish_image_tasks(
        self,
        *,
        db: AsyncSession,
        user_id: str,
        tasks: list[StoryboardImageTask],
    ) -> None: ...

    async def arq_pool(self) -> object: ...


class StoryboardRuntimeAdapter:
    """Resolve dependencies from the compatibility route module at call time."""

    def __init__(self, bindings: ModuleType) -> None:
        self._bindings = bindings

    def now(self):
        return self._bindings._now()

    def new_id(self) -> str:
        return self._bindings.new_uuid7()

    async def get_run(self, db, **kwargs):
        return await self._bindings._get_run(db, **kwargs)

    async def load_steps(self, db, run_id, **kwargs):
        return await self._bindings._load_steps(db, run_id, **kwargs)

    async def get_step(self, db, run, step_id, **kwargs):
        return await self._bindings._get_step(db, run, step_id, **kwargs)

    async def assembly_step(self, db, run, **kwargs):
        return await self._bindings._assembly_step(db, run, **kwargs)

    async def get_or_create_conversation(self, db, **kwargs):
        return await self._bindings._get_or_create_storyboard_conversation(
            db,
            **kwargs,
        )

    async def sync_outputs(self, db, run):
        await self._bindings._sync_storyboard_outputs(db, run)

    async def build_run_out(self, db, run):
        return await self._bindings._build_run_out(db, run)

    async def list_item_out(self, db, run):
        return await self._bindings._list_item_out(db, run)

    async def publish_event(self, user_id, run_id, event_name, data):
        await self._bindings._publish_storyboard_event(
            user_id,
            run_id,
            event_name,
            data,
        )

    async def publish_image_task(self, **kwargs):
        await self._bindings._publish_storyboard_image_task(**kwargs)

    async def publish_image_tasks(self, **kwargs):
        await self._bindings._publish_storyboard_image_tasks(**kwargs)

    async def arq_pool(self):
        return await self._bindings.get_arq_pool()
