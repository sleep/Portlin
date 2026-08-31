#!/usr/bin/env python3
"""Test the expansion path against a real mounted filesystem, without booting.

Growing the stick is three commands on a live root: growpart, cryptsetup resize,
resize2fs. All three work on a mounted filesystem, which means none of them need
a boot to exercise -- and the wizard's device discovery can be pointed at a real
loop device just as easily as at a real stick.

This is the test that should have existed before any of the expansion code was
written. It is the one that would have caught the malformed partx argument, the
empty lsblk PKNAME on dm devices, and the udev-symlink assumption -- each of
which instead reached a USB stick.

The wizard keeps its own copy of this logic (apply_expand), because it runs at
first boot before any package can be installed, and the packaged tool
(portlin-expand) is what everyone runs after that. The tier rule accepts that
the two implementations can drift, which is exactly why --packaged drives the
real shipped command through this same harness rather than only the wizard's
copy of it.

    python3 scripts/test-expand.py [--encrypt] [--packaged]
"""

from __future__ import annotations

import argparse
import builtins
import getpass
import os
import pathlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WIZARD = REPO / "portlin" / "resources" / "firstboot" / "portlin-firstboot"
RUNTIME_DIR = REPO / "portlin" / "resources" / "runtime"
TOOL = RUNTIME_DIR / "portlin-expand"
PASSPHRASE = "expand-harness-passphrase"
DISK = Path("/tmp/portlin-expand-test.img")
DISK_SIZE = 32 * 1024**3
ROOT_MIB = 6500
MOUNT = Path("/mnt/expand-test")


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str]) -> str:
    result = run(argv)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def ensure_node(path: str) -> None:
    name = Path(path).name
    numbers = Path("/sys/class/block") / name / "dev"
    if not numbers.exists():
        raise SystemExit(f"kernel does not know about {path}")
    major, minor = numbers.read_text().strip().split(":")
    if Path(path).exists():
        os.unlink(path)
    must(["mknod", "-m", "0660", path, "b", major, minor])


def wizard_functions() -> dict:
    """The wizard's real discovery and expansion code, loaded as written.

    Imported from the shipped file rather than reimplemented, so this cannot
    drift from what actually runs on the stick.
    """
    source = WIZARD.read_text()
    namespace: dict = {
        "Path": pathlib.Path, "re": re, "os": os, "subprocess": subprocess,
        "log": lambda message: print(f"    wizard: {message}", flush=True),
    }
    exec(source[source.index("def _root_devices"):source.index("def step_expand")], namespace)
    exec(source[source.index("def _unused_inside_partition"):source.index("def step_expand")], namespace)
    exec(source[source.index("def _resize_mapping"):source.index("def step_autologin")], namespace)
    exec(source[source.index("def apply_expand"):source.index("def step_autologin")], namespace)
    namespace["ask_password"] = lambda *args, **kwargs: PASSPHRASE
    namespace["message"] = lambda *args, **kwargs: None

    def runner(argv, *, check=True, stdin=None):
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr}")
        return result

    namespace["run"] = runner
    return namespace


def run_packaged_expand(root_device: str) -> int:
    """Run the real shipped portlin-expand, exec'd from the file as written.

    Unlike the wizard, portlin-expand finds the root through the shared
    ``devices`` module rather than a copy of the same functions inlined into
    this harness's namespace -- so redirecting its `findmnt -no SOURCE /` means
    patching the ``subprocess`` name inside that already-imported module,
    which every one of its functions reads at call time. RUNTIME_DIR is put on
    sys.path in place of the /usr/lib/portlin the tool expects on a real stick.
    """
    if str(RUNTIME_DIR) not in sys.path:
        sys.path.insert(0, str(RUNTIME_DIR))
    import devices

    original_subprocess = devices.subprocess
    original_input = builtins.input
    original_getpass = getpass.getpass
    devices.subprocess = _RedirectedSubprocess(str(MOUNT), root_device)
    # Answers the "This cannot be undone" confirmation. A passphrase is also
    # primed in case cryptsetup falls back to prompting for one, whether or
    # not the kernel keyring makes that fallback necessary here.
    builtins.input = lambda prompt="": "y"
    getpass.getpass = lambda prompt="": PASSPHRASE

    try:
        source = TOOL.read_text()
        namespace: dict = {"__name__": "portlin_expand_under_test"}
        exec(compile(source, str(TOOL), "exec"), namespace)
        return namespace["main"]()
    finally:
        devices.subprocess = original_subprocess
        builtins.input = original_input
        getpass.getpass = original_getpass


