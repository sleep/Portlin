"""Block device enumeration and the safety rules that guard a destructive write.

This tool's worst possible failure is erasing someone's internal disk. The rules
live here as a pure function returning a list of problems, so every one of them
is covered by a test rather than by hope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import TargetError
from .layout import MIN_TARGET_BYTES, format_size
from .runner import Runner

LSBLK_COLUMNS = "NAME,PATH,MODEL,SIZE,RM,HOTPLUG,TYPE,TRAN,MOUNTPOINTS"

# Enough of a real lsblk response for dry runs to stay coherent.
_DRY_LSBLK = json.dumps(
    {
        "blockdevices": [
            {
                "name": "sdz",
                "path": "/dev/sdz",
                "model": "DRY RUN",
                "size": 32 * 1024**3,
                "rm": True,
                "hotplug": True,
                "type": "disk",
                "tran": "usb",
                "mountpoints": [None],
                "children": [],
            }
        ]
    }
)


@dataclass(frozen=True)
class BlockDevice:
    path: str
    name: str
    model: str
    size_bytes: int
    removable: bool
    transport: str
    mountpoints: tuple[str, ...]

    @property
    def is_mounted(self) -> bool:
        return bool(self.mountpoints)

    def describe(self) -> str:
        flags = []
        if self.removable:
            flags.append("removable")
        if self.transport:
            flags.append(self.transport)
        if self.is_mounted:
            flags.append(f"mounted: {', '.join(self.mountpoints)}")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        model = self.model or "unknown model"
        return f"{self.path}  {format_size(self.size_bytes)}  {model}{suffix}"


def _collect_mountpoints(node: dict) -> list[str]:
    """Every mountpoint on a device or any of its partitions."""
    found = [m for m in (node.get("mountpoints") or []) if m]
    for child in node.get("children") or []:
        found.extend(_collect_mountpoints(child))
    return found


def parse_lsblk(payload: str) -> list[BlockDevice]:
    """Parse ``lsblk -J -b`` output into BlockDevice records.

    Only whole disks are returned. Partitions, loop devices and device-mapper
    nodes are not valid targets and are filtered out here rather than being
    rejected later with a confusing error.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TargetError(f"could not parse lsblk output: {exc}") from exc

    devices: list[BlockDevice] = []
    for node in data.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        # lsblk reports rm=false for many USB enclosures and card readers, so
        # hotplug is the more reliable signal. Either one counts.
        removable = bool(node.get("rm")) or bool(node.get("hotplug"))
        devices.append(
            BlockDevice(
                path=node.get("path") or f"/dev/{node.get('name')}",
                name=node.get("name") or "",
                model=(node.get("model") or "").strip(),
                size_bytes=int(node.get("size") or 0),
                removable=removable,
                transport=(node.get("tran") or "").strip(),
                mountpoints=tuple(_collect_mountpoints(node)),
            )
        )
    return devices


def list_devices(runner: Runner) -> list[BlockDevice]:
    payload = runner.output(
        ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS],
        dry_stdout=_DRY_LSBLK,
    )
    return parse_lsblk(payload)


def find_device(runner: Runner, path: str) -> BlockDevice | None:
    for device in list_devices(runner):
        if device.path == path:
            return device
    return None


def safety_problems(device: BlockDevice, *, force: bool = False) -> list[str]:
    """Reasons this device must not be written to.

    An empty list means the write may proceed after user confirmation.
    ``force`` waives the removability check only. Size and mount state are never
    waived: writing to a mounted filesystem corrupts it regardless of intent, and
    a stick too small to hold the system is useless even if the write succeeds.
    """
    problems: list[str] = []

    if not device.removable and not force:
        problems.append(
            f"{device.path} is not removable and looks like an internal disk. "
            f"Pass --force only if you are certain."
        )
    if device.size_bytes < MIN_TARGET_BYTES:
        problems.append(
            f"{device.path} is {format_size(device.size_bytes)}, below the "
            f"{format_size(MIN_TARGET_BYTES)} minimum."
        )
    if device.is_mounted:
        problems.append(
            f"{device.path} has mounted filesystems ({', '.join(device.mountpoints)}). "
            f"Unmount them first."
        )
    return problems
