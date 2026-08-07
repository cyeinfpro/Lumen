#!/usr/bin/env python3
"""Validate an API-authored update request before invoking update.sh as root."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from maintenance_marker_lock import marker_lock

_DEFAULT_REQUEST = Path("/opt/lumendata/backup/.update.request.json")
_DEFAULT_SCRIPT = Path("/opt/lumen/current/scripts/update.sh")
_DEFAULT_JOURNAL = Path("/opt/lumen/shared/.update-journal.json")
_DEFAULT_RECOVERY_MARKER = Path("/opt/lumen/shared/.update-resume")
_DEFAULT_TRIGGER = Path("/opt/lumendata/backup/.update.trigger")
_DEFAULT_RUNNING = Path("/opt/lumendata/backup/.update.running")
_DEFAULT_CLAIM = Path("/opt/lumen/shared/.update-claim.json")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_MAX_RUNTIME_BYTES = 64 * 1024
_ALLOWED_FIELDS = {
    "schema",
    "target_tag",
    "channel",
    "force_redeploy",
    "idempotency_key",
    "proxy_url",
    "issued_at",
}
_TAG_RE = re.compile(
    r"^(?:v[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z.-]+)?|main)$"
)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_CHANNELS = {"stable", "main", "pinned", "minor", "major"}
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_MAX_REQUEST_AGE = timedelta(minutes=5)
_ACTIVE_STATUSES = {"running", "failed"}
_TERMINAL_STATUSES = {"complete", "manual_required", "rolled_back"}
_CONSUMABLE_STATUSES = {"complete", "rolled_back"}


class UpdateRequestError(ValueError):
    pass


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateRequestError("request is not a regular file")
        if info.st_size <= 0 or info.st_size > _MAX_REQUEST_BYTES:
            raise UpdateRequestError("request size is invalid")
        data = os.read(fd, _MAX_REQUEST_BYTES + 1)
        if len(data) != info.st_size:
            raise UpdateRequestError("request changed while being read")
        return data
    finally:
        os.close(fd)


def _validated_proxy_url(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise UpdateRequestError("proxy_url is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise UpdateRequestError("proxy_url contains control characters")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in _PROXY_SCHEMES or not parsed.hostname:
        raise UpdateRequestError("proxy_url scheme or host is invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise UpdateRequestError("proxy_url must not contain path, query, or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UpdateRequestError("proxy_url port is invalid") from exc
    return raw


def load_request(
    path: Path,
    *,
    allow_stale: bool = False,
) -> dict[str, object]:
    try:
        payload = json.loads(_read_regular_file(path))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateRequestError("cannot read update request") from exc
    if not isinstance(payload, dict):
        raise UpdateRequestError("request must be an object")
    extra = set(payload) - _ALLOWED_FIELDS
    missing = _ALLOWED_FIELDS - set(payload)
    if extra or missing:
        raise UpdateRequestError("request fields do not match schema")
    if payload.get("schema") != 1:
        raise UpdateRequestError("unsupported request schema")

    target_tag = payload.get("target_tag")
    channel = payload.get("channel")
    idempotency_key = payload.get("idempotency_key")
    issued_at = payload.get("issued_at")
    force_redeploy = payload.get("force_redeploy")
    if not isinstance(target_tag, str) or not _TAG_RE.fullmatch(target_tag):
        raise UpdateRequestError("target_tag is invalid")
    if not isinstance(channel, str) or channel not in _CHANNELS:
        raise UpdateRequestError("channel is invalid")
    if (
        not isinstance(idempotency_key, str)
        or not _IDEMPOTENCY_RE.fullmatch(idempotency_key)
    ):
        raise UpdateRequestError("idempotency_key is invalid")
    if not isinstance(force_redeploy, bool):
        raise UpdateRequestError("force_redeploy must be boolean")
    if not isinstance(issued_at, str):
        raise UpdateRequestError("issued_at is invalid")
    try:
        issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateRequestError("issued_at is invalid") from exc
    if issued.tzinfo is None:
        raise UpdateRequestError("issued_at must include a timezone")
    age = datetime.now(timezone.utc) - issued.astimezone(timezone.utc)
    if not allow_stale and (
        age < -timedelta(minutes=1) or age > _MAX_REQUEST_AGE
    ):
        raise UpdateRequestError("request is stale")

    return {
        "target_tag": target_tag,
        "channel": channel,
        "force_redeploy": force_redeploy,
        "idempotency_key": idempotency_key,
        "proxy_url": _validated_proxy_url(payload.get("proxy_url")),
        "issued_at": issued_at,
    }


def load_journal(path: Path) -> dict[str, object] | None:
    try:
        raw = _read_regular_file_with_limit(path, _MAX_JOURNAL_BYTES)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateRequestError("cannot read update journal") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateRequestError("update journal is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 2:
        raise UpdateRequestError("update journal schema is invalid")
    status = payload.get("status")
    if status not in _ACTIVE_STATUSES | _TERMINAL_STATUSES:
        raise UpdateRequestError("update journal status is invalid")
    return payload


def _read_regular_file_with_limit(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > limit:
            raise UpdateRequestError("state file size or type is invalid")
        data = os.read(fd, limit + 1)
        if len(data) != info.st_size:
            raise UpdateRequestError("state file changed while being read")
        return data
    finally:
        os.close(fd)


def journal_is_active(payload: dict[str, object] | None) -> bool:
    return payload is not None and payload.get("status") in _ACTIVE_STATUSES


def _request_contract(request: dict[str, object]) -> dict[str, object]:
    key = str(request["idempotency_key"]).encode("utf-8")
    return {
        "channel": request["channel"],
        "resolved_tag": request["target_tag"],
        "force_redeploy": request["force_redeploy"],
        "idempotency_key_sha256": hashlib.sha256(key).hexdigest(),
    }


def journal_request_matches(
    payload: dict[str, object],
    request: dict[str, object],
) -> bool:
    journal_request = payload.get("request")
    return isinstance(journal_request, dict) and journal_request == _request_contract(
        request
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(descriptor)


def _atomic_write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
    ):
        raise UpdateRequestError("runtime claim destination is unsafe")
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        encoded = (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        view = memoryview(encoded)
        os.fchmod(descriptor, 0o600)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting update runner claim")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _private_json(path: Path, limit: int) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > limit
        ):
            raise UpdateRequestError("runtime claim is invalid")
        raw = os.read(descriptor, limit + 1)
        if len(raw) != info.st_size:
            raise UpdateRequestError("runtime claim changed while being read")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateRequestError("runtime claim is invalid") from exc
    if not isinstance(payload, dict):
        raise UpdateRequestError("runtime claim is invalid")
    return payload


def _file_sha256(path: Path, limit: int = _MAX_RUNTIME_BYTES) -> str | None:
    try:
        raw = _read_regular_file_with_limit(path, limit)
    except FileNotFoundError:
        return None
    return hashlib.sha256(raw).hexdigest()


def _runtime_artifact_claim(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": _file_sha256(path)}


def write_runtime_claim(
    claim_path: Path,
    request: dict[str, object],
    request_path: Path,
    trigger_path: Path,
    running_path: Path,
) -> None:
    _atomic_write_private_json(
        claim_path,
        {
            "schema": 1,
            "request": _request_contract(request),
            "artifacts": {
                "request": _runtime_artifact_claim(request_path),
                "trigger": _runtime_artifact_claim(trigger_path),
                "running": _runtime_artifact_claim(running_path),
            },
        },
    )


def load_runtime_claim(path: Path) -> dict[str, object]:
    payload = _private_json(path, _MAX_RUNTIME_BYTES)
    if payload.get("schema") != 1:
        raise UpdateRequestError("runtime claim schema is invalid")
    request = payload.get("request")
    artifacts = payload.get("artifacts")
    if not isinstance(request, dict) or not isinstance(artifacts, dict):
        raise UpdateRequestError("runtime claim contract is invalid")
    return payload


def archive_terminal_journal(
    journal_path: Path,
    payload: dict[str, object],
) -> Path:
    if payload.get("status") not in _TERMINAL_STATUSES:
        raise UpdateRequestError("only terminal journals can be archived")
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise UpdateRequestError("terminal journal operation_id is invalid")
    safe_operation = re.sub(r"[^A-Za-z0-9._-]+", "_", operation_id)[:120]
    if not safe_operation:
        raise UpdateRequestError("terminal journal operation_id is invalid")
    journal_raw = _read_regular_file_with_limit(journal_path, _MAX_JOURNAL_BYTES)
    journal_digest = hashlib.sha256(journal_raw).hexdigest()
    request = payload.get("request")
    request_digest = "unbound"
    if isinstance(request, dict):
        candidate = request.get("idempotency_key_sha256")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            request_digest = candidate[:16]
    archive_dir = Path(
        os.environ.get(
            "LUMEN_UPDATE_JOURNAL_ARCHIVE",
            str(journal_path.parent / ".update-journal-archive"),
        )
    )
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_info = archive_dir.lstat()
    if (
        not stat.S_ISDIR(archive_info.st_mode)
        or archive_info.st_uid != os.geteuid()
        or stat.S_IMODE(archive_info.st_mode) != 0o700
    ):
        raise UpdateRequestError("update journal archive directory is unsafe")
    target = archive_dir / (
        f"{safe_operation}.{request_digest}.{payload['status']}."
        f"{journal_digest[:16]}.json"
    )
    try:
        existing_digest = _file_sha256(target, _MAX_JOURNAL_BYTES)
    except UpdateRequestError as exc:
        raise UpdateRequestError("existing update journal archive is unsafe") from exc
    if existing_digest is not None:
        if existing_digest != journal_digest:
            raise UpdateRequestError("update journal archive identity collision")
        journal_path.unlink()
    else:
        os.replace(journal_path, target)
        os.chmod(target, 0o600)
    _fsync_directory(archive_dir)
    _fsync_directory(journal_path.parent)
    return target


def build_environment(request: dict[str, object]) -> dict[str, str]:
    target_tag = str(request["target_tag"])
    env = {
        "HOME": "/root",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LUMEN_UPDATE_NONINTERACTIVE": "1",
        "LUMEN_UPDATE_MODE": "fast",
        "LUMEN_UPDATE_FAST_BACKUP": "1",
        "LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP": "1",
        "LUMEN_UPDATE_GIT_PULL": "1",
        "LUMEN_UPDATE_BUILD": "0",
        "LUMEN_UPDATE_CHANNEL": str(request["channel"]),
        "LUMEN_UPDATE_RESOLVED_TAG": target_tag,
        "LUMEN_UPDATE_IDEMPOTENCY_KEY": str(request["idempotency_key"]),
        "LUMEN_IMAGE_TAG": target_tag,
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    version_match = re.fullmatch(
        r"v([0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?)",
        target_tag,
    )
    if version_match:
        env["LUMEN_VERSION"] = version_match.group(1)
    if request["force_redeploy"]:
        env["LUMEN_UPDATE_FORCE_REDEPLOY"] = "1"
    proxy_url = request.get("proxy_url")
    if isinstance(proxy_url, str):
        for key in (
            "LUMEN_UPDATE_PROXY_URL",
            "LUMEN_HTTP_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env[key] = proxy_url
    return env


def _runtime_paths() -> tuple[Path, Path, Path, Path, Path, Path]:
    return (
        Path(os.environ.get("LUMEN_UPDATE_REQUEST", _DEFAULT_REQUEST)),
        Path(os.environ.get("LUMEN_UPDATE_JOURNAL", _DEFAULT_JOURNAL)),
        Path(
            os.environ.get(
                "LUMEN_UPDATE_RECOVERY_MARKER",
                _DEFAULT_RECOVERY_MARKER,
            )
        ),
        Path(os.environ.get("LUMEN_UPDATE_TRIGGER", _DEFAULT_TRIGGER)),
        Path(os.environ.get("LUMEN_UPDATE_RUNNING", _DEFAULT_RUNNING)),
        Path(os.environ.get("LUMEN_UPDATE_CLAIM", _DEFAULT_CLAIM)),
    )


def _unlink_claimed_artifact(
    claim: dict[str, object],
    name: str,
    expected_path: Path,
) -> None:
    if name == "running":
        with marker_lock(expected_path.parent):
            try:
                values: dict[str, str] = {}
                for line in expected_path.read_text(encoding="utf-8").splitlines():
                    key, sep, value = line.partition("=")
                    if sep:
                        values[key] = value.strip()
                if values.get("owner") == "host" and int(
                    values.get("generation", "0")
                ) >= 1:
                    expected_path.unlink()
                    _fsync_directory(expected_path.parent)
                    return
            except (FileNotFoundError, OSError, ValueError):
                pass
    artifacts = claim.get("artifacts")
    if not isinstance(artifacts, dict):
        raise UpdateRequestError("runtime claim artifacts are invalid")
    record = artifacts.get(name)
    if not isinstance(record, dict) or record.get("path") != str(expected_path):
        raise UpdateRequestError(f"runtime claim {name} path is invalid")
    expected_digest = record.get("sha256")
    if expected_digest is None:
        return
    if not isinstance(expected_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        expected_digest,
    ):
        raise UpdateRequestError(f"runtime claim {name} digest is invalid")
    current_digest = _file_sha256(expected_path)
    if current_digest != expected_digest:
        if current_digest is not None:
            print(
                f"update runner cleanup preserved changed {name} artifact",
                file=sys.stderr,
            )
        return
    expected_path.unlink()
    _fsync_directory(expected_path.parent)


def cleanup_runtime_files() -> int:
    (
        request_path,
        journal_path,
        marker_path,
        trigger_path,
        running_path,
        claim_path,
    ) = _runtime_paths()
    try:
        journal = load_journal(journal_path)
    except UpdateRequestError as exc:
        print(f"update runner cleanup preserved recovery state: {exc}", file=sys.stderr)
        return 0
    if journal is None or journal_is_active(journal):
        print("update runner cleanup preserved active request state", file=sys.stderr)
        return 0
    if journal.get("status") not in _CONSUMABLE_STATUSES:
        print(
            "update runner cleanup preserved manual recovery state",
            file=sys.stderr,
        )
        return 0
    try:
        claim = load_runtime_claim(claim_path)
    except FileNotFoundError:
        print("update runner cleanup found no consumed-request claim", file=sys.stderr)
        return 0
    except (OSError, UpdateRequestError) as exc:
        print(f"update runner cleanup preserved request state: {exc}", file=sys.stderr)
        return 0
    claimed_request = claim.get("request")
    journal_request = journal.get("request")
    if not isinstance(claimed_request, dict) or journal_request != claimed_request:
        print(
            "update runner cleanup preserved request with different journal identity",
            file=sys.stderr,
        )
        return 0
    try:
        _unlink_claimed_artifact(claim, "request", request_path)
        _unlink_claimed_artifact(claim, "trigger", trigger_path)
        _unlink_claimed_artifact(claim, "running", running_path)
    except (OSError, UpdateRequestError) as exc:
        print(f"update runner cleanup could not consume claimed state: {exc}", file=sys.stderr)
        return 0
    operation_id = journal.get("operation_id")
    if isinstance(operation_id, str) and operation_id:
        try:
            marker_raw = _read_regular_file_with_limit(marker_path, _MAX_RUNTIME_BYTES)
        except FileNotFoundError:
            pass
        except (OSError, UpdateRequestError) as exc:
            print(
                f"update runner cleanup preserved recovery marker: {exc}",
                file=sys.stderr,
            )
        else:
            if marker_raw == f"{operation_id}\n".encode("utf-8"):
                marker_path.unlink()
                _fsync_directory(marker_path.parent)
    try:
        claim_path.unlink()
        _fsync_directory(claim_path.parent)
    except FileNotFoundError:
        pass
    return 0


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _load_current_request(
    request_path: Path,
    *,
    allow_stale: bool,
) -> dict[str, object] | None:
    try:
        return load_request(request_path, allow_stale=allow_stale)
    except FileNotFoundError:
        return None


def _adopt_running_marker_unlocked(path: Path) -> str | None:
    """Adopt the API marker before reading or mutating slow update state."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateRequestError("cannot read update ownership marker") from exc
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    operation_id = values.get("operation_id")
    try:
        generation = int(values.get("generation", "0"))
    except ValueError as exc:
        raise UpdateRequestError("update ownership generation is invalid") from exc
    if values.get("owner") != "api" or generation != 0 or not operation_id:
        return None
    values["owner"] = "host"
    values["generation"] = str(generation + 1)
    values["pid"] = str(os.getpid())
    values["adopted_at"] = datetime.now(timezone.utc).isoformat()
    payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o660)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
    return operation_id


