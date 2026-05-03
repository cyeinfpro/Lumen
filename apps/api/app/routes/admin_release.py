"""Admin release management — list & rollback the symlink-based release tree.

The release agent owns ``${LUMEN_ROOT}/releases/<id>/`` and the
``current``/``previous`` symlinks; we just inspect that layout and, on
rollback, swap the symlinks via systemd-run.

Rollback strategy:
  - We never invoke ``update.sh``. Instead we emit an inline shell snippet
    via the same systemd-run fallback chain (``trigger_update`` uses) so the
    swap + service restarts run under PID 1, outside lumen-api's sandbox.
  - The snippet uses the ``::lumen-step::`` protocol, so the existing SSE
    stream (``/admin/update/stream``) can render rollback progress without
    a separate channel.
  - Schema-mismatched rollbacks are rejected up-front: a release whose
    ``alembic_head_expected`` differs from the live DB head crosses a
    migration boundary and requires manual ``alembic downgrade`` first.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ._admin_common import (
    admin_http as _http,
    cleanup_marker_when_done,
    write_admin_audit_isolated,
)
from .admin_update import (
    ReleaseInfo,
    _list_releases,
    _log_attempt_failure,
    _lumen_root,
    _open_update_log,
    _read_marker,
    _resolve_release,
    _run_systemd_command,
    _systemd_run_available,
    _systemd_run_inline_attempts,
    _update_log_path,
    _update_marker_path,
    _write_marker,
)


router = APIRouter(prefix="/admin/release", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RollbackIn(BaseModel):
    release_id: str


class RollbackOut(BaseModel):
    accepted: bool
    target: ReleaseInfo
    started_at: datetime
    unit: str | None = None
    note: str


# ---------------------------------------------------------------------------
# DB head probe
# ---------------------------------------------------------------------------


async def _read_db_alembic_head(db: AsyncSession) -> str | None:
    """Return the version_num the DB is currently pinned at.

    We could go through ``alembic.runtime.migration.MigrationContext`` like
    ``main._check_alembic_head`` does, but a direct SELECT is far cheaper for
    this hot endpoint (the DB head is already authoritative — there's only
    one row in alembic_version). Returns ``None`` if the table is missing,
    which is the correct answer for a brand-new DB that hasn't been stamped.
    """
    try:
        result = await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        row = result.first()
    except Exception:
        return None
    if row is None:
        return None
    value = row[0]
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ReleaseInfo])
async def list_releases(_admin: AdminUser) -> list[ReleaseInfo]:
    """Mirror of the ``releases`` field in ``/admin/update/status``.

    Useful for the rollback selector UI which doesn't otherwise need the full
    update status payload (log_tail / phases / running flag).
    """
    return await asyncio.to_thread(_list_releases)


def _build_rollback_script(*, target_id: str, lumen_root: Path) -> str:
    """Compose the shell snippet that performs the symlink swap + restart.

    We use the same step-protocol verbs ``update.sh`` emits so the SSE stream
    can colour the rollback identically to a forward update. ``mv -T`` is
    atomic on Linux for the same filesystem, so a crash mid-rollback leaves
    either the old or the new ``current`` — never a half-swapped symlink.
    """
    root_q = shlex.quote(str(lumen_root))
    target_q = shlex.quote(target_id)
    return rf"""
set -euo pipefail
ROOT={root_q}
TARGET={target_q}
ts() {{ date -u +%FT%TZ; }}

ROLLBACK_START="$(ts)"
echo "::lumen-step:: phase=rollback status=start ts=$ROLLBACK_START"
echo "::lumen-info:: phase=rollback key=target value=$TARGET"

# Capture the current release id so we can flip ``previous`` at the same time.
CURRENT_ID=""
if [ -L "$ROOT/current" ]; then
  CURRENT_TARGET="$(readlink "$ROOT/current")"
  CURRENT_ID="$(basename "$CURRENT_TARGET")"
fi
echo "::lumen-info:: phase=rollback key=previous_current value=$CURRENT_ID"

