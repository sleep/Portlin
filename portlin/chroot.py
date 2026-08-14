"""Chroot lifecycle: bind mounts, service suppression, name resolution.

Used by both halves of portlin. ``build`` chroots into a freshly debootstrapped
directory to install packages; ``write`` chroots into the mounted target to
generate the initramfs and install GRUB.

Teardown is the part that matters. A chroot left with /dev and /proc bound into
it is genuinely dangerous, because a later ``rm -rf`` of the directory walks
straight into the host's device tree. Everything registered here is unwound in
reverse on exit, including on exception.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from pathlib import Path

from .runner import Runner
from .templates import render_policy_rc_d

log = logging.getLogger("portlin")

POLICY_RC_D = "usr/sbin/policy-rc.d"
RESOLV_CONF = "etc/resolv.conf"

# Recursive binds so that nested mounts such as /dev/pts and /dev/shm come along.
BIND_MOUNTS = [
    ("/dev", "dev", "rbind"),
    ("/proc", "proc", "rbind"),
    ("/sys", "sys", "rbind"),
    ("/run", "run", "rbind"),
]

CHROOT_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "DEBCONF_NONINTERACTIVE_SEEN": "true",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}


class Chroot:
    """Context manager giving a prepared chroot and a way to run commands in it."""

    def __init__(self, root: Path | str, runner: Runner, *, network: bool = True) -> None:
        self.root = Path(root)
        self.runner = runner
        self.network = network
        self._stack = ExitStack()

    def __enter__(self) -> "Chroot":
        try:
            self._prepare()
        except Exception:
            self._stack.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._stack.close()

    def _prepare(self) -> None:
        for source, relative, kind in BIND_MOUNTS:
            destination = self.root / relative
            self.runner.run(["mkdir", "-p", str(destination)])
            self.runner.run(["mount", f"--{kind}", source, str(destination)])
            self._stack.callback(self._unmount, destination)

        # Suppress daemon starts before the first package is unpacked, otherwise a
        # maintainer script talks to the host's init.
        policy = self.root / POLICY_RC_D
        self.runner.write_file(policy, render_policy_rc_d(), mode=0o755)
        self._stack.callback(self._remove, policy)

        if self.network:
            self._install_resolv_conf()

    def _install_resolv_conf(self) -> None:
        """Give the chroot working DNS without disturbing what the image ships.

        The file is written directly rather than bind-mounted because a bind
        mount over resolv.conf survives into the tarball as an empty file if the
        unwind order ever goes wrong.
        """
        target = self.root / RESOLV_CONF
        host = Path("/etc/resolv.conf")
        content = ""
        if host.exists() and not self.runner.dry_run:
            content = host.read_text()
        if not content.strip():
            content = "nameserver 1.1.1.1\nnameserver 9.9.9.9\n"
        self.runner.write_file(target, content)
        self._stack.callback(self._remove, target)

    def _unmount(self, path: Path) -> None:
        # -R because the binds are recursive; -l as a fallback for the case where
        # something in the chroot still holds a reference.
        result = self.runner.run(["umount", "-R", str(path)], check=False)
        if not result.ok:
            log.warning("lazy-unmounting %s after umount failed", path)
            self.runner.run(["umount", "-R", "-l", str(path)], check=False)

    def _remove(self, path: Path) -> None:
        self.runner.run(["rm", "-f", str(path)], check=False)

    def run(self, argv: list[str], **kwargs) -> object:
        """Run a command inside the chroot."""
        env = {**CHROOT_ENV, **(kwargs.pop("env", None) or {})}
        return self.runner.run(
            ["chroot", str(self.root), *[str(a) for a in argv]],
            env=env,
            **kwargs,
        )

    def apt(self, argv: list[str], **kwargs) -> object:
        """Run apt-get inside the chroot under eatmydata.

        eatmydata drops the fsync calls dpkg makes after every file. In a chroot
        being built from scratch there is nothing to lose to a crash worth the
        time it costs, and it roughly halves the install.
        """
        return self.run(
            [
                "eatmydata",
                "apt-get",
                "-y",
                "-o", "Dpkg::Options::=--force-confnew",
                "-o", "Acquire::Retries=3",
                *argv,
            ],
            **kwargs,
        )

    def write_file(self, relative: str, content: str, *, mode: int = 0o644) -> None:
        self.runner.write_file(self.root / relative.lstrip("/"), content, mode=mode)
