"""Compatibility facade for apparel scene planning."""

from __future__ import annotations

import sys

from app.workflow_domain import apparel_scene_planner as _implementation

sys.modules[__name__] = _implementation
