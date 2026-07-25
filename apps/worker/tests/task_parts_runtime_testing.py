from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from typing import Any

import pytest


@contextmanager
def synchronize_module_ports(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    ports: Any,
) -> Iterator[None]:
    """Mirror legacy module monkeypatches into an explicit immutable port set."""

    original_values = {
        field.name: getattr(ports, field.name) for field in fields(ports)
    }
    original_setattr = monkeypatch.setattr

    def synchronized_setattr(
        target: Any,
        name: str,
        value: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_setattr(target, name, value, *args, **kwargs)
        if target is module and name in original_values:
            object.__setattr__(ports, name, value)

    monkeypatch.setattr = synchronized_setattr  # type: ignore[method-assign]
    try:
        yield
    finally:
        for name, value in original_values.items():
            object.__setattr__(ports, name, value)
