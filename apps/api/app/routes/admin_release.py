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
import json
import logging
import os
import re
import shlex
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..services.system_lock import LockBusy, SystemOperationLockService
from .admin_update_marker import UpdateMarkerBusy
from ._admin_common import (
    admin_http as _http,
    cleanup_marker_when_done,
    write_admin_audit_isolated,
)
from .admin_backups import maintenance_marker_busy
from .admin_update import (
    ReleaseInfo,
    list_releases as update_list_releases,
    log_attempt_failure as update_log_attempt_failure,
    lumen_root as update_lumen_root,
    open_update_log as update_open_update_log,
    read_marker as update_read_marker,
    resolve_release as update_resolve_release,
    run_systemd_command as update_run_systemd_command,
    systemd_run_available as update_systemd_run_available,
    systemd_run_inline_attempts as update_systemd_run_inline_attempts,
    update_log_path as update_update_log_path,
    update_marker_path as update_update_marker_path,
    write_marker as update_write_marker,
)


_MARKER_CLEANUP_RUNTIME_STATE_KEY = "_admin_release_marker_cleanup_runtime"
_ALEMBIC_REVISION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _MarkerCleanupRuntime:
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def schedule(
        self,
        proc: subprocess.Popen[bytes],
        cleanup: Callable[[subprocess.Popen[bytes]], Awaitable[None]],
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(cleanup(proc))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def shutdown(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)


def _marker_cleanup_runtime(request: Request) -> _MarkerCleanupRuntime:
    runtime = getattr(request.app.state, _MARKER_CLEANUP_RUNTIME_STATE_KEY, None)
    if not isinstance(runtime, _MarkerCleanupRuntime):
        raise RuntimeError("admin release marker cleanup runtime is unavailable")
    return runtime


@asynccontextmanager
async def _marker_cleanup_lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = _MarkerCleanupRuntime()
    setattr(app.state, _MARKER_CLEANUP_RUNTIME_STATE_KEY, runtime)
    try:
        yield
    finally:
        await runtime.shutdown()
        if getattr(app.state, _MARKER_CLEANUP_RUNTIME_STATE_KEY, None) is runtime:
            delattr(app.state, _MARKER_CLEANUP_RUNTIME_STATE_KEY)


router = APIRouter(
    prefix="/admin/release",
    tags=["admin"],
    lifespan=_marker_cleanup_lifespan,
)
update_router = APIRouter(prefix="/admin/update", tags=["admin"])


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


class RollbackSchemaUnknown(RuntimeError):
    """The rollback target and live schema cannot be proven compatible."""


class RollbackGateError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _strict_release_expected_heads(
    release_dir: Path,
    target_id: str,
) -> frozenset[str]:
    manifest = release_dir / ".lumen_release.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RollbackSchemaUnknown(
            "release manifest is missing or unreadable"
        ) from exc
    if not isinstance(raw, dict):
        raise RollbackSchemaUnknown("release manifest must be a JSON object")

    manifest_id = str(raw.get("id") or "").strip()
    if manifest_id and manifest_id != target_id:
        raise RollbackSchemaUnknown("release manifest id does not match directory")

    expected = str(raw.get("alembic_head_expected") or "").strip()
    if not expected or not _ALEMBIC_REVISION_RE.fullmatch(expected):
        raise RollbackSchemaUnknown("release manifest has no valid alembic head")
    return frozenset({expected})


async def _read_db_alembic_heads(db: AsyncSession) -> frozenset[str]:
    result = await db.execute(text("SELECT version_num FROM alembic_version"))
    heads = frozenset(
        str(value).strip()
        for value in result.scalars().all()
        if value is not None and str(value).strip()
    )
    if not heads or any(not _ALEMBIC_REVISION_RE.fullmatch(head) for head in heads):
        raise RollbackSchemaUnknown("database alembic head is empty or invalid")
    return heads


