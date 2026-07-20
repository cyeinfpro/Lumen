"""Compatibility facade for the adult showcase shot pool."""

from __future__ import annotations

import sys

from app.workflow_domain import showcase_shot_pool_adult as _implementation

sys.modules[__name__] = _implementation
