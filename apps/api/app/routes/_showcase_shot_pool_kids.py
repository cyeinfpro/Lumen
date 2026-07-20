"""Compatibility facade for child and toddler showcase shot pools."""

from __future__ import annotations

import sys

from app.workflow_domain import showcase_shot_pool_kids as _implementation

sys.modules[__name__] = _implementation
