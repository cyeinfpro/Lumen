from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from urllib.parse import quote

from .definitions import SSH_HOST_KEY_FINGERPRINT_RE, ProviderProxyDefinition


def _proxy_host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def socks_proxy_url(proxy: ProviderProxyDefinition) -> str | None:
    if not proxy.enabled or proxy.protocol != "socks5":
        return None
    host = _proxy_host_for_url(proxy.host)
    auth = ""
    if proxy.username:
        auth = quote(proxy.username, safe="")
        if proxy.password:
            auth += f":{quote(proxy.password, safe='')}"
        auth += "@"
    return f"socks5h://{auth}{host}:{proxy.port}"


@dataclass
class _SshTunnel:
    url: str
    process: asyncio.subprocess.Process


class _SshTunnelRuntime:
    def __init__(self) -> None:
        self.tunnels: dict[str, _SshTunnel] = {}
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        self.tunnels.clear()


_SSH_TUNNEL_RUNTIME = _SshTunnelRuntime()
_SSH_TUNNEL_START_ATTEMPTS = 3
_SSH_TUNNEL_READY_CHECKS = 30


def _ssh_tunnel_key(proxy: ProviderProxyDefinition) -> str:
    password_digest = (
        hashlib.sha256(proxy.password.encode("utf-8")).hexdigest()
        if proxy.password
        else ""
    )
    return "\x1f".join(
        [
            proxy.name,
            proxy.host,
            str(proxy.port),
            proxy.username or "",
            password_digest,
            proxy.private_key_path or "",
            proxy.known_hosts_path or "",
            proxy.host_key_fingerprint or "",
        ]
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _local_port_accepts(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(2), timeout=0.3)
        return reply == b"\x05\x00"
    except Exception:
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _secret_dir() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return xdg
    try:
        run_user = f"/run/user/{os.getuid()}"
    except AttributeError:  # pragma: no cover - non-POSIX
        run_user = ""
    if run_user and os.path.isdir(run_user):
        return run_user
    return tempfile.gettempdir()


def _atomic_secret_open(prefix: str, mode: int) -> tuple[int, str]:
    base_dir = _secret_dir()
    for _ in range(8):
        path = os.path.join(base_dir, f"{prefix}{secrets.token_hex(8)}")
        try:
            fd = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                mode,
            )
        except FileExistsError:
            continue
        return fd, path
    raise RuntimeError("failed to allocate unique secret filename")


