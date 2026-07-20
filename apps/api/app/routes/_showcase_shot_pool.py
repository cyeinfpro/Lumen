"""Compatibility facade for showcase shot-pool contracts and selectors."""

from __future__ import annotations

import sys

from app.workflow_domain import showcase_shot_pool as _implementation

sys.modules[__name__] = _implementation
