import os
import re
import stat
import sys
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Iterator


def _decode_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _local_filesystem(fd: int, path: Path) -> None:
    info = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
    mount_id = next(
        (line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:")),
        None,
    )
    if not sys.platform.startswith("linux") or mount_id is None:
        raise PermissionError
    for entry in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = entry.split()
        if fields[0] != mount_id:
            continue
        mount_path = Path(_decode_mount_path(fields[4]))
        filesystem = fields[fields.index("-") + 1]
        if path.is_relative_to(mount_path) and filesystem in {"ext4", "xfs", "btrfs"}:
            return
        break
    raise PermissionError


if sys.platform == "linux":

    @contextmanager
    def directory(path: Path) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        uid = os.geteuid()
        with ExitStack() as stack:
            fd = os.open(path.anchor, flags)
            stack.callback(os.close, fd)
            for part in path.parts[1:]:
                parent = os.fstat(fd)
                mode = stat.S_IMODE(parent.st_mode)
                if parent.st_uid not in {0, uid} or (
                    mode & 0o022 and not mode & stat.S_ISVTX
                ):
                    raise PermissionError
                fd = os.open(part, flags, dir_fd=fd)
                stack.callback(os.close, fd)
            info = os.fstat(fd)
            if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise PermissionError
            if Path(os.readlink(f"/proc/self/fd/{fd}")) != path:
                raise PermissionError
            _local_filesystem(fd, path)
            yield fd

    @contextmanager
    def file_handle(
        directory_fd: int, path: Path, *, create: bool = False
    ) -> Iterator[int]:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(
            path.name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=directory_fd
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise PermissionError
            yield fd
        finally:
            os.close(fd)

    def lock(fd: int) -> bool:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
else:

    def directory(path: Path) -> AbstractContextManager[int]:
        raise PermissionError

    def file_handle(
        directory_fd: int, path: Path, *, create: bool = False
    ) -> AbstractContextManager[int]:
        raise PermissionError

    def lock(fd: int) -> bool:
        raise PermissionError
