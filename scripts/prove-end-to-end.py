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
    # printf, not echo: with --key-file - a trailing newline is part of the
    # key, and the hook created the container without one.
    if ! printf '%s' "{PASSPHRASE}" | cryptsetup open --key-file - "$p" portlin_root 2>&1; then
        echo "OPEN_FAILED"; losetup -d "$DEV"; exit 0
    fi
    ROOTDEV=/dev/mapper/portlin_root
fi
echo "OPENED"
dumpe2fs -h "$ROOTDEV" 2>/dev/null | grep -E "^Block count|^Block size"
M=$(mktemp -d)
if mount "$ROOTDEV" "$M" 2>&1; then
    echo "MOUNTED"
    grep -c '^user:' "$M/etc/passwd" 2>/dev/null | sed 's/^/ACCOUNTS=/'
    [ -e "$M/var/lib/portlin/firstboot-pending" ] && echo "SENTINEL_LEFT" || echo "SENTINEL_GONE"
    # The defaults the new settings screens apply when every answer is Enter.
    grep -q '^ENABLED=yes' "$M/etc/ufw/ufw.conf" 2>/dev/null && echo "FIREWALL_ON"
    [ -e "$M/etc/systemd/system/multi-user.target.wants/ssh.service" ] && echo "SSH_ENABLED"
    [ -s "$M/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml" ] && echo "SCREEN_LOCK"
    grep -q '^scale=auto' "$M/etc/portlin/display.conf" 2>/dev/null && echo "SCALE_AUTO"
    [ -s "$M/etc/NetworkManager/conf.d/50-portlin-mac.conf" ] && echo "MAC_RULE"
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
        # One Enter per screen: the wizard is curses, so an early key waits for
        # the next screen instead of being dropped, and a spare one would answer
        # a question nobody looked at. qemu's network is wired, so there is no
        # Wi-Fi list, and the passphrase was chosen at boot, so no offer to
        # change it.
        steps = [
            ("welcome", None, 8),
            ("keyboard", None, 25),
            ("language", None, 10),
            ("time zone", None, 10),
            ("hardware clock", None, 10),
            ("network", None, 20),
            ("full name", None, 5),
            ("username", None, 5),
            ("password", ACCOUNT_PASSWORD, 5),
            ("password again", ACCOUNT_PASSWORD, 12),
            ("security", None, 10),
            ("appearance", None, 10),
            ("hardware", None, 25),
            ("services", None, 10),
            ("storage", None, 20),
        ]
        for index, (label, text, wait) in enumerate(steps, start=3):
            log(f"  step: {label}")
            guest.screenshot(f"{index:02d}-{label.replace(' ', '-')}")
            if text is None:
                guest.enter()
            else:
                guest.answer(text)
            time.sleep(wait)

        guest.screenshot(f"{len(steps) + 3:02d}-summary")
        log("  step: summary -> apply")
        guest.enter()

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
        guest.enter()
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
    else:
        if "ACCOUNTS=1" not in output:
            verdict.append("setup did not create the account")
        if "SENTINEL_GONE" not in output:
            verdict.append("setup did not finish: its sentinel is still there")
        for marker, problem in (
            ("FIREWALL_ON", "the firewall was not switched on"),
            ("SCREEN_LOCK", "the screen-lock defaults were not written"),
            ("SCALE_AUTO", "the display-scale setting was not written"),
            ("MAC_RULE", "the hardware-address rule was not written"),
        ):
            if marker not in output:
                verdict.append(problem)
        if "SSH_ENABLED" in output:
            verdict.append("the SSH server is enabled although setup was told to leave it off")

    print()
    if verdict:
        for problem in verdict:
            print(f"FAIL: {problem}")
        print(f"\nScreenshots: {SHOTS}")
        return 1
    print(f"PASS: booted, {'encrypted, ' if encrypt else ''}ran setup, expanded, and mounts clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