# 1. Atomic switch of ``current`` → releases/<target>.
SWITCH_START="$(ts)"
SWITCH_T0=$(date +%s%3N)
echo "::lumen-step:: phase=switch status=start ts=$SWITCH_START"
if [ ! -d "$ROOT/releases/$TARGET" ]; then
  echo "release directory missing: $ROOT/releases/$TARGET" >&2
  echo "::lumen-step:: phase=switch status=done rc=1 dur_ms=0 ts=$(ts)"
  echo "::lumen-step:: phase=rollback status=done rc=1 dur_ms=0 ts=$(ts)"
  exit 1
fi
TMP_LINK="$ROOT/.current.tmp.$$"
ln -s "releases/$TARGET" "$TMP_LINK"
mv -T "$TMP_LINK" "$ROOT/current"
# Flip previous to whatever current pointed at before the swap.
if [ -n "$CURRENT_ID" ] && [ -d "$ROOT/releases/$CURRENT_ID" ]; then
  TMP_PREV="$ROOT/.previous.tmp.$$"
  ln -s "releases/$CURRENT_ID" "$TMP_PREV"
  mv -T "$TMP_PREV" "$ROOT/previous"
fi
SWITCH_T1=$(date +%s%3N)
echo "::lumen-step:: phase=switch status=done rc=0 dur_ms=$((SWITCH_T1-SWITCH_T0)) ts=$(ts)"

