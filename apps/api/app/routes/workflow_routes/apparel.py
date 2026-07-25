"""Thin apparel workflow route compatibility module."""

from __future__ import annotations

from ...workflow_services import apparel_endpoints as _impl


entry_router = _impl.entry_router
project_router = _impl.project_router
FACADE_EXPORTS = _impl.FACADE_EXPORTS

create_apparel_model_showcase = _impl.create_apparel_model_showcase
list_apparel_model_library = _impl.list_apparel_model_library
sync_apparel_model_library_presets = _impl.sync_apparel_model_library_presets
get_apparel_model_library_item_binary = _impl.get_apparel_model_library_item_binary
get_apparel_model_library_item_thumb = _impl.get_apparel_model_library_item_thumb
create_apparel_model_library_item = _impl.create_apparel_model_library_item
patch_apparel_model_library_item = _impl.patch_apparel_model_library_item
_delete_apparel_model_library_item_for_user = (
    _impl.delete_apparel_model_library_item_for_user
)
delete_apparel_model_library_item = _impl.delete_apparel_model_library_item
batch_delete_apparel_model_library_items = (
    _impl.batch_delete_apparel_model_library_items
)
approve_product_analysis = _impl.approve_product_analysis
create_model_candidates = _impl.create_model_candidates
select_apparel_model_library_item = _impl.select_apparel_model_library_item


__all__ = [*FACADE_EXPORTS, "FACADE_EXPORTS", "entry_router", "project_router"]


# Public compatibility contracts.
delete_apparel_model_library_item_for_user = _delete_apparel_model_library_item_for_user
