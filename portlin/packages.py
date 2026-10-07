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
    # The SSH server for remote unlock: a small sshd that runs inside the
    # initramfs so an encrypted stick in a machine with no screen can be
    # given its passphrase over the network. Only -initramfs, which carries
    # dropbear-bin with it: the plain "dropbear" package would start a second
    # SSH server on the running system beside openssh. Its own hook puts it
    # in every initramfs it is installed on, so portlin's gate hook takes it
    # back out unless first boot, or portlin-remote-unlock, switched it on.
    "dropbear-initramfs",
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
    # rsync copies and zstd compresses for portlin-migrate, which ships in
    # portlin-runtime. Here rather than in TOOLS for the same reason as
    # pciutils: a --minimal stick has the tool and must be able to run it.
    "rsync",
    "zstd",
    # Ghidra's official GitHub release is a ZIP. portlin-runtime unpacks it
    # under /opt, including on a --minimal stick written without a network.
    "unzip",
    # What the AI agents' installer scripts in the Software catalog expect
    # the system to have already: Goose ships as a .tar.bz2, and the Node.js
    # Hermes fetches links libatomic. The scripts run as the user and cannot
    # apt-get what is missing. Both are small; git, which Hermes and OpenClaw
    # also need, is not, so it is in TOOLS and their entries ask for it on
    # a --minimal stick.
    "bzip2",
    "libatomic1",
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
    # Both offered by the first-boot wizard, which runs with no network, so
    # they have to be on the stick already. The SSH server ships disabled and
    # without host keys (see rootfs); ufw ships installed but switched off.
    "openssh-server",
    "ufw",
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
#
# Only the three defaults-adjacent sets ship: Papirus (the default, and the
# only archive set covering the software the Software app installs),
# Papirus's light-panel face, and Adwaita (the GTK fallback libgtk-3 pulls
# in anyway). The elementary-xfce and Numix sets used to ship too, 138 MB for
# two picker entries; they moved to the Software app, which can fetch them
# onto a networked stick in one step.
ICON_THEME_PACKAGES = {
    "Papirus-Dark": "papirus-icon-theme",
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
    # The locker the wizard's screen-lock settings configure. xflock4 prefers
    # it over light-locker, and naming it here is what makes it the one there.
    "xfce4-screensaver",
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
    # For the intro film, which draws through cairo from a GTK draw handler.
    "python3-gi-cairo",
    "librsvg2-common",
    # The Migrate window draws its progress strip and status marks with
    # cairo, and a Python draw handler needs this to receive a context.
    "python3-gi-cairo",
    # For the Software app. pkexec is what it elevates through, and it is a
    # separate package from polkitd in trixie. mate-polkit is the agent that
    # draws the password prompt in an Xfce session; it arrives as a Recommends
    # of xfce4 today, but a line in someone else's package is not a promise.
    "pkexec",
    "mate-polkit",
]

# The lite session, which the first-boot wizard offers in place of Xfce on
# machines with little memory: labwc for windows, waybar for the panel, fuzzel
# for the applications menu, mako for notifications, swayidle and swaylock for
# the screen lock. Xfce's own applications (Thunar, the terminal) run in it
# unchanged. Installed on every stick that has the desktop, because first boot
# has no network to fetch them with once someone picks it.
LITE = [
    "labwc",
    # For the X11 programs the Software app installs. Everything portlin itself
    # ships is GTK3 and runs on Wayland directly.
    "xwayland",
    "waybar",
    "fuzzel",
    "mako-notifier",
    "swaybg",
    "swayidle",
    "swaylock",
    # The lite session's display scaling reads and sets each screen with it.
    "wlr-randr",
    # Xfce's power manager answers the brightness keys in the full session;
    # labwc hands them to this.
    "brightnessctl",
    # GTK on Wayland takes its theme from GSettings rather than settings.ini,
    # so the session's autostart copies the chosen theme across with the
    # gsettings tool, into dconf, against the schema that names the keys.
    "libglib2.0-bin",
    "dconf-gsettings-backend",
    "gsettings-desktop-schemas",
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
    # 48 MB with the perl stack it brings, but the zsh prompt reads it, AI
    # agents' installers clone with it, and a stick with no network cannot
    # fetch it at the moment it turns out to be needed.
    "git",
    "usbutils",
    "lshw",
    "file",
    "tree",
    # The alternative to bash that first boot and portlin-shell offer, with
    # the two plugins its themed ~/.zshrc loads. In the image because first
    # boot has no network to fetch them with.
    "zsh",
    "zsh-autosuggestions",
    "zsh-syntax-highlighting",
    # The system-info display the portlin-branded ~/.config/fastfetch face
    # (shipped by portlin-desktop) runs on. Not a dependency of that package
    # because a --minimal rootfs can be written without it and the bashrc
    # banner uses portlin-welcome, which needs nothing.
    "fastfetch",
]

# Installed before everything else, and not part of any group: xterm's only
# job here is to be the x-terminal-emulator provider apt finds first. Two
# recommends (xdg-utils' libfile-desktopentry-perl, xinit) name that virtual,
# and apt marks command-line packages in order, so whichever terminal happens
# to sort first in the index would otherwise win a tiebreak inside apt and
# land on the desktop as a second, surprise terminal. Explicit is not just
# cheaper (xterm is a couple of MB) - it is deterministic.
SEED_FIRST = ["xterm"]

# Never wanted. plymouth is a boot splash whose entire job is hiding the boot
# log, and it fights the first-boot wizard for the console while
# plymouth-quit-wait can deadlock against the display manager. Purged at write
# time too, so a cached rootfs built before this also loses it.
#
# firmware-nvidia-graphics arrives not by any list but as a Recommends of
# firmware-misc-nonfree: 63 MB of NVIDIA GPU blobs on a stick whose NVIDIA
# users are exactly the ones opting in through the Software app's nvidia-driver
# entry, which installs the firmware itself. NEVER_INSTALL alone cannot stop a
# recommend, so it is purged at write time as well, and removal needs no
# network, which is the same plymouth argument.
NEVER_INSTALL = ["plymouth", "plymouth-label", "firmware-nvidia-graphics"]

GROUPS: dict[str, list[str]] = {
    "boot": BOOT,
    "firmware": FIRMWARE,
    "system": SYSTEM,
    "storage": STORAGE,
    "network": NETWORK,
    "desktop": DESKTOP,
    "lite": LITE,
    "audio": AUDIO,
    "fonts": FONTS,
    "apps": APPS,
    "tools": TOOLS,
}

# boot and system alone produce a bootable but headless stick. Everything else is
# opt-out territory.
MINIMAL_GROUPS = ["boot", "system", "storage", "network"]
DEFAULT_GROUPS = list(GROUPS)


def image_names() -> list[str]:
    """Every package any build of the image installs or refuses, whatever the
    groups chosen. portlin-runtime ships the list, and portlin-migrate never
    offers one of these as an old stick's own choice: the new stick has it,
    or its setup removed it (the desktop it was not given) on purpose."""
    names: set[str] = {*BOOTSTRAP_INCLUDE, *SEED_FIRST, *NEVER_INSTALL,
                       *THEME_PACKAGES.values(), *ICON_THEME_PACKAGES.values()}
    for group in GROUPS.values():
        names.update(group)
    return sorted(names)


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
