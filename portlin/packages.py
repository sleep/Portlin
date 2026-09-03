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
    # lspci, for the Software app's hardware scan. Here rather than in TOOLS
    # because a --minimal stick carries portlin-runtime, which runs it.
    "pciutils",
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

# Every theme the first-boot wizard can offer, by the name Xfce knows it as.
# GTK3 has Adwaita-dark built in and would cost nothing, but it has no xfwm4
# counterpart, so the window decorations stay light around dark windows. What
# qualifies a theme for this list is carrying gtk-2.0, gtk-3.0 and xfwm4
# variants under one name, which is what makes a desktop dark all the way to
# the title bar.
#
# All of them are installed, not just the default: first boot runs on a stick
# with no network, so a theme the wizard offers but the image never installed
# is a menu entry that produces an unstyled desktop.
THEME_PACKAGES = {
    # Numix is the only theme in the archive that is dark, accents in red and
    # ships xfwm4. It has no separate dark directory: the dark face comes from
    # gtk-application-prefer-dark-theme, which the shipped GTK settings set, so
    # GTK3 goes dark and the handful of remaining GTK2 applications do not.
    "Numix": "numix-gtk-theme",
    "Greybird-dark": "greybird-gtk-theme",
    "Blackbird": "blackbird-gtk-theme",
}

# Which of them the image boots with. The wizard offers this one first, and
# every shipped theme file names it; tests hold those three in agreement.
DEFAULT_THEME = "Numix"

# Every icon theme the first-boot wizard can offer, by the name that appears in
# /usr/share/icons and in xsettings, mapped to the Debian package that installs
# it. Declared rather than spelled: papirus-icon-theme installs five theme
# names and numix-icon-theme-circle installs two, so no rule turning a theme
# name into a package name is right for all of them. A test reads this mapping
# rather than transforming a string, because the transform was the bug.
#
# All of them are installed, not just the default, for the same reason every
# widget theme above is: first boot runs on a stick with no network, and an
# icon theme the wizard offers but the image never installed is not a menu
# entry that falls back to the stock set. It is a desktop with a wallpaper and
# blank space where every icon was.
ICON_THEME_PACKAGES = {
    "Papirus-Dark": "papirus-icon-theme",
    "elementary-xfce": "elementary-xfce-icon-theme",
    "Numix-Circle": "numix-icon-theme-circle",
    "Papirus": "papirus-icon-theme",
    "Adwaita": "adwaita-icon-theme",
}

# Papirus-Dark, because the deciding criterion is coverage of the software the
# Software app installs. It is the only set in the archive that carries icons
# for Signal, Zed, Cursor, Mullvad and their generation, and it is drawn
# light-on-dark, which is what a dark panel needs. Its palette also keeps red
# for destructive actions, so the crimson accent stays the only crimson on the
# desktop.
#
# Deliberately not a Numix icon theme despite the Numix widget theme: they are
# different upstreams that share a word, and the icon one is effectively
# frozen.
DEFAULT_ICON_THEME = "Papirus-Dark"

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
    # The two plugins portlin's own panel layout names. Both arrive as Depends
    # of xfce4-goodies today, but a line in someone else's package is not a
    # promise, and this file exists precisely so the contents are not a
    # metapackage's expansion. A layout that names a plugin the image did not
    # install is not a panel missing one item: it is a hole with nothing on
    # screen to say why.
    "xfce4-genmon-plugin",
    "xfce4-whiskermenu-plugin",
    "thunar",
    "thunar-archive-plugin",
    "xarchiver",
    "desktop-base",
    *THEME_PACKAGES.values(),
    # Icons, as opposed to the widget themes above. Every set the first-boot
    # picker can offer, because first boot has no network and a name the image
    # never installed cannot be fetched. adwaita-icon-theme is among them and
    # also carries the gtk-update-icon-cache dependency that builds every other
    # set's cache at install time, so none of this costs anything at first boot.
    *dict.fromkeys(ICON_THEME_PACKAGES.values()),
    # Adwaita 48 moved its full-colour legacy artwork out into this package and
    # made it a Suggests, which the build does not install. Three megabytes to
    # put back the icons that stock Adwaita is assumed to still have.
    "adwaita-icon-theme-legacy",
    "xdg-utils",
    # For portlin's own About dialog rather than for Xfce. They belong here
    # rather than only in portlin-desktop's Depends because write installs that
    # package into a chroot with no network: anything it depends on has to be
    # in the rootfs already, put there by build, which is the half that can
    # still reach an archive. python3-gi and the GTK typelib are what the
    # dialog is written against; librsvg2-common carries the gdk-pixbuf loader
    # without which its SVG logo does not render.
    "python3-gi",
    "gir1.2-gtk-3.0",
    "librsvg2-common",
    # For the Software app. pkexec is what it elevates through, and it is a
    # separate package from polkitd in trixie. mate-polkit is the agent that
    # draws the password prompt in an Xfce session; it arrives as a Recommends
    # of xfce4 today, but a line in someone else's package is not a promise.
    "pkexec",
    "mate-polkit",
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
