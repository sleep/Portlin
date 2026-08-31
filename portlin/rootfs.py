"""The slow half: debootstrap a Debian system and freeze it as a tarball.

The product of this module is deliberately anonymous. It contains no hostname
worth keeping, no user, no machine-id, no SSH host key and no fstab, because all
of those are either identity (the first-boot wizard's job) or target-specific
(the write stage's job). Keeping the tarball free of both is what makes it
reusable across any number of sticks.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from . import packages, templates
from .chroot import Chroot
from .config import BuildConfig
from .errors import BuildError
from .runner import Runner

log = logging.getLogger("portlin")

RESOURCES = Path(__file__).parent / "resources"
FIRSTBOOT_SENTINEL = "var/lib/portlin/firstboot-pending"
FIRSTBOOT_SCRIPT = "usr/local/sbin/portlin-firstboot"
FIRSTBOOT_UNIT = "etc/systemd/system/portlin-firstboot.service"

VERSION = "0.1.0"


def build_rootfs(cfg: BuildConfig, runner: Runner) -> Path:
    """Build the rootfs and return the path to the produced tarball."""
    work_dir = cfg.work_dir or _work_dir(runner)
    root = work_dir / "root"
    log.info("building %s rootfs in %s", cfg.suite, root)

    try:
        _debootstrap(cfg, runner, root)
        _configure_apt(cfg, runner, root)
        with Chroot(root, runner) as chroot:
            _install_packages(cfg, runner, chroot)
            _configure_system(cfg, runner, chroot)
            _anonymise(runner, chroot)
        _pack(cfg, runner, root)
    finally:
        if cfg.work_dir is None and not cfg.keep_work_dir and not runner.dry_run:
            shutil.rmtree(work_dir, ignore_errors=True)

    return cfg.output


def _work_dir(runner: Runner) -> Path:
    """Scratch space for the build tree. A dry run leaves nothing behind."""
    if runner.dry_run:
        return Path("/tmp/portlin-build-dryrun")
    return Path(tempfile.mkdtemp(prefix="portlin-build-"))


def _debootstrap(cfg: BuildConfig, runner: Runner, root: Path) -> None:
    runner.run(["mkdir", "-p", str(root)])
    runner.run(
        [
            "debootstrap",
            "--arch=amd64",
            f"--components={','.join(cfg.component_list)}",
            f"--include={','.join(packages.BOOTSTRAP_INCLUDE)}",
            cfg.suite,
            str(root),
            cfg.mirror,
        ]
    )


def _configure_apt(cfg: BuildConfig, runner: Runner, root: Path) -> None:
    runner.write_file(
        root / "etc/apt/sources.list",
        templates.render_sources_list(
            suite=cfg.suite,
            mirror=cfg.mirror,
            security_mirror=cfg.security_mirror,
            components=cfg.components,
        ),
    )
    # Recommends stay on: an Xfce desktop assembled without them is missing
    # thumbnailers, mount helpers and portals, and feels broken in ways that are
    # tedious to diagnose. Suggests stay off, since those are genuinely optional.
    runner.write_file(
        root / "etc/apt/apt.conf.d/99portlin",
        "\n".join(
            [
                'APT::Install-Recommends "true";',
                'APT::Install-Suggests "false";',
                'Acquire::Languages "none";',
                "",
            ]
        ),
    )


def _install_packages(cfg: BuildConfig, runner: Runner, chroot: Chroot) -> None:
    packages = cfg.package_list()
    log.info("installing %d packages", len(packages))
    chroot.apt(["update"])
    chroot.apt(["dist-upgrade"])
    chroot.apt(["install", *packages])


def _configure_system(cfg: BuildConfig, runner: Runner, chroot: Chroot) -> None:
    chroot.write_file("etc/initramfs-tools/conf.d/portlin", templates.render_initramfs_conf())
    chroot.write_file("etc/cryptsetup-initramfs/conf-hook", templates.render_cryptsetup_hook_conf())
    chroot.write_file("etc/default/grub", templates.render_default_grub())
    chroot.write_file("etc/default/zramswap", templates.render_zram_conf())
    chroot.write_file("etc/portlin-release", templates.render_os_release_extra(VERSION))

    chroot.write_file("etc/hostname", f"{cfg.hostname}\n")
    chroot.write_file(
        "etc/hosts",
        "\n".join(
            [
                "127.0.0.1\tlocalhost",
                f"127.0.1.1\t{cfg.hostname}",
                "::1\tlocalhost ip6-localhost ip6-loopback",
                "ff02::1\tip6-allnodes",
                "ff02::2\tip6-allrouters",
                "",
            ]
        ),
    )

    # A working locale before the wizard runs, so the wizard itself renders.
    chroot.write_file(
        "etc/locale.gen",
        "\n".join([f"{cfg.locale} UTF-8", "C.UTF-8 UTF-8", ""]),
    )
    chroot.run(["locale-gen"])
    chroot.write_file("etc/default/locale", f'LANG="{cfg.locale}"\n')
    chroot.write_file(
        "etc/default/keyboard",
        "\n".join(
            [
                "XKBMODEL=pc105",
                f'XKBLAYOUT="{cfg.keymap}"',
                'XKBVARIANT=""',
                'XKBOPTIONS=""',
                'BACKSPACE="guess"',
                "",
            ]
        ),
    )
    chroot.run(["ln", "-sf", f"/usr/share/zoneinfo/{cfg.timezone}", "/etc/localtime"])
    chroot.write_file("etc/timezone", f"{cfg.timezone}\n")

    # debootstrap leaves root with an empty password field, which on a console
    # means passwordless root. Lock it; the wizard creates a sudo-capable user.
    chroot.run(["passwd", "--lock", "root"])

    chroot.run(["systemctl", "enable", "zramswap.service"], check=False)
    chroot.run(["systemctl", "enable", "NetworkManager.service"], check=False)

    _configure_desktop(cfg, chroot)


def _configure_desktop(cfg: BuildConfig, chroot: Chroot) -> None:
    """Make the desktop dark by default.

    Everything lands in /etc/xdg, which is where both xfconf and GTK look for
    system defaults. That keeps the tarball anonymous in the same way the rest
    of the build does: no home directory is touched, so these are defaults every
    account inherits rather than values frozen into one user's config at the
    moment the wizard created them. Changing the theme in Settings writes to
    ~/.config as usual and wins from then on.

    Skipped entirely for a headless build. None of this software is installed
    there, and configuration for absent packages misrepresents what is in the
    image.
    """
    if "xfce4" not in cfg.package_list():
        return

    xfconf = "etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
    chroot.write_file(f"{xfconf}/xsettings.xml", templates.render_xsettings_channel())
    chroot.write_file(f"{xfconf}/xfwm4.xml", templates.render_xfwm4_channel())
    chroot.write_file("etc/xdg/gtk-3.0/settings.ini", templates.render_gtk3_settings())
    chroot.write_file("etc/xdg/gtk-4.0/settings.ini", templates.render_gtk4_settings())
    chroot.write_file(
        "etc/xdg/xfce4/terminal/terminalrc", templates.render_terminal_config()
    )
    chroot.write_file(
        "etc/lightdm/lightdm-gtk-greeter.conf.d/10-portlin.conf",
        templates.render_lightdm_greeter_conf(),
    )


def _anonymise(runner: Runner, chroot: Chroot) -> None:
    """Strip everything that would make two sticks from this tarball identical.

    An empty (not absent) /etc/machine-id is the documented way to ask systemd to
    generate a fresh one on next boot. Absent would work too, but an empty file
    also keeps the read-only /etc case working, and it is what systemd's own
    image-building guidance recommends.
    """
    chroot.apt(["clean"])
    chroot.run(["sh", "-c", "rm -rf /var/lib/apt/lists/*"])
    chroot.run(["sh", "-c", "rm -f /etc/ssh/ssh_host_*"])
    chroot.run(["sh", "-c", ": > /etc/machine-id"])
    chroot.run(["sh", "-c", "rm -f /var/lib/dbus/machine-id"])
    chroot.run(["sh", "-c", "rm -f /var/log/*.log /var/log/*/*.log"], check=False)


def _pack(cfg: BuildConfig, runner: Runner, root: Path) -> None:
    output = cfg.output
    runner.run(["mkdir", "-p", str(output.parent)])
    # --numeric-owner because the build host's /etc/passwd is irrelevant to the
    # image; --xattrs and --acls because capabilities on binaries like ping are
    # stored as extended attributes and silently vanish without them.
    runner.run(
        [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--xattrs-include=*",
            "--acls",
            "-I", "zstd -T0 -6",
            "-cf", str(output),
            "-C", str(root),
            ".",
        ]
    )
    if not runner.dry_run and not output.exists():
        raise BuildError(f"tar reported success but {output} does not exist")
