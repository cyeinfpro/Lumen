#!/usr/bin/env python3
"""Atomically publish staged installer trees under durable transaction intent."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

WATCHED_SIGNALS = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
TOKEN_NAME = ".lumen-bootstrap-transaction"
INTENT_NAME = "intent.json"
VALIDATED_NAME = "validated"
TRACE_ENV = "LUMEN_BOOTSTRAP_TRACE_FILE"
FAILPOINT_ENV = "LUMEN_BOOTSTRAP_FAILPOINT"


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(fd)


def fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"cannot fsync non-regular file: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    for directory, names, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        for name in files:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError(f"staged tree contains unsupported entry: {path}")
            fsync_regular_file(path)
        for name in names:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError(f"staged tree contains unsupported entry: {path}")
        fsync_directory(base)


def trace(event: str) -> None:
    raw = os.environ.get(TRACE_ENV, "")
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{event}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def failpoint(name: str) -> None:
    configured = os.environ.get(FAILPOINT_ENV, "")
    if configured == name:
        raise RuntimeError(f"bootstrap failpoint: {name}")
    if configured == f"sigkill:{name}":
        os.kill(os.getpid(), signal.SIGKILL)


def atomic_exchange(left: Path, right: Path) -> None:
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = function(left_bytes, right_bytes, 2)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = function(-100, left_bytes, -100, right_bytes, 2)
    else:
        raise OSError(errno.ENOTSUP, "atomic directory exchange unsupported")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    fsync_directory(left.parent)
    if right.parent != left.parent:
        fsync_directory(right.parent)


def exchange_with_signals_blocked(left: Path, right: Path) -> None:
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, WATCHED_SIGNALS)
    try:
        atomic_exchange(left, right)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def validate_scripts_tree(root: Path) -> None:
    required = (
        "install.sh",
        "lib.sh",
        "install/bootstrap_transaction.py",
        "update/entry_lock.py",
    )
    for relative in required:
        path = root / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError(f"staged scripts missing regular file: {relative}")

    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise OSError(f"staged scripts contain unsupported entry: {path}")
        for name in files:
            path = Path(directory) / name
            if name.endswith(".sh"):
                subprocess.run(["bash", "-n", path], check=True)
            elif name.endswith(".py"):
                compile(path.read_bytes(), str(path), "exec")


def tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(info.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"d\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"f\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise OSError(f"unsupported fingerprint entry: {path}")
        digest.update(b"\0")
    return digest.hexdigest()


def same_open_file(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_uid == os.geteuid()
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    )


def lock_timeout() -> float:
    try:
        return max(
            0.0,
            float(os.environ.get("LUMEN_SELF_UPDATE_LOCK_TIMEOUT", "60")),
        )
    except ValueError:
        return 60.0


def acquire_lock_path(
    lock_path: Path,
    *,
    inherited: bool = False,
) -> int:
    if inherited:
        inherited_raw = os.environ.get("LUMEN_SCRIPT_UNIT_LOCK_FD", "")
        inherited_path = os.environ.get("LUMEN_SCRIPT_UNIT_LOCK_PATH", "")
        try:
            inherited_fd = int(inherited_raw)
        except ValueError:
            inherited_fd = -1
        if inherited_path == str(lock_path) and same_open_file(
            inherited_fd, lock_path
        ):
            fcntl.flock(inherited_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(inherited_fd, True)
            return inherited_fd

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(lock_path, flags, 0o600)
    if not same_open_file(fd, lock_path):
        os.close(fd)
        raise OSError("transaction lock path is unsafe")
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + lock_timeout()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise SystemExit(75)
            time.sleep(0.05)
    os.set_inheritable(fd, True)
    return fd


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def copy_path(source: Path, target: Path) -> None:
    remove_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, target)


def source_ignore(directory: str, names: list[str]) -> set[str]:
    path = Path(directory)
    ignored = {
        name
        for name in names
        if name in {".git", ".venv", "node_modules", "__pycache__"}
    }
    if path.name == "worker":
        ignored.add("var")
    if path.name == "web":
        ignored.update({".next", "node_modules", ".env.local"})
    return ignored


def stage_inplace_repository(source: Path, active: Path, stage: Path) -> None:
    shutil.copytree(
        source,
        stage,
        symlinks=True,
        copy_function=shutil.copy2,
        ignore=source_ignore,
    )
    preserve = (
        "shared",
        "releases",
        "current",
        "previous",
        "var",
        ".update.log",
        ".install-logs",
        "apps/worker/var",
        "apps/web/.next",
        "apps/web/.env.local",
        "apps/web/node_modules",
    )
    for relative in preserve:
        current = active / relative
        if current.exists() or current.is_symlink():
            copy_path(current, stage / relative)
    for current in active.glob(".env*"):
        if current.name == ".env.example":
            continue
        copy_path(current, stage / current.name)


def write_durable_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = (json.dumps(payload, sort_keys=True) + "\n").encode("ascii")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def write_durable_marker(path: Path, value: str) -> None:
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(fd, f"{value}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def read_intent(transaction: Path) -> dict[str, object]:
    payload = json.loads((transaction / INTENT_NAME).read_text(encoding="ascii"))
    if payload.get("schema") != 1 or payload.get("kind") not in {
        "scripts",
        "inplace",
    }:
        raise OSError("invalid bootstrap transaction intent")
    active = Path(str(payload.get("active", "")))
    staged = Path(str(payload.get("staged", "")))
    if not active.is_absolute() or staged.parent != transaction:
        raise OSError("bootstrap transaction paths are invalid")
    if payload.get("token") is None or payload.get("fingerprint") is None:
        raise OSError("bootstrap transaction intent is incomplete")
    return payload


def validation_root(intent: dict[str, object], tree: Path) -> Path:
    return tree if intent["kind"] == "scripts" else tree / "scripts"


def remove_transaction(transaction: Path) -> None:
    parent = transaction.parent
    shutil.rmtree(transaction, ignore_errors=False)
    fsync_directory(parent)


def recover_transaction(transaction: Path, *, acquire_locks: bool) -> None:
    transaction = transaction.resolve()
    info = transaction.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
    ):
        raise OSError("unsafe bootstrap transaction directory")
    intent_path = transaction / INTENT_NAME
    if not intent_path.exists():
        remove_transaction(transaction)
        return

    intent = read_intent(transaction)
    active = Path(str(intent["active"]))
    staged = Path(str(intent["staged"]))
    token = str(intent["token"])
    lock_fds: list[int] = []
    try:
        if acquire_locks:
            for raw in intent.get("lock_paths", []):
                lock_fds.append(acquire_lock_path(Path(str(raw))))
        if (transaction / VALIDATED_NAME).is_file():
            marker = validation_root(intent, active) / TOKEN_NAME
            if marker.is_file():
                marker.unlink()
                fsync_directory(marker.parent)
            if staged.exists():
                remove_path(staged)
                fsync_directory(transaction)
            trace("old_tree_removed")
            remove_transaction(transaction)
            return

        active_marker = validation_root(intent, active) / TOKEN_NAME
        staged_marker = validation_root(intent, staged) / TOKEN_NAME
        active_has_token = (
            active_marker.is_file()
            and active_marker.read_text(encoding="ascii").strip() == token
        )
        staged_has_token = (
            staged_marker.is_file()
            and staged_marker.read_text(encoding="ascii").strip() == token
        )
        if active_has_token and staged.exists():
            exchange_with_signals_blocked(active, staged)
            trace("rollback_exchange_complete")
        elif not staged_has_token:
            raise OSError("cannot determine bootstrap transaction swap state")
        remove_transaction(transaction)
    finally:
        for fd in reversed(lock_fds):
            os.close(fd)


def accept_transaction(transaction: Path) -> None:
    transaction = transaction.resolve()
    intent = read_intent(transaction)
    active = Path(str(intent["active"]))
    root = validation_root(intent, active)
    token_path = root / TOKEN_NAME
    if (
        not token_path.is_file()
        or token_path.read_text(encoding="ascii").strip() != intent["token"]
    ):
        raise OSError("active bootstrap tree is not transaction-bound")
    validate_scripts_tree(root)
    if tree_fingerprint(root) != intent["fingerprint"]:
        raise OSError("active bootstrap tree changed before validation")

    write_durable_marker(
        transaction / VALIDATED_NAME,
        str(intent["token"]),
    )
    trace("validation_durable")
    failpoint("after_validation")

    token_path.unlink()
    fsync_directory(token_path.parent)
    staged = Path(str(intent["staged"]))
    if staged.exists():
        remove_path(staged)
        fsync_directory(transaction)
    trace("old_tree_removed")
    remove_transaction(transaction)


def commit_staged_tree(active: Path, staged: Path, transaction: Path) -> None:
    active = active.absolute()
    staged = staged.absolute()
    transaction = transaction.absolute()
    if staged.parent != transaction or active.is_symlink() or not active.is_dir():
        raise OSError("invalid staged tree transaction paths")
    token = secrets.token_hex(16)
    helper_copy = transaction / "bootstrap_transaction.py"
    shutil.copy2(Path(__file__), helper_copy)
    fsync_regular_file(helper_copy)
    (staged / TOKEN_NAME).write_text(f"{token}\n", encoding="ascii")
    fsync_tree(staged)
    trace("stage_fsync_complete")
    failpoint("after_stage_fsync")

    fingerprint = tree_fingerprint(staged)
    intent = {
        "schema": 1,
        "kind": "scripts",
        "active": str(active),
        "staged": str(staged),
        "token": token,
        "fingerprint": fingerprint,
        "lock_paths": [f"{active.resolve()}.lumen-self-update.lock"],
    }
    write_durable_json(transaction / INTENT_NAME, intent)
    trace("intent_durable")
    failpoint("after_intent")

    exchange_with_signals_blocked(active, staged)
    trace("exchange_complete")
    failpoint("after_exchange")
    active_token = active / TOKEN_NAME
    if (
        not active_token.is_file()
        or active_token.read_text(encoding="ascii").strip() != token
        or tree_fingerprint(active) != fingerprint
    ):
        raise OSError("post-swap staged tree validation failed")

    write_durable_marker(transaction / VALIDATED_NAME, token)
    trace("validation_durable")
    failpoint("after_validation")
    active_token.unlink()
    fsync_directory(active)
    remove_path(staged)
    fsync_directory(transaction)
    trace("old_tree_removed")
    remove_transaction(transaction)


def run_child(
    command: list[str],
    env: dict[str, str],
    *,
    pass_fds: tuple[int, ...],
    raw_stdin: bool,
) -> int:
    if raw_stdin and not os.isatty(0):
        while os.read(0, 65536):
            pass
    stdin: int | None = None
    try:
        stdin = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        pass
    child = subprocess.Popen(
        command,
        env=env,
        stdin=stdin,
        pass_fds=pass_fds,
    )
    if stdin is not None:
        os.close(stdin)

    saved_handlers: dict[int, object] = {}

    def forward(signum: int, _frame: object) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for watched in WATCHED_SIGNALS:
        saved_handlers[watched] = signal.getsignal(watched)
        signal.signal(watched, forward)
    try:
        result = child.wait()
    finally:
        for watched, handler in saved_handlers.items():
            signal.signal(watched, handler)
    return 128 - result if result < 0 else result


def publish_and_run(kind: str, argv: list[str]) -> int:
    if len(argv) < 6 or argv[4] != "--":
        return 2
    source = Path(argv[0]).resolve()
    active = Path(argv[1]).absolute()
    clone_tmp = Path(argv[2])
    raw_stdin = argv[3] == "1"
    command = argv[5:]
    if active.is_symlink() or not active.is_dir():
        raise OSError("active bootstrap target is not a real directory")

    lock_fds: list[int] = []
    lock_paths: list[Path] = []
    inherited_fd: int | None = None
    if kind == "scripts":
        lock_path = Path(f"{active.resolve()}.lumen-self-update.lock")
        inherited_fd = acquire_lock_path(lock_path, inherited=True)
        lock_fds.append(inherited_fd)
        lock_paths.append(lock_path)
    else:
        root_lock = Path(f"{active.resolve()}.lumen-bootstrap.lock")
        lock_fds.append(acquire_lock_path(root_lock))
        lock_paths.append(root_lock)

    transaction = Path(
        tempfile.mkdtemp(
            prefix=f".{active.name}.bootstrap.",
            dir=active.parent,
        )
    )
    fsync_directory(active.parent)
    stage = transaction / "staged"
    token = secrets.token_hex(16)
    saved_handlers: dict[int, object] = {}

    def abort(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for watched in WATCHED_SIGNALS:
        saved_handlers[watched] = signal.getsignal(watched)
        signal.signal(watched, abort)
    try:
        helper_copy = transaction / "bootstrap_transaction.py"
        shutil.copy2(Path(__file__), helper_copy)
        fsync_regular_file(helper_copy)
        fsync_directory(transaction)
        if kind == "scripts":
            shutil.copytree(
                source,
                stage,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            scripts_root = stage
        else:
            stage_inplace_repository(source, active, stage)
            scripts_root = stage / "scripts"
        validate_scripts_tree(scripts_root)
        (scripts_root / TOKEN_NAME).write_text(f"{token}\n", encoding="ascii")
        fsync_tree(stage)
        trace("stage_fsync_complete")
        failpoint("after_stage_fsync")

        intent = {
            "schema": 1,
            "kind": kind,
            "active": str(active),
            "staged": str(stage),
            "token": token,
            "fingerprint": tree_fingerprint(scripts_root),
            "lock_paths": [str(path) for path in lock_paths],
        }
        write_durable_json(transaction / INTENT_NAME, intent)
        trace("intent_durable")
        failpoint("after_intent")

        exchange_with_signals_blocked(active, stage)
        trace("exchange_complete")
        failpoint("after_exchange")

        env = os.environ.copy()
        env["LUMEN_RAW_BOOTSTRAP_TRANSACTION"] = str(transaction)
        if kind == "scripts" and inherited_fd is not None:
            env["LUMEN_SCRIPT_UNIT_LOCK_FD"] = str(inherited_fd)
            env["LUMEN_SCRIPT_UNIT_LOCK_PATH"] = str(lock_paths[0])
            pass_fds = (inherited_fd,)
        else:
            env.pop("LUMEN_SCRIPT_UNIT_LOCK_FD", None)
            env.pop("LUMEN_SCRIPT_UNIT_LOCK_PATH", None)
            pass_fds = ()
        result = run_child(
            command,
            env,
            pass_fds=pass_fds,
            raw_stdin=raw_stdin,
        )
        if transaction.exists():
            validated = (transaction / VALIDATED_NAME).is_file()
            recover_transaction(transaction, acquire_locks=False)
            if result == 0 and not validated:
                return 70
        return result
    except BaseException:
        if transaction.exists():
            try:
                recover_transaction(transaction, acquire_locks=False)
            except BaseException:
                pass
        raise
    finally:
        for watched, handler in saved_handlers.items():
            signal.signal(watched, handler)
        shutil.rmtree(clone_tmp, ignore_errors=True)
        for fd in reversed(lock_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "exchange":
        if len(sys.argv) != 4:
            return 2
        exchange_with_signals_blocked(
            Path(sys.argv[2]).resolve(),
            Path(sys.argv[3]).resolve(),
        )
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "accept":
        accept_transaction(Path(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "recover":
        recover_transaction(Path(sys.argv[2]), acquire_locks=True)
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "recover-held":
        recover_transaction(Path(sys.argv[2]), acquire_locks=False)
        return 0
    if len(sys.argv) == 5 and sys.argv[1] == "commit-staged":
        commit_staged_tree(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
        )
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "publish":
        return publish_and_run("scripts", sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "publish-inplace":
        return publish_and_run("inplace", sys.argv[2:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
