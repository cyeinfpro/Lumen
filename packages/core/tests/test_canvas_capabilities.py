from types import SimpleNamespace

import pytest

from lumen_core.canvas_capabilities import image_capability, task_model_snapshot
from lumen_core.canvas import canvas_execution_fingerprint


def test_default_model_is_explicit_and_unknown_models_fail_closed():
    capability = image_capability(None)
    assert capability["model"] == "gpt-image-2"
    assert "max" not in capability["render_qualities"]
    assert "max" in image_capability("gpt-image-2.5-flare")["render_qualities"]
    with pytest.raises(ValueError, match="unsupported"):
        image_capability("unknown")


def test_capability_versions_participate_in_execution_identity():
    common = dict(
        definition_hash="a" * 64,
        input_hash="b" * 64,
        node_schema_version=1,
        effective_model="gpt-image-2",
        processor_version="v1",
    )
    cap = image_capability(None)
    first = canvas_execution_fingerprint(**common, effective_provider_capability=cap)
    changed = canvas_execution_fingerprint(
        **common,
        effective_provider_capability={
            **cap,
            "maximum_outputs": 1,
        },
    )
    assert first != changed


def test_task_model_snapshot_excludes_request_secrets_and_links():
    task = SimpleNamespace(
        model="video-model",
        action="i2v",
        duration_s=5,
        resolution="720p",
        upstream_request={"api_key": "secret", "input_image_url": "signed-url"},
        provider_name="provider",
        provider_kind="volcano",
    )
    snapshot = task_model_snapshot(task, kind="video")
    assert snapshot["model"] == "video-model"
    assert snapshot["capability"]["source"] == "admitted_task"
    assert "secret" not in str(snapshot)
    assert "signed-url" not in str(snapshot)
