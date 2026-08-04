from __future__ import annotations

from contextlib import contextmanager
import fcntl
import grp
import hashlib
import importlib.util
import os
import pwd
import secrets
import shlex
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS = ROOT / "scripts" / "backup_permissions.py"
JOURNAL = ROOT / "scripts" / "restore_journal.py"
LIB = ROOT / "scripts" / "lib.sh"
RUNTIME = ROOT / "scripts" / "lib" / "runtime.sh"
INSTALL_OPERATIONS = ROOT / "scripts" / "install" / "operations.sh"
UPDATE_ACTIVATION = ROOT / "scripts" / "update" / "services" / "release_activation.sh"
MIGRATE = ROOT / "scripts" / "migrate_to_releases.sh"
TIMESTAMP = "20260803-010203"
PERMISSIONS_SPEC = importlib.util.spec_from_file_location(
    "backup_permissions",
    PERMISSIONS,
)
assert PERMISSIONS_SPEC is not None and PERMISSIONS_SPEC.loader is not None
BACKUP_PERMISSIONS = importlib.util.module_from_spec(PERMISSIONS_SPEC)
sys.modules[PERMISSIONS_SPEC.name] = BACKUP_PERMISSIONS
PERMISSIONS_SPEC.loader.exec_module(BACKUP_PERMISSIONS)


def _current_identity() -> tuple[str, str]:
    user = pwd.getpwuid(os.geteuid())
    group = grp.getgrgid(user.pw_gid)
    return user.pw_name, group.gr_name


def _nonroot_identity(*, excluding: set[int] | None = None) -> tuple[str, str] | None:
    excluded = excluding or set()
    for user in pwd.getpwall():
        if user.pw_uid == 0 or user.pw_uid in excluded:
            continue
        try:
            group = grp.getgrgid(user.pw_gid)
        except KeyError:
            continue
        return user.pw_name, group.gr_name
    return None


