from __future__ import annotations

import sys
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Callable, ParamSpec, TypeVar, cast

from lumen_core.models import OutboxEvent

from ...workflow_services.facade import FacadeRuntime, bind_facade


P = ParamSpec("P")
R = TypeVar("R")
_SOURCE_ATTR = "__workflow_route_source__"


class _PublishBundle:
    def __init__(
        self,
        *,
        assistant_msg_id: str,
        message_ids: list[str],
        outbox_payloads: list[dict[str, Any]],
        outbox_rows: list[OutboxEvent],
    ) -> None:
        self.assistant_msg_id = assistant_msg_id
        self.message_ids = message_ids
        self.outbox_payloads = outbox_payloads
        self.outbox_rows = outbox_rows


class RouteFacade:
    """Context-local compatibility facade for extracted workflow routes."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.runtime = FacadeRuntime(f"{module_name}-facade")
        self._default_facade: Any | None = None
        # Route modules can be loaded through an alias by test harnesses or
        # application bootstrap code. Keep the historical canonical keys
        # available so late-bound facade lookups never depend on import order.
        module = sys.modules.get(module_name)
        if module is not None:
            short_name = module_name.rsplit(".", 1)[-1]
            canonical_name = f"app.routes.workflow_routes.{short_name}"
            sys.modules.setdefault(canonical_name, module)

    @property
    def module(self) -> Any:
        return sys.modules[self.module_name]

    def configure(self, facade: Any) -> None:
        self._default_facade = facade

    def current(self) -> Any:
        return self.runtime.current(self._default_facade or self.module)

    def entry(self, function: Callable[P, R]) -> Callable[P, R]:
        """Wrap an extracted function while preserving its public signature."""

        if iscoroutinefunction(function):

            @wraps(function)
            async def async_entry(*args: P.args, **kwargs: P.kwargs) -> Any:
                target = self._override(async_entry, function.__name__)
                if target is not None:
                    return await target(*args, **kwargs)
                return await function(*args, **kwargs)

            return cast(Callable[P, R], async_entry)

        @wraps(function)
        def entry(*args: P.args, **kwargs: P.kwargs) -> R:
            target = self._override(entry, function.__name__)
            if target is not None:
                return target(*args, **kwargs)
            return function(*args, **kwargs)

        return entry

    def sync_hook(self, name: str) -> Callable[..., Any]:
        def hook(*args: Any, **kwargs: Any) -> Any:
            target = self._required_hook(name, hook)
            return target(*args, **kwargs)

        hook.__name__ = name
        return hook

    def async_hook(self, name: str) -> Callable[..., Any]:
        async def hook(*args: Any, **kwargs: Any) -> Any:
            target = self._required_hook(name, hook)
            return await target(*args, **kwargs)

        hook.__name__ = name
        return hook

    def install_hooks(
        self,
        namespace: dict[str, Any],
        *,
        sync_names: tuple[str, ...] = (),
        async_names: tuple[str, ...] = (),
    ) -> None:
        namespace.update({name: self.sync_hook(name) for name in sync_names})
        namespace.update({name: self.async_hook(name) for name in async_names})

    def export(self, facade: Any, names: tuple[str, ...]) -> None:
        """Install historical ``workflows`` aliases bound to this facade."""

        self.configure(facade)
        for name in names:
            value = getattr(self.module, name)
            if callable(value):
                bound = bind_facade(value, facade=facade, runtime=self.runtime)
                setattr(bound, _SOURCE_ATTR, value)
                value = bound
            setattr(facade, name, value)

    def _override(self, source: Callable[..., Any], name: str) -> Any | None:
        current = self.current()
        if current is self.module:
            return None
        target = getattr(current, name, None)
        if target is None or target is source:
            return None
        if getattr(target, _SOURCE_ATTR, None) is source:
            return None
        return target

    def _required_hook(
        self,
        name: str,
        source: Callable[..., Any],
    ) -> Callable[..., Any]:
        target = getattr(self.current(), name, None)
        if target is None or target is source:
            raise RuntimeError(f"workflow route hook is not configured: {name}")
        return cast(Callable[..., Any], target)