def _write_secret_file(value: str) -> str:
    fd, path = _atomic_secret_open("lumen-ssh-secret-", 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.write("\n")
    return path


def _write_ssh_askpass_helper() -> str:
    fd, path = _atomic_secret_open("lumen-ssh-askpass-", 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
        handle.write('cat "$LUMEN_SSH_PASSWORD_FILE"\n')
    return path


def _normalize_ssh_fingerprint(value: str) -> str:
    return value.strip().rstrip("=")


def _ssh_key_fingerprint(key_blob: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii")
    return f"SHA256:{encoded.rstrip('=')}"


def _known_hosts_file_error(
    proxy: ProviderProxyDefinition,
    path: str,
    detail: str,
) -> RuntimeError:
    return RuntimeError(f"ssh proxy {proxy.name} known_hosts {detail}: {path}")


def _open_known_hosts_file(
    proxy: ProviderProxyDefinition,
    path: str,
) -> tuple[int, os.stat_result]:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise _known_hosts_file_error(proxy, path, "file is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise _known_hosts_file_error(proxy, path, "path must not be a symlink")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = -1
    try:
        source_fd = os.open(path, flags)
        file_stat = os.fstat(source_fd)
    except OSError as exc:
        if source_fd >= 0:
            os.close(source_fd)
        if exc.errno == errno.ELOOP:
            raise _known_hosts_file_error(
                proxy,
                path,
                "path must not be a symlink",
            ) from exc
        raise _known_hosts_file_error(proxy, path, "file is unavailable") from exc
    if (path_stat.st_dev, path_stat.st_ino) != (
        file_stat.st_dev,
        file_stat.st_ino,
    ):
        os.close(source_fd)
        raise _known_hosts_file_error(
            proxy,
            path,
            "path changed during validation",
        )
    return source_fd, file_stat


def _validate_known_hosts_file(
    proxy: ProviderProxyDefinition,
    path: str,
    file_stat: os.stat_result,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise _known_hosts_file_error(proxy, path, "path is not a regular file")
    if not (file_stat.st_mode & 0o444):
        raise _known_hosts_file_error(proxy, path, "file is not readable")
    if file_stat.st_mode & 0o022:
        raise _known_hosts_file_error(
            proxy,
            path,
            "file is group/world writable",
        )
    if file_stat.st_size <= 0:
        raise _known_hosts_file_error(proxy, path, "file is empty")


def _copy_file_descriptor(source_fd: int, target_fd: int) -> int:
    copied = 0
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            return copied
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            if written <= 0:
                raise OSError("known_hosts snapshot write returned no progress")
            copied += written
            view = view[written:]


def _known_hosts_stat_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _copy_known_hosts_snapshot(
    proxy: ProviderProxyDefinition,
    path: str,
    source_fd: int,
    source_stat: os.stat_result,
) -> str:
    snapshot_fd, snapshot_path = _atomic_secret_open(
        "lumen-ssh-known-hosts-",
        0o600,
    )
    try:
        copied = _copy_file_descriptor(source_fd, snapshot_fd)
        final_stat = os.fstat(source_fd)
        if copied != source_stat.st_size or _known_hosts_stat_signature(
            final_stat
        ) != _known_hosts_stat_signature(source_stat):
            raise _known_hosts_file_error(
                proxy,
                path,
                "file changed during snapshot",
            )
    except BaseException:
        os.close(snapshot_fd)
        _unlink_quietly(snapshot_path)
        raise
    else:
        os.close(snapshot_fd)
        return snapshot_path


def _validated_known_hosts_path(proxy: ProviderProxyDefinition) -> str | None:
    raw_path = (proxy.known_hosts_path or "").strip()
    if not raw_path:
        return None
    path = os.path.abspath(os.path.expanduser(raw_path))
    source_fd, file_stat = _open_known_hosts_file(proxy, path)
    try:
        _validate_known_hosts_file(proxy, path, file_stat)
        return _copy_known_hosts_snapshot(
            proxy,
            path,
            source_fd,
            file_stat,
        )
    finally:
        os.close(source_fd)


def _parse_ssh_keyscan_output(
    output: str,
    *,
    expected_fingerprint: str,
) -> str | None:
    expected = _normalize_ssh_fingerprint(expected_fingerprint)
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            key_blob = base64.b64decode(fields[2], validate=True)
        except (TypeError, ValueError):
            continue
        if hmac.compare_digest(_ssh_key_fingerprint(key_blob), expected):
            return line
    return None


async def _scan_ssh_host_key(
    proxy: ProviderProxyDefinition,
    *,
    fingerprint: str,
) -> str:
    keyscan_bin = shutil.which("ssh-keyscan")
    if not keyscan_bin:
        raise RuntimeError(
            f"ssh proxy {proxy.name} requires ssh-keyscan for fingerprint verification"
        )
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                keyscan_bin,
                "-T",
                "5",
                "-p",
                str(proxy.port),
                "--",
                proxy.host,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"ssh proxy {proxy.name} host key scan failed: {type(exc).__name__}"
        ) from None
    output = result.stdout if isinstance(result.stdout, str) else ""
    matched = _parse_ssh_keyscan_output(
        output,
        expected_fingerprint=fingerprint,
    )
    if matched is None:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        suffix = f": {detail[:200]}" if detail else ""
        raise RuntimeError(
            f"ssh proxy {proxy.name} host key fingerprint mismatch{suffix}"
        )
    return matched


def _write_known_hosts_line(line: str) -> str:
    fd, path = _atomic_secret_open("lumen-ssh-known-hosts-", 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
    except BaseException:
        _unlink_quietly(path)
        raise
    return path


async def _prepare_ssh_host_key_verification(
    proxy: ProviderProxyDefinition,
) -> tuple[str, str | None]:
    fingerprint = (proxy.host_key_fingerprint or "").strip()
    if fingerprint and not SSH_HOST_KEY_FINGERPRINT_RE.fullmatch(fingerprint):
        raise RuntimeError(
            f"ssh proxy {proxy.name} has an invalid host key fingerprint"
        )
    if fingerprint:
        matched_line = await _scan_ssh_host_key(proxy, fingerprint=fingerprint)
        temporary_path = _write_known_hosts_line(matched_line)
        return temporary_path, temporary_path

    known_hosts_path = _validated_known_hosts_path(proxy)
    if known_hosts_path is None:
        raise RuntimeError(
            f"ssh proxy {proxy.name} requires known_hosts_path or "
            "host_key_fingerprint; refusing unknown host key"
        )
    return known_hosts_path, known_hosts_path


def _unlink_quietly(path: str | None) -> None:
    if not path:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


async def _read_process_stderr(
    proc: asyncio.subprocess.Process,
    *,
    limit: int = 2000,
) -> str:
    if proc.stderr is None:
        return ""
    try:
        raw = await asyncio.wait_for(proc.stderr.read(limit), timeout=0.2)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except Exception:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _running_ssh_tunnel(key: str) -> _SshTunnel | None:
    tunnel = _SSH_TUNNEL_RUNTIME.tunnels.get(key)
    if tunnel is None or tunnel.process.returncode is not None:
        return None
    return tunnel


async def _close_stale_ssh_tunnels(
    proxy: ProviderProxyDefinition,
    current_key: str,
) -> None:
    for old_key, tunnel in list(_SSH_TUNNEL_RUNTIME.tunnels.items()):
        if old_key == current_key or not old_key.startswith(f"{proxy.name}\x1f"):
            continue
        _SSH_TUNNEL_RUNTIME.tunnels.pop(old_key, None)
        await _terminate_process(tunnel.process)


def _ssh_tunnel_command(
    ssh_bin: str,
    proxy: ProviderProxyDefinition,
    *,
    local_port: int,
    known_hosts_path: str,
) -> list[str]:
    target = f"{proxy.username}@{proxy.host}" if proxy.username else proxy.host
    command = [
        ssh_bin,
        "-N",
        "-D",
        f"127.0.0.1:{local_port}",
        "-p",
        str(proxy.port),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        f"GlobalKnownHostsFile={os.devnull}",
        "-o",
        "UpdateHostkeys=no",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if proxy.password:
        command.extend(
            [
                "-o",
                "BatchMode=no",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=yes",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive,publickey",
            ]
        )
    else:
        command.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
            ]
        )
    if proxy.private_key_path:
        command.extend(["-i", proxy.private_key_path])
    command.extend(["--", target])
    return command


def _ssh_password_command(
    command: list[str],
    proxy: ProviderProxyDefinition,
) -> tuple[list[str], dict[str, str] | None, str | None, str | None]:
    if not proxy.password:
        return command, None, None, None
    password_file = _write_secret_file(proxy.password)
    sshpass_bin = shutil.which("sshpass")
    if sshpass_bin:
        return (
            [sshpass_bin, "-f", password_file, *command],
            os.environ.copy(),
            None,
            password_file,
        )
    askpass_path = _write_ssh_askpass_helper()
    env = os.environ.copy()
    env["SSH_ASKPASS"] = askpass_path
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", "localhost:0")
    env["LUMEN_SSH_PASSWORD_FILE"] = password_file
    return command, env, askpass_path, password_file


async def _wait_for_ssh_tunnel(
    proc: asyncio.subprocess.Process,
    local_port: int,
) -> tuple[str | None, str]:
    for _ in range(_SSH_TUNNEL_READY_CHECKS):
        if proc.returncode is not None:
            stderr = await _read_process_stderr(proc)
            return None, f"exited with {proc.returncode}: {stderr}".strip()
        if await _local_port_accepts(local_port):
            return f"socks5h://127.0.0.1:{local_port}", ""
        await asyncio.sleep(0.1)
    stderr = await _read_process_stderr(proc)
    return None, f"timed out waiting for local SOCKS port: {stderr}".strip()


async def _start_ssh_tunnel_attempt(
    proxy: ProviderProxyDefinition,
    *,
    ssh_bin: str,
    key: str,
) -> tuple[str | None, str]:
    (
        known_hosts_path,
        temporary_known_hosts_path,
    ) = await _prepare_ssh_host_key_verification(proxy)
    proc: asyncio.subprocess.Process | None = None
    tunnel_started = False
    askpass_path = None
    password_file = None
    try:
        local_port = _free_local_port()
        command = _ssh_tunnel_command(
            ssh_bin,
            proxy,
            local_port=local_port,
            known_hosts_path=known_hosts_path,
        )
        command, env, askpass_path, password_file = _ssh_password_command(
            command,
            proxy,
        )
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        url, error = await _wait_for_ssh_tunnel(proc, local_port)
        if url is not None:
            _SSH_TUNNEL_RUNTIME.tunnels[key] = _SshTunnel(url=url, process=proc)
            tunnel_started = True
        return url, error
    finally:
        if proc is not None and not tunnel_started:
            await _terminate_process(proc)
        _unlink_quietly(askpass_path)
        _unlink_quietly(password_file)
        _unlink_quietly(temporary_known_hosts_path)


async def _ensure_ssh_socks_proxy(proxy: ProviderProxyDefinition) -> str:
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("ssh binary not found; cannot start ssh proxy")
    key = _ssh_tunnel_key(proxy)
    existing = _running_ssh_tunnel(key)
    if existing is not None:
        return existing.url

    async with _SSH_TUNNEL_RUNTIME.lock:
        existing = _running_ssh_tunnel(key)
        if existing is not None:
            return existing.url
        await _close_stale_ssh_tunnels(proxy, key)

        last_error = ""
        for _attempt in range(_SSH_TUNNEL_START_ATTEMPTS):
            url, last_error = await _start_ssh_tunnel_attempt(
                proxy,
                ssh_bin=ssh_bin,
                key=key,
            )
            if url is not None:
                return url

        raise RuntimeError(
            f"ssh proxy {proxy.name} failed to start after "
            f"{_SSH_TUNNEL_START_ATTEMPTS} attempts: {last_error}"
        )


async def resolve_provider_proxy_url(
    proxy: ProviderProxyDefinition | None,
) -> str | None:
    if proxy is None or not proxy.enabled:
        return None
    if proxy.protocol == "socks5":
        return socks_proxy_url(proxy)
    if proxy.protocol == "ssh":
        return await _ensure_ssh_socks_proxy(proxy)
    raise RuntimeError(f"unsupported proxy protocol: {proxy.protocol}")


async def close_provider_proxy_tunnels() -> None:
    tunnels = list(_SSH_TUNNEL_RUNTIME.tunnels.values())
    _SSH_TUNNEL_RUNTIME.clear()
    for tunnel in tunnels:
        await _terminate_process(tunnel.process)