def _run_permissions(
    backup_root: Path,
    *,
    service_user: str,
    service_group: str,
    legacy_owner_user: str = "root",
    with_lock: bool = True,
) -> subprocess.CompletedProcess[str]:
    maintenance_root = backup_root.parent / f".{backup_root.name}-maintenance"
    maintenance_root.mkdir(exist_ok=True)
    command = [
        sys.executable,
        str(PERMISSIONS),
        "ensure-backup-layout",
        str(backup_root),
        "--service-user",
        service_user,
        "--service-group",
        service_group,
        "--legacy-owner-user",
        legacy_owner_user,
        "--maintenance-lock-root",
        str(maintenance_root),
    ]
    if not with_lock:
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            command() {{
                if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                    return 1
                fi
                builtin command "$@"
            }}
            . {shlex.quote(str(LIB))}
            lumen_acquire_lock {shlex.quote(str(maintenance_root))} permission-test
            lumen_export_borrowed_maintenance_lock \
                {shlex.quote(str(maintenance_root))}
            exec {" ".join(shlex.quote(part) for part in command)}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_journal(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(JOURNAL), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_backup_journal(path: Path, phase: str = "writers_stopping") -> None:
    result = _run_journal(
        "backup-write",
        str(path),
        "--operation-id",
        "backup-permissions-test",
        "--phase",
        phase,
        "--service",
        "api",
        "--service",
        "worker",
    )
    assert result.returncode == 0, result.stderr


def _write_restore_journal(path: Path) -> None:
    result = _run_journal(
        "write",
        str(path),
        "--operation-id",
        "restore-permissions-test",
        "--timestamp",
        TIMESTAMP,
        "--phase",
        "writers_stopping",
        "--pg-db",
        "lumen",
        "--pg-container",
        "lumen-pg",
        "--redis-container",
        "lumen-redis",
        "--redis-state",
        "untouched",
        "--services-stopped",
        "0",
        "--redis-needs-start",
        "0",
        "--pg-swap-in-progress",
        "0",
        "--pg-promoted",
        "0",
    )
    assert result.returncode == 0, result.stderr


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _metadata_with_uid(metadata: os.stat_result, user_id: int) -> os.stat_result:
    values = list(metadata)
    values[4] = user_id
    return os.stat_result(values)


@contextmanager
def _maintenance_lock_environment(
    monkeypatch: pytest.MonkeyPatch,
    maintenance_root: Path,
):
    maintenance_root.mkdir()
    parent = maintenance_root.parent
    anchor_key = hashlib.sha256(
        str(maintenance_root).encode("utf-8")
    ).hexdigest()[:32]
    anchor_dir = parent / f".lumen-maintenance.{anchor_key}.lock.d"
    local_dir = maintenance_root / ".lumen-maintenance.lock.d"
    anchor_owner_token = f".owner.{secrets.token_hex(8)}"
    local_owner_token = f".owner.{secrets.token_hex(8)}"
    anchor_owner_dir = anchor_dir / anchor_owner_token
    local_owner_dir = local_dir / local_owner_token
    anchor_owner_file = anchor_owner_dir / "owner"
    local_owner_file = local_owner_dir / "owner"
    anchor_capability = secrets.token_hex(32)
    local_capability = secrets.token_hex(32)
    start_token = BACKUP_PERMISSIONS._process_start_token(os.getpid())

    def write_owner(path: Path, token: str, capability: str) -> None:
        path.write_text(
            "\n".join(
                (
                    f"pid={os.getpid()}",
                    f"start_token={start_token}",
                    f"owner_id={token}",
                    "capability_sha256="
                    f"{hashlib.sha256(capability.encode('ascii')).hexdigest()}",
                    "script=pytest",
                    "",
                )
            ),
            encoding="ascii",
        )
        path.chmod(0o600)

    anchor_dir.mkdir(mode=0o700)
    anchor_owner_dir.mkdir(mode=0o700)
    local_dir.mkdir(mode=0o700)
    local_owner_dir.mkdir(mode=0o700)
    write_owner(
        anchor_owner_file,
        anchor_owner_token,
        anchor_capability,
    )
    write_owner(local_owner_file, local_owner_token, local_capability)
    parent_metadata = parent.stat()
    root_metadata = maintenance_root.stat()
    anchor_metadata = anchor_dir.stat()
    anchor_identity = (anchor_metadata.st_dev, anchor_metadata.st_ino)
    local_metadata = local_dir.stat()
    local_identity = (local_metadata.st_dev, local_metadata.st_ino)
    environment = {
        "LUMEN_BORROWED_MAINTENANCE_LOCK_KIND": "mkdir",
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT": str(maintenance_root),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH": str(parent),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_NAME": maintenance_root.name,
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV": str(
            parent_metadata.st_dev
        ),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO": str(
            parent_metadata.st_ino
        ),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_DEV": str(root_metadata.st_dev),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_INO": str(root_metadata.st_ino),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY": anchor_key,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH": str(anchor_dir),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV": str(
            anchor_metadata.st_dev
        ),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO": str(
            anchor_metadata.st_ino
        ),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_PATH": str(anchor_dir),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH": str(local_dir),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN": local_owner_token,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY": local_capability,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN": anchor_owner_token,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID": str(os.getpid()),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN": start_token,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY": anchor_capability,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    try:
        yield maintenance_root
    finally:
        for directory, identity, owner_file, owner_dir in (
            (
                local_dir,
                local_identity,
                local_owner_file,
                local_owner_dir,
            ),
            (
                anchor_dir,
                anchor_identity,
                anchor_owner_file,
                anchor_owner_dir,
            ),
        ):
            try:
                current = os.lstat(directory)
            except FileNotFoundError:
                current = None
            if current is not None and (
                current.st_dev,
                current.st_ino,
            ) == identity:
                owner_file.unlink(missing_ok=True)
                try:
                    owner_dir.rmdir()
                except FileNotFoundError:
                    pass
                try:
                    directory.rmdir()
                except FileNotFoundError:
                    pass


@contextmanager
def _maintenance_flock_environment(
    monkeypatch: pytest.MonkeyPatch,
    maintenance_root: Path,
):
    maintenance_root.mkdir()
    parent = maintenance_root.parent
    parent_descriptor = os.open(
        parent,
        BACKUP_PERMISSIONS._DIRECTORY_FLAGS,
    )
    fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
    lock_path = maintenance_root / ".lumen-maintenance.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    os.fchmod(descriptor, 0o600)
    capability = secrets.token_hex(32)
    start_token = BACKUP_PERMISSIONS._process_start_token(os.getpid())
    payload = "\n".join(
        (
            f"pid={os.getpid()}",
            f"start_token={start_token}",
            "owner_id=flock",
            "capability_sha256="
            f"{hashlib.sha256(capability.encode('ascii')).hexdigest()}",
            "script=pytest",
            "",
        )
    ).encode("ascii")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, payload)
    os.fsync(descriptor)
    metadata = os.fstat(descriptor)
    parent_metadata = os.fstat(parent_descriptor)
    root_metadata = maintenance_root.stat()
    anchor_key = hashlib.sha256(
        str(maintenance_root).encode("utf-8")
    ).hexdigest()[:32]
    environment = {
        "LUMEN_BORROWED_MAINTENANCE_LOCK_KIND": "flock",
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT": str(maintenance_root),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH": str(parent),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_NAME": maintenance_root.name,
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV": str(
            parent_metadata.st_dev
        ),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO": str(
            parent_metadata.st_ino
        ),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_DEV": str(root_metadata.st_dev),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_INO": str(root_metadata.st_ino),
        "LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY": anchor_key,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH": str(parent),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV": str(
            parent_metadata.st_dev
        ),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO": str(
            parent_metadata.st_ino
        ),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_PATH": str(lock_path),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_DEV": str(metadata.st_dev),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_INO": str(metadata.st_ino),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH": str(lock_path),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN": "flock",
        "LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY": capability,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN": "flock",
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID": str(os.getpid()),
        "LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN": start_token,
        "LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY": capability,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    try:
        yield maintenance_root, descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        try:
            fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(parent_descriptor)
        try:
            current = os.lstat(lock_path)
        except FileNotFoundError:
            current = None
        if current is not None and (
            current.st_dev,
            current.st_ino,
        ) == (metadata.st_dev, metadata.st_ino):
            lock_path.unlink()


