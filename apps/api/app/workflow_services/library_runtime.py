"""Shared late-bound runtime for apparel library service modules."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from .facade import FacadeRuntime


FACADE_RUNTIME = FacadeRuntime("workflow-library-sync-facade")
_SERVICE_MODULE = f"{__package__}.library_sync"


def _service_module() -> ModuleType:
    module = sys.modules.get(_SERVICE_MODULE)
    if module is None:
        module = importlib.import_module(_SERVICE_MODULE)
    return module


def runtime() -> Any:
    return FACADE_RUNTIME.current(_service_module())
