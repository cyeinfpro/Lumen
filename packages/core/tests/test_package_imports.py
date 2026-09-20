"""Preserve package-level imports without eager ORM initialization."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_lightweight_core_imports_do_not_initialize_database_models(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "packages" / "core"))
    check = subprocess.run(
        [sys.executable, "-c", """
import sys
import lumen_core
assert lumen_core.__version__
assert 'lumen_core.models' not in sys.modules
assert 'sqlalchemy' not in sys.modules
import lumen_core.context_window
import lumen_core.backup_integrity
assert 'lumen_core.models' not in sys.modules
assert 'sqlalchemy' not in sys.modules
"""],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.parametrize("name", ["models", "constants", "sizing", "context_window"])
def test_existing_package_exports_keep_module_identity(name):
    import lumen_core

    assert name in dir(lumen_core)
    assert getattr(lumen_core, name) is importlib.import_module(f"lumen_core.{name}")
    assert getattr(lumen_core, name) is getattr(lumen_core, name)


def test_unknown_core_export_raises_attribute_error():
    import lumen_core

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(lumen_core, "not_a_public_core_module")