def filesystem_gib(device: str) -> float:
    dump = run(["dumpe2fs", "-h", device])
    blocks = re.search(r"Block count:\s+(\d+)", dump.stdout)
    size = re.search(r"Block size:\s+(\d+)", dump.stdout)
    if not blocks or not size:
        raise SystemExit(f"could not read the size of {device}:\n{dump.stderr}")
    return int(blocks.group(1)) * int(size.group(1)) / 1024**3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--encrypt", action="store_true")
    parser.add_argument(
        "--packaged", action="store_true",
        help="drive the real portlin-expand command instead of the wizard's apply_expand",
    )
    args = parser.parse_args()

    if os.geteuid() != 0 or sys.platform != "linux":
        raise SystemExit("needs Linux and root; run inside the container harness")

    run(["umount", str(MOUNT)])
    run(["dmsetup", "remove", "-f", "portlin_root"])

    DISK.unlink(missing_ok=True)
    with open(DISK, "wb") as handle:
        handle.truncate(DISK_SIZE)

    # An 8 GB image's layout on a 32 GB disk: ~24 GB unclaimed at the end.
    must(["sgdisk", "-n1:0:+1M", "-t1:EF02", "-n2:0:+512M", "-t2:EF00",
          "-n3:0:+1024M", "-t3:8300", f"-n4:0:+{ROOT_MIB}M", "-t4:8300", str(DISK)])
    loop = must(["losetup", "-P", "-f", "--show", str(DISK)])
    partition = f"{loop}p4"

    try:
        ensure_node(partition)
        root_device = partition

        if args.encrypt:
            print("== creating a LUKS container ==", flush=True)
            must_input = subprocess.run(
                ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2",
                 "--pbkdf", "argon2id", "--pbkdf-memory", "32768",
                 "--key-file", "-", partition],
                input=PASSPHRASE, capture_output=True, text=True)
            if must_input.returncode != 0:
                raise SystemExit(f"luksFormat failed: {must_input.stderr}")
            opened = subprocess.run(
                ["cryptsetup", "open", "--key-file", "-", partition, "portlin_root"],
                input=PASSPHRASE, capture_output=True, text=True)
            if opened.returncode != 0:
                raise SystemExit(f"open failed: {opened.stderr}")
            root_device = "/dev/mapper/portlin_root"

        must(["mkfs.ext4", "-q", "-F", root_device])
        MOUNT.mkdir(parents=True, exist_ok=True)
        must(["mount", root_device, str(MOUNT)])
        (MOUNT / "canary.txt").write_text("survives expansion\n")

        before = filesystem_gib(root_device)
        print(f"== filesystem before: {before:.1f} GiB ==", flush=True)

        failures = []
        expanded = False

        if args.packaged:
            print("== running the packaged portlin-expand ==", flush=True)
            rc = run_packaged_expand(root_device)
            if rc != 0:
                failures.append(f"portlin-expand exited {rc}")
            else:
                expanded = True
        else:
            namespace = wizard_functions()
            # Point the wizard's discovery at this mount rather than the real root.
            namespace["subprocess"] = _RedirectedSubprocess(str(MOUNT), root_device)

            located = namespace["_root_devices"]()
            print(f"  discovery -> {located}", flush=True)
            if located is None:
                failures.append("the wizard could not identify the root device")
            else:
                disk, part, _ = located
                free = namespace["_free_space_bytes"](disk, part)
                print(f"  free space -> {free / 1024**3:.1f} GiB", flush=True)
                if free < 20 * 1024**3:
                    failures.append(f"only {free / 1024**3:.1f} GiB seen free; expected ~24")

                print("== running apply_expand ==", flush=True)
                namespace["apply_expand"]()
                expanded = True

        if expanded:
            after = filesystem_gib(root_device)
            print(f"== filesystem after: {after:.1f} GiB ==", flush=True)
            if after < 25:
                failures.append(f"filesystem is {after:.1f} GiB; it did not grow")

            canary = MOUNT / "canary.txt"
            if not canary.exists() or "survives" not in canary.read_text():
                failures.append("data did not survive expansion")
            if run(["e2fsck", "-fn", root_device]).returncode > 1:
                failures.append("filesystem is inconsistent after expansion")

        print()
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}")
            return 1
        print("PASS: discovered the disk, grew the filesystem, data intact")
        return 0
    finally:
        run(["umount", str(MOUNT)])
        run(["cryptsetup", "close", "portlin_root"])
        run(["losetup", "-d", loop])
        DISK.unlink(missing_ok=True)


class _RedirectedSubprocess:
    """Makes a `findmnt /` answer with the test mount instead of the real root.

    Shared by both the wizard and the packaged tool's discovery. Only that one
    question is redirected; every other command runs for real, so the device
    walk, the sysfs reads and the three resize commands are exactly the ones
    that run on a stick.
    """

    def __init__(self, mountpoint: str, device: str) -> None:
        self.mountpoint = mountpoint
        self.device = device

    def run(self, argv, **kwargs):
        if argv[:2] == ["findmnt", "-no"] and argv[-1] == "/":
            argv = argv[:-1] + [self.mountpoint]
        return subprocess.run(argv, **kwargs)

    def __getattr__(self, name):
        return getattr(subprocess, name)


if __name__ == "__main__":
    sys.exit(main())
