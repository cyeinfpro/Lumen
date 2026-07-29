from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

from app.canvas_services import read_repair
from app.images.adapters import filesystem_store
from app.images.adapters.filesystem_store_parts import (
    objects as filesystem_objects,
)
from app.images.adapters.filesystem_store_parts import (
    publish as filesystem_publish,
)
from app.images.adapters.filesystem_store_parts import (
    reconcile as filesystem_reconcile,
)
from app.images.adapters.filesystem_store_parts import (
    retention as filesystem_retention,
)
from app.images.adapters.filesystem_store_parts import (
    staging as filesystem_staging,
)
from app.images.adapters.filesystem_store_parts import (
    variants as filesystem_variants,
)
from app.images.domain import artifact, resource_estimate, variants
from app.routes import _video_reference_media
from app.routes.prompt_parts import upstream
from app.services import email, github_releases
from app.services.admin import request_events
from app.services.storyboard import common
from app import config, task_billing


API_ROOT = Path(__file__).resolve().parents[1]

TAIL_MODULES = (
    "app/canvas_services/read_repair.py",
    "app/config.py",
    "app/images/adapters/filesystem_store.py",
    "app/images/adapters/filesystem_store_parts/objects.py",
    "app/images/adapters/filesystem_store_parts/publish.py",
    "app/images/adapters/filesystem_store_parts/reconcile.py",
    "app/images/adapters/filesystem_store_parts/retention.py",
    "app/images/adapters/filesystem_store_parts/staging.py",
    "app/images/adapters/filesystem_store_parts/variants.py",
    "app/images/domain/artifact.py",
    "app/images/domain/resource_estimate.py",
    "app/images/domain/variants.py",
    "app/routes/_video_reference_media.py",
    "app/routes/prompt_parts/upstream.py",
    "app/services/admin/request_events.py",
    "app/services/storyboard/common.py",
    "app/task_billing.py",
    "app/services/email.py",
    "app/services/github_releases.py",
)

FILESYSTEM_RESPONSIBILITIES = {
    "identity": filesystem_objects,
    "stage": filesystem_staging,
    "publish_path": filesystem_publish,
    "processing_path": filesystem_variants,
    "_process_staged_record": filesystem_reconcile,
    "sweep_staged": filesystem_retention,
}


def test_tail_constants_are_immutable() -> None:
    assert isinstance(read_repair._TERMINAL_TASK_STATUSES, frozenset)
    assert isinstance(config._TRUE_ENV_VALUES, frozenset)
    assert isinstance(filesystem_store._LINK_UNSUPPORTED_ERRNOS, frozenset)
    assert isinstance(artifact._ALLOWED_TRANSITIONS, MappingProxyType)
    assert isinstance(resource_estimate._MODE_CHANNELS, MappingProxyType)
    assert isinstance(variants.VARIANT_MEDIA_TYPE, MappingProxyType)
    assert isinstance(_video_reference_media._KIND_LABELS, MappingProxyType)
    assert isinstance(upstream.RETRYABLE_HTTP_STATUS, frozenset)
    assert isinstance(request_events._REQUEST_EVENT_STATUSES, frozenset)
    assert isinstance(request_events._REQUEST_EVENT_RANGE_HOURS, MappingProxyType)
    assert isinstance(request_events._MODEL_SHORT_LABELS, MappingProxyType)
    assert isinstance(common.STORYBOARD_ASSET_KINDS, frozenset)
    assert isinstance(common.SHOT_STATUS_RANK, MappingProxyType)
    assert isinstance(task_billing._IMAGE_BILLING_TIER_VALUES, frozenset)
    assert isinstance(task_billing._IMAGE_RENDER_QUALITY_VALUES, frozenset)
    assert isinstance(email._DEV_ENVS, frozenset)
    assert isinstance(github_releases._ALLOWED_RELEASE_HOSTS, frozenset)


def test_filesystem_store_is_a_thin_responsibility_facade() -> None:
    facade_path = API_ROOT / "app/images/adapters/filesystem_store.py"
    assert len(facade_path.read_text(encoding="utf-8").splitlines()) < 800

    for method_name, owner in FILESYSTEM_RESPONSIBILITIES.items():
        method = getattr(filesystem_store.FileSystemArtifactStore, method_name)
        assert method_name not in filesystem_store.FileSystemArtifactStore.__dict__
        assert method.__module__ == owner.__name__

    for owner in set(FILESYSTEM_RESPONSIBILITIES.values()):
        owner_path = Path(owner.__file__ or "")
        assert len(owner_path.read_text(encoding="utf-8").splitlines()) < 800


def test_tail_modules_have_no_private_cross_module_imports() -> None:
    for relative in TAIL_MODULES:
        tree = ast.parse((API_ROOT / relative).read_text(encoding="utf-8"))
        private_imports = [
            (node.lineno, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name.startswith("_") and not alias.name.startswith("__")
        ]
        assert private_imports == [], f"{relative}: {private_imports!r}"


def test_request_events_route_uses_public_service_contracts() -> None:
    source = (API_ROOT / "app/routes/admin.py").read_text(encoding="utf-8")
    assert "_request_events._REQUEST_EVENT_" not in source
    assert "_request_events._duration_ms" not in source
