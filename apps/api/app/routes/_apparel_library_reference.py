"""Compatibility facade for apparel reference-profile helpers."""

from __future__ import annotations

import sys

from app.workflow_domain import apparel_library_reference as _implementation

sys.modules[__name__] = _implementation