async def _validate_rollback_target(
    *,
    release_dir: Path,
    target_id: str,
    releases: list[ReleaseInfo],
    db: AsyncSession,
) -> tuple[ReleaseInfo, str, str]:
    target = next((release for release in releases if release.id == target_id), None)
    if target is None:
        raise RollbackGateError(
            "release_manifest_unknown",
            "target release is not present in the validated release inventory",
            409,
        )
    if target.is_current:
        raise RollbackGateError(
            "already_current",
            f"release '{target_id}' is already current",
            409,
        )

    try:
        expected_heads = await asyncio.to_thread(
            _strict_release_expected_heads,
            release_dir,
            target_id,
        )
    except RollbackSchemaUnknown as exc:
        raise RollbackGateError(
            "release_manifest_unknown",
            str(exc),
            409,
        ) from exc

    try:
        db_heads = await _read_db_alembic_heads(db)
    except RollbackSchemaUnknown as exc:
        raise RollbackGateError(
            "database_schema_unknown",
            str(exc),
            409,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("rollback schema probe failed target=%s", target_id)
        raise RollbackGateError(
            "database_schema_probe_failed",
            "database schema could not be verified",
            503,
        ) from exc

    if db_heads != expected_heads:
        raise RollbackGateError(
            "schema_mismatch",
            "database heads do not exactly match the target release",
            409,
            details={
                "db_heads": sorted(db_heads),
                "release_heads": sorted(expected_heads),
            },
        )
    return target, next(iter(expected_heads)), next(iter(db_heads))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ReleaseInfo])
async def list_releases(_admin: AdminUser) -> list[ReleaseInfo]:
    """Mirror of the ``releases`` field in ``/admin/update/status``.

    Useful for the rollback selector UI which doesn't otherwise need the full
    update status payload (log_tail / phases / running flag).
    """
    return await asyncio.to_thread(update_list_releases)


def _build_rollback_script(*, target_id: str, lumen_root: Path) -> str:
    """Compose the shell snippet that performs the symlink swap + restart.

    We use the same step-protocol verbs ``update.sh`` emits so the SSE stream
    can colour the rollback identically to a forward update. ``mv -T`` is
    atomic on Linux for the same filesystem, so a crash mid-rollback leaves
    either the old or the new ``current`` — never a half-swapped symlink.
    """
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", target_id):
        raise ValueError("invalid release id")
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
  SHARED_ENV="$ROOT/shared/.env"
  TARGET_IMAGE_TAG=""
  TARGET_VERSION=""
  if [ -f "$ROOT/current/.image-tag" ]; then
    TARGET_IMAGE_TAG="$(head -n1 "$ROOT/current/.image-tag" | tr -d '[:space:]')"
  fi
  if [ -f "$ROOT/current/VERSION" ]; then
    TARGET_VERSION="$(head -n1 "$ROOT/current/VERSION" | tr -d '[:space:]')"
  fi
  if [ -n "$TARGET_IMAGE_TAG" ] && declare -F lumen_set_image_tag_in_env >/dev/null 2>&1; then
    if ! lumen_set_image_tag_in_env "$SHARED_ENV" "$TARGET_IMAGE_TAG"; then
      compose_rc=1
      echo "failed to restore shared LUMEN_IMAGE_TAG=$TARGET_IMAGE_TAG" >&2
    else
      echo "::lumen-info:: phase=containers key=image_tag value=$TARGET_IMAGE_TAG"
    fi
  fi
  if [ -n "$TARGET_VERSION" ] && declare -F lumen_set_env_value_in_file >/dev/null 2>&1; then
    if ! lumen_set_env_value_in_file "$SHARED_ENV" LUMEN_VERSION "$TARGET_VERSION"; then
      compose_rc=1
      echo "failed to restore shared LUMEN_VERSION=$TARGET_VERSION" >&2
    else
      echo "::lumen-info:: phase=containers key=version value=$TARGET_VERSION"
    fi
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
rm -f "{shlex.quote(str(update_update_marker_path()))}"
"""


def _rollback_unit_name(started_at: datetime) -> str:
    stamp = started_at.strftime("%Y%m%d%H%M%S")
    return f"lumen-rollback-{stamp}-{os.getpid()}.service"


def _kill_launched_script(proc: subprocess.Popen[bytes]) -> None:
    """Abort a freshly-spawned rollback script after losing the marker claim.

    The script runs in its own session (start_new_session=True), so
    SIGTERM to the group reaches the script as well as its bash wrapper;
    escalate to SIGKILL if it does not exit within 5s.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


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
    log_path = update_update_log_path()
    root = update_lumen_root()
    env = os.environ.copy()
    env["LUMEN_UPDATE_SYSTEMD_UNIT"] = unit
    runtime_dir = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")

    if not update_write_marker(0, started_at.isoformat(), unit=unit):
        raise UpdateMarkerBusy("another update or rollback is already running")
    attempted: list[str] = []

    for label, command in update_systemd_run_inline_attempts(
        unit=unit,
        root=root,
        log_path=log_path,
        inline_script=inline_script,
    ):
        attempted.append(label)
        result = update_run_systemd_command(command, env, root)
        if result.returncode == 0:
            return unit, attempted
        update_log_attempt_failure(log_fh, label, result)

    # Every attempt failed; clear the marker so a follow-up trigger isn't
    # blocked by a phantom lock.
    try:
        update_update_marker_path().unlink()
    except OSError:
        pass
    return None, attempted


