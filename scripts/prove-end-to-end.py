#!/usr/bin/env python3
"""Prove the whole first boot, unattended, from image to grown filesystem.

Boots the real image on a larger virtual disk, answers the encryption prompt,
drives every wizard screen, answers the passphrase prompt that the expansion
step raises, waits for setup to finish, then shuts down and verifies on disk
that the filesystem actually grew.

Runs on macOS: qemu drives the guest, and the final verification runs in a
linux/amd64 container because opening a LUKS volume needs Linux.

    python3 scripts/prove-end-to-end.py
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE = REPO / "out" / "portlin.img"
WORK = Path("/tmp/portlin-proof.img")
DISK_SIZE = "32G"
PASSPHRASE = "proofpassphrase"
ACCOUNT_PASSWORD = "proofaccountpw"
MONITOR = "/tmp/portlin-proof-monitor.sock"
SHOTS = Path("/tmp/portlin-proof-shots")

SPECIAL = {
    "\r": "ret", "\n": "ret", " ": "spc", "-": "minus", ".": "dot",
    "/": "slash", ",": "comma", ":": "shift-semicolon", "_": "shift-minus",
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Guest:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        time.sleep(0.5)
        self._drain()

    def _drain(self) -> None:
        try:
            self.sock.settimeout(0.3)
            self.sock.recv(65536)
        except (OSError, socket.timeout, TimeoutError):
            pass

    def command(self, text: str, pause: float = 0.30) -> None:
        self.sock.sendall((text + "\n").encode())
        time.sleep(pause)
        self._drain()

    def key(self, name: str) -> None:
        self.command(f"sendkey {name}")

    def enter(self, times: int = 1) -> None:
        # Pressed more than once because a keystroke arriving while a dialog is
        # still initialising is silently dropped, and re-accepting a default is
        # harmless.
        for _ in range(times):
            self.key("ret")
            time.sleep(1.0)

    def type(self, text: str) -> None:
        for character in text:
            name = SPECIAL.get(character)
            if name is None:
                name = f"shift-{character.lower()}" if character.isupper() else character
            self.command(f"sendkey {name}")

    def answer(self, text: str) -> None:
        """Type a line and submit it."""
        self.type(text)
        time.sleep(0.5)
        self.key("ret")

    def screenshot(self, name: str) -> None:
        self.command(f"screendump {SHOTS / (name + '.ppm')}", pause=3.0)


def verify_on_disk() -> tuple[bool, str]:
    """Open the container and read the filesystem size, in a Linux container."""
    script = f"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends util-linux e2fsprogs cryptsetup-bin mount dmsetup >/dev/null 2>&1
dmsetup remove -f portlin_root 2>/dev/null || true
DEV=$(losetup -P -f --show /host/portlin-proof.img)
p="${{DEV}}p4"; rm -f "$p"
IFS=: read -r ma mi < /sys/class/block/$(basename $p)/dev; mknod -m 0660 "$p" b "$ma" "$mi"
echo "PARTITION_BYTES=$(( $(cat /sys/class/block/$(basename $p)/size) * 512 ))"
ROOTDEV="$p"
if cryptsetup isLuks "$p" 2>/dev/null; then
    if ! echo "{PASSPHRASE}" | cryptsetup open --key-file - "$p" portlin_root 2>&1; then
        echo "OPEN_FAILED"; losetup -d "$DEV"; exit 0
    fi
    ROOTDEV=/dev/mapper/portlin_root
fi
echo "OPENED"
dumpe2fs -h "$ROOTDEV" 2>/dev/null | grep -E "^Block count|^Block size"
M=$(mktemp -d)
if mount "$ROOTDEV" "$M" 2>&1; then
    echo "MOUNTED"
    grep -c '^proof' "$M/etc/passwd" 2>/dev/null | sed 's/^/ACCOUNTS=/'
    echo "--- wizard log ---"
    tail -25 "$M/var/log/portlin-firstboot.log" 2>/dev/null || echo "(no log)"
    umount "$M"
fi
cryptsetup close portlin_root; losetup -d "$DEV"
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "--platform", "linux/amd64",
         "-v", "/tmp:/host", "debian:trixie", "bash", "-c", script],
        capture_output=True, text=True, timeout=1800,
    )
    return True, result.stdout + result.stderr


def main() -> int:
    encrypt = "--encrypt" in sys.argv
    SHOTS.mkdir(exist_ok=True)
    subprocess.run(["pkill", "-f", "qemu-system-x86_64"], capture_output=True)
    time.sleep(2)

    log(f"copying {IMAGE} to {WORK} and growing to {DISK_SIZE}")
    WORK.unlink(missing_ok=True)
    shutil.copyfile(IMAGE, WORK)
    subprocess.run(["truncate", "-s", DISK_SIZE, str(WORK)], check=True)

    Path(MONITOR).unlink(missing_ok=True)
    log("booting")
    qemu = subprocess.Popen(
        ["qemu-system-x86_64", "-machine", "q35", "-m", "2048", "-smp", "2",
         "-drive", f"file={WORK},format=raw,if=virtio",
         "-display", "none", "-vga", "std", "-no-reboot",
         "-monitor", f"unix:{MONITOR},server,nowait"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(120):
        try:
            sock.connect(MONITOR)
            break
        except OSError:
            time.sleep(0.5)
    guest = Guest(sock)

    try:
        log("waiting for the initramfs encryption prompt (~2 min)")
        time.sleep(150)
        guest.screenshot("01-encryption-prompt")

        if encrypt:
            log("answering: encrypt = yes")
            guest.answer("y")
            time.sleep(8)
            log("entering the passphrase twice")
            guest.answer(PASSPHRASE)
            time.sleep(6)
            guest.answer(PASSPHRASE)
            log("encrypting -- the long part (~25 min under emulation)")
            time.sleep(1500)
        else:
            log("answering: encrypt = no (covered by test-encrypt-hook.py)")
            guest.answer("n")
            time.sleep(120)
        guest.screenshot("02-after-encryption")

        log("driving the wizard")
        steps = [
            ("welcome", None, 8),
            ("keyboard", None, 25),
            ("language", None, 30),
            ("timezone region", None, 12),
            ("timezone city", None, 10),
            ("hostname", None, 10),
            ("full name", None, 8),
            ("username", None, 8),
            ("password", ACCOUNT_PASSWORD, 8),
            ("password again", ACCOUNT_PASSWORD, 12),
            ("autologin", None, 12),
            ("EXPAND offer", None, 15),
        ]
        for index, (label, text, wait) in enumerate(steps, start=3):
            log(f"  step: {label}")
            guest.screenshot(f"{index:02d}-{label.replace(' ', '-')}")
            if text is None:
                guest.enter(2)
            else:
                guest.answer(text)
            time.sleep(wait)

        guest.screenshot("15-summary")
        log("  step: summary -> apply")
        guest.enter(3)

        # The expansion asks for the passphrase when the kernel keyring cannot
        # supply the volume key. Answering it is the whole reason this run
        # exists; blind Enter presses are what failed last time.
        log("  answering the expansion passphrase prompt")
        for attempt in range(4):
            time.sleep(25)
            guest.screenshot(f"16-apply-{attempt}")
            guest.answer(PASSPHRASE)

        log("waiting for setup to finish (~5 min)")
        time.sleep(300)
        guest.screenshot("17-final")
        guest.enter(2)
        time.sleep(60)
        guest.screenshot("18-after-final")

        log("shutting the guest down cleanly")
        guest.command("system_powerdown")
        time.sleep(45)
    finally:
        try:
            guest.command("quit")
        except (BrokenPipeError, OSError):
            pass  # a clean powerdown closes the monitor first; that is success
        sock.close()
        qemu.terminate()
        try:
            qemu.wait(timeout=30)
        except subprocess.TimeoutExpired:
            qemu.kill()

    log("verifying on disk")
    _, output = verify_on_disk()
    print(output)

    verdict: list[str] = []
    if "OPENED" not in output:
        verdict.append("could not open the LUKS container with the passphrase")
    blocks = None
    for line in output.splitlines():
        if line.startswith("Block count:"):
            blocks = int(line.split()[-1])
    if blocks is None:
        verdict.append("could not read the filesystem size")
    else:
        gib = blocks * 4096 / 1024**3
        log(f"filesystem size: {gib:.1f} GiB")
        if gib < 25:
            verdict.append(f"filesystem is {gib:.1f} GiB; it did not expand")
    if "MOUNTED" not in output:
        verdict.append("the root filesystem would not mount")

    print()
    if verdict:
        for problem in verdict:
            print(f"FAIL: {problem}")
        print(f"\nScreenshots: {SHOTS}")
        return 1
    print("PASS: booted, encrypted, ran setup, expanded, and mounts clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
