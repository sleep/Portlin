"""Shared block-device lookups for the portlin runtime tools.

Stdlib-only, and imported with no dependency on the portlin package itself,
because this file ships onto a stick where only python3 is guaranteed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def command_output(argv: list[str]) -> str:
    """Run a command and return its stripped stdout, or "" if it is missing.

    Shared by every tool that shells out to lsblk, findmnt and friends, so the
    "command not found" handling is written once rather than copied into each.
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        return ""


def root_source() -> str:
    """The device or mapper node findmnt reports as the source of ``/``."""
    return command_output(["findmnt", "-no", "SOURCE", "/"])


def sysfs_node(device: str) -> Path | None:
    """Sysfs directory for a device node, located by its device number.

    Resolving /dev/mapper/<name> as a symlink only works where udev created it
    that way. Without udev -- in an initramfs, a container, or a minimal system
    -- cryptsetup makes a real device node there instead, and resolve() returns
    the path unchanged, pointing at a sysfs entry that does not exist. The
    major:minor pair is how the kernel identifies the device either way.
    """
    try:
        status = os.stat(device)
    except OSError:
        return None
    path = Path(f"/sys/dev/block/{os.major(status.st_rdev)}:{os.minor(status.st_rdev)}")
    return path if path.exists() else None


def backing_partition(source: str) -> str:
    """Kernel name of the partition holding ``source``.

    On an encrypted stick the root is a device-mapper node, and dm devices have
    no "device" link in sysfs -- which is why `lsblk -o PKNAME` returns an empty
    string for them and every lookup built on it silently produced nothing. The
    supported relationship is /sys/block/<dm>/slaves/, listing what the mapping
    is built on top of.
    """
    node = sysfs_node(source)
    if node is None:
        return ""
    slaves = node / "slaves"
    if slaves.is_dir():
        entries = sorted(entry.name for entry in slaves.iterdir())
        if entries:
            return entries[0]
    # Not a mapping at all: the source is already the partition.
    return node.resolve().name
