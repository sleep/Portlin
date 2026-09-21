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


# Accounts below this are system accounts, and nobody is the one above it.
FIRST_USER_UID = 1000
NOBODY_UID = 65534

# Where the wizard records its two yes/no answers. Both are read as answers
# rather than copied as files, so first boot can put them through its own
# screens and the same visudo check it applies to a fresh answer.
SUDOERS_DROPIN = "etc/sudoers.d/50-portlin-nopasswd"
AUTOLOGIN_CONF = "etc/lightdm/lightdm.conf.d/10-portlin.conf"


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    gecos: str
    shell: str
    home: str  # relative to the root: "home/somebody"
    password_hash: str
    groups: tuple[str, ...]
    sudo_nopasswd: bool
    autologin: bool


@dataclass(frozen=True)
class Identity:
    hostname: str = ""
    locale: str = ""
    keyboard: str = ""
    timezone: str = ""


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text()
    except OSError:
        return ""


def _shadow_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for line in _read(root, "etc/shadow").splitlines():
        fields = line.split(":")
        if len(fields) >= 2:
            hashes[fields[0]] = fields[1]
    return hashes


def _group_memberships(root: Path) -> dict[str, list[str]]:
    """Group names by member, from the fourth field of /etc/group."""
    memberships: dict[str, list[str]] = {}
    for line in _read(root, "etc/group").splitlines():
        fields = line.split(":")
        if len(fields) < 4:
            continue
        for member in filter(None, fields[3].split(",")):
            memberships.setdefault(member, []).append(fields[0])
    return memberships


def read_accounts(root: Path) -> list[Account]:
    """Every person's account on the source: uid 1000 and up, nobody excluded."""
    hashes = _shadow_hashes(root)
    memberships = _group_memberships(root)
    waiver = _read(root, SUDOERS_DROPIN)
    autologin = _read(root, AUTOLOGIN_CONF)
    accounts = []
    for line in _read(root, "etc/passwd").splitlines():
        fields = line.split(":")
        if len(fields) < 7:
            continue
        name, _, uid, gid, gecos, home, shell = fields[:7]
        try:
            uid_number, gid_number = int(uid), int(gid)
        except ValueError:
            continue
        if uid_number < FIRST_USER_UID or uid_number == NOBODY_UID:
            continue
        accounts.append(
            Account(
                name=name,
                uid=uid_number,
                gid=gid_number,
                gecos=gecos,
                shell=shell,
                home=home.lstrip("/"),
                password_hash=hashes.get(name, ""),
                groups=tuple(memberships.get(name, [])),
                sudo_nopasswd=name in waiver.split(),
                autologin=f"autologin-user={name}" in autologin,
            )
        )
    return accounts


def _shell_value(text: str, key: str) -> str:
    """The value of KEY=value or KEY="value" in a file of shell assignments."""
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"')
    return ""


def read_identity(root: Path) -> Identity:
    timezone = _read(root, "etc/timezone").strip()
    if not timezone:
        try:
            target = os.readlink(root / "etc" / "localtime")
            timezone = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            timezone = ""
    return Identity(
        hostname=_read(root, "etc/hostname").strip(),
        locale=_shell_value(_read(root, "etc/default/locale"), "LANG"),
        keyboard=_shell_value(_read(root, "etc/default/keyboard"), "XKBLAYOUT"),
        timezone=timezone,
    )
