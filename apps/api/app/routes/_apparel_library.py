"""Compatibility facade for the neutral apparel-library domain module."""

from __future__ import annotations

import sys

from app.workflow_domain import apparel_library as _implementation

sys.modules[__name__] = _implementation
