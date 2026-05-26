# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parents[3]

hiddenimports = [
    "aiosqlite",
    "sqlite_vec",
    "lumen_core.desktop_import",
    "alembic.runtime.migration",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "app.routes.conversations",
    "app.routes.desktop",
    "app.routes.events",
    "app.routes.generations",
    "app.routes.images",
    "app.routes.messages",
    "app.routes.memories",
    "app.routes.prompts",
    "app.routes.providers",
    "app.routes.regenerate",
    "app.routes.shares",
    "app.routes.system_prompts",
    "app.routes.tasks",
]

excluded_db_drivers = [
    "asyncpg",
    "psycopg",
    "psycopg2",
    "pg8000",
    "MySQLdb",
    "pymysql",
]

datas = [
    (str(ROOT / "apps/api/alembic.ini"), "."),
    (str(ROOT / "apps/api/alembic/desktop"), "alembic/desktop"),
] + collect_data_files("sqlite_vec")

a = Analysis(
    [str(ROOT / "apps/api/app/desktop_entry.py")],
    pathex=[str(ROOT / "apps/api"), str(ROOT / "packages/core")],
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
    name="lumen-api",
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="lumen-api",
)
