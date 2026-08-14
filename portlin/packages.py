"""The package set installed into the image.

Grouped rather than pulled from task-xfce-desktop so that the contents are
visible, diffable and reviewable. A task package is a black box whose expansion
changes between releases; this list changes only when someone edits it.
"""

from __future__ import annotations

# Pulled in by debootstrap itself so the very first apt run inside the chroot
# already has working TLS and a way to avoid interactive prompts. eatmydata is
# here because it makes dpkg skip fsync, which roughly halves the wall time of
# installing a desktop into a chroot.
BOOTSTRAP_INCLUDE = [
    "ca-certificates",
    "apt-utils",
    "locales",
    "eatmydata",
]

# Bootloader and initramfs. The -bin variants are deliberate: grub-pc and
# grub-efi-amd64 run debconf and try to decide for themselves where to install,
# which is exactly the decision portlin needs to make explicitly.
BOOT = [
    "linux-image-amd64",
    "initramfs-tools",
    "grub-common",
    "grub2-common",
    "grub-pc-bin",
    "grub-efi-amd64-bin",
    "efibootmgr",
    "cryptsetup",
    "cryptsetup-initramfs",
]

# Both microcode packages, because the stick does not know whose CPU it will
# wake up on. Each is a no-op on the other vendor's hardware.
FIRMWARE = [
    "firmware-linux",
    "firmware-misc-nonfree",
    "firmware-iwlwifi",
    "firmware-realtek",
    "firmware-atheros",
    "firmware-brcm80211",
    "firmware-sof-signed",
    # Debian names the AMD one after the architecture, not the vendor. The
    # obvious guess, amd-microcode, does not exist in any suite.
    "intel-microcode",
    "amd64-microcode",
]

SYSTEM = [
    "systemd-timesyncd",
    "dbus",
    "polkitd",
    "sudo",
    "console-setup",
    "keyboard-configuration",
    "tzdata",
    "whiptail",
    "python3",
    "zram-tools",
    "bash-completion",
    "less",
    "man-db",
]

STORAGE = [
    # growpart, which grows the last partition of a live disk in place. This is
    # what lets one small image fill whatever stick it lands on.
    "cloud-guest-utils",
    "e2fsprogs",
    "dosfstools",
    "exfatprogs",
    "ntfs-3g",
    "gdisk",
    "parted",
    "udisks2",
    "gvfs",
    "gvfs-backends",
    "gvfs-fuse",
]

NETWORK = [
    "network-manager",
    "network-manager-gnome",
    "wpasupplicant",
    "wireless-tools",
    "iw",
    "iproute2",
    "openssh-client",
    "curl",
    "wget",
    "ca-certificates",
]

# xserver-xorg-video-all and -input-all are the portability equivalent of
# MODULES=most: install every driver rather than the one the build host uses.
DESKTOP = [
    "xserver-xorg",
    "xserver-xorg-video-all",
    "xserver-xorg-input-all",
    "xinit",
    "x11-xserver-utils",
    "lightdm",
    "lightdm-gtk-greeter",
    "xfce4",
    "xfce4-goodies",
    "xfce4-terminal",
    "xfce4-power-manager",
    "xfce4-screenshooter",
    "thunar",
    "thunar-archive-plugin",
    "xarchiver",
    "desktop-base",
    "xdg-utils",
]

AUDIO = [
    "pipewire-audio",
    "wireplumber",
    "pavucontrol",
]

FONTS = [
    "fonts-dejavu",
    "fonts-liberation2",
    "fonts-noto-color-emoji",
]

APPS = [
    "firefox-esr",
    "mousepad",
    "ristretto",
    "galculator",
]

TOOLS = [
    "nano",
    "vim-tiny",
    "htop",
    "rsync",
    "git",
    "pciutils",
    "usbutils",
    "lshw",
    "file",
    "tree",
]

# Never wanted. plymouth is a boot splash whose entire job is hiding the boot
# log, and it fights the first-boot wizard for the console while
# plymouth-quit-wait can deadlock against the display manager. Purged at write
# time too, so a cached rootfs built before this also loses it.
NEVER_INSTALL = ["plymouth", "plymouth-label"]

GROUPS: dict[str, list[str]] = {
    "boot": BOOT,
    "firmware": FIRMWARE,
    "system": SYSTEM,
    "storage": STORAGE,
    "network": NETWORK,
    "desktop": DESKTOP,
    "audio": AUDIO,
    "fonts": FONTS,
    "apps": APPS,
    "tools": TOOLS,
}

# boot and system alone produce a bootable but headless stick. Everything else is
# opt-out territory.
MINIMAL_GROUPS = ["boot", "system", "storage", "network"]
DEFAULT_GROUPS = list(GROUPS)


def resolve(
    groups: list[str] | None = None,
    *,
    extra: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[str]:
    """Flatten the requested groups into a sorted, de-duplicated package list."""
    from .errors import BuildError

    selected = DEFAULT_GROUPS if groups is None else groups
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        raise BuildError(
            f"unknown package group(s): {', '.join(unknown)}. "
            f"Known groups: {', '.join(GROUPS)}"
        )

    packages: set[str] = set()
    for group in selected:
        packages.update(GROUPS[group])
    packages.update(extra or [])
    packages.difference_update(exclude or [])
    packages.difference_update(NEVER_INSTALL)
    return sorted(packages)