def _set_permissions_argv(
    monkeypatch: pytest.MonkeyPatch,
    backup_root: Path,
    maintenance_root: Path,
) -> None:
    service_user, service_group = _current_identity()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PERMISSIONS),
            "ensure-backup-layout",
            str(backup_root),
            "--service-user",
            service_user,
            "--service-group",
            service_group,
            "--legacy-owner-user",
            "root",
            "--maintenance-lock-root",
            str(maintenance_root),
        ],
    )


def _run_supported_writer(
    maintenance_root: Path,
    marker: Path,
) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("LUMEN_BORROWED_MAINTENANCE_LOCK_")
    }
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            command() {{
                if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                    return 1
                fi
                builtin command "$@"
            }}
            . {shlex.quote(str(LIB))}
            if ! lumen_try_acquire_lock \
                    {shlex.quote(str(maintenance_root))} supported-writer; then
                exit 75
            fi
            printf 'entered\\n' > {shlex.quote(str(marker))}
            lumen_release_lock
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def _inject_unsafe_node(
    directory: Path,
    scratch: Path,
    node_kind: str,
) -> tuple[Path, tuple[Path, ...]]:
    injected = directory / f"injected-{node_kind}"
    if node_kind == "symlink":
        target = scratch / "symlink-target"
        target.write_text("keep\n", encoding="utf-8")
        injected.symlink_to(target)
        return injected, (target,)
    if node_kind == "hardlink":
        source = scratch / "hardlink-source"
        source.write_bytes(b"source")
        os.link(source, injected)
        return injected, (source,)
    if node_kind == "fifo":
        os.mkfifo(injected)
        return injected, ()
    if node_kind == "regular":
        injected.write_bytes(b"concurrent")
        injected.chmod(0o644)
        return injected, ()
    raise AssertionError(f"unsupported node kind: {node_kind}")


def _cleanup_injected_node(injected: Path | None, related: tuple[Path, ...]) -> None:
    if injected is not None:
        try:
            os.unlink(injected)
        except FileNotFoundError:
            pass
    for path in related:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _assert_injected_node_is_unsafe(
    injected: Path,
    related: tuple[Path, ...],
    node_kind: str,
) -> None:
    metadata = os.lstat(injected)
    if node_kind == "symlink":
        assert stat.S_ISLNK(metadata.st_mode)
        assert related[0].read_text(encoding="utf-8") == "keep\n"
    elif node_kind == "hardlink":
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_nlink == 2
    elif node_kind == "fifo":
        assert stat.S_ISFIFO(metadata.st_mode)
    else:
        assert stat.S_ISREG(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o644


def test_first_backup_journal_survives_install_ensure_and_remains_writable(
    tmp_path: Path,
) -> None:
    user, group = _current_identity()
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    journal = backup_root / ".recovery" / "backup.json"
    _write_backup_journal(journal)
    assert _mode(journal.parent) == 0o700
    assert _mode(journal) == 0o600

    shared_archive = backup_root / "pg" / "legacy.pg.dump.gz"
    shared_archive.parent.mkdir()
    shared_archive.write_bytes(b"legacy")
    shared_archive.chmod(0o644)
    deploy_root = tmp_path / "deploy"
    (deploy_root / "shared").mkdir(parents=True)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(LIB))}
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_warn() {{ :; }}
            log_info() {{ :; }}
            lumen_run_as_root() {{
                case "${{1:-}}" in
                    usermod|groupadd|useradd|chown) return 0 ;;
                esac
                "$@"
            }}
            LUMEN_BACKUP_SERVICE_USER={shlex.quote(user)}
            LUMEN_BACKUP_SERVICE_GROUP={shlex.quote(group)}
            LUMEN_CONFIG_READ_GROUP={shlex.quote(group)}
            LUMEN_DEPLOY_ROOT={shlex.quote(str(deploy_root))}
            lumen_acquire_lock "$LUMEN_DEPLOY_ROOT" permission-test
            lumen_ensure_backup_service_user {shlex.quote(str(backup_root))}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert _mode(backup_root) == 0o770
    assert _mode(backup_root / "pg") == 0o770
    assert _mode(backup_root / "redis") == 0o770
    assert _mode(shared_archive) == 0o660
    assert _mode(journal.parent) == 0o700
    assert _mode(journal) == 0o600
    assert journal.parent.stat().st_uid == os.geteuid()
    assert os.access(journal.parent, os.W_OK)

    _write_backup_journal(journal, phase="writers_starting")
    loaded = _run_journal("backup-load-shell", str(journal))
    assert loaded.returncode == 0, loaded.stderr
    assert "BACKUP_JOURNAL_PHASE=writers_starting" in loaded.stdout
    cleared = _run_journal("backup-clear", str(journal))
    assert cleared.returncode == 0, cleared.stderr
    assert not journal.exists()

    restore_journal = tmp_path / "restore-state" / "active.json"
    _write_restore_journal(restore_journal)
    assert _mode(restore_journal.parent) == 0o700
    assert _mode(restore_journal) == 0o600
    restored = _run_journal("load-shell", str(restore_journal))
    assert restored.returncode == 0, restored.stderr
    cleared_restore = _run_journal("clear", str(restore_journal))
    assert cleared_restore.returncode == 0, cleared_restore.stderr
    assert not restore_journal.exists()


