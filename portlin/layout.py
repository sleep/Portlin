"""Partition planning. Pure functions only: no I/O, no subprocess, no root.

The layout is fixed in shape and variable only in the size of the root
partition, which absorbs whatever is left. Keeping this module pure is what lets
the trickiest arithmetic in the project be tested exhaustively in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass

MIB = 1024 * 1024
GIB = 1024 * MIB

BIOS_BOOT_MIB = 1
ESP_MIB = 512
BOOT_MIB = 1024

# GPT keeps a primary header plus partition array at the front and a backup copy
# at the very end. 2 MiB of slack covers both with room for 1 MiB alignment.
GPT_OVERHEAD_MIB = 2

# A LUKS2 header with the default 16 MiB keyslot area sits in front of the
# filesystem, so an encrypted root holds that much less data.
LUKS_HEADER_MIB = 16

# Debian plus Xfce occupies roughly 3.6 GiB installed. The image only has to hold
# that plus working room, because the root filesystem grows to fill the stick on
# first boot -- the image size and the stick size are deliberately unrelated.
MIN_ROOT_MIB = 5 * 1024

# Default size for a generated image. Small on purpose: this is what gets written
# to the stick, so every gigabyte here is time spent flashing for space that
# first-boot expansion would have claimed anyway.
DEFAULT_IMAGE_BYTES = 8 * GIB

# Policy floor for a whole stick.
MIN_TARGET_BYTES = 8 * GIB

ROLE_BIOS = "bios"
ROLE_ESP = "esp"
ROLE_BOOT = "boot"
ROLE_ROOT = "root"

LABEL_ESP = "PORTLIN-ESP"
LABEL_BOOT = "portlin-boot"
LABEL_ROOT = "portlin-root"

MAPPER_NAME = "portlin_root"


@dataclass(frozen=True)
class Partition:
    number: int
    role: str
    typecode: str
    name: str
    size_mib: int | None  # None means "the rest of the device"


@dataclass(frozen=True)
class PartitionPlan:
    partitions: tuple[Partition, ...]
    root_mib: int
    encrypted: bool

    def by_role(self, role: str) -> Partition:
        for part in self.partitions:
            if part.role == role:
                return part
        raise KeyError(role)


def plan_partitions(size_bytes: int, *, encrypted: bool = False) -> PartitionPlan:
    """Build the partition plan for a target of ``size_bytes``.

    Raises LayoutError when the remaining space cannot hold a usable root.
    """
    from .errors import LayoutError

    if size_bytes <= 0:
        raise LayoutError("target reports a size of zero bytes")

    size_mib = size_bytes // MIB
    fixed = GPT_OVERHEAD_MIB + BIOS_BOOT_MIB + ESP_MIB + BOOT_MIB
    overhead = fixed + (LUKS_HEADER_MIB if encrypted else 0)
    root_mib = size_mib - overhead

    if root_mib < MIN_ROOT_MIB:
        raise LayoutError(
            f"target is too small: {size_mib} MiB leaves {root_mib} MiB for root, "
            f"but at least {MIN_ROOT_MIB} MiB is required "
            f"(Debian plus Xfce needs roughly 6 GiB installed)"
        )

    partitions = (
        Partition(1, ROLE_BIOS, "EF02", "portlin-bios", BIOS_BOOT_MIB),
        Partition(2, ROLE_ESP, "EF00", "portlin-esp", ESP_MIB),
        Partition(3, ROLE_BOOT, "8300", "portlin-boot", BOOT_MIB),
        Partition(4, ROLE_ROOT, "8300", "portlin-root", None),
    )
    return PartitionPlan(partitions, root_mib, encrypted)


def partition_path(device: str, number: int) -> str:
    """Device path for partition ``number`` of ``device``.

    Kernel naming splits on whether the parent name already ends in a digit:
    ``/dev/sdb`` yields ``/dev/sdb1``, but ``/dev/nvme0n1`` and ``/dev/loop0``
    yield ``/dev/nvme0n1p1`` and ``/dev/loop0p1``. Getting this wrong silently
    targets the wrong block device, so it is a function with its own tests.
    """
    device = device.rstrip("/")
    separator = "p" if device[-1:].isdigit() else ""
    return f"{device}{separator}{number}"


def sgdisk_argv(device: str, plan: PartitionPlan) -> list[list[str]]:
    """The sgdisk invocations that realise ``plan`` on ``device``."""
    create: list[str] = ["sgdisk"]
    for part in plan.partitions:
        size = "0" if part.size_mib is None else f"+{part.size_mib}M"
        create += [
            f"-n{part.number}:0:{size}",
            f"-t{part.number}:{part.typecode}",
            f"-c{part.number}:{part.name}",
        ]
    create.append(device)

    bios = plan.by_role(ROLE_BIOS)
    return [
        ["sgdisk", "--zap-all", device],
        create,
        # Attribute 2 is "legacy BIOS bootable". Some older firmware refuses to
        # boot a GPT disk without it even when a BIOS boot partition exists.
        ["sgdisk", f"-A{bios.number}:set:2", device],
    ]


def format_size(size_bytes: int) -> str:
    """Human-readable size, used in confirmation prompts."""
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            precision = 0 if unit in ("B", "KiB") else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