def _schedule_marker_cleanup_when_done(
    runtime: _MarkerCleanupRuntime,
    proc: subprocess.Popen[bytes],
) -> asyncio.Task[None]:
    return runtime.schedule(proc, _cleanup_marker_when_done)


async def _cleanup_marker_when_done(proc: subprocess.Popen[bytes]) -> None:
    await cleanup_marker_when_done(
        proc,
        read_marker_fn=update_read_marker,
        marker_path_fn=update_update_marker_path,
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

    lock_service = SystemOperationLockService(
        fallback_busy=lambda: (
            update_read_marker() is not None or maintenance_marker_busy()
        )
    )
    lock = None
    try:
        lock = await lock_service.acquire(
            operation="rollback", owner=str(admin.id), ttl_sec=1800
        )
    except LockBusy:
        raise _http(
            "update_running",
            "Lumen update, rollback, backup, or restore is already running; wait for it to finish first",
            409,
        )

    release_root = update_lumen_root()
    launched = False
    release_reason = "launch_failed"
    try:
        release_dir = await asyncio.to_thread(
            update_resolve_release,
            release_root,
            target_id,
        )
        if release_dir is None:
            release_reason = "release_not_found"
            raise _http("release_not_found", f"release '{target_id}' not found", 404)

        releases = await asyncio.to_thread(update_list_releases, limit=None)
        try:
            target, expected_head, db_head = await _validate_rollback_target(
                release_dir=release_dir,
                target_id=target_id,
                releases=releases,
                db=db,
            )
        except RollbackGateError as exc:
            release_reason = exc.code
            raise _http(
                exc.code,
                exc.message,
                exc.status_code,
                details=exc.details,
            ) from exc

        started_at = datetime.now(timezone.utc)
        inline_script = _build_rollback_script(
            target_id=target_id,
            lumen_root=release_root,
        )

        log_fh = update_open_update_log()
        unit: str | None = None
        pid: int | None = None
        proc: subprocess.Popen[bytes] | None = None
        try:
            log_fh.write(
                "\n=== update trigger "  # use the same delimiter the parser expects
                f"at={started_at.isoformat()} user={admin.id} mode=rollback target={target_id} ===\n"
            )
            log_fh.flush()

            if update_systemd_run_available():
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
                proc = subprocess.Popen(
                    ["/usr/bin/env", "bash", "-lc", inline_script],
                    cwd=str(release_root),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
                pid = proc.pid
                if not update_write_marker(proc.pid, started_at.isoformat()):
                    _kill_launched_script(proc)
                    raise UpdateMarkerBusy(
                        "another update or rollback is already running"
                    )
        except UpdateMarkerBusy:
            raise _http(
                "update_running",
                "Lumen update, rollback, backup, or restore is already running; "
                "wait for it to finish first",
                409,
            ) from None
        finally:
            log_fh.close()

        if proc is not None:
            _schedule_marker_cleanup_when_done(
                _marker_cleanup_runtime(request),
                proc,
            )
        if unit is not None or pid:
            launched = True
            release_reason = "launched"

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
            raise _http(
                "rollback_launch_failed", "could not launch rollback runner", 500
            )

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
    finally:
        if lock is not None:
            await lock_service.release(lock, succeeded=launched, reason=release_reason)


@update_router.post(
    "/rollback-previous",
    response_model=RollbackOut,
    dependencies=[Depends(verify_csrf)],
)
async def rollback_previous_release(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RollbackOut:
    releases = await asyncio.to_thread(update_list_releases, limit=None)
    previous = next((r for r in releases if r.is_previous), None)
    if previous is None:
        raise _http("no_previous", "no previous release is available", 409)
    return await rollback_release(
        RollbackIn(release_id=previous.id),
        request,
        admin,
        db,
    )


__all__ = [
    "router",
    "update_router",
    "RollbackIn",
    "RollbackOut",
    "RollbackGateError",
    "RollbackSchemaUnknown",
    "_build_rollback_script",
    "_read_db_alembic_heads",
    "_strict_release_expected_heads",
    "_validate_rollback_target",
]
