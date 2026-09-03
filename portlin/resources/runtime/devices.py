"""Shared block-device lookups for the portlin runtime tools.

Stdlib-only, and imported with no dependency on the portlin package itself,
because this file ships onto a stick where only python3 is guaranteed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# ext4 metadata is not a fixed quantity, so a fixed constant is wrong at one end
# of the range or the other. statvfs already excludes the superblock, group
# descriptors, bitmaps, inode tables and journal, and the measured gap between a
# partition and the statvfs size of the filesystem filling it is a flat ~2.1%:
# 674 MB on a 31 GB partition, 1311 MB on a 62.9 GB one. The slack therefore
# scales, with a floor that covers an encrypted root's 16 MiB LUKS header on a
# partition small enough for the fraction to fall below it.
INSIDE_SLACK_FRACTION = 0.03
INSIDE_SLACK_FLOOR_BYTES = 64 * 1024**2

# Below this, nothing reports unclaimed space. Expansion is worth mentioning in
# whole gigabytes; anything smaller is measurement noise rather than space a
# user could usefully claim.
REPORT_FLOOR_BYTES = 1024**3


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


def sysfs_sectors(name: str, field: str) -> int:
    """A sector count for a block device, from sysfs, or 0 if unreadable.

    Distinct from sysfs_node above, which locates a device by its device number
    because a /dev/mapper name cannot be resolved by path without udev. This one
    is given a kernel name that already exists in sysfs and only reads geometry
    from it.

    sysfs is world-readable, which is what makes partition geometry available to
    the tools at all: portlin-info runs as an ordinary user, so the wizard's
    dumpe2fs route is closed to it.
    """
    try:
        return int((Path("/sys/class/block") / name / field).read_text().strip())
    except (OSError, ValueError):
        return 0


def backing_disk(partition: str) -> str:
    """The whole disk behind ``partition``.

    -d (no-deps) matters here: without it, lsblk lists the whole subtree
    rooted at the partition, and on the ordinary case this runs against --
    a live, mounted, encrypted stick, where an open LUKS mapping sits on top
    of the very partition being asked about -- that is two rows instead of
    one, and PKNAME comes back as two lines glued together by a newline.
    """
    return command_output(["lsblk", "-dno", "PKNAME", f"/dev/{partition}"]) or partition


def disk_tail_bytes(disk: str, partition: str) -> int:
    """Unallocated bytes on the drive after the root partition.

    The term that matters most, and the one the unclaimed-space report exists
    for: a stick written by putting the fixed-size image onto a larger drive has
    all of its unclaimed space here, outside the partition entirely, where a
    comparison between the filesystem and its own partition cannot see it.
    """
    disk_sectors = sysfs_sectors(disk, "size")
    end = sysfs_sectors(partition, "start") + sysfs_sectors(partition, "size")
    if not disk_sectors or end <= 0:
        return 0
    # The GPT backup header and its partition array occupy the last 33 sectors.
    return max(0, (disk_sectors - end - 34) * 512)


def unused_inside_partition(filesystem_bytes: int, partition_bytes: int) -> int:
    """Bytes inside the partition the filesystem has not claimed.

    The second of the two gaps, and not the same as the first. An expansion
    interrupted after growpart leaves a full-size partition holding the original
    filesystem, so a check that only looked at the drive tail would find nothing
    to do and never mention it again.
    """
    if partition_bytes <= 0:
        return 0
    slack = max(INSIDE_SLACK_FLOOR_BYTES, int(partition_bytes * INSIDE_SLACK_FRACTION))
    return max(0, partition_bytes - filesystem_bytes - slack)


def unclaimed_bytes(
    filesystem_bytes: int, partition_bytes: int, tail_bytes: int
) -> int:
    """Space this stick could still claim, across both gaps.

    Reporting either gap alone gets it wrong in opposite directions. Comparing
    the filesystem against the whole disk counts the fixed partitions ahead of
    root and nags on every stick forever; comparing it against only its own
    partition goes silent on the ordinary case of an unexpanded image sitting on
    a much larger drive.
    """
    total = tail_bytes + unused_inside_partition(filesystem_bytes, partition_bytes)
    return total if total >= REPORT_FLOOR_BYTES else 0
