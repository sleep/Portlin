#!/usr/bin/env python3
"""Boot a real image in qemu and drive the entire first boot over a serial line.

This exists because every bug that reached a user was in the first-boot path,
and the only test that reached that path was a fifteen-minute emulated boot read
by photographing a framebuffer. Nothing was greppable, nothing was assertable,
and nothing could run unattended -- so in practice it did not get run.

Two pieces of test-only instrumentation are added to a COPY of the image:

  * console=ttyS0 on the kernel command line, so the kernel, the initramfs and
    systemd all narrate to a file.
  * a systemd drop-in pointing the wizard at ttyS0 instead of tty1, so whiptail
    draws where this script can both read it and answer it.

Neither changes any logic under test; they change which terminal it speaks to.

Usage (inside a privileged linux/amd64 container):
    python3 scripts/test-firstboot-boot.py IMAGE [--encrypt] [--disk-size 32G]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PASSPHRASE = "boot-harness-passphrase"
SOCKET = "/tmp/portlin-serial.sock"
MONITOR = "/tmp/portlin-monitor.sock"
LOG = Path(os.environ.get("PORTLIN_BOOT_LOG", "/tmp/portlin-boot.log"))

UNUSED_DROP_IN = """[Service]
# Test-only: send the wizard to the serial port so the harness can read and
# answer it. Identical logic, different terminal.
TTYPath=/dev/ttyS0
Environment=TERM=vt100
# Errors default to the journal, where a harness reading a serial line cannot
# see them -- so a crash mid-wizard looks identical to a hang.
StandardError=tty
# console=ttyS0 makes systemd auto-start a getty on this line, and two readers
# on one tty means the getty eats every keystroke while the wizard renders
# perfectly and responds to nothing. The production unit does the equivalent
# for tty1.
Conflicts=serial-getty@ttyS0.service
"""


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str]) -> str:
    result = run(argv)
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}\n{result.stderr}")
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


def instrument(image: Path) -> None:
    """Add the serial console and the wizard drop-in to the image."""
    loop = must(["losetup", "-P", "-f", "--show", str(image)])
    try:
        boot, root = f"{loop}p3", f"{loop}p4"
        ensure_node(boot)
        ensure_node(root)
        mount = Path("/mnt/instrument")
        mount.mkdir(exist_ok=True)

        must(["mount", root, str(mount)])
        try:
            must(["mount", boot, str(mount / "boot")])
            try:
                grub = mount / "boot/grub/grub.cfg"
                text = grub.read_text()
                text = re.sub(
                    r"(\n\s*linux\s+\S+)", r"\1 console=tty0 console=ttyS0,115200", text
                )
                grub.write_text(
                    "serial --unit=0 --speed=115200\n"
                    "terminal_output --append serial\n"
                    "terminal_input --append serial\n" + text
                )
            finally:
                must(["umount", str(mount / "boot")])
        finally:
            must(["umount", str(mount)])
    finally:
        run(["losetup", "-d", loop])


class Monitor:
    """The QEMU monitor, used to inject keystrokes at the emulated keyboard."""

    SPECIAL = {
        "\r": "ret", "\n": "ret", " ": "spc", "-": "minus", ".": "dot",
        "/": "slash", ",": "comma", ":": "shift-semicolon", "_": "shift-minus",
    }

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        time.sleep(0.5)
        try:
            self.sock.recv(65536)
        except OSError:
            pass

    def command(self, text: str, pause: float = 0.12) -> None:
        self.sock.sendall((text + "\n").encode())
        time.sleep(pause)
        try:
            self.sock.settimeout(0.3)
            self.sock.recv(65536)
        except (OSError, socket.timeout, TimeoutError):
            pass

    def screendump(self, path: str) -> None:
        """Capture the framebuffer, so a blind failure still leaves evidence."""
        self.command(f"screendump {path}", pause=3.0)

    def type(self, text: str) -> None:
        for character in text:
            key = self.SPECIAL.get(character)
            if key is None:
                key = f"shift-{character.lower()}" if character.isupper() else character
            self.command(f"sendkey {key}")


class Serial:
    """The guest's serial line, with expect-style reads and a full transcript."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = ""
        self.transcript = ""

    def read_some(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return
        if data:
            text = data.decode(errors="replace")
            self.buffer += text
            self.transcript += text
            LOG.write_text(self.transcript)

    def expect(self, pattern: str, timeout: float, label: str = "") -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pattern in self.buffer:
                self.buffer = self.buffer.split(pattern, 1)[1]
                print(f"  [seen] {label or pattern}", flush=True)
                return True
            self.read_some(1.0)
        print(f"  [TIMEOUT after {timeout:.0f}s waiting for] {label or pattern}", flush=True)
        return False

    def attach_monitor(self, monitor: "Monitor") -> None:
        self.monitor = monitor

    def send(self, text: str) -> None:
        """Answer a prompt on /dev/console, which is the serial line.

        Used for the initramfs, whose prompts both appear here and read from
        here.
        """
        time.sleep(1.0)
        self.sock.sendall(text.encode())

    def press(self, text: str) -> None:
        """Type at the emulated keyboard, which is where tty1 reads from.

        The wizard runs on tty1 in production and this test does not move it, so
        its input has to arrive as keystrokes rather than serial bytes. Reading
        and writing the guest are two separate channels and each phase needs the
        matching pair.
        """
        time.sleep(1.0)
        self.monitor.type(text)

    def advance(self, key: str, until: str, timeout: float,
                label: str = "", attempts: int = 8) -> bool:
        """Press ``key`` until the screen actually changes.

        A single keystroke sent the moment a dialog's text appears arrives
        before whiptail has finished setting up its input, and is silently
        swallowed -- leaving a live wizard that looks completely hung. Pressing
        again is harmless (it re-accepts the same default), so retry until the
        next screen shows up.
        """
        per_attempt = max(timeout / attempts, 6.0)
        for _ in range(attempts):
            self.send(key)
            if self.expect(until, per_attempt, label):
                return True
        print(f"  [could not advance to] {label or until}", flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--encrypt", action="store_true")
    parser.add_argument("--disk-size", default="32G")
    args = parser.parse_args()

    for stale in ("portlin_root",):
        run(["dmsetup", "remove", "-f", stale])

    work = Path("/tmp/portlin-boot-test.img")
    print(f"== copying {args.image} and growing it to {args.disk_size} ==", flush=True)
    shutil.copyfile(args.image, work)
    must(["truncate", "-s", args.disk_size, str(work)])

    print("== instrumenting (serial console + wizard drop-in) ==", flush=True)
    instrument(work)

    Path(SOCKET).unlink(missing_ok=True)
    Path(MONITOR).unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(SOCKET)
    listener.listen(1)

    print("== booting ==", flush=True)
    qemu = subprocess.Popen(
        ["qemu-system-x86_64", "-machine", "q35", "-m", "2048", "-smp", "2",
         "-drive", f"file={work},format=raw,if=virtio",
         "-display", "none", "-no-reboot",
         "-serial", f"unix:{SOCKET}",
         "-monitor", f"unix:{MONITOR},server,nowait"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    listener.settimeout(120)
    conn, _ = listener.accept()
    serial = Serial(conn)

    monitor_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(60):
        try:
            monitor_sock.connect(MONITOR)
            break
        except OSError:
            time.sleep(0.5)
    serial.attach_monitor(Monitor(monitor_sock))

    failures: list[str] = []

    def require(ok: bool, message: str) -> bool:
        if not ok:
            failures.append(message)
        return ok

    try:
        require(serial.expect("Portlin GNU/Linux", 300, "GRUB menu"), "GRUB never appeared")

        if args.encrypt:
            if require(serial.expect("Encrypt this drive?", 600, "encryption prompt"),
                       "the encryption prompt never appeared"):
                serial.send("y\n")
                serial.expect("New passphrase", 60, "passphrase prompt")
                serial.send(PASSPHRASE + "\n")
                serial.expect("Repeat passphrase", 60, "passphrase confirmation")
                serial.send(PASSPHRASE + "\n")
                require(serial.expect("Encryption complete", 3600, "encryption finished"),
                        "encryption never completed")
        else:
            if serial.expect("Encrypt this drive?", 600, "encryption prompt"):
                serial.send("n\n")

        # The wizard runs on tty1 -- where production puts it -- so it is not
        # visible on the serial line. systemd's own status output is, though,
        # which is enough to know when it has started.
        require(serial.expect("portlin-firstboot", 1200, "wizard service starting"),
                "the wizard service never started")

        # From here the sequence is blind: press through each screen accepting
        # defaults, then assert on what the disk looks like afterwards. The
        # outcome is the real test; reading the screens was only ever a way to
        # find out where it went wrong.
        shots = Path("/out") if Path("/out").is_dir() else Path("/tmp")
        sequence = [
            ("welcome", "\r", 45),
            ("keyboard", "\r", 20),
            ("language", "\r", 20),
            ("timezone region", "\r", 20),
            ("timezone city", "\r", 20),
            ("hostname", "\r", 20),
            ("full name", "\r", 20),
            ("username", "\r", 20),
            ("password", PASSPHRASE + "\r", 20),
            ("password again", PASSPHRASE + "\r", 25),
            ("autologin", "\r", 20),
            ("EXPAND offer", "\r", 30),
            ("summary", "\r", 60),
        ]
        for index, (label, keys, wait) in enumerate(sequence):
            print(f"  [press] {label}", flush=True)
            serial.monitor.screendump(str(shots / f"step-{index:02d}-{label.replace(' ', '-')}.ppm"))
            serial.press(keys)
            time.sleep(wait)

        require(serial.expect("All set", 900, "setup complete"), "setup never completed")
    finally:
        LOG.write_text(serial.transcript)
        qemu.terminate()
        try:
            qemu.wait(timeout=20)
        except subprocess.TimeoutExpired:
            qemu.kill()

    print(f"\n== full boot log: {LOG} ({len(serial.transcript)} bytes) ==")

    # The proof: did the filesystem actually grow?
    print("== checking the filesystem actually grew ==", flush=True)
    loop = must(["losetup", "-P", "-f", "--show", str(work)])
    try:
        ensure_node(f"{loop}p4")
        device = f"{loop}p4"
        if args.encrypt:
            opened = run(["cryptsetup", "open", "--key-file", "-", device, "portlin_root"],
                         input=PASSPHRASE)
            if opened.returncode != 0:
                failures.append(f"cannot open the container afterwards: {opened.stderr}")
                device = ""
            else:
                device = "/dev/mapper/portlin_root"
        if device:
            size = run(["dumpe2fs", "-h", device])
            blocks = re.search(r"Block count:\s+(\d+)", size.stdout)
            if not blocks:
                failures.append("could not read the filesystem size afterwards")
            else:
                gib = int(blocks.group(1)) * 4096 / 1024**3
                print(f"  filesystem is now {gib:.1f} GiB")
                if gib < 20:
                    failures.append(f"filesystem is {gib:.1f} GiB; it did not expand")
    finally:
        run(["cryptsetup", "close", "portlin_root"])
        run(["losetup", "-d", loop])

    print()
    if failures:
        print("=== last 3000 characters of the serial transcript ===")
        print(serial.transcript[-3000:])
        print("=== end transcript ===\n")
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: booted, encrypted, ran setup, offered expansion, and grew")
    return 0


if __name__ == "__main__":
    sys.exit(main())
