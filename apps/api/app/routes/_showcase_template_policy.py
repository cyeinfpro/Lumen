"""Compatibility facade for showcase template policy."""

from __future__ import annotations

import sys

from app.workflow_domain import showcase_template_policy as _implementation

sys.modules[__name__] = _implementation
