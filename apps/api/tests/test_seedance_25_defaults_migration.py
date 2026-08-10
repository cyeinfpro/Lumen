from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0065_seedance_25_video_defaults.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "seedance_25_defaults_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_estimates_cover_all_seedance_25_durations() -> None:
    migration = _load_migration()

    model = migration._official_estimates()["seedance-2.5"]  # noqa: SLF001

    assert set(model) == {
        "t2v",
        "i2v",
        "reference",
        "reference_image",
        "reference_video",
    }
    assert len(model["t2v"]) == 2 * 27
    assert model["t2v"]["480p:5"] == 50_220
    assert model["t2v"]["720p:5"] == 108_681
    assert model["reference_video"]["480p:5"] == 351_540
    assert model["reference_video"]["720p:5"] == 760_765
    assert model["reference_video"]["720p:30"] > model["t2v"]["720p:30"]


def test_merge_estimates_preserves_other_models() -> None:
    migration = _load_migration()
    raw = json.dumps({"seedance-2.0": {"t2v": {"720p:5": 108_900}}})

    merged = json.loads(migration._merge_estimates(raw))  # noqa: SLF001

    assert merged["seedance-2.0"]["t2v"]["720p:5"] == 108_900
    assert merged["seedance-2.5"]["t2v"]["720p:30"] > 0


def test_merge_provider_models_only_extends_volcano_providers() -> None:
    migration = _load_migration()
    raw = json.dumps(
        {
            "providers": [
                {
                    "name": "volcano-main",
                    "kind": "volcano",
                    "models": {
                        "seedance-2.0:t2v": "doubao-seedance-2-0-260128",
                    },
                },
                {
                    "name": "third-party",
                    "kind": "volcano_third_party",
                    "models": {"seedance-2.0:t2v": "custom-model"},
                },
            ]
        }
    )

    merged = json.loads(migration._merge_provider_models(raw))  # noqa: SLF001

    volcano_models = merged["providers"][0]["models"]
    assert volcano_models["seedance-2.5:t2v"] == "doubao-seedance-2-5-260628"
    assert volcano_models["seedance-2.5:i2v"] == "doubao-seedance-2-5-260628"
    assert volcano_models["seedance-2.5:reference"] == "doubao-seedance-2-5-260628"
    assert "seedance-2.5:t2v" not in merged["providers"][1]["models"]


def test_merge_provider_models_does_not_override_custom_seedance_25() -> None:
    migration = _load_migration()
    raw = json.dumps(
        [
            {
                "name": "volcano-main",
                "kind": "volcano",
                "models": {
                    "seedance-2.5:t2v": "custom-seedance-25-endpoint",
                },
            }
        ]
    )

    merged = json.loads(migration._merge_provider_models(raw))  # noqa: SLF001

    assert merged[0]["models"]["seedance-2.5:t2v"] == "custom-seedance-25-endpoint"
    assert merged[0]["models"]["seedance-2.5:i2v"] == "doubao-seedance-2-5-260628"


def test_price_rows_use_official_input_video_split() -> None:
    migration = _load_migration()
    prices = {
        variant: price
        for _row_id, variant, price, _note in migration._PRICE_ROWS  # noqa: SLF001
    }

    assert prices["t2v_480p"] == 70_000_000
    assert prices["reference_image_720p"] == 70_000_000
    assert prices["reference_video_480p"] == 42_000_000
    assert prices["reference_video_720p"] == 42_000_000
