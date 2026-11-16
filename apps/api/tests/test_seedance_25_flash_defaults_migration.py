from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/0073_seedance_25_flash_defaults.py"


def _load():
    spec = importlib.util.spec_from_file_location("flash_migration_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_flash_official_prices_and_estimates():
    m = _load()
    prices = dict(m._PRICE_ROWS)
    assert len(prices) == 10
    assert prices["t2v_480p"] == 46_000_000
    assert prices["reference_image_720p"] == 46_000_000
    assert prices["reference_video_480p"] == 28_000_000
    assert prices["reference_video_720p"] == 28_000_000
    estimates = m._official_estimates()
    assert len(estimates["t2v"]) == 54
    assert estimates["t2v"]["480p:5"] == 50220
    assert estimates["reference_video"]["720p:5"] == 760765
    assert all(not key.startswith("1080p") for action in estimates.values() for key in action)


def test_flash_merges_preserve_overrides_and_other_models():
    m = _load()
    raw = json.dumps({"other": {}, "seedance-2.5-flash": {"t2v": {"720p:5": 999}}})
    result = json.loads(m._merge_estimates(raw))
    assert result["seedance-2.5-flash"]["t2v"]["720p:5"] == 999
    assert "other" in result
    assert m._merge_estimates("invalid-json") == "invalid-json"
    assert m._merge_estimates("[]") == "[]"
    providers = [
        {"kind": "volcano", "models": {"seedance-2.5-flash": "custom"}},
        {"kind": "volcano", "models": {"seedance-2.5-flash:t2v": "custom-t2v"}},
        {"kind": "volcano_third_party", "models": {}},
    ]
    merged = json.loads(m._merge_provider_models(json.dumps(providers)))
    assert merged[0]["models"] == {"seedance-2.5-flash": "custom"}
    assert merged[1]["models"]["seedance-2.5-flash:t2v"] == "custom-t2v"
    assert merged[1]["models"]["seedance-2.5-flash:i2v"] == "doubao-seedance-2-5-flash-261115"
    assert merged[2]["models"] == {}


def test_flash_migration_sqlite_dry_run_idempotent_and_nondestructive():
    m = _load()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as db:
        db.execute(sa.text("""CREATE TABLE system_settings (
            id TEXT PRIMARY KEY, key TEXT UNIQUE, value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""))
        db.execute(sa.text("""CREATE TABLE pricing_rules (
            id TEXT PRIMARY KEY, scope TEXT, key TEXT, variant TEXT, unit TEXT,
            price_micro INTEGER, enabled BOOLEAN, note TEXT,
            UNIQUE(scope,key,variant,unit)
        )"""))
        db.execute(sa.text("INSERT INTO system_settings (id,key,value) VALUES ('provider','video.providers',:value)"),
                   {"value": json.dumps([{"kind": "volcano", "models": {"seedance-2.5:t2v": "existing"}}])})
        db.execute(sa.text("""INSERT INTO pricing_rules VALUES (
            'custom','video','seedance-2.5-flash','t2v_480p','per_mtoken',123,false,'custom'
        )"""))
        m.op = SimpleNamespace(get_bind=lambda: db)
        m.upgrade()
        m.upgrade()
        assert db.execute(sa.text("SELECT count(*) FROM pricing_rules")).scalar_one() == 10
        assert db.execute(sa.text("SELECT price_micro FROM pricing_rules WHERE id='custom'")).scalar_one() == 123
        assert db.execute(sa.text("SELECT enabled FROM pricing_rules WHERE id='custom'")).scalar_one() == 0
        config = json.loads(db.execute(sa.text("SELECT value FROM system_settings WHERE key='video.providers'")).scalar_one())
        assert config[0]["models"]["seedance-2.5:t2v"] == "existing"
        assert config[0]["models"]["seedance-2.5-flash:t2v"] == "doubao-seedance-2-5-flash-261115"
        m.downgrade()
        assert db.execute(sa.text("SELECT count(*) FROM pricing_rules")).scalar_one() == 10