def test_legacy_0770_recovery_directory_migrates_to_service_owner(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    recovery = backup_root / ".recovery"
    recovery.mkdir(parents=True)
    recovery.chmod(0o770)
    journal = recovery / "backup.json"
    journal.write_text('{"legacy":true}\n', encoding="utf-8")
    journal.chmod(0o660)

    if os.geteuid() == 0:
        target = _nonroot_identity()
        if target is None:
            pytest.skip("no non-root service identity is available")
        service_user, service_group = target
        expected_uid = pwd.getpwnam(service_user).pw_uid
        expected_gid = grp.getgrnam(service_group).gr_gid
    else:
        service_user, service_group = _current_identity()
        expected_uid = os.geteuid()
        expected_gid = os.getegid()

    result = _run_permissions(
        backup_root,
        service_user=service_user,
        service_group=service_group,
    )

    assert result.returncode == 0, result.stderr
    assert _mode(recovery) == 0o700
    assert _mode(journal) == 0o600
    assert recovery.stat().st_uid == expected_uid
    assert recovery.stat().st_gid == expected_gid
    assert journal.stat().st_uid == expected_uid
    assert journal.stat().st_gid == expected_gid


def test_deep_shared_target_owned_nodes_are_accepted_and_normalized(
    tmp_path: Path,
) -> None:
    user, group = _current_identity()
    backup_root = tmp_path / "backup"
    deep_dir = backup_root / "pg" / "daily" / "region-a"
    deep_dir.mkdir(parents=True)
    archive = deep_dir / "snapshot.dump"
    archive.write_bytes(b"payload")
    archive.chmod(0o644)

    result = _run_permissions(
        backup_root,
        service_user=user,
        service_group=group,
    )

    assert result.returncode == 0, result.stderr
    for directory in (
        backup_root,
        backup_root / "pg",
        backup_root / "pg" / "daily",
        deep_dir,
        backup_root / "redis",
    ):
        assert directory.stat().st_uid == os.geteuid()
        assert directory.stat().st_gid == os.getegid()
        assert _mode(directory) == 0o770
    assert archive.stat().st_uid == os.geteuid()
    assert archive.stat().st_gid == os.getegid()
    assert _mode(archive) == 0o660


def test_shared_legacy_owner_nodes_migrate_to_target_uid(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    deep_dir = backup_root / "redis" / "legacy"
    deep_dir.mkdir(parents=True)
    payload = deep_dir / "dump.rdb"
    payload.write_bytes(b"legacy")

    if os.geteuid() == 0:
        target = _nonroot_identity()
        if target is None:
            pytest.skip("no non-root service identity is available")
        service_user, service_group = target
        legacy_owner_user = "root"
    else:
        service_user, service_group = _current_identity()
        legacy_owner_user = service_user
    expected_uid = pwd.getpwnam(service_user).pw_uid
    expected_gid = grp.getgrnam(service_group).gr_gid

    result = _run_permissions(
        backup_root,
        service_user=service_user,
        service_group=service_group,
        legacy_owner_user=legacy_owner_user,
    )

    assert result.returncode == 0, result.stderr
    for path in (backup_root, backup_root / "redis", deep_dir, payload):
        assert path.stat().st_uid == expected_uid
        assert path.stat().st_gid == expected_gid


@pytest.mark.parametrize("node_kind", ["directory", "file"])
def test_deep_shared_third_party_owner_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    node_kind: str,
) -> None:
    backup_root = tmp_path / "backup"
    deep_dir = backup_root / "pg" / "deep"
    deep_dir.mkdir(parents=True)
    foreign = deep_dir / "foreign"
    if node_kind == "directory":
        foreign.mkdir()
    else:
        foreign.write_bytes(b"payload")
    target_uid = os.geteuid()
    legacy_uid = 0
    foreign_uid = max(target_uid, legacy_uid) + 1000
    real_entry_metadata = BACKUP_PERMISSIONS._entry_metadata

    def fake_entry_metadata(directory_fd: int, name: str):
        metadata = real_entry_metadata(directory_fd, name)
        if metadata is not None and name == "foreign":
            return _metadata_with_uid(metadata, foreign_uid)
        return metadata

    monkeypatch.setattr(
        BACKUP_PERMISSIONS,
        "_entry_metadata",
        fake_entry_metadata,
    )
    root_fd = os.open(backup_root, BACKUP_PERMISSIONS._DIRECTORY_FLAGS)
    try:
        with BACKUP_PERMISSIONS._TreeStabilityGuard() as guard:
            with pytest.raises(
                BACKUP_PERMISSIONS.BackupPermissionError,
                match=r"pg/deep/foreign owner mismatch",
            ):
                BACKUP_PERMISSIONS._validate_shared_tree(
                    root_fd,
                    target_user_id=target_uid,
                    legacy_owner_id=legacy_uid,
                    guard=guard,
                    skip_recovery=True,
                )
    finally:
        os.close(root_fd)


def test_entry_metadata_uses_lstat_semantics(tmp_path: Path) -> None:
    directory = tmp_path / "root"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("keep\n", encoding="utf-8")
    (directory / "link").symlink_to(target)
    directory_fd = os.open(directory, BACKUP_PERMISSIONS._DIRECTORY_FLAGS)
    try:
        metadata = BACKUP_PERMISSIONS._entry_metadata(directory_fd, "link")
    finally:
        os.close(directory_fd)

    assert metadata is not None
    assert stat.S_ISLNK(metadata.st_mode)


@pytest.mark.parametrize("node_kind", ["directory", "file"])
def test_open_rejects_lstat_to_open_inode_replacement(
    tmp_path: Path,
    node_kind: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = parent / "victim"
    if node_kind == "directory":
        victim.mkdir()
    else:
        victim.write_bytes(b"old")
    parent_fd = os.open(parent, BACKUP_PERMISSIONS._DIRECTORY_FLAGS)
    try:
        expected = BACKUP_PERMISSIONS._entry_metadata(parent_fd, victim.name)
        assert expected is not None
        victim.rename(parent / "replaced")
        if node_kind == "directory":
            victim.mkdir()
            opener = BACKUP_PERMISSIONS._open_child_directory
            error = "directory changed while opening"
        else:
            victim.write_bytes(b"new")
            opener = BACKUP_PERMISSIONS._open_regular_file
            error = "backup file changed while opening"
        with pytest.raises(
            BACKUP_PERMISSIONS.BackupPermissionError,
            match=error,
        ):
            opener(parent_fd, victim.name, expected)
    finally:
        os.close(parent_fd)


def test_shared_tree_rejects_hardlinks_and_special_files(tmp_path: Path) -> None:
    user, group = _current_identity()
    hardlink_root = tmp_path / "hardlink-backup"
    hardlink_root.mkdir()
    payload = hardlink_root / "payload"
    payload.write_bytes(b"payload")
    os.link(payload, hardlink_root / "payload-copy")

    hardlink_result = _run_permissions(
        hardlink_root,
        service_user=user,
        service_group=group,
    )

    assert hardlink_result.returncode == 2
    assert "multiple hard links" in hardlink_result.stderr

    special_root = tmp_path / "special-backup"
    special_root.mkdir()
    os.mkfifo(special_root / "unexpected.fifo")
    special_result = _run_permissions(
        special_root,
        service_user=user,
        service_group=group,
    )

    assert special_result.returncode == 2
    assert "not a regular file" in special_result.stderr


@pytest.mark.parametrize("tree_kind", ["shared", "recovery"])
@pytest.mark.parametrize(
    "node_kind",
    ["symlink", "hardlink", "fifo", "regular"],
)
def test_concurrent_injection_fails_closed_before_permission_migration_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tree_kind: str,
    node_kind: str,
) -> None:
    backup_root = tmp_path / "backup"
    shared = backup_root / "pg"
    recovery = backup_root / ".recovery"
    shared.mkdir(parents=True)
    recovery.mkdir()
    (shared / "existing.dump").write_bytes(b"shared")
    (recovery / "backup.json").write_text("{}\n", encoding="utf-8")
    injection_dir = shared if tree_kind == "shared" else recovery
    hook_name = (
        "_set_private_file_permissions"
        if tree_kind == "shared"
        else "_set_shared_file_permissions"
    )
    original_hook = getattr(BACKUP_PERMISSIONS, hook_name)
    injection_requested = threading.Event()
    injection_finished = threading.Event()
    hook_triggered = threading.Event()
    hook_lock = threading.Lock()
    injection_errors: list[BaseException] = []
    injected: Path | None = None
    related: tuple[Path, ...] = ()

    def pause_for_injection(*args, **kwargs):
        result = original_hook(*args, **kwargs)
        with hook_lock:
            should_pause = not hook_triggered.is_set()
            if should_pause:
                hook_triggered.set()
        if should_pause:
            injection_requested.set()
            if not injection_finished.wait(timeout=5):
                raise RuntimeError("concurrent injection did not finish")
        return result

    monkeypatch.setattr(BACKUP_PERMISSIONS, hook_name, pause_for_injection)

    def inject() -> None:
        nonlocal injected, related
        try:
            if not injection_requested.wait(timeout=5):
                raise RuntimeError("permission migration did not reach injection point")
            injected, related = _inject_unsafe_node(
                injection_dir,
                tmp_path / "scratch",
                node_kind,
            )
        except BaseException as exc:
            injection_errors.append(exc)
        finally:
            injection_finished.set()

    (tmp_path / "scratch").mkdir()
    injector = threading.Thread(target=inject, name=f"inject-{tree_kind}-{node_kind}")
    injector.start()
    try:
        with _maintenance_lock_environment(
            monkeypatch,
            tmp_path / "maintenance",
        ) as maintenance_root:
            _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
            assert BACKUP_PERMISSIONS.main() == 2
        error = capsys.readouterr().err
        assert "backup permission error:" in error
        assert "changed during" in error
        injector.join(timeout=5)
        assert not injector.is_alive()
        assert hook_triggered.is_set()
        assert not injection_errors
        assert injected is not None
        assert os.path.lexists(injected)
        _assert_injected_node_is_unsafe(injected, related, node_kind)
    finally:
        injection_finished.set()
        injector.join(timeout=5)
        _cleanup_injected_node(injected, related)
        if injected is not None:
            assert not os.path.lexists(injected)


def test_backup_layout_requires_a_live_maintenance_lock_proof(
    tmp_path: Path,
) -> None:
    user, group = _current_identity()
    result = _run_permissions(
        tmp_path / "backup",
        service_user=user,
        service_group=group,
        with_lock=False,
    )

    assert result.returncode == 2
    assert "maintenance lock proof is missing" in result.stderr


def test_backup_layout_rejects_forged_maintenance_lock_capability(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    with _maintenance_lock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as maintenance_root:
        monkeypatch.setenv(
            "LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY",
            "forged-capability",
        )
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 2

    assert "capability mismatch" in capsys.readouterr().err


def test_backup_layout_accepts_held_flock_and_rejects_released_flock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    with _maintenance_flock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as (maintenance_root, descriptor):
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 0
        os.fchmod(descriptor, 0o660)
        assert BACKUP_PERMISSIONS.main() == 0
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        assert BACKUP_PERMISSIONS.main() == 2

    assert "maintenance root-local flock is not held" in capsys.readouterr().err


def test_recovery_parent_entry_replacement_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    recovery = backup_root / ".recovery"
    recovery.mkdir(parents=True)
    (recovery / "backup.json").write_text("{}\n", encoding="utf-8")
    displaced = tmp_path / "displaced-recovery"
    replacement_file = recovery / "injected.json"
    original = BACKUP_PERMISSIONS._open_or_create_private_recovery

    def replace_after_open(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        recovery.rename(displaced)
        recovery.mkdir(mode=0o700)
        replacement_file.write_text("{}\n", encoding="utf-8")
        replacement_file.chmod(0o644)
        return descriptor

    monkeypatch.setattr(
        BACKUP_PERMISSIONS,
        "_open_or_create_private_recovery",
        replace_after_open,
    )
    with _maintenance_lock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as maintenance_root:
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 2

    error = capsys.readouterr().err
    assert "private recovery path no longer names the opened directory" in error
    assert replacement_file.is_file()
    assert _mode(replacement_file) == 0o644
    assert (displaced / "backup.json").is_file()


def test_backup_root_replacement_before_helper_commit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    displaced = tmp_path / "displaced-backup"
    replacement_file = backup_root / "unsafe.json"
    original = BACKUP_PERMISSIONS._TreeStabilityGuard.verify_final
    swapped = False

    def verify_then_replace(self):
        nonlocal swapped
        result = original(self)
        if not swapped:
            swapped = True
            backup_root.rename(displaced)
            backup_root.mkdir(mode=0o777)
            backup_root.chmod(0o777)
            replacement_file.write_text("{}\n", encoding="utf-8")
            replacement_file.chmod(0o644)
        return result

    monkeypatch.setattr(
        BACKUP_PERMISSIONS._TreeStabilityGuard,
        "verify_final",
        verify_then_replace,
    )
    with _maintenance_lock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as maintenance_root:
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 2

    assert "backup root path entry changed" in capsys.readouterr().err
    assert replacement_file.is_file()
    assert _mode(backup_root) == 0o777
    assert _mode(replacement_file) == 0o644
    assert (displaced / ".recovery").is_dir()


def test_backup_root_swap_after_helper_return_blocks_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    displaced = tmp_path / "displaced-backup"
    with _maintenance_lock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as maintenance_root:
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        binding = BACKUP_PERMISSIONS._open_directory_path_binding(backup_root)
        token = binding.token()
        binding.close()
        assert BACKUP_PERMISSIONS.main() == 0
        backup_root.rename(displaced)
        backup_root.mkdir(mode=0o777)
        backup_root.chmod(0o777)
        (backup_root / "activation-blocked.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        with pytest.raises(
            BACKUP_PERMISSIONS.BackupPermissionError,
            match="directory path binding changed",
        ):
            BACKUP_PERMISSIONS._verify_directory_binding_token(
                backup_root,
                token,
            )

    assert (backup_root / "activation-blocked.json").is_file()
    assert (displaced / ".recovery").is_dir()


def test_maintenance_lock_spans_helper_return_through_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    injected = tmp_path / "writer-entered"
    activation = tmp_path / "activation-complete"
    attempts: list[subprocess.CompletedProcess[str]] = []
    original_verify_final = BACKUP_PERMISSIONS._TreeStabilityGuard.verify_final

    def verify_then_inject(self) -> None:
        original_verify_final(self)
        attempts.append(
            _run_supported_writer(tmp_path / "maintenance", injected)
        )

    monkeypatch.setattr(
        BACKUP_PERMISSIONS._TreeStabilityGuard,
        "verify_final",
        verify_then_inject,
    )
    with _maintenance_lock_environment(
        monkeypatch,
        tmp_path / "maintenance",
    ) as maintenance_root:
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 0
        assert [attempt.returncode for attempt in attempts] == [75]
        assert not injected.exists()

        after_return = _run_supported_writer(maintenance_root, injected)
        assert after_return.returncode == 75
        activation.write_text("complete\n", encoding="utf-8")
        assert not injected.exists()

    after_activation = _run_supported_writer(maintenance_root, injected)
    assert after_activation.returncode == 0, after_activation.stderr
    assert activation.is_file()
    assert injected.read_text(encoding="utf-8") == "entered\n"


@pytest.mark.parametrize("lock_location", ["anchor", "local"])
def test_replaced_maintenance_lock_path_fails_closed_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    lock_location: str,
) -> None:
    backup_root = tmp_path / "backup"
    maintenance_root = tmp_path / "maintenance"
    displaced = tmp_path / "displaced-lock"
    outside = tmp_path / "outside-lock"
    outside.mkdir()
    with _maintenance_lock_environment(
        monkeypatch,
        maintenance_root,
    ):
        anchor_key = hashlib.sha256(
            str(maintenance_root).encode("utf-8")
        ).hexdigest()[:32]
        if lock_location == "anchor":
            lock_path = (
                maintenance_root.parent
                / f".lumen-maintenance.{anchor_key}.lock.d"
            )
        else:
            lock_path = maintenance_root / ".lumen-maintenance.lock.d"
        lock_path.rename(displaced)
        lock_path.symlink_to(outside, target_is_directory=True)
        _set_permissions_argv(monkeypatch, backup_root, maintenance_root)
        assert BACKUP_PERMISSIONS.main() == 2

    assert "symlink" in capsys.readouterr().err
    assert lock_path.is_symlink()
    assert displaced.is_dir()


@pytest.mark.parametrize("symlink_location", ["recovery", "shared"])
def test_backup_layout_migration_rejects_symlinks_without_following_them(
    tmp_path: Path,
    symlink_location: str,
) -> None:
    user, group = _current_identity()
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    if symlink_location == "recovery":
        (backup_root / ".recovery").symlink_to(outside, target_is_directory=True)
    else:
        (backup_root / "shared-link").symlink_to(outside, target_is_directory=True)

    result = _run_permissions(
        backup_root,
        service_user=user,
        service_group=group,
    )

    assert result.returncode == 2
    assert "symlink" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    linked = backup_root / (
        ".recovery" if symlink_location == "recovery" else "shared-link"
    )
    assert linked.is_symlink()


def test_backup_layout_migration_rejects_untrusted_private_owner(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    recovery = backup_root / ".recovery"
    recovery.mkdir(parents=True)
    recovery.chmod(0o770)
    journal = recovery / "backup.json"
    journal.write_text("{}\n", encoding="utf-8")
    journal.chmod(0o660)

    if os.geteuid() == 0:
        wrong = _nonroot_identity()
        if wrong is None:
            pytest.skip("no non-root owner identity is available")
        wrong_user, wrong_group = wrong
        os.chown(
            recovery,
            pwd.getpwnam(wrong_user).pw_uid,
            grp.getgrnam(wrong_group).gr_gid,
        )
        os.chown(
            journal,
            pwd.getpwnam(wrong_user).pw_uid,
            grp.getgrnam(wrong_group).gr_gid,
        )
        service_user = "root"
        service_group = grp.getgrgid(0).gr_name
    else:
        target = _nonroot_identity(excluding={os.geteuid()})
        if target is None:
            pytest.skip("no distinct service identity is available")
        service_user, service_group = target

    result = _run_permissions(
        backup_root,
        service_user=service_user,
        service_group=service_group,
    )

    assert result.returncode == 2
    assert "owner mismatch" in result.stderr
    assert _mode(recovery) == 0o770
    assert _mode(journal) == 0o660


def test_install_and_update_stop_before_activation_when_permission_migration_fails(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "activation-called"
    user, group = _current_identity()
    install_result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(INSTALL_OPERATIONS))}
            log_error() {{ :; }}
            log_info() {{ :; }}
            lumen_ensure_backup_service_user() {{ return 1; }}
            lumen_release_harden_ownership() {{
                touch {shlex.quote(str(marker))}
            }}
            install_transaction_harden_journal() {{ :; }}
            DEPLOY_ROOT={shlex.quote(str(tmp_path / "deploy"))}
            RELEASE_DIR={shlex.quote(str(tmp_path / "release"))}
            SHARED_DIR={shlex.quote(str(tmp_path / "shared"))}
            LUMEN_DATA_ROOT={shlex.quote(str(tmp_path / "data"))}
            LUMEN_APP_UID="$(id -u)"
            LUMEN_APP_GID="$(id -g)"
            LUMEN_INSTALL_OPERATOR_USER={shlex.quote(user)}
            LUMEN_INSTALL_CONFIG_GROUP={shlex.quote(group)}
            if harden_install_release_ownership; then
                exit 99
            fi
            test ! -e {shlex.quote(str(marker))}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert install_result.returncode == 0, install_result.stderr

    update_result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(UPDATE_ACTIVATION))}
            log_error() {{ :; }}
            log_info() {{ :; }}
            lumen_ensure_backup_service_user() {{ return 1; }}
            lumen_release_harden_ownership() {{
                touch {shlex.quote(str(marker))}
            }}
            ROOT={shlex.quote(str(tmp_path / "deploy"))}
            NEW_RELEASE={shlex.quote(str(tmp_path / "release"))}
            SHARED_ENV={shlex.quote(str(tmp_path / "shared" / ".env"))}
            LUMEN_DATA_ROOT={shlex.quote(str(tmp_path / "data"))}
            if lumen_update_harden_release_ownership; then
                exit 99
            fi
            test ! -e {shlex.quote(str(marker))}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert update_result.returncode == 0, update_result.stderr
    migration = MIGRATE.read_text(encoding="utf-8")
    assert migration.count("if ! lumen_ensure_backup_service_user") == 2
    assert "备份目录权限迁移失败，拒绝安装 systemd units。" in migration
    assert "备份目录权限迁移失败，拒绝完成 release ownership 收口。" in migration


def test_runtime_never_recursively_relaxes_private_recovery_directory() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert "chmod -R g+rwX" not in runtime
    assert "chgrp -R" not in runtime
    assert "backup_permissions.py" in runtime
    assert "--legacy-owner-user root" in runtime
