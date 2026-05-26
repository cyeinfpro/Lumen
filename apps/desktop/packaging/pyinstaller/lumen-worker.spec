# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[3]

hiddenimports = [
    "aiosqlite",
    "sqlite_vec",
    "app.tasks.auto_title",
    "app.tasks.completion",
    "app.tasks.context_image_caption",
    "app.tasks.context_summary",
    "app.tasks.generation",
    "app.tasks.memory_extraction",
    "app.tasks.outbox",
    "app.tasks.state",
] + collect_submodules("tiktoken_ext")

excluded_db_drivers = [
    "asyncpg",
    "psycopg",
    "psycopg2",
    "pg8000",
    "MySQLdb",
    "pymysql",
]

datas = collect_data_files("sqlite_vec")

a = Analysis(
    [str(ROOT / "apps/worker/app/desktop_entry.py")],
    pathex=[str(ROOT / "apps/worker"), str(ROOT / "packages/core")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "apps/desktop/packaging/pyinstaller/hooks")],
    runtime_hooks=[],
    excludes=excluded_db_drivers,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lumen-worker",
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="lumen-worker",
)
