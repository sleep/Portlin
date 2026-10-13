#!/usr/bin/env python3
"""Run the initramfs encryption script for real, without booting anything.

The script is an ordinary program: given a block device, a $ROOT, and a console
to talk to, it does its work. So this builds exactly that -- a loop device laid
out like a portlin stick, a stub /scripts/functions, a fake /proc/cmdline and a
pty standing in for /dev/console -- drives it end to end, and then checks the
result is a LUKS container whose filesystem actually mounts.

Then the other ways a boot can go, each on a fresh drive: answering no,
failing to pick a passphrase, and an encryption killed partway, as by a power
cut, which the next boot has to finish.

Roughly one minute, versus fifteen for an emulated boot read off a photograph.
Every bug found in that script so far would have died here.

Requires Linux, root, and losetup. Run it inside a privileged linux/amd64
container on any other host; see scripts/test-firstboot.sh.
"""

from __future__ import annotations

import os
import pty
import re
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


def drive_script(script_env: dict[str, str], answers: list[str]) -> tuple[int, str]:
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
    code = proc.wait()
    print(transcript)
    return code, transcript


class Drive:
    """A loop device laid out like a stick, with a canary file on its root."""

    def __init__(self) -> None:
        DISK.unlink(missing_ok=True)
        with open(DISK, "wb") as handle:
            handle.truncate(DISK_SIZE)
        must(["sgdisk", "-n1:0:+1M", "-t1:EF02", "-n2:0:+512M", "-t2:EF00",
              "-n3:0:+1024M", "-t3:8300", f"-n4:0:+{ROOT_MIB}M", "-t4:8300", str(DISK)])
        self.loop = must(["losetup", "-P", "-f", "--show", str(DISK)])
        self.root = f"{self.loop}p4"
        ensure_node(self.root)
        must(["mkfs.ext4", "-q", "-F", self.root])

        # Real content, so a corrupted result is detectable rather than theoretical.
        Path("/mnt/seed").mkdir(exist_ok=True)
        must(["mount", self.root, "/mnt/seed"])
        (Path("/mnt/seed") / "canary.txt").write_text("survives encryption\n")
        must(["umount", "/mnt/seed"])

        self.uuid = must(["blkid", "-s", "UUID", "-o", "value", self.root])
        Path("/dev/disk/by-uuid").mkdir(parents=True, exist_ok=True)
        link = Path("/dev/disk/by-uuid") / self.uuid
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(self.root)

    def env(self) -> dict[str, str]:
        return {"ROOT": f"UUID={self.uuid}"}

    def close(self) -> None:
        run(["umount", "/mnt/seed"])
        run(["cryptsetup", "close", "portlin_root"])
        run(["losetup", "-d", self.loop])
        DISK.unlink(missing_ok=True)


def clear_run_state() -> None:
    for name in ("luks-pass", "encrypt-failed", "encrypt-declined", "just-encrypted"):
        (Path("/run/portlin") / name).unlink(missing_ok=True)


def check_unlocked(drive: Drive, failures: list[str]) -> None:
    """The mapping the script left open is a clean filesystem with the canary."""
    if run(["cryptsetup", "isLuks", drive.root]).returncode != 0:
        failures.append("the partition is not a LUKS container")

    # Check the mapping the SCRIPT created, which is what the rest of boot
    # would use. Opening a second one only proves the passphrase works and
    # fails with "already mapped" precisely when the script did its job.
    mapper = Path("/dev/mapper/portlin_root")
    if not mapper.exists():
        failures.append("the script did not leave the container unlocked")
        return
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
                    drive.root, "portlin_root"], input=PASSPHRASE)
    if reopened.returncode != 0:
        failures.append(f"cannot reopen on a later boot: {reopened.stderr}")


def check_stash(failures: list[str]) -> None:
    # The passphrase has to survive into userspace. On this boot the crypttab
    # keyscript cannot stash it -- there is no crypttab yet -- and without a
    # stash the first-boot wizard asks for it again, minutes after it was
    # typed here, to grow the mapping into the rest of the drive.
    stash = Path("/run/portlin/luks-pass")
    if not stash.exists():
        failures.append("the hook left no passphrase for the wizard")
        return
    if stash.read_bytes() != PASSPHRASE.encode():
        failures.append("the stash is not the passphrase that was typed")
    mode = stash.stat().st_mode & 0o777
    if mode != 0o600:
        failures.append(f"the stash is readable beyond root (mode {mode:04o})")


def check_still_plain(drive: Drive, failures: list[str]) -> None:
    if run(["cryptsetup", "isLuks", drive.root]).returncode == 0:
        failures.append("the partition was encrypted anyway")
    elif run(["e2fsck", "-fn", drive.root]).returncode > 1:
        failures.append("the plaintext filesystem was damaged")


