"""Frozen workflow compatibility and ownership ledger for the C workstream."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompatibilityExport:
    legacy_path: str
    owner_path: str
    category: str
    retirement: str


WORKFLOW_COMPATIBILITY_EXPORTS: tuple[CompatibilityExport, ...] = (
    CompatibilityExport(
        legacy_path="app.routes.workflows._PublishBundle",
        owner_path="app.workflow_domain.workflow_contracts.PublishBundle",
        category="type",
        retirement="static_reexport",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows._sync_library_presets_from_github_folder",
        owner_path=(
            "app.workflow_services.library_sync.sync_library_presets_from_github_folder"
        ),
        category="service",
        retirement="static_reexport",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows._prepare_showcase_preflight_impl",
        owner_path=(
            "app.workflow_services.showcase_orchestration."
            "prepare_showcase_preflight_impl"
        ),
        category="service",
        retirement="static_reexport",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows._build_run_out",
        owner_path="app.workflow_services.workflow_runtime.build_run_out",
        category="query",
        retirement="static_reexport",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows.create_apparel_model_showcase",
        owner_path=(
            "app.workflow_services.apparel_endpoints.create_apparel_model_showcase"
        ),
        category="http_endpoint",
        retirement="keep_endpoint",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows.generate_apparel_model_library_job",
        owner_path=(
            "app.workflow_services.model_library_endpoints."
            "generate_apparel_model_library_job"
        ),
        category="http_endpoint",
        retirement="keep_endpoint",
    ),
    CompatibilityExport(
        legacy_path="app.routes.workflows.create_poster_design_workflow",
        owner_path=(
            "app.workflow_services.poster_endpoints.create_poster_design_workflow"
        ),
        category="http_endpoint",
        retirement="keep_endpoint",
    ),
)


RETIRED_HIDDEN_DEPENDENCIES: tuple[str, ...] = (
    "app.routes.workflow_routes._facade.RouteFacade",
    "app.workflow_services.facade.FacadeRuntime context lookup",
    "app.workflow_services.library_runtime",
    "app.workflow_services.showcase_runtime",
    "app.workflow_services.workflow_runtime dynamic module-cache lookup",
    "workflow service imports from app.routes",
)


__all__ = [
    "CompatibilityExport",
    "RETIRED_HIDDEN_DEPENDENCIES",
    "WORKFLOW_COMPATIBILITY_EXPORTS",
]
