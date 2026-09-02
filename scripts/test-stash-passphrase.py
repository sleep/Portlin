#!/usr/bin/env python3
"""Drive the crypttab keyscript for real, and expand with what it stashed.

The keyscript is the only part of portlin whose output *is* key material:
cryptroot runs it as `run_keyscript | unlock_mapping`, so a stray newline on
stdout is a stick that stops unlocking. A unit test can read the source and say
the shape looks right; only a real LUKS device can say the passphrase came back
byte for byte, that the stash is unreadable to anyone but root, and that the
mapping actually grows from it without a prompt.

That last point is the reason the keyscript exists. After boot the volume key is
gone for good: cryptsetup hands it to the kernel as a "logon" key, whose payload
userspace may never read back, and the keyring holding it belongs to the
initramfs process that unlocked the disk. This harness proves the plain resize
fails exactly that way, and that the stash rescues it.

Requires Linux, root and losetup. Run it inside a privileged linux/amd64
container on any other host; see the harness target in the Makefile.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEYSCRIPT = REPO / "portlin" / "resources" / "firstboot" / "portlin-stash-passphrase"
ASKPASS = Path("/lib/cryptsetup/askpass")
STASH = Path("/run/portlin/luks-pass")
DISK = Path("/tmp/portlin-stash-test.img")
NAME = "portlin_root"
# Spaces and a dollar sign, because the keyscript passes this through a shell
# and a passphrase is the last place to discover a quoting bug.
PASSPHRASE = "harness pass with $dollar and spaces"
SMALL = 64 * 1024**2
LARGE = 128 * 1024**2
MOUNT = Path("/mnt/portlin-stash-test")


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str]) -> str:
    result = run(argv)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def install_askpass_stub() -> Path | None:
    """Stand in for the initramfs prompt at the path the keyscript calls.

    Returns whatever was there before so the harness can put it back, since
    a container running this may have the real cryptsetup installed.
    """
    saved = None
    if ASKPASS.exists():
        saved = ASKPASS.with_suffix(".harness-saved")
        ASKPASS.rename(saved)
    ASKPASS.parent.mkdir(parents=True, exist_ok=True)
    ASKPASS.write_text('#!/bin/sh\nprintf %s "$HARNESS_PASSPHRASE"\n')
    ASKPASS.chmod(0o755)
    return saved


def unlock_through_keyscript(loop: str) -> subprocess.CompletedProcess:
    """Unlock exactly the way cryptroot does: keyscript stdout into cryptsetup."""
    environment = {
        **os.environ,
        "HARNESS_PASSPHRASE": PASSPHRASE,
        "CRYPTTAB_NAME": NAME,
    }
    keyscript = subprocess.Popen(
        ["/bin/sh", str(KEYSCRIPT)], stdout=subprocess.PIPE, env=environment
    )
    opened = subprocess.run(
        ["cryptsetup", "open", "--key-file", "-", loop, NAME],
        stdin=keyscript.stdout,
        capture_output=True,
        text=True,
    )
    keyscript.stdout.close()
    keyscript.wait()
    return opened


def stick_contains_passphrase() -> list[str]:
    """Every file on the mounted stick whose bytes contain the passphrase.

    Deliberately the decrypted view. Grepping the raw image would come back
    clean whatever happened, because the filesystem sits inside the LUKS
    container and is ciphertext on disk -- which would make this a test that
    can only pass. What matters is whether the passphrase became a file on the
    stick at all, and that is only visible through the mapping.
    """
    needle = PASSPHRASE.encode()
    hits = []
    for path in MOUNT.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                if needle in path.read_bytes():
                    hits.append(str(path))
            except OSError:
                continue
    return hits


def main() -> int:
    if os.geteuid() != 0:
        print("this harness needs root", file=sys.stderr)
        return 1

    failures: list[str] = []
    saved_askpass = install_askpass_stub()

    DISK.unlink(missing_ok=True)
    must(["truncate", "-s", str(SMALL), str(DISK)])
    loop = must(["losetup", "--find", "--show", str(DISK)])

    # luksFormat takes the passphrase on stdin, which must() cannot supply.
    # pbkdf2 at a thousand iterations keeps the harness quick; a real stick uses
    # argon2id and portlin's own memory cap.
    formatted = subprocess.run(
        ["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode",
         "--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000",
         "--key-file", "-", loop],
        input=PASSPHRASE, capture_output=True, text=True,
    )
    if formatted.returncode != 0:
        run(["losetup", "-d", loop])
        raise SystemExit(f"could not format the container: {formatted.stderr}")

    # A filesystem inside the container, made before the flow runs, so the scan
    # below inspects a stick that has actually been lived in rather than an
    # empty device that could not have held anything.
    prepared = subprocess.run(["cryptsetup", "open", "--key-file", "-", loop, NAME],
                              input=PASSPHRASE, capture_output=True, text=True)
    if prepared.returncode != 0:
        run(["losetup", "-d", loop])
        raise SystemExit(f"could not open the container to format it: {prepared.stderr}")
    must(["mkfs.ext4", "-q", f"/dev/mapper/{NAME}"])
    run(["cryptsetup", "close", NAME])

    try:
        STASH.unlink(missing_ok=True)

        opened = unlock_through_keyscript(loop)
        if opened.returncode != 0:
            failures.append(f"the keyscript did not unlock the disk: {opened.stderr}")
            return report(failures)

        if not STASH.exists():
            failures.append("the keyscript unlocked the disk but stashed nothing")
        else:
            mode = STASH.stat().st_mode & 0o777
            if mode != 0o600:
                failures.append(f"the stash is mode {mode:o}, expected 600")
            if STASH.read_text() != PASSPHRASE:
                # Byte for byte: a trailing newline here is a rejected passphrase.
                failures.append("the stash does not hold the passphrase verbatim")

        must(["truncate", "-s", str(LARGE), str(DISK)])
        must(["losetup", "-c", loop])
        before = int(must(["blockdev", "--getsize64", f"/dev/mapper/{NAME}"]))

        # The situation the wizard is actually in, and the reason for all this.
        plain = subprocess.run(["cryptsetup", "resize", NAME],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if plain.returncode == 0:
            print("NOTE: the plain resize succeeded; this kernel kept the volume key")

        resized = subprocess.run(
            ["cryptsetup", "resize", "--key-file", "-", NAME],
            input=STASH.read_text() if STASH.exists() else "",
            capture_output=True, text=True,
        )
        if resized.returncode != 0:
            failures.append(f"the stashed passphrase was refused: {resized.stderr}")
        after = int(must(["blockdev", "--getsize64", f"/dev/mapper/{NAME}"]))
        if after <= before:
            failures.append(f"the mapping did not grow ({before} -> {after})")

        # Now the question the whole design turns on: after a real unlock, a
        # real stash and a real expansion, is the passphrase anywhere on the
        # stick?
        MOUNT.mkdir(parents=True, exist_ok=True)
        must(["mount", f"/dev/mapper/{NAME}", str(MOUNT)])
        must(["resize2fs", f"/dev/mapper/{NAME}"])

        # Positive control first. A scan that reports nothing is worth having
        # only once it has been shown to report something.
        planted = MOUNT / "control"
        planted.write_bytes(PASSPHRASE.encode())
        run(["sync"])
        if not stick_contains_passphrase():
            failures.append("the scan cannot detect a passphrase it was handed; it proves nothing")
        planted.unlink()
        run(["sync"])

        leaked = stick_contains_passphrase()
        if leaked:
            failures.append(f"the passphrase was written to the stick: {leaked}")

        # And the stash itself is on another filesystem entirely, not this one.
        if STASH.exists():
            if STASH.stat().st_dev == MOUNT.stat().st_dev:
                failures.append("the stash shares a filesystem with the stick")

        run(["umount", str(MOUNT)])

        # The stash is an optimisation; the unlock is the machine booting at all.
        # Permission bits cannot express this, because the keyscript runs as root
        # and root writes into a 0500 directory regardless. Putting a plain file
        # where the directory belongs is a refusal even root cannot talk past.
        run(["cryptsetup", "close", NAME])
        STASH.unlink(missing_ok=True)
        if STASH.parent.is_dir():
            STASH.parent.rmdir()
        STASH.parent.write_text("not a directory\n")
        try:
            degraded = unlock_through_keyscript(loop)
            if degraded.returncode != 0:
                failures.append("a stash that cannot be written stopped the unlock")
        finally:
            STASH.parent.unlink(missing_ok=True)

        return report(failures)
    finally:
        run(["umount", str(MOUNT)])
        run(["cryptsetup", "close", NAME])
        run(["losetup", "-d", loop])
        DISK.unlink(missing_ok=True)
        STASH.unlink(missing_ok=True)
        ASKPASS.unlink(missing_ok=True)
        if saved_askpass is not None:
            saved_askpass.rename(ASKPASS)


def report(failures: list[str]) -> int:
    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: unlocks, stashes to RAM only, expands without a prompt,\n      and leaves no copy of the passphrase on the stick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
