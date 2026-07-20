"""Compatibility facade for showcase model policy."""

from __future__ import annotations

import sys

from app.workflow_domain import showcase_model_policy as _implementation

sys.modules[__name__] = _implementation
