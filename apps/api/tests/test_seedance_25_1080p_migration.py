from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0067_seedance_25_1080p.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "seedance_25_1080p_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_1080p_estimates_cover_all_seedance_25_durations() -> None:
    migration = _load_migration()

    estimates = migration._official_1080p_estimates()  # noqa: SLF001

    assert set(estimates) == {
        "t2v",
        "i2v",
        "reference",
        "reference_image",
        "reference_video",
    }
    assert len(estimates["t2v"]) == 27
    assert estimates["t2v"]["1080p:5"] == 244_556
    assert estimates["reference_video"]["1080p:5"] == 1_711_891
    assert (
        estimates["reference_video"]["1080p:30"]
        > estimates["t2v"]["1080p:30"]
    )


def test_merge_estimates_preserves_existing_resolutions_and_custom_1080p() -> None:
    migration = _load_migration()
    raw = json.dumps(
        {
            "seedance-2.5": {
                "t2v": {
                    "720p:5": 108_681,
                    "1080p:5": 999_999,
                }
            },
            "seedance-2.0": {"t2v": {"720p:5": 108_900}},
        }
    )

    merged = json.loads(migration._merge_estimates(raw))  # noqa: SLF001

    assert merged["seedance-2.0"]["t2v"]["720p:5"] == 108_900
    assert merged["seedance-2.5"]["t2v"]["720p:5"] == 108_681
    assert merged["seedance-2.5"]["t2v"]["1080p:5"] == 999_999
    assert merged["seedance-2.5"]["t2v"]["1080p:30"] == 1_467_335
    assert merged["seedance-2.5"]["reference_video"]["1080p:30"] == 2_934_670


def test_remove_estimates_only_removes_values_owned_by_migration() -> None:
    migration = _load_migration()
    merged = json.loads(migration._merge_estimates(None))  # noqa: SLF001
    merged["seedance-2.5"]["t2v"]["1080p:5"] = 999_999

    stripped = json.loads(
        migration._remove_estimates(json.dumps(merged))  # noqa: SLF001
    )

    assert stripped["seedance-2.5"]["t2v"]["1080p:5"] == 999_999
    assert "1080p:6" not in stripped["seedance-2.5"]["t2v"]
    assert "1080p:5" not in stripped["seedance-2.5"]["reference_video"]


def test_price_rows_use_official_1080p_input_video_split() -> None:
    migration = _load_migration()
    prices = {
        variant: price
        for _row_id, variant, price, _note in migration._PRICE_ROWS  # noqa: SLF001
    }

    assert prices["t2v_1080p"] == 77_000_000
    assert prices["reference_image_1080p"] == 77_000_000
    assert prices["reference_video_1080p"] == 46_000_000
