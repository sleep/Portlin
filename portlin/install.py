"""The fast half: lay out a target, unpack the rootfs, make it bootable.

The structure of this module is one long sequence of destructive steps, each of
which registers its own undo on an ExitStack as it succeeds. If step nine fails,
steps eight through one unwind in exact reverse: unmount the ESP, unmount /boot,
unmount root, close the LUKS mapping, detach the loop device, remove the temp
mountpoint. Leaving a dangling /dev/mapper entry or a bound chroot behind is the
kind of failure that costs someone an afternoon, so the unwinding is not
best-effort, it is the design.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from . import __version__, crypto, templates
from .chroot import Chroot
from .config import WriteConfig
from .errors import TargetError
from .layout import (
    ROLE_BIOS,
    LABEL_BOOT,
    LABEL_ESP,
    LABEL_ROOT,
    MAPPER_NAME,
    ROLE_BOOT,
    ROLE_ESP,
    ROLE_ROOT,
    PartitionPlan,
    plan_partitions,
    sgdisk_argv,
)
from .runner import Runner
from .target import Target, open_target

log = logging.getLogger("portlin")

# How long to wait for partition device nodes after rewriting the table.
PARTITION_WAIT_SECONDS = 10

# Where the kernel publishes every block device it knows about, including the
# major:minor needed to create a node by hand.
SYSFS_BLOCK = Path("/sys/class/block")

# Where udev publishes filesystem-UUID symlinks. grub-mkconfig consults this
# directory to decide whether it may use root=UUID= instead of a device path.
BY_UUID = Path("/dev/disk/by-uuid")

DRY_UUIDS = {
    "root": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "boot": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "esp": "CCCC-CCCC",
}


def write_stick(cfg: WriteConfig, runner: Runner) -> None:
    if not runner.dry_run and not cfg.rootfs.exists():
        raise TargetError(f"rootfs tarball not found: {cfg.rootfs}")

    with ExitStack() as stack:
        target = stack.enter_context(
            open_target(cfg.target, runner, image_size=cfg.image_size)
        )
        plan = plan_partitions(target.size_bytes(), encrypted=cfg.encrypt)

        log.info("partitioning %s", target.device)
        _partition(runner, target.device, plan)
        _await_partitions(runner, target, plan)

        esp_dev = target.partition(plan.by_role(ROLE_ESP).number)
        boot_dev = target.partition(plan.by_role(ROLE_BOOT).number)
        root_part = target.partition(plan.by_role(ROLE_ROOT).number)

        luks_uuid = ""
        if cfg.encrypt:
            log.info("creating the LUKS2 container")
            assert cfg.passphrase is not None
            crypto.luks_format(runner, root_part, cfg.passphrase, label=cfg.label)
            luks_uuid = crypto.luks_uuid(runner, root_part)
            # Registered before opening so it unwinds after every unmount.
            stack.callback(crypto.luks_close, runner, name=MAPPER_NAME)
            root_dev = crypto.luks_open(runner, root_part, cfg.passphrase)
        else:
            root_dev = root_part

        log.info("creating filesystems")
        _make_filesystems(runner, esp_dev, boot_dev, root_dev)

        mountpoint = _make_mountpoint(runner)
        stack.callback(_rmdir, runner, mountpoint)
        _mount_tree(stack, runner, mountpoint, root_dev, boot_dev, esp_dev)

        log.info("unpacking %s", cfg.rootfs)
        _unpack(runner, cfg.rootfs, mountpoint)

        uuids = {
            "root": _blkid_uuid(runner, root_dev, DRY_UUIDS["root"]),
            "boot": _blkid_uuid(runner, boot_dev, DRY_UUIDS["boot"]),
            "esp": _blkid_uuid(runner, esp_dev, DRY_UUIDS["esp"]),
        }
        _write_target_config(runner, mountpoint, cfg, uuids, luks_uuid)
        _ensure_by_uuid_links(
            stack,
            runner,
            {uuids["root"]: root_dev, uuids["boot"]: boot_dev, uuids["esp"]: esp_dev},
        )

        log.info("generating the initramfs and installing GRUB")
        with Chroot(mountpoint, runner, network=False) as chroot:
            _remove_boot_splash(chroot)
            _install_firstboot(chroot)
            chroot.run(["update-initramfs", "-u", "-k", "all"])
            _install_grub(chroot, target.device)

        runner.run(["sync"])

    log.info("done")


def _partition(runner: Runner, device: str, plan: PartitionPlan) -> None:
    for argv in sgdisk_argv(device, plan):
        runner.run(argv)
    # The kernel does not necessarily notice a rewritten partition table on its
    # own, and udev needs a moment to create the new nodes. Both of these are
    # best-effort: partprobe can fail on a busy device and udevadm is absent
    # wherever there is no udev, in which case there is no race to settle.
    runner.run(["partprobe", device], check=False)
    runner.run(["udevadm", "settle"], check=False)


def _await_partitions(runner: Runner, target: Target, plan: PartitionPlan) -> None:
    """Block until the partition device nodes actually exist.

    Firing partprobe and hoping is the single most common source of "device does
    not exist" one line later, because node creation is asynchronous and the
    mechanisms differ by environment: udev on a normal desktop, devtmpfs alone in
    a container, and on a loop device nothing at all unless the table is
    explicitly re-read. Rather than guess which applies, wait for the observable
    outcome and nudge the kernel with partx if it has not happened.
    """
    if runner.dry_run:
        return

    expected = [
        target.partition(part.number)
        for part in plan.partitions
        if part.role != ROLE_BIOS  # never formatted, so its node is not needed
    ]
    deadline = time.monotonic() + PARTITION_WAIT_SECONDS
    nudges = [
        # First possibility: the kernel has not re-read the table at all.
        lambda missing: runner.run(["partx", "-a", target.device], check=False),
        # Second: the kernel knows about the partitions but nothing created the
        # nodes, because /dev is a plain tmpfs with no udev and no devtmpfs.
        lambda missing: _create_nodes_from_sysfs(runner, missing),
    ]

    while True:
        missing = [path for path in expected if not _node_is_current(path)]
        if not missing:
            return
        if nudges:
            nudges.pop(0)(missing)
            continue
        if time.monotonic() >= deadline:
            raise TargetError(
                f"partition nodes did not appear within {PARTITION_WAIT_SECONDS}s: "
                f"{', '.join(missing)}. The partition table was written, but the "
                f"kernel never exposed the partitions."
            )
        time.sleep(0.25)


def _ensure_by_uuid_links(
    stack: ExitStack, runner: Runner, mapping: dict[str, str]
) -> None:
    """Make /dev/disk/by-uuid entries exist for the target's filesystems.

    grub-mkconfig emits root=UUID=... only when /dev/disk/by-uuid/<uuid> is
    present: Debian's 10_linux does a literal `test -e` on that path and falls
    back to the raw device path when it fails. Those symlinks are udev's work, so
    anywhere without udev every unencrypted stick comes out with the build host's
    /dev/loopNpM in its boot line and boots on precisely nothing.

    Only links that do not already exist are created, and each is removed on the
    way out, so a host with a working udev is left exactly as it was found.
    """
    if runner.dry_run:
        return
    runner.run(["mkdir", "-p", str(BY_UUID)], check=False)
    for uuid, device in mapping.items():
        link = BY_UUID / uuid
        if link.is_symlink() or link.exists():
            continue
        log.debug("creating %s -> %s for grub-mkconfig", link, device)
        runner.run(["ln", "-s", device, str(link)], check=False)
        stack.callback(runner.run, ["rm", "-f", str(link)], check=False)


def _sysfs_device_number(name: str) -> tuple[int, int] | None:
    """The (major, minor) the kernel currently reports for a block device."""
    sysfs = SYSFS_BLOCK / name / "dev"
    try:
        major, minor = sysfs.read_text().strip().split(":")
        return int(major), int(minor)
    except (OSError, ValueError):
        return None


def _node_is_current(path: str) -> bool:
    """Whether ``path`` is a node that still points at the right device.

    Existence alone is not enough. Detaching and reattaching a loop device gets
    its partitions fresh minor numbers from the dynamic 259 pool, so a node left
    behind by an earlier attach is a perfectly valid block file pointing at
    nothing. Treating that as present produces "can't open blockdev" several
    steps later, which reads like failing hardware rather than a stale node.
    """
    node = Path(path)
    if not node.exists():
        return False
    expected = _sysfs_device_number(node.name)
    if expected is None:
        # Nothing to compare against, so trust what is there. This is the normal
        # case for a device whose name does not appear under /sys/class/block.
        return True
    status = node.stat()
    return (os.major(status.st_rdev), os.minor(status.st_rdev)) == expected


def _create_nodes_from_sysfs(runner: Runner, missing: list[str]) -> None:
    """Create block device nodes the way udev would, from sysfs.

    Every partition the kernel knows about has a /sys/class/block/<name>/dev
    file holding its major:minor. Normally udev reads that and calls mknod, and
    on a devtmpfs the kernel does it itself. In a container /dev is an ordinary
    tmpfs with neither, so the partitions exist to the kernel and are simply
    unreachable by path. Doing it by hand costs one mknod and makes portlin work
    in environments that have no udev at all.
    """
    for path in missing:
        name = Path(path).name
        number = _sysfs_device_number(name)
        if number is None:
            log.debug("%s is unknown to the kernel, not just missing a node", name)
            continue
        major, minor = number
        if Path(path).exists():
            # Stale: right name, wrong device. Remove it so mknod can succeed.
            log.debug("replacing stale node %s", path)
            runner.run(["rm", "-f", path], check=False)
        log.debug("creating %s as block device %d:%d", path, major, minor)
        runner.run(
            ["mknod", "--mode=0660", path, "b", str(major), str(minor)], check=False
        )


def _make_filesystems(runner: Runner, esp: str, boot: str, root: str) -> None:
    runner.run(["mkfs.vfat", "-F", "32", "-n", LABEL_ESP, esp])
    runner.run(["mkfs.ext4", "-q", "-F", "-L", LABEL_BOOT, boot])
    runner.run(["mkfs.ext4", "-q", "-F", "-L", LABEL_ROOT, root])


def _make_mountpoint(runner: Runner) -> Path:
    """A private mountpoint for the target tree.

    A dry run must not leave a stray directory behind, so it gets a nominal path
    that is never created.
    """
    if runner.dry_run:
        return Path("/tmp/portlin-mnt-dryrun")
    return Path(tempfile.mkdtemp(prefix="portlin-mnt-"))


def _mount_tree(
    stack: ExitStack,
    runner: Runner,
    mountpoint: Path,
    root: str,
    boot: str,
    esp: str,
) -> None:
    """Mount root, then /boot, then /boot/efi. LIFO unwind unmounts them in reverse."""
    _mount(stack, runner, root, mountpoint)
    runner.run(["mkdir", "-p", str(mountpoint / "boot")])
    _mount(stack, runner, boot, mountpoint / "boot")
    runner.run(["mkdir", "-p", str(mountpoint / "boot/efi")])
    _mount(stack, runner, esp, mountpoint / "boot/efi")


def _mount(stack: ExitStack, runner: Runner, source: str, destination: Path) -> None:
    runner.run(["mount", source, str(destination)])
    stack.callback(_umount, runner, destination)


def _umount(runner: Runner, path: Path) -> None:
    runner.run(["umount", str(path)], check=False)


def _rmdir(runner: Runner, path: Path) -> None:
    runner.run(["rmdir", str(path)], check=False)


def _unpack(runner: Runner, tarball: Path, destination: Path) -> None:
    runner.run(
        [
            "tar",
            "--numeric-owner",
            "--xattrs",
            "--xattrs-include=*",
            "--acls",
            "--checkpoint=2000",
            "--checkpoint-action=echo",
            "-I", "zstd -d",
            "-xf", str(tarball),
            "-C", str(destination),
        ]
    )


def _blkid_uuid(runner: Runner, device: str, dry_value: str) -> str:
    uuid = runner.output(
        ["blkid", "-s", "UUID", "-o", "value", device],
        dry_stdout=dry_value,
    )
    if not uuid:
        raise TargetError(f"could not read a filesystem UUID from {device}")
    return uuid


def _write_target_config(
    runner: Runner,
    mountpoint: Path,
    cfg: WriteConfig,
    uuids: dict[str, str],
    luks_uuid: str,
) -> None:
    runner.write_file(
        mountpoint / "etc/fstab",
        templates.render_fstab(
            root_uuid=uuids["root"],
            boot_uuid=uuids["boot"],
            esp_uuid=uuids["esp"],
        ),
    )
    if cfg.encrypt:
        # crypttab must exist before update-initramfs, because the cryptsetup
        # hook reads it to decide which unlock support to bake in. Written after
        # unpacking and before the chroot for exactly that reason.
        runner.write_file(
            mountpoint / "etc/crypttab",
            templates.render_crypttab(luks_uuid=luks_uuid, discard=cfg.discard),
        )
    # Rewritten here rather than trusted from the tarball so that a stick built
    # from an older rootfs still gets current boot settings. An unencrypted stick
    # is offered encryption on first boot; an encrypted one already made that
    # choice and must never be offered it again.
    runner.write_file(
        mountpoint / "etc/default/grub",
        templates.render_default_grub(offer_encryption=not cfg.encrypt),
    )
    # Stamped here rather than during build: the rootfs tarball is reusable for
    # months, so a version baked into it would describe the tarball rather than
    # the stick, and the update channel needs a version it can trust.
    runner.write_file(
        mountpoint / "etc/portlin-release",
        templates.render_os_release_extra(__version__),
    )


def _remove_boot_splash(chroot: Chroot) -> None:
    """Remove plymouth.

    A boot splash exists to hide the boot log. On a stick that has to work on
    unfamiliar hardware, seeing the log is worth more than hiding it -- and
    plymouth costs far more than it gives here. It owns the console during boot,
    so the first-boot wizard has to wrestle it for the terminal; and
    plymouth-quit-wait blocks until plymouth exits, which on real hardware can
    deadlock against a display manager that is itself waiting for the wizard.

    Both problems disappear entirely when the splash is not there. Removal needs
    no network, so it happens at write time and applies to any cached rootfs.
    Done before update-initramfs so plymouth's initramfs hook goes with it.
    """
    chroot.run(
        ["apt-get", "purge", "-y", "plymouth", "plymouth-label"],
        check=False,
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )


def _install_firstboot(chroot: Chroot) -> None:
    """Install the first-boot wizard into the target.

    Deliberately here rather than in the rootfs build. The wizard is portlin's
    own code, so shipping whatever version was frozen into a cached tarball
    months ago is a footgun: a fixed wizard would silently not reach the stick
    until someone remembered to rebuild. Installing it at write time means the
    stick always carries the current wizard, and iterating on it costs a
    two-minute write instead of a twenty-minute debootstrap.
    """
    from .rootfs import FIRSTBOOT_SCRIPT, FIRSTBOOT_SENTINEL, FIRSTBOOT_UNIT, RESOURCES

    chroot.write_file(
        FIRSTBOOT_SCRIPT,
        (RESOURCES / "firstboot" / "portlin-firstboot").read_text(),
        mode=0o755,
    )
    chroot.write_file(
        FIRSTBOOT_UNIT,
        (RESOURCES / "firstboot" / "portlin-firstboot.service").read_text(),
    )
    chroot.write_file(FIRSTBOOT_SENTINEL, "pending\n")
    chroot.run(["systemctl", "enable", "portlin-firstboot.service"])

    # The initramfs side of the encryption offer. Installed on every stick,
    # including encrypted ones, so that a stick is never missing the machinery
    # it would need; it does nothing unless portlin.encrypt=ask is on the
    # kernel command line, which only unencrypted sticks get.
    chroot.write_file(
        "etc/initramfs-tools/hooks/portlin-encrypt",
        (RESOURCES / "firstboot" / "portlin-encrypt.hook").read_text(),
        mode=0o755,
    )
    chroot.write_file(
        "etc/initramfs-tools/scripts/local-top/portlin-encrypt",
        (RESOURCES / "firstboot" / "portlin-encrypt.local-top").read_text(),
        mode=0o755,
    )


def _install_grub(chroot: Chroot, device: str) -> None:
    """Install GRUB twice so the stick boots on both firmware families.

    The legacy pass writes core.img into the BIOS boot partition. The UEFI pass
    uses --removable, which places the bootloader at EFI/BOOT/BOOTX64.EFI, the
    fallback path every UEFI implementation probes on removable media. Without
    it the stick would only boot on machines whose firmware had previously been
    told about it.

    --no-nvram is the counterpart: it stops grub-install writing a boot entry
    into the build machine's own firmware, which would be both useless and rude.
    """
    chroot.run(
        [
            "grub-install",
            "--target=i386-pc",
            "--boot-directory=/boot",
            "--recheck",
            "--no-floppy",
            device,
        ]
    )
    chroot.run(
        [
            "grub-install",
            "--target=x86_64-efi",
            "--efi-directory=/boot/efi",
            "--boot-directory=/boot",
            "--removable",
            "--no-nvram",
            "--recheck",
        ]
    )
    chroot.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
