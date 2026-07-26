from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from typing import Any

import pytest


@contextmanager
def synchronize_module_ports(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    ports: Any,
) -> Iterator[None]:
    """Mirror legacy module monkeypatches into an explicit immutable port set."""

    bindings: dict[str, tuple[Any, Any]] = {}

    def collect(owner: Any) -> None:
        for field in fields(owner):
            value = getattr(owner, field.name)
            if (
                is_dataclass(value)
                and type(value).__module__.endswith(".runtime")
                and type(value).__name__.endswith("Ports")
            ):
                collect(value)
                continue
            if field.name in bindings:
                raise AssertionError(f"duplicate runtime port name: {field.name}")
            bindings[field.name] = (owner, value)

    collect(ports)
    original_setattr = monkeypatch.setattr

    def synchronized_setattr(
        target: Any,
        name: str,
        value: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_setattr(target, name, value, *args, **kwargs)
        if target is module and name in bindings:
            owner, _original = bindings[name]
            object.__setattr__(owner, name, value)

    monkeypatch.setattr = synchronized_setattr  # type: ignore[method-assign]
    try:
        yield
    finally:
        for name, (owner, value) in bindings.items():
            object.__setattr__(owner, name, value)
