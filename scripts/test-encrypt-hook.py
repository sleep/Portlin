#!/usr/bin/env python3
"""Run the initramfs encryption script for real, without booting anything.

The script is an ordinary program: given a block device, a $ROOT, and a console
to talk to, it does its work. So this builds exactly that -- a loop device laid
out like a portlin stick, a stub /scripts/functions, a fake /proc/cmdline and a
pty standing in for /dev/console -- drives it end to end, and then checks the
result is a LUKS container whose filesystem actually mounts.

Roughly one minute, versus fifteen for an emulated boot read off a photograph.
Every bug found in that script so far would have died here.

Requires Linux, root, and losetup. Run it inside a privileged linux/amd64
container on any other host; see scripts/test-firstboot.sh.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "portlin" / "resources" / "firstboot" / "portlin-encrypt.local-top"
PASSPHRASE = "harness-test-passphrase"
DISK = Path("/tmp/portlin-hook-test.img")
DISK_SIZE = 12 * 1024**3
ROOT_MIB = 6500


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str]) -> str:
    result = run(argv)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def ensure_node(path: str) -> None:
    """Create the partition node if nothing else has. No udev in a container."""
    name = Path(path).name
    dev = Path("/sys/class/block") / name / "dev"
    if not dev.exists():
        raise SystemExit(f"kernel does not know about {path}")
    major, minor = dev.read_text().strip().split(":")
    if Path(path).exists():
        os.unlink(path)
    must(["mknod", "-m", "0660", path, "b", major, minor])


def stub_initramfs_environment() -> None:
    """The pieces of an initramfs the script expects to find."""
    Path("/scripts").mkdir(exist_ok=True)
    Path("/scripts/functions").write_text(
        "resolve_device() {\n"
        '    case "$1" in\n'
        '        UUID=*) echo "/dev/disk/by-uuid/${1#UUID=}" ;;\n'
        '        *) echo "$1" ;;\n'
        "    esac\n"
        "}\n"
    )
    # The script gates on this, and a container's /proc/cmdline is the host's.
    fake = Path("/tmp/fake-cmdline")
    fake.write_text("BOOT_IMAGE=/vmlinuz root=UUID=x ro quiet portlin.encrypt=ask\n")
    run(["mount", "--bind", str(fake), "/proc/cmdline"])


def drive(script_env: dict[str, str], answers: list[str]) -> tuple[int, str]:
    """Run the script with a pty for its console, feeding it ``answers``."""
    master, slave = pty.openpty()
    env = {**os.environ, **script_env, "PORTLIN_CONSOLE": os.ttyname(slave)}

    output = open("/tmp/hook-stdout.log", "w")
    proc = subprocess.Popen(
        ["sh", str(SCRIPT)], env=env, stdout=output, stderr=subprocess.STDOUT, text=True
    )
    os.close(slave)

    for answer in answers:
        time.sleep(1.5)
        os.write(master, (answer + "\n").encode())

    transcript = ""
    deadline = time.time() + 600
    os.set_blocking(master, False)
    while proc.poll() is None and time.time() < deadline:
        try:
            chunk = os.read(master, 65536)
            if chunk:
                transcript += chunk.decode(errors="replace")
        except (BlockingIOError, OSError):
            time.sleep(0.2)
    try:
        transcript += os.read(master, 1 << 20).decode(errors="replace")
    except OSError:
        pass
    os.close(master)
    output.close()
    # Anything the script's commands wrote outside the console belongs in the
    # transcript too; losing it is how a failure reason goes unseen.
    transcript += "\n--- script stdout/stderr ---\n"
    transcript += Path("/tmp/hook-stdout.log").read_text()
    return proc.wait(), transcript


def main() -> int:
    if os.geteuid() != 0 or sys.platform != "linux":
        raise SystemExit("needs Linux and root; run inside the container harness")

    for stale in ("portlin_root", "portlin_harness"):
        run(["dmsetup", "remove", "-f", stale])

    DISK.unlink(missing_ok=True)
    with open(DISK, "wb") as handle:
        handle.truncate(DISK_SIZE)

    must(["sgdisk", "-n1:0:+1M", "-t1:EF02", "-n2:0:+512M", "-t2:EF00",
          "-n3:0:+1024M", "-t3:8300", f"-n4:0:+{ROOT_MIB}M", "-t4:8300", str(DISK)])
    loop = must(["losetup", "-P", "-f", "--show", str(DISK)])
    root_part = f"{loop}p4"

    try:
        ensure_node(root_part)
        must(["mkfs.ext4", "-q", "-F", root_part])

        # Real content, so a corrupted result is detectable rather than theoretical.
        Path("/mnt/seed").mkdir(exist_ok=True)
        must(["mount", root_part, "/mnt/seed"])
        (Path("/mnt/seed") / "canary.txt").write_text("survives encryption\n")
        must(["umount", "/mnt/seed"])

        uuid = must(["blkid", "-s", "UUID", "-o", "value", root_part])
        stub_initramfs_environment()
        Path("/dev/disk/by-uuid").mkdir(parents=True, exist_ok=True)
        link = Path("/dev/disk/by-uuid") / uuid
        if not link.exists():
            link.symlink_to(root_part)

        print(f"== driving the hook against {root_part} ==")
        code, transcript = drive(
            {"ROOT": f"UUID={uuid}"}, ["y", PASSPHRASE, PASSPHRASE]
        )
        print(transcript)

        failures = []
        if "Encryption complete" not in transcript:
            failures.append("the script never reported completing encryption")
        if run(["cryptsetup", "isLuks", root_part]).returncode != 0:
            failures.append("the partition is not a LUKS container")

        # Check the mapping the SCRIPT created, which is what the rest of boot
        # would use. Opening a second one only proves the passphrase works and
        # fails with "already mapped" precisely when the script did its job.
        mapper = Path("/dev/mapper/portlin_root")
        if not mapper.exists():
            failures.append("the script did not leave the container unlocked")
        else:
            check = run(["e2fsck", "-fn", str(mapper)])
            if check.returncode > 1:
                # The bug that shipped: a filesystem larger than its device.
                failures.append(f"filesystem is inconsistent:\n{check.stdout}")
            mounted = run(["mount", str(mapper), "/mnt/seed"])
            if mounted.returncode != 0:
                failures.append(f"cannot mount the unlocked root: {mounted.stderr}")
            else:
                canary = Path("/mnt/seed/canary.txt")
                if not canary.exists() or "survives" not in canary.read_text():
                    failures.append("data did not survive encryption")
                run(["umount", "/mnt/seed"])

            # And it must still open from scratch, as it will on every later boot.
            run(["cryptsetup", "close", "portlin_root"])
            reopened = run(["cryptsetup", "open", "--key-file", "-",
                            root_part, "portlin_root"], input=PASSPHRASE)
            if reopened.returncode != 0:
                failures.append(f"cannot reopen on a later boot: {reopened.stderr}")

        # The passphrase has to survive into userspace. On this boot the
        # crypttab keyscript cannot stash it -- there is no crypttab yet -- and
        # without a stash the first-boot wizard asks for it again, minutes after
        # it was typed here, to grow the mapping into the rest of the drive.
        stash = Path("/run/portlin/luks-pass")
        if not stash.exists():
            failures.append("the hook left no passphrase for the wizard")
        else:
            if stash.read_bytes() != PASSPHRASE.encode():
                failures.append("the stash is not the passphrase that was typed")
            mode = stash.stat().st_mode & 0o777
            if mode != 0o600:
                failures.append(f"the stash is readable beyond root (mode {mode:04o})")

        print()
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}")
            return 1
        print("PASS: encrypted in place, opens, mounts clean, data intact,"
              " passphrase handed to the wizard")
        return 0
    finally:
        Path("/run/portlin/luks-pass").unlink(missing_ok=True)
        run(["umount", "/mnt/seed"])
        run(["cryptsetup", "close", "portlin_root"])
        run(["umount", "/proc/cmdline"])
        run(["losetup", "-d", loop])
        DISK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