def scenario_encrypt(drive: Drive) -> list[str]:
    code, transcript = drive_script(drive.env(), ["y", PASSPHRASE, PASSPHRASE])
    failures = []
    if "Encryption complete" not in transcript:
        failures.append("the script never reported completing encryption")
    # cryptsetup's own summary line, printed only when progress reporting
    # is live. Without it the console sits blank for the whole encryption.
    if "Finished, time" not in transcript:
        failures.append("cryptsetup showed no progress on the console")
    # Every block is rewritten, so the prompt has to say how many there are.
    if f"{ROOT_MIB // 1024} GiB partition" not in transcript:
        failures.append("the prompt did not give the partition's size")
    check_unlocked(drive, failures)
    check_stash(failures)
    return failures


def scenario_decline(drive: Drive) -> list[str]:
    code, transcript = drive_script(drive.env(), ["n"])
    failures = []
    if "Continuing without encryption" not in transcript:
        failures.append("declining did not say so")
    if not Path("/run/portlin/encrypt-declined").exists():
        failures.append("declining left nothing for the finaliser to retire the offer by")
    if Path("/run/portlin/encrypt-failed").exists():
        failures.append("declining was recorded as a failure")
    check_still_plain(drive, failures)
    return failures


def scenario_no_passphrase(drive: Drive) -> list[str]:
    # Matching but too short, three times: the case that once got through.
    # The last answer is the keypress the failure waits for.
    code, transcript = drive_script(drive.env(), ["y"] + ["short"] * 6 + [""])
    failures = []
    if "Press Enter to continue" not in transcript:
        failures.append("the failure did not wait to be read")
    failed = Path("/run/portlin/encrypt-failed")
    if not failed.exists() or "passphrase" not in failed.read_text():
        failures.append("the failure was not recorded for the wizard")
    check_still_plain(drive, failures)
    return failures


def scenario_interrupted(drive: Drive) -> list[str]:
    """Kill an encryption partway, then boot again and finish it."""
    failures = []
    # The shrink the script would have done first; the header needs the room.
    sectors = int(Path(f"/sys/class/block/{Path(drive.root).name}/size").read_text())
    must(["e2fsck", "-f", "-y", drive.root])
    must(["resize2fs", drive.root, f"{(sectors * 512 - 32 * 1024**2) // 1024}K"])
    log = open("/tmp/killed-reencrypt.log", "w+")
    proc = subprocess.Popen(
        ["cryptsetup", "reencrypt", "--encrypt", "--type", "luks2",
         "--reduce-device-size", "32M", "--pbkdf", "argon2id",
         "--pbkdf-memory", "65536", "--iter-time", "200",
         "--key-file", "-", "--progress-frequency", "1", drive.root],
        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(PASSPHRASE.encode())
    proc.stdin.close()
    deadline = time.time() + 300
    while time.time() < deadline:
        log.seek(0)
        if re.search(r"Progress:\s+[1-9]\d\.", log.read()):
            break
        time.sleep(0.2)
    else:
        return ["the encryption to interrupt never got going"]
    proc.kill()
    proc.wait()
    dump = run(["cryptsetup", "luksDump", drive.root]).stdout
    if "online-reencrypt" not in dump:
        return ["killing cryptsetup did not leave an encryption in progress"]

    # A wrong passphrase first: it must be refused without touching anything.
    code, transcript = drive_script(drive.env(), ["not-the-passphrase", PASSPHRASE])
    if "interrupted last time" not in transcript:
        failures.append("the next boot did not recognise the interrupted encryption")
    if "That passphrase was not accepted" not in transcript:
        failures.append("a wrong passphrase was not refused")
    if "Finished, time" not in transcript:
        failures.append("the encryption was not resumed")
    if "online-reencrypt" in run(["cryptsetup", "luksDump", drive.root]).stdout:
        failures.append("the drive is still marked as mid-encryption")
    check_unlocked(drive, failures)
    check_stash(failures)
    return failures


SCENARIOS = [
    ("encrypt", scenario_encrypt),
    ("decline", scenario_decline),
    ("no usable passphrase", scenario_no_passphrase),
    ("interrupted, then resumed", scenario_interrupted),
]


def main() -> int:
    if os.geteuid() != 0 or sys.platform != "linux":
        raise SystemExit("needs Linux and root; run inside the container harness")

    for stale in ("portlin_root", "portlin_harness"):
        run(["dmsetup", "remove", "-f", stale])
    stub_initramfs_environment()

    failed = False
    try:
        for name, scenario in SCENARIOS:
            print(f"== {name} ==")
            clear_run_state()
            drive = Drive()
            try:
                failures = scenario(drive)
            finally:
                drive.close()
            for failure in failures:
                print(f"FAIL ({name}): {failure}")
            if not failures:
                print(f"PASS ({name})")
            failed = failed or bool(failures)
    finally:
        clear_run_state()
        run(["umount", "/proc/cmdline"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
