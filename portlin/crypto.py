"""LUKS2 container lifecycle.

Passphrases reach cryptsetup on stdin as ``secret_input`` so they never appear in
an argument vector, where any other user on the machine could read them out of
/proc, nor in portlin's own command log.
"""

from __future__ import annotations

from .errors import TargetError
from .layout import MAPPER_NAME
from .runner import Runner

# cryptsetup sizes argon2id by benchmarking the machine doing the formatting.
# On a workstation that yields parameters a low-RAM laptop cannot satisfy, and
# since the stick is meant to be unlocked on unknown hardware, the defaults are
# actively wrong here. 256 MiB and 2000 ms are comfortably strong while staying
# openable on a machine with 2 GB of RAM.
PBKDF_MEMORY_KIB = 256 * 1024
PBKDF_TIME_MS = 2000

LUKS_FORMAT_ARGS = [
    "--type", "luks2",
    "--pbkdf", "argon2id",
    "--pbkdf-memory", str(PBKDF_MEMORY_KIB),
    "--iter-time", str(PBKDF_TIME_MS),
    "--cipher", "aes-xts-plain64",
    "--key-size", "512",
    "--hash", "sha256",
]


def luks_format(runner: Runner, device: str, passphrase: str, *, label: str = "portlin") -> None:
    if not passphrase:
        raise TargetError("refusing to create a LUKS container with an empty passphrase")
    runner.run(
        [
            "cryptsetup",
            "luksFormat",
            "--batch-mode",
            *LUKS_FORMAT_ARGS,
            "--label", label,
            "--key-file", "-",
            device,
        ],
        secret_input=passphrase,
    )


def luks_open(runner: Runner, device: str, passphrase: str, *, name: str = MAPPER_NAME) -> str:
    runner.run(
        ["cryptsetup", "open", "--key-file", "-", device, name],
        secret_input=passphrase,
    )
    return mapper_path(name)


def luks_close(runner: Runner, *, name: str = MAPPER_NAME) -> None:
    # check=False: this runs during teardown, where raising would replace the
    # real failure with a less useful one.
    runner.run(["cryptsetup", "close", name], check=False)


def mapper_path(name: str = MAPPER_NAME) -> str:
    return f"/dev/mapper/{name}"


def luks_uuid(runner: Runner, device: str) -> str:
    uuid = runner.output(
        ["cryptsetup", "luksUUID", device],
        dry_stdout="11111111-1111-1111-1111-111111111111",
    )
    if not uuid:
        raise TargetError(f"could not read the LUKS UUID of {device}")
    return uuid
