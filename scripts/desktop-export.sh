#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-lumen-desktop-export}"
mkdir -p "$OUT_DIR"

PG_CONTAINER="${LUMEN_PG_CONTAINER:-lumen-pg}"
DB_USER="${DB_USER:-lumen}"
DB_NAME="${DB_NAME:-lumen}"
STORAGE_ROOT="${LUMEN_STORAGE_ROOT:-/opt/lumendata/storage}"

docker exec "$PG_CONTAINER" pg_dump -Fc -U "$DB_USER" "$DB_NAME" > "$OUT_DIR/lumen-export.dump"
docker exec "$PG_CONTAINER" pg_dump --data-only --no-owner --no-acl -U "$DB_USER" "$DB_NAME" > "$OUT_DIR/lumen-export.copy.sql"
tar -C "$STORAGE_ROOT" -czf "$OUT_DIR/lumen-storage.tar.gz" .

printf 'Wrote %s/lumen-export.dump, %s/lumen-export.copy.sql, and %s/lumen-storage.tar.gz\n' "$OUT_DIR" "$OUT_DIR" "$OUT_DIR"
