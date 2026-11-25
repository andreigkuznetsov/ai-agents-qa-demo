import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def private_journal_dir(tmp_path: Path) -> Path:
    return make_private_journal_dir(tmp_path)


def make_private_journal_dir(parent: Path) -> Path:
    path = parent / "private"
    path.mkdir(mode=0o700)
    if sys.platform == "win32":
        from app.logistics_agent_storage_win import _operator_sid

        sid = _operator_sid()
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    elif sys.platform == "linux":
        from app.logistics_agent_storage_posix import _local_filesystem

        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                _local_filesystem(fd, path)
            except PermissionError:
                pytest.skip(
                    "Linux SQLite tests require a real local ext4, XFS or Btrfs filesystem"
                )
        finally:
            os.close(fd)
    else:
        pytest.skip("SQLite profile is defined for Linux and Windows")
    return path