def adopt_running_marker(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    with marker_lock(path.parent):
        return _adopt_running_marker_unlocked(path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--cleanup"]:
        return cleanup_runtime_files()
    if args:
        print("update runner rejected request: unexpected arguments", file=sys.stderr)
        return 2
    (
        request_path,
        journal_path,
        _marker_path,
        trigger_path,
        running_path,
        claim_path,
    ) = _runtime_paths()
    update_script = Path(os.environ.get("LUMEN_UPDATE_SCRIPT", _DEFAULT_SCRIPT))
    try:
        adopt_running_marker(running_path)
        journal = load_journal(journal_path)
        active = journal_is_active(journal)
        request = _load_current_request(request_path, allow_stale=active)
        if active:
            if request is None:
                raise UpdateRequestError(
                    "active journal requires the preserved update request"
                )
            if (
                isinstance(journal, dict)
                and journal.get("request") is not None
                and not journal_request_matches(journal, request)
            ):
                raise UpdateRequestError(
                    "active journal belongs to a different update request"
                )
        elif journal is not None:
            if request is None:
                if _path_exists(trigger_path) or _path_exists(running_path):
                    raise UpdateRequestError(
                        "terminal journal has incomplete pending request state"
                    )
                print("update runner found only a terminal journal; no request pending")
                return 0
            if journal_request_matches(journal, request):
                write_runtime_claim(
                    claim_path,
                    request,
                    request_path,
                    trigger_path,
                    running_path,
                )
                print("update runner found an already-consumed request")
                return 0
            archived = archive_terminal_journal(journal_path, journal)
            print(f"update runner archived terminal journal at {archived}", flush=True)
            journal = None
            active = False
        if request is None:
            raise UpdateRequestError("cannot read update request")
        script_info = update_script.stat()
        if not stat.S_ISREG(script_info.st_mode):
            raise UpdateRequestError("update script is not a regular file")
        write_runtime_claim(
            claim_path,
            request,
            request_path,
            trigger_path,
            running_path,
        )
    except (OSError, UpdateRequestError) as exc:
        print(f"update runner rejected request: {exc}", file=sys.stderr)
        return 2
    environment = build_environment(request)
    if active:
        environment["LUMEN_UPDATE_RESUME"] = "1"
        environment["LUMEN_UPDATE_JOURNAL"] = str(journal_path)
        environment["LUMEN_UPDATE_RECOVERY_MARKER"] = str(
            Path(
                os.environ.get(
                    "LUMEN_UPDATE_RECOVERY_MARKER",
                    _DEFAULT_RECOVERY_MARKER,
                )
            )
        )
    os.execve(
        "/usr/bin/env",
        ["/usr/bin/env", "bash", str(update_script)],
        environment,
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
