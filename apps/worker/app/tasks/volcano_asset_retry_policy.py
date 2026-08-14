"""Retry limits shared by Volcano asset submission and recovery."""

from __future__ import annotations

VOLCANO_ASSET_PRE_SUBMIT_RETRY_LIMIT = 5
VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT = 20

__all__ = [
    "VOLCANO_ASSET_PRE_SUBMIT_RETRY_LIMIT",
    "VOLCANO_ASSET_UNCERTAIN_SUBMIT_RETRY_LIMIT",
]
