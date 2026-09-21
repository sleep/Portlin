#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""What portlin-migrate reads and plans, kept apart from what it runs.

Every function here is pure over paths and strings: it reads a source root
that has already been mounted, and it produces ordered command lines rather
than running them. That is the shape build_rootfs and write_stick have, and
for the same reason: the ordered command list is what the unit tests can
assert on a machine with no root, no rsync and no block devices, and the
harness is what proves rsync did what the list says.

Stdlib-only, and imported with no dependency on the portlin package itself,
because this file ships onto a stick where only python3 is guaranteed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Labels write puts on partitions 3 and 4. The boot label is the one that
# identifies a stick, because it is readable whether or not the root is
# encrypted.
BOOT_LABEL = "portlin-boot"
ROOT_LABEL = "portlin-root"
ARCHIVE_SUFFIX = ".portlin-backup.tar.zst"
MANIFEST = "manifest.json"
RELEASE_FILE = "etc/portlin-release"

# PARTN rather than guessing from the name: sda4 and nvme0n1p4 spell the
# number differently, and lsblk knows it either way.
LSBLK_COLUMNS = "PATH,LABEL,FSTYPE,SIZE,MODEL,TRAN,PARTN"


@dataclass(frozen=True)
class Candidate:
    """A place a migration could come from."""

    kind: str  # "stick" or "archive"
    path: str  # the root partition, or the archive file
    model: str = ""  # the disk model, or the mount an archive was found on
    size: str = ""
    encrypted: bool = False
    version: str = ""


def lsblk_argv() -> list[str]:
    return ["lsblk", "--json", "-o", LSBLK_COLUMNS]


def parse_lsblk(text: str, running_disk: str) -> list[Candidate]:
    """Every portlin stick in lsblk's output, other than the one running.

    Excluded by disk rather than by partition, so the running stick's own
    /boot is never offered as somewhere to migrate from.
    """
    try:
        disks = json.loads(text).get("blockdevices", [])
    except (ValueError, AttributeError):
        return []
    found = []
    for disk in disks:
        if disk.get("path") == running_disk:
            continue
        parts = {
            child.get("partn"): child
            for child in disk.get("children", [])
            if child.get("partn") is not None
        }
        boot, root = parts.get(3), parts.get(4)
        if not boot or not root or boot.get("label") != BOOT_LABEL:
            continue
        fstype = root.get("fstype") or ""
        if fstype == "crypto_LUKS":
            encrypted = True
        elif fstype == "ext4" and root.get("label") == ROOT_LABEL:
            encrypted = False
        else:
            continue
        found.append(
            Candidate(
                "stick",
                root["path"],
                (disk.get("model") or "").strip(),
                disk.get("size") or "",
                encrypted,
            )
        )
    return found


def archive_candidates(mounts: list[Path]) -> list[Candidate]:
    """Archives sitting directly under each mount, and nowhere deeper.

    Directly under, because that is where export puts them and where a person
    dropping one onto a drive puts it. Walking a whole backup drive to find
    one file is the kind of scan that makes a menu take a minute to appear.
    """
    found = []
    for mount in mounts:
        try:
            entries = sorted(mount.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(ARCHIVE_SUFFIX) and entry.is_file():
                found.append(Candidate("archive", str(entry), mount.name))
    return found


def describe(candidate: Candidate) -> str:
    """One menu line per candidate, the same words in every front end."""
    if candidate.kind == "archive":
        return f"backup on {candidate.model}   {Path(candidate.path).name}"
    version = f"portlin {candidate.version}" if candidate.version else "portlin"
    detail = f"{version}, encrypted" if candidate.encrypted else version
    return f"{candidate.model} {candidate.size}   {detail}"


def human(size: int) -> str:
    """Decimal units, the way portlin-info reports the drive."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = 0
    while value >= 1000 and unit < len(units) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    text = f"{value:.1f}".removesuffix(".0")
    return f"{text} {units[unit]}"


def read_release(root: Path) -> dict[str, str]:
    """The KEY=value pairs of /etc/portlin-release, or nothing when absent."""
    try:
        text = (root / RELEASE_FILE).read_text()
    except OSError:
        return {}
    pairs = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            pairs[key.strip()] = value.strip().strip('"')
    return pairs
