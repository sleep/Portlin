"""Host checks that must pass before anything destructive or slow begins.

The point is to fail in a second rather than twenty minutes into a debootstrap,
and to fail with a sentence that names the package to install rather than a
FileNotFoundError from deep inside an orchestration.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass

from .errors import PreflightError

# Binary -> the Debian package that provides it, so the error can be actionable.
BUILD_TOOLS = {
    "debootstrap": "debootstrap",
    "chroot": "coreutils",
    "tar": "tar",
    "zstd": "zstd",
    "mount": "mount",
    "umount": "mount",
}

WRITE_TOOLS = {
    "sgdisk": "gdisk",
    "partprobe": "parted",
    "mkfs.vfat": "dosfstools",
    "mkfs.ext4": "e2fsprogs",
    "blkid": "util-linux",
    "blockdev": "util-linux",
    "lsblk": "util-linux",
    "losetup": "util-linux",
    "mount": "mount",
    "umount": "mount",
    "chroot": "coreutils",
    "tar": "tar",
    "zstd": "zstd",
}

ENCRYPT_TOOLS = {
    "cryptsetup": "cryptsetup",
}

# Nice to have, never required. udevadm settle waits out the race between the
# kernel rewriting a partition table and udev creating the new device nodes. If
# udevadm is absent there is no udev daemon, so there is no race to wait for and
# partprobe alone is sufficient. Demanding it would lock minimal and
# containerised hosts out of writing a stick for no reason.
OPTIONAL_TOOLS = {
    "udevadm": "systemd",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def missing_tools(tools: dict[str, str]) -> dict[str, str]:
    return {binary: pkg for binary, pkg in tools.items() if shutil.which(binary) is None}


def check_root() -> Check:
    is_root = os.geteuid() == 0
    return Check(
        "root",
        is_root,
        "running as root" if is_root else "portlin must run as root (try sudo)",
    )


def check_arch() -> Check:
    machine = platform.machine()
    ok = machine in ("x86_64", "amd64")
    detail = f"host architecture is {machine}"
    if not ok:
        detail += (
            ". Building a Debian amd64 system requires running amd64 binaries, "
            "so this must run on an x86_64 host."
        )
    return Check("architecture", ok, detail)


def check_linux() -> Check:
    ok = platform.system() == "Linux"
    return Check(
        "kernel",
        ok,
        "Linux" if ok else f"{platform.system()} cannot create Linux block devices or chroots",
    )


def check_tools(tools: dict[str, str], label: str) -> Check:
    missing = missing_tools(tools)
    if not missing:
        return Check(label, True, f"all {len(tools)} required tools present")
    packages = sorted(set(missing.values()))
    return Check(
        label,
        False,
        f"missing: {', '.join(sorted(missing))}. Install with: apt install {' '.join(packages)}",
    )


def check_optional_tools() -> Check:
    """Always reports ok. Present so 'doctor' can mention what is missing."""
    missing = missing_tools(OPTIONAL_TOOLS)
    if not missing:
        return Check("optional tools", True, "all present")
    packages = sorted(set(missing.values()))
    return Check(
        "optional tools",
        True,
        f"absent but not required: {', '.join(sorted(missing))} "
        f"(from {', '.join(packages)})",
    )


def run_checks(*, need_build: bool, need_write: bool, need_encrypt: bool) -> list[Check]:
    checks = [check_linux(), check_arch(), check_root()]
    if need_build:
        checks.append(check_tools(BUILD_TOOLS, "build tools"))
    if need_write:
        checks.append(check_tools(WRITE_TOOLS, "write tools"))
        checks.append(check_optional_tools())
    if need_encrypt:
        checks.append(check_tools(ENCRYPT_TOOLS, "encryption tools"))
    return checks


def require(*, need_build: bool = False, need_write: bool = False, need_encrypt: bool = False) -> None:
    """Raise PreflightError listing every problem at once.

    Reporting all failures together matters: a user missing three packages should
    learn that in one run, not discover them one reboot at a time.
    """
    failures = [c for c in run_checks(
        need_build=need_build, need_write=need_write, need_encrypt=need_encrypt
    ) if not c.ok]
    if failures:
        lines = "\n".join(f"  - {c.name}: {c.detail}" for c in failures)
        raise PreflightError(f"host is not ready:\n{lines}")