# 2. Re-sync docker compose to the rollback target's compose file. If a prior
#    update bumped postgres/redis image versions, a naked symlink swap would
#    leave systemd services pointing at containers that don't match the rolled-
#    back code's expectations. ``docker compose up -d --wait`` is idempotent —
#    if the compose config matches what's already running, it returns instantly.
COMPOSE_START="$(ts)"
COMPOSE_T0=$(date +%s%3N)
echo "::lumen-step:: phase=containers status=start ts=$COMPOSE_START"
compose_rc=0
if [ -f "$ROOT/current/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
  if [ -f "$ROOT/current/scripts/lib.sh" ]; then
    # shellcheck source=/dev/null
    . "$ROOT/current/scripts/lib.sh"
  fi
  if declare -F lumen_ensure_compose_db_env_vars >/dev/null 2>&1 \
    && ! lumen_ensure_compose_db_env_vars "$ROOT/current/.env"; then
    compose_rc=1
    echo "compose env validation failed; rollback continues but containers may be stale" >&2
  elif ! (cd "$ROOT/current" && docker compose up -d --wait); then
    compose_rc=1
    echo "docker compose up failed; rollback continues but containers may be stale" >&2
  fi
else
  echo "::lumen-info:: phase=containers key=note value=skipped"
fi
COMPOSE_T1=$(date +%s%3N)
echo "::lumen-step:: phase=containers status=done rc=$compose_rc dur_ms=$((COMPOSE_T1-COMPOSE_T0)) ts=$(ts)"

# 3. Restart services. systemctl restart sends SIGTERM and waits up to
#    TimeoutStopSec — for lumen-worker that's 180s, enough for arq to finish
#    most in-flight image jobs gracefully. lumen-api is restarted last so we
#    don't kill the process that owns this systemd-run invocation mid-rollback.
RESTART_START="$(ts)"
RESTART_T0=$(date +%s%3N)
echo "::lumen-step:: phase=restart status=start ts=$RESTART_START"
restart_rc=0
for unit in lumen-worker.service lumen-web.service lumen-tgbot.service; do
  if ! systemctl restart "$unit"; then
    restart_rc=1
    echo "restart $unit failed" >&2
  fi
done
# lumen-api last; --no-block lets systemd return immediately so this script
# doesn't block forever waiting on a restart that may itself be us.
if ! systemctl --no-block restart lumen-api.service; then
  restart_rc=1
  echo "restart lumen-api failed" >&2
fi
RESTART_T1=$(date +%s%3N)
echo "::lumen-step:: phase=restart status=done rc=$restart_rc dur_ms=$((RESTART_T1-RESTART_T0)) ts=$(ts)"

# 4. Best-effort post-restart healthz. Failure does not abort rollback —
#    the operator can recover via the existing /admin/update plumbing.
HEALTH_START="$(ts)"
HEALTH_T0=$(date +%s%3N)
echo "::lumen-step:: phase=health_post status=start ts=$HEALTH_START"
health_rc=0
if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if ! curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    health_rc=1
  fi
fi
HEALTH_T1=$(date +%s%3N)
echo "::lumen-step:: phase=health_post status=done rc=$health_rc dur_ms=$((HEALTH_T1-HEALTH_T0)) ts=$(ts)"

# 5. Final phase marker so the SSE stream sees a clean terminal event.
ROLLBACK_T1=$(date +%s%3N)
ROLLBACK_T0_S=$(date -d "$ROLLBACK_START" +%s 2>/dev/null || echo 0)
ROLLBACK_T1_S=$(date +%s)
ROLLBACK_DUR=$(((ROLLBACK_T1_S - ROLLBACK_T0_S) * 1000))
echo "::lumen-step:: phase=rollback status=done rc=$restart_rc dur_ms=$ROLLBACK_DUR ts=$(ts)"

# Clean the marker so the SSE stream / status endpoint flips back to running=False.
rm -f "{shlex.quote(str(_update_marker_path()))}"
"""


def _rollback_unit_name(started_at: datetime) -> str:
    stamp = started_at.strftime("%Y%m%d%H%M%S")
    return f"lumen-rollback-{stamp}-{os.getpid()}.service"


def _start_rollback_systemd_unit(
    *,
    inline_script: str,
    started_at: datetime,
    log_fh,  # type: ignore[no-untyped-def]
) -> tuple[str | None, list[str]]:
    """Run the rollback shell snippet via systemd-run.

    Returns ``(unit_name_or_None, attempted_labels)``. Each failed attempt is
    appended to ``log_fh`` for operator diagnosis, mirroring the pattern in
    ``_start_update_systemd_unit``.
    """
    unit = _rollback_unit_name(started_at)
    log_path = _update_log_path()
    root = _lumen_root()
    env = os.environ.copy()
    env["LUMEN_UPDATE_SYSTEMD_UNIT"] = unit
    runtime_dir = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")

    _write_marker(0, started_at.isoformat(), unit=unit)
    attempted: list[str] = []

    for label, command in _systemd_run_inline_attempts(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
    ):
        attempted.append(label)
        result = _run_systemd_command(command, env, root)
        if result.returncode == 0:
            return unit, attempted
        _log_attempt_failure(log_fh, label, result)

    # Every attempt failed; clear the marker so a follow-up trigger isn't
    # blocked by a phantom lock.
    try:
        _update_marker_path().unlink()
    except OSError:
        pass
    return None, attempted


def _start_rollback_subprocess(
    *,
    inline_script: str,
    started_at: datetime,
    log_fh,  # type: ignore[no-untyped-def]
) -> int:
    """Last-resort fallback: detached bash subprocess.

    Used when systemd-run is unavailable (dev / containers). ``log_fh`` is
    inherited so all stdout still feeds ``.update.log``.
    """
    proc = subprocess.Popen(
        ["/usr/bin/env", "bash", "-lc", inline_script],
        cwd=str(_lumen_root()),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    _write_marker(proc.pid, started_at.isoformat())
    asyncio.create_task(_cleanup_marker_when_done(proc))
    return proc.pid


async def _cleanup_marker_when_done(proc: subprocess.Popen[bytes]) -> None:
    await cleanup_marker_when_done(
        proc,
        read_marker_fn=_read_marker,
        marker_path_fn=_update_marker_path,
    )


@router.post(
    "/rollback",
    response_model=RollbackOut,
    dependencies=[Depends(verify_csrf)],
)
async def rollback_release(
    body: RollbackIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RollbackOut:
    target_id = (body.release_id or "").strip()
    if not target_id:
        raise _http("invalid_request", "release_id is required", 422)

    # Reject if an update is already in flight — concurrent symlink swaps
    # would race the marker file and leave conflicting log streams.
    if _read_marker() is not None:
        raise _http(
            "update_running",
            "Lumen update or rollback already running; wait for it to finish first",
            409,
        )

    lumen_root = _lumen_root()
    release_dir = await asyncio.to_thread(_resolve_release, lumen_root, target_id)
    if release_dir is None:
        raise _http("release_not_found", f"release '{target_id}' not found", 404)

    releases = await asyncio.to_thread(_list_releases)
    target = next((r for r in releases if r.id == target_id), None)
    if target is None:
        # _resolve_release succeeded but the release lacks .lumen_release.json;
        # fall through with a synthetic entry so the operator at least sees
        # the swap happen, but flag schema_unknown so they know we couldn't
        # validate the alembic head.
        target = ReleaseInfo(id=target_id)

    if target.is_current:
        raise _http("already_current", f"release '{target_id}' is already current", 409)

    # Schema gate. A release whose expected head differs from the live DB
    # head means rolling back without first running ``alembic downgrade``
    # would either crash on missing tables or run user code against a
    # newer-than-expected schema. Either way: refuse and tell the operator.
    expected_head = (target.alembic_head_expected or "").strip()
    db_head = await _read_db_alembic_head(db)
    if expected_head and db_head and expected_head != db_head:
        raise _http(
            "schema_mismatch",
            (
                f"DB head {db_head} != release expected {expected_head}; "
                "rollback would cross schema boundary, manual intervention required"
            ),
            409,
            details={"db_head": db_head, "release_head": expected_head},
        )

    started_at = datetime.now(timezone.utc)
    inline_script = _build_rollback_script(target_id=target_id, lumen_root=lumen_root)

    log_fh = _open_update_log()
    unit: str | None = None
    pid: int | None = None
    try:
        log_fh.write(
            "\n=== update trigger "  # use the same delimiter the parser expects
            f"at={started_at.isoformat()} user={admin.id} mode=rollback target={target_id} ===\n"
        )
        log_fh.flush()

        if _systemd_run_available():
            unit, _attempts = await asyncio.to_thread(
                _start_rollback_systemd_unit,
                inline_script=inline_script,
                started_at=started_at,
                log_fh=log_fh,
            )
        if unit is None:
            log_fh.write(
                "\n[fallback] launching rollback as a detached subprocess; "
                "lumen-api restart will be the last step.\n"
            )
            log_fh.flush()
            pid = await asyncio.to_thread(
                _start_rollback_subprocess,
                inline_script=inline_script,
                started_at=started_at,
                log_fh=log_fh,
            )
    finally:
        log_fh.close()

    previous_id = next(
        (r.id for r in releases if r.is_current),
        None,
    )
    await write_admin_audit_isolated(
        request,
        admin,
        event_type="admin.release.rollback",
        details={
            "release_id": target_id,
            "previous_id": previous_id,
            "unit": unit,
            "pid": pid,
            "alembic_head_expected": expected_head or None,
            "alembic_head_db": db_head,
        },
    )

    if unit is None and pid is None:
        # Both systemd-run and subprocess failed (e.g. exec missing). Surface
        # 500 instead of returning accepted=True for an aborted rollback.
        raise _http("rollback_launch_failed", "could not launch rollback runner", 500)

    return RollbackOut(
        accepted=True,
        target=target,
        started_at=started_at,
        unit=unit,
        note=(
            "回滚已在后台启动；可通过 GET /admin/update/stream 监听进度，"
            "或轮询 GET /admin/update/status。完成后服务会逐个重启，期间可能短暂不可用。"
        ),
    )


__all__ = [
    "router",
    "RollbackIn",
    "RollbackOut",
    "_build_rollback_script",
    "_read_db_alembic_head",
]
