"""Fixed-size descriptor-relative directory pages with resumable offsets."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from dataclasses import dataclass


DIRECTORY_PAGE_BYTES = 4_096
MAX_DIRECTORY_PAGE_BYTES = 65_536
MAX_DIRECTORY_PAGE_ENTRIES = MAX_DIRECTORY_PAGE_BYTES // 8


@dataclass(frozen=True)
class DirectoryPage:
    names: tuple[bytes, ...]
    next_offset: int
    reached_end: bool
    bytes_read: int


_LIBC = ctypes.CDLL(None, use_errno=True)


def _darwin_reader() -> object | None:
    function = getattr(_LIBC, "getdirentries", None)
    if function is None:
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_long),
    ]
    function.restype = ctypes.c_int
    return function


def _linux_reader() -> tuple[object, bool] | None:
    function = getattr(_LIBC, "getdents64", None)
    if function is not None:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        function.restype = ctypes.c_ssize_t
        return function, False
    machine = platform.machine().lower()
    syscall_number = {
        "aarch64": 61,
        "arm64": 61,
        "riscv64": 61,
        "x86_64": 217,
        "amd64": 217,
    }.get(machine)
    syscall = getattr(_LIBC, "syscall", None)
    if syscall is None or syscall_number is None:
        return None
    syscall.restype = ctypes.c_long
    return (syscall, True)


_SYSTEM = platform.system()
_DARWIN_READER = _darwin_reader() if _SYSTEM == "Darwin" else None
_LINUX_READER = _linux_reader() if _SYSTEM == "Linux" else None


def directory_offsets_available() -> bool:
    return _DARWIN_READER is not None or _LINUX_READER is not None


def _read_darwin(fd: int, buffer: ctypes.Array[ctypes.c_char]) -> int:
    reader = _DARWIN_READER
    if reader is None:
        raise OSError(errno.ENOTSUP, "Darwin directory reader unavailable")
    base = ctypes.c_long()
    result = reader(fd, buffer, len(buffer), ctypes.byref(base))
    return int(result)


def _read_linux(fd: int, buffer: ctypes.Array[ctypes.c_char]) -> int:
    loaded = _LINUX_READER
    if loaded is None:
        raise OSError(errno.ENOTSUP, "Linux directory reader unavailable")
    reader, uses_syscall = loaded
    if uses_syscall:
        machine = platform.machine().lower()
        syscall_number = 217 if machine in {"x86_64", "amd64"} else 61
        result = reader(syscall_number, fd, buffer, len(buffer))
    else:
        result = reader(fd, buffer, len(buffer))
    return int(result)


def _parse_darwin(data: bytes) -> tuple[bytes, ...]:
    names: list[bytes] = []
    position = 0
    while position < len(data):
        inode = int.from_bytes(data[position : position + 4], "little")
        record_length = int.from_bytes(
            data[position + 4 : position + 6],
            "little",
        )
        name_length = data[position + 7]
        if (
            record_length < 8
            or position + record_length > len(data)
            or name_length > record_length - 8
        ):
            raise OSError(errno.EIO, "invalid Darwin directory record")
        name = data[position + 8 : position + 8 + name_length]
        if inode and name not in {b".", b".."}:
            names.append(name)
        position += record_length
    return tuple(names)


def _parse_linux(data: bytes) -> tuple[bytes, ...]:
    names: list[bytes] = []
    position = 0
    while position < len(data):
        record_length = int.from_bytes(
            data[position + 16 : position + 18],
            "little",
        )
        if record_length < 19 or position + record_length > len(data):
            raise OSError(errno.EIO, "invalid Linux directory record")
        raw_name = data[position + 19 : position + record_length]
        name = raw_name.split(b"\x00", 1)[0]
        if name not in {b"", b".", b".."}:
            names.append(name)
        position += record_length
    return tuple(names)


def read_directory_page(
    directory_fd: int,
    *,
    device: int,
    inode: int,
    offset: int,
    buffer_bytes: int = DIRECTORY_PAGE_BYTES,
) -> DirectoryPage:
    if not directory_offsets_available():
        raise OSError(
            errno.ENOTSUP,
            "resumable directory offsets are unavailable",
        )
    if buffer_bytes < DIRECTORY_PAGE_BYTES:
        buffer_bytes = DIRECTORY_PAGE_BYTES
    if buffer_bytes > MAX_DIRECTORY_PAGE_BYTES:
        raise ValueError("directory page exceeds hard byte limit")

    page_fd = os.dup(directory_fd)
    try:
        before = os.fstat(page_fd)
        if before.st_dev != device or before.st_ino != inode:
            raise OSError(errno.ESTALE, "directory identity changed")
        os.lseek(page_fd, offset, os.SEEK_SET)
        buffer = ctypes.create_string_buffer(buffer_bytes)
        ctypes.set_errno(0)
        if _SYSTEM == "Darwin":
            byte_count = _read_darwin(page_fd, buffer)
            parser = _parse_darwin
        elif _SYSTEM == "Linux":
            byte_count = _read_linux(page_fd, buffer)
            parser = _parse_linux
        else:
            raise OSError(errno.ENOTSUP, "unsupported directory reader")
        if byte_count < 0:
            error = ctypes.get_errno() or errno.EIO
            raise OSError(error, os.strerror(error))
        next_offset = os.lseek(page_fd, 0, os.SEEK_CUR)
        after = os.fstat(page_fd)
        if after.st_dev != device or after.st_ino != inode:
            raise OSError(errno.ESTALE, "directory identity changed")
        if byte_count and next_offset == offset:
            raise OSError(errno.EIO, "directory offset did not advance")
        return DirectoryPage(
            names=parser(buffer.raw[:byte_count]),
            next_offset=next_offset,
            reached_end=byte_count == 0,
            bytes_read=byte_count,
        )
    finally:
        os.close(page_fd)
