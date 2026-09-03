#!/usr/bin/env bash
# Structural verification of a written stick or image.
#
# These are the checks that catch the failures which otherwise only show up as
# "this machine won't boot it": a missing UEFI fallback path, a BIOS boot
# partition with no GRUB in it, an fstab naming a device instead of a UUID.
#
# Requires Linux and root (loop devices and mounts).
set -euo pipefail

IMAGE="${1:?usage: verify-image.sh <image-or-device>}"
FAILURES=0

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "verify-image.sh needs Linux (loop devices and mounts)" >&2
    exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
    echo "verify-image.sh must run as root" >&2
    exit 2
fi

# Tools this script cannot do its job without. A missing one is an environment
# problem, not a finding about the image, and the two must never be reported in
# the same voice: without sgdisk the partition checks all report FAIL on a
# perfectly good table, and without cryptsetup an encrypted stick reports as
# plain and is then mounted as if it were.
REQUIRED_TOOLS=(sgdisk cryptsetup losetup mount umount mountpoint mknod stat dd grep)

require_tools() {
    local missing=()
    local tool
    for tool in "${REQUIRED_TOOLS[@]}"; do
        command -v "$tool" >/dev/null || missing+=("$tool")
    done
    if (( ${#missing[@]} )); then
        printf 'verify-image.sh needs these and cannot find them: %s\n' \
            "${missing[*]}" >&2
        printf 'Refusing to run: an incomplete check reads as a verdict on the image.\n' >&2
        exit 2
    fi
}
require_tools

if [[ -b "$IMAGE" ]]; then
    DEV="$IMAGE"
    DETACH=""
else
    DEV="$(losetup -P -f --show "$IMAGE")"
    DETACH="$DEV"
fi

MNT="$(mktemp -d)"
cleanup() {
    mountpoint -q "$MNT/boot/efi" && umount "$MNT/boot/efi" || true
    mountpoint -q "$MNT/boot" && umount "$MNT/boot" || true
    mountpoint -q "$MNT" && umount "$MNT" || true
    [[ -n "${MAPPING:-}" ]] && cryptsetup close "$MAPPING" 2>/dev/null || true
    [[ -n "$DETACH" ]] && losetup -d "$DETACH" || true
    rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

part() { # partition device for number $1
    if [[ "$DEV" =~ [0-9]$ ]]; then echo "${DEV}p$1"; else echo "${DEV}$1"; fi
}

ensure_nodes() {
    # Do udev's job where there is no udev. In a container /dev is a plain tmpfs,
    # so the kernel knows about the partitions -- they are listed in sysfs -- but
    # nothing ever creates a node to reach them through.
    # Existence is not enough: reattaching a loop device reallocates its
    # partitions' minor numbers, so a node from an earlier attach is a valid
    # block file pointing at nothing.
    local number path name sysfs major minor current
    for number in 1 2 3 4; do
        path="$(part "$number")"
        name="$(basename "$path")"
        sysfs="/sys/class/block/$name/dev"
        [[ -r "$sysfs" ]] || continue
        IFS=: read -r major minor < "$sysfs"
        if [[ -b "$path" ]]; then
            current="$(stat -c '%t:%T' "$path" 2>/dev/null || echo)"
            [[ "$current" == "$(printf '%x:%x' "$major" "$minor")" ]] && continue
            rm -f "$path"
        fi
        mknod -m 0660 "$path" b "$major" "$minor" 2>/dev/null || true
    done
}
ensure_nodes

echo "Partition table"
sgdisk -p "$DEV" >/dev/null 2>&1 && pass "GPT is readable" || fail "GPT is unreadable"
sgdisk -i 1 "$DEV" 2>/dev/null | grep -q "21686148-6449-6E6F-744E-656564454649" \
    && pass "partition 1 is a BIOS boot partition" \
    || fail "partition 1 is not a BIOS boot partition (legacy BIOS will not boot)"
sgdisk -i 2 "$DEV" 2>/dev/null | grep -qi "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" \
    && pass "partition 2 is an EFI system partition" \
    || fail "partition 2 is not an ESP (UEFI will not boot)"
# sgdisk reports attributes as a 64-bit hex word rather than by name, so the
# check has to read bit 2 out of it rather than grep for a phrase.
ATTRS="$(sgdisk -i 1 "$DEV" 2>/dev/null | sed -n 's/^Attribute flags: *//p')"
if [[ -n "$ATTRS" ]] && (( ( 16#$ATTRS >> 2 ) & 1 )); then
    pass "legacy BIOS bootable attribute is set"
else
    fail "legacy BIOS bootable attribute is missing (flags=${ATTRS:-none})"
fi

echo
echo "Legacy BIOS boot path"
# GRUB's stage 1 lives in the first sector and identifies itself in plain text.
# grep reads the raw sector directly. -a keeps it from bailing out on the
# surrounding machine code, and the C locale keeps the match byte-exact.
# Piping through strings first would also work, but it pulls in binutils, which
# no other check here needs and which base images do not carry.
if dd if="$DEV" bs=512 count=1 status=none | LC_ALL=C grep -qa GRUB; then
    pass "GRUB boot code is present in the MBR"
else
    fail "no GRUB boot code in the MBR"
fi

echo
echo "Filesystems"
ROOT_PART="$(part 4)"
if cryptsetup isLuks "$ROOT_PART" 2>/dev/null; then
    pass "root partition is a LUKS container"
    cryptsetup luksDump "$ROOT_PART" | grep -q "argon2id" \
        && pass "key derivation is argon2id" \
        || fail "key derivation is not argon2id"
    ENCRYPTED=1
    MAPPING="portlin_verify"
    echo "  (enter the stick's passphrase to continue)"
    cryptsetup open "$ROOT_PART" "$MAPPING"
    ROOT_DEV="/dev/mapper/$MAPPING"
else
    ENCRYPTED=0
    pass "root partition is unencrypted (as expected for a plain build)"
    ROOT_DEV="$ROOT_PART"
fi

mount "$ROOT_DEV" "$MNT"
mount "$(part 3)" "$MNT/boot"
mount "$(part 2)" "$MNT/boot/efi"

echo
echo "UEFI boot path"
# The fallback path is what makes the stick boot on a machine whose firmware has
# never heard of it. Without this file the stick is UEFI-unbootable on any
# machine it has not previously been registered with.
[[ -f "$MNT/boot/efi/EFI/BOOT/BOOTX64.EFI" ]] \
    && pass "EFI/BOOT/BOOTX64.EFI removable fallback exists" \
    || fail "EFI/BOOT/BOOTX64.EFI is missing"

echo
echo "Kernel and initramfs"
ls "$MNT"/boot/vmlinuz-* >/dev/null 2>&1 && pass "a kernel is installed" || fail "no kernel in /boot"
ls "$MNT"/boot/initrd.img-* >/dev/null 2>&1 && pass "an initramfs is present" || fail "no initramfs in /boot"
[[ -f "$MNT/boot/grub/grub.cfg" ]] && pass "grub.cfg was generated" || fail "no grub.cfg"

if [[ -f "$MNT/boot/grub/grub.cfg" ]]; then
    # Two forms are correct and portable: root=UUID=... on a plain stick, and
    # root=/dev/mapper/portlin_root on an encrypted one, where the mapper name
    # comes from our own crypttab and so means the same thing everywhere. What
    # must never appear is a raw kernel device path -- particularly the build
    # host's loop device, which grub-mkconfig will happily bake in.
    if grep -Eq 'root=(UUID=|/dev/mapper/portlin_root)' "$MNT/boot/grub/grub.cfg"; then
        pass "kernel command line identifies root portably"
    else
        fail "kernel command line does not identify root portably"
    fi
    if grep -Eq 'root=/dev/(sd|nvme|mmcblk|loop|vd)' "$MNT/boot/grub/grub.cfg"; then
        fail "kernel command line names a raw device path (correct on one machine only)"
    else
        pass "kernel command line contains no raw device path"
    fi
    grep -q 'search --no-floppy --fs-uuid' "$MNT/boot/grub/grub.cfg" \
        && pass "GRUB locates /boot by filesystem UUID" \
        || fail "GRUB does not locate /boot by UUID"

    # The boot theme. Every part of it fails on its own terms and all of them
    # end at the same plain text menu, which is also what a stick with no
    # theme at all shows -- so none of this is visible to the eye afterwards.
    THEME_DIR="boot/grub/themes/portlin"

    test -f "$MNT/$THEME_DIR/theme.txt" \
        && pass "the boot theme is installed" \
        || fail "no /$THEME_DIR/theme.txt (the boot menu is unbranded)"

    head -c 8 "$MNT/$THEME_DIR/logo.png" 2>/dev/null | grep -qa PNG \
        && pass "the mark is installed for the boot menu" \
        || fail "/$THEME_DIR/logo.png is missing or is not a PNG"

    head -c 8 "$MNT/$THEME_DIR/background.png" 2>/dev/null | grep -qa PNG \
        && pass "the boot background is installed" \
        || fail "/$THEME_DIR/background.png is missing or is not a PNG"

    # grub-mkconfig emits a loadfont for every .pf2 in the theme's own
    # directory and looks nowhere else. A font kept somewhere tidier is never
    # loaded, and the menu entries then draw as nothing on a screen that
    # otherwise looks entirely correct.
    test -f "$MNT/$THEME_DIR/unicode.pf2" \
        && pass "the boot menu font sits in the theme directory" \
        || fail "/$THEME_DIR/unicode.pf2 is missing (menu entries render blank)"

    # The three lines grub-mkconfig writes only because those files were in
    # place before it ran. Checked separately from the files themselves: a
    # theme written after grub-mkconfig leaves the directory looking perfect
    # and the generated config still pointing at nothing.
    grep -q 'set theme=.*themes/portlin/theme.txt' "$MNT/boot/grub/grub.cfg" \
        && pass "grub.cfg selects the portlin boot theme" \
        || fail "grub.cfg sets no theme (the theme was written too late)"

    grep -q 'loadfont .*themes/portlin/unicode.pf2' "$MNT/boot/grub/grub.cfg" \
        && pass "grub.cfg loads the boot theme font" \
        || fail "grub.cfg has no loadfont for the theme font"

    grep -q '^insmod png' "$MNT/boot/grub/grub.cfg" \
        && pass "grub.cfg loads the png reader for the mark" \
        || fail "grub.cfg has no insmod png (the mark cannot be decoded)"

    # 05_debian_theme resolves a background on every grub-mkconfig and always
    # resolves one. Its chain ends at desktop-base, so an unset GRUB_BACKGROUND
    # is not a bare screen -- it is Debian's wallpaper behind portlin's boot
    # log, on the terminal screen the menu is replaced by. The theme cannot
    # prevent that, and no other assertion here notices it.
    grep -q 'background_image .*themes/portlin/background.png' \
        "$MNT/boot/grub/grub.cfg" \
        && pass "grub.cfg draws the boot log on portlin's background" \
        || fail "grub.cfg names no portlin background (desktop-base won instead)"

    # The counterpart, and the only evidence Debian's render is off the stick
    # rather than merely unreferenced. 05_debian_theme copies a background that
    # lives on another filesystem into /boot/grub, and deletes that copy only
    # once the background it resolves is one it can read in place.
    test -e "$MNT/boot/grub/.background_cache.png" \
        && fail "/boot/grub/.background_cache.png remains (desktop-base's render is still on the stick)" \
        || pass "no cached background from desktop-base is left on /boot"
fi

echo
echo "Portability"
if grep -Eq '^\s*/dev/(sd|nvme|mmcblk|loop)' "$MNT/etc/fstab"; then
    fail "fstab references a kernel device path (correct on exactly one machine)"
else
    pass "fstab uses UUIDs only"
fi
grep -q "GRUB_DISABLE_OS_PROBER=true" "$MNT/etc/default/grub" \
    && pass "os-prober is disabled" \
    || fail "os-prober is enabled (will mount and advertise host disks)"
grep -q "MODULES=most" "$MNT/etc/initramfs-tools/conf.d/portlin" \
    && pass "initramfs contains all drivers" \
    || fail "initramfs is built with host-specific modules only"
[[ ! -s "$MNT/etc/machine-id" ]] \
    && pass "machine-id is empty and will regenerate" \
    || fail "machine-id is baked in (clones will be twins)"
compgen -G "$MNT/etc/ssh/ssh_host_*" >/dev/null \
    && fail "SSH host keys are baked into the image" \
    || pass "no SSH host keys baked in"

echo
echo "First boot"
[[ -f "$MNT/var/lib/portlin/firstboot-pending" ]] \
    && pass "first-boot wizard is armed" \
    || fail "first-boot sentinel is missing (no account will be created)"
[[ -x "$MNT/usr/local/sbin/portlin-firstboot" ]] \
    && pass "wizard is executable" \
    || fail "wizard is missing or not executable"
[[ -L "$MNT/etc/systemd/system/multi-user.target.wants/portlin-firstboot.service" ]] \
    && pass "wizard unit is enabled" \
    || fail "wizard unit is not enabled"

# The cryptsetup package ships an empty /etc/crypttab template, so its mere
# presence says nothing. Only an encrypted stick must have a real entry.
if [[ "$ENCRYPTED" == "1" ]]; then
    grep -q "^portlin_root\s\+UUID=" "$MNT/etc/crypttab" 2>/dev/null \
        && pass "crypttab identifies the container by UUID" \
        || fail "crypttab does not use a UUID"
else
    grep -q "^portlin_root" "$MNT/etc/crypttab" 2>/dev/null \
        && fail "an unencrypted stick has a crypttab entry" \
        || pass "no crypttab entry, as expected for a plain build"
fi

echo
echo "Runtime packages"
test -f "$MNT/var/lib/dpkg/info/portlin-runtime.list" \
    && pass "portlin-runtime is installed" \
    || fail "portlin-runtime is not installed (no updates will reach this stick)"

# The keyring package ships its key and apt source together, and only once a
# real signing key has been committed to the repository (portlin-archive-
# keyring.gpg ships as a zero-byte placeholder until then). Asserting either
# unconditionally would fail this check on every build made before that key
# exists, for a reason that has nothing to do with the image.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYRING_PLACEHOLDER="$REPO_ROOT/portlin/resources/keyring/portlin-archive-keyring.gpg"
if [[ -s "$KEYRING_PLACEHOLDER" ]]; then
    test -f "$MNT/etc/apt/sources.list.d/portlin.sources" \
        && pass "the portlin apt source is present" \
        || fail "the portlin apt source is missing"

    test -s "$MNT/usr/share/keyrings/portlin-archive-keyring.gpg" \
        && pass "the archive keyring is present" \
        || fail "the archive keyring is missing or empty (apt will reject the archive)"
else
    echo "  (skip) apt source and archive keyring: no real signing key is committed yet"
fi

test -L "$MNT/etc/systemd/system/multi-user.target.wants/portlin-finalise-encryption.service" \
    && pass "the encryption finaliser is enabled" \
    || fail "the encryption finaliser is not enabled"

for tool in portlin-info portlin-expand portlin-encrypt portlin-install; do
    test -x "$MNT/usr/bin/$tool" \
        && pass "$tool is executable" \
        || fail "$tool is missing or not executable"
done

test -f "$MNT/usr/lib/portlin/catalog.py" \
    && pass "the software catalog is installed" \
    || fail "usr/lib/portlin/catalog.py is missing (portlin-install can list nothing)"

test -f "$MNT/usr/share/polkit-1/actions/org.portlin.install.policy" \
    && pass "the polkit action for portlin-install is installed" \
    || fail "org.portlin.install.policy is missing (Software cannot elevate)"

# The two the installer shells out to. Neither is in any file list, and
# without them portlin-install starts, lists the catalog, and then fails at
# the first thing anybody wanted it for.
test -x "$MNT/usr/bin/lspci" \
    && pass "lspci is installed for the driver scan" \
    || fail "lspci is missing (the driver scan has nothing to read)"

test -x "$MNT/usr/bin/curl" \
    && pass "curl is installed for the installer's downloads" \
    || fail "curl is missing (nothing the installer downloads can be fetched)"

# portlin-desktop carries the theme and the wallpapers, and write installs it only
# when the rootfs actually has a desktop. Probing the same way install.py does,
# rather than asserting unconditionally, keeps a --minimal image from failing a
# check about software it was never meant to contain.
if test -x "$MNT/usr/bin/startxfce4"; then
    test -d "$MNT/usr/share/backgrounds/portlin" \
        && pass "portlin-desktop wallpapers are installed" \
        || fail "portlin-desktop wallpapers are missing (portlin-desktop did not install)"

    test -f "$MNT/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml" \
        && pass "the theme defaults are in the xdg directory portlin owns" \
        || fail "the theme defaults are missing from /etc/xdg/xdg-portlin"

    # Both halves, because either one alone is silent. The defaults are inert
    # unless the session searches that directory, and a stick whose desktop
    # merely looks wrong says nothing about which half went missing.
    test -x "$MNT/usr/bin/portlin-software" \
        && pass "portlin-software is executable" \
        || fail "portlin-software is missing or not executable"

    test -f "$MNT/usr/share/applications/portlin-software.desktop" \
        && pass "Software is in the applications menu" \
        || fail "portlin-software.desktop is missing (nothing opens the Software app)"

    test -x "$MNT/usr/bin/pkexec" \
        && pass "pkexec is installed for the Software app" \
        || fail "pkexec is missing (Software cannot ask for a password)"

    # pkexec needs an agent running in the session to draw the prompt, and its
    # absence is silent: pkexec exits 127 and the app can only report that
    # nobody answered.
    AGENT_ENTRY="$MNT/etc/xdg/autostart/polkit-mate-authentication-agent-1.desktop"
    if test -f "$AGENT_ENTRY"; then
        pass "a polkit authentication agent starts with the session"
        if grep -q '^OnlyShowIn=' "$AGENT_ENTRY"; then
            grep -q '^OnlyShowIn=.*XFCE' "$AGENT_ENTRY" \
                && pass "the agent shows in Xfce sessions" \
                || fail "the agent's OnlyShowIn excludes XFCE (no prompt will appear)"
        fi
    else
        fail "no polkit agent autostart entry (pkexec prompts have nowhere to appear)"
    fi

    grep -q xdg-portlin \
        "$MNT/etc/X11/Xsession.d/40portlin-desktop_xdg-config-dirs" 2>/dev/null \
        && pass "the session puts portlin's xdg directory on XDG_CONFIG_DIRS" \
        || fail "nothing puts /etc/xdg/xdg-portlin on XDG_CONFIG_DIRS"

    # The menu entry and the program it names, separately: an entry whose Exec
    # points at nothing that was installed is not a missing feature, it is a
    # line in the applications menu that does nothing when clicked.
    test -x "$MNT/usr/bin/portlin-about" \
        && pass "portlin-about is executable" \
        || fail "portlin-about is missing or not executable"

    test -f "$MNT/usr/share/applications/portlin-about.desktop" \
        && pass "About Portlin is in the applications menu" \
        || fail "portlin-about.desktop is missing (nothing opens the About dialog)"

    # Without this, X-Xfce-Toplevel still keeps the entry out of a submenu,
    # but it lands wherever the generic merge puts it rather than right above
    # About Xfce.
    test -f "$MNT/etc/xdg/menus/xfce-applications-merged/portlin-about.menu" \
        && pass "About Portlin is positioned above About Xfce in the menu" \
        || fail "portlin-about.menu is missing (About Portlin lands away from About Xfce)"

    test -x "$MNT/usr/bin/portlin-caffeine" \
        && pass "portlin-caffeine is executable" \
        || fail "portlin-caffeine is missing or not executable"

    test -f "$MNT/usr/share/applications/portlin-caffeine.desktop" \
        && pass "Caffeine is in the applications menu" \
        || fail "portlin-caffeine.desktop is missing (nothing launches the applet)"

    # The applet is meant to be in the panel without anyone launching it, and
    # the autostart entry is the only thing that puts it there. Its absence
    # looks exactly like a working stick until someone goes looking for a cup
    # that never appeared.
    test -f "$MNT/etc/xdg/autostart/portlin-caffeine.desktop" \
        && pass "Caffeine starts with the session" \
        || fail "no autostart entry (the applet never reaches the panel)"

    # Both states, because the icon is the whole indicator: a stick missing the
    # active one keeps the machine awake showing an empty cup.
    for state in on off; do
        test -f "$MNT/usr/share/portlin/caffeine-$state.svg" \
            && pass "the caffeine $state icon is installed" \
            || fail "caffeine-$state.svg is missing (the panel cannot show that state)"
    done

    # The two commands the applet shells out to. Neither is in its file list,
    # both are declared dependencies, and without either the applet still
    # starts and still shows a filled cup while the machine goes to sleep.
    test -x "$MNT/usr/bin/xset" \
        && pass "xset is installed for the caffeine applet" \
        || fail "xset is missing (nothing stops X blanking the screen)"

    test -x "$MNT/usr/bin/systemd-inhibit" \
        && pass "systemd-inhibit is installed for the caffeine applet" \
        || fail "systemd-inhibit is missing (nothing takes the logind lock)"

    # The applications menu button. Debian's panel layout declares the plugin
    # with no button-icon, so it falls back to the icon name compiled into it
    # and portlin's icon theme answers to that name. Every step here fails
    # invisibly: the button simply keeps Xfce's own icon.
    ICON_THEME_DIR="usr/share/icons/Portlin"

    test -f "$MNT/$ICON_THEME_DIR/index.theme" \
        && pass "the portlin icon theme is installed" \
        || fail "no /$ICON_THEME_DIR/index.theme (the icon theme does not exist)"

    # It provides one icon. Without an Inherits line, selecting it does not
    # fall back to the stock icons -- it takes every other icon off the
    # desktop, which is a far louder failure than the one it was meant to fix.
    grep -q "^Inherits=" "$MNT/$ICON_THEME_DIR/index.theme" \
        && pass "the icon theme inherits a stock set" \
        || fail "Portlin/index.theme inherits nothing (the desktop loses every other icon)"

    test -d "$MNT/usr/share/icons/Adwaita" \
        && pass "the inherited icon theme is installed" \
        || fail "Adwaita is missing (the portlin theme inherits from nothing)"

    test -f "$MNT/$ICON_THEME_DIR/scalable/apps/org.xfce.panel.applicationsmenu.svg" \
        && pass "the mark is installed as the applications menu button" \
        || fail "the menu button icon is missing (the menu keeps Xfce's own)"

    grep -q 'IconThemeName" type="string" value="Portlin"' \
        "$MNT/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml" \
        && pass "the session asks for the portlin icon theme" \
        || fail "xsettings names no icon theme (nothing ever selects it)"

    grep -q "^icon-theme-name=Portlin" \
        "$MNT/etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf" \
        && pass "the greeter asks for the portlin icon theme" \
        || fail "the greeter names no icon theme (login runs on the stock set)"

    # The greeter reads a literal path: it runs before any session, so there is
    # no XDG_CONFIG_DIRS and no xfconf to indirect through. A path to a render
    # that never shipped leaves lightdm's own grey behind the login prompt and
    # says nothing about it. Resolved out of the file rather than repeated here.
    GREETER_BACKGROUND="$(sed -n 's/^background=//p' \
        "$MNT/etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf")"
    test -f "$MNT/$GREETER_BACKGROUND" \
        && pass "the greeter background is a wallpaper the image ships" \
        || fail "the greeter background $GREETER_BACKGROUND is not installed"

    # The name the plugin asks for is compiled into it, not configurable. A
    # Debian that renames its menu icon leaves every check above passing and
    # every stick showing Xfce's own button. This is the one that notices.
    PANEL_PLUGIN="$MNT/usr/lib/x86_64-linux-gnu/xfce4/panel/plugins/libapplicationsmenu.so"
    if test -f "$PANEL_PLUGIN"; then
        grep -qa "org.xfce.panel.applicationsmenu" "$PANEL_PLUGIN" \
            && pass "the panel still asks for the icon name portlin provides" \
            || fail "libapplicationsmenu asks for some other icon name now"
    fi

    # The button's text label. There is no name-based indirection for text the
    # way there is for the icon, so this is a direct xfconf property write
    # keyed by plugin-1's numeric id -- the id Debian's own shipped
    # /etc/xdg/xfce4/panel/default.xml assigns to applicationsmenu. The check
    # right below this one is what notices if that ever stops being true.
    PANEL_DEFAULTS="$MNT/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
    PORTLIN_VERSION="$(sed -n 's/^PORTLIN_VERSION=//p' "$MNT/etc/portlin-release" 2>/dev/null)"

    grep -q '"plugin-1" type="empty"' "$PANEL_DEFAULTS" 2>/dev/null \
        && pass "the panel label targets plugin-1" \
        || fail "no plugin-1 override in $PANEL_DEFAULTS (the button keeps no label)"

    grep -qF "\"button-title\" type=\"string\" value=\"Portlin ${PORTLIN_VERSION:-unknown}\"" \
        "$PANEL_DEFAULTS" 2>/dev/null \
        && pass "the menu button is labelled with the installed version" \
        || fail "the panel button-title does not match /etc/portlin-release"

    grep -q '"show-button-title" type="bool" value="true"' "$PANEL_DEFAULTS" 2>/dev/null \
        && pass "the menu button is set to show its label" \
        || fail "show-button-title is not enabled (the label would be invisible)"

    # Debian's own layout, not portlin's overlay: this is the fact the label
    # above depends on. If a future Debian renumbers its default panel, this
    # is the check that fails instead of the stick quietly mislabelling
    # whatever plugin-1 has become.
    DEBIAN_PANEL_DEFAULT="$MNT/etc/xdg/xfce4/panel/default.xml"
    if test -f "$DEBIAN_PANEL_DEFAULT"; then
        grep -q '"plugin-1" type="string" value="applicationsmenu"' "$DEBIAN_PANEL_DEFAULT" \
            && pass "Debian's panel layout still assigns plugin-1 to applicationsmenu" \
            || fail "plugin-1 is no longer applicationsmenu in Debian's default panel layout"
    fi

    # Icon=portlin in the About entry resolves here. In hicolor rather than in
    # the portlin theme, because every icon theme falls back to hicolor and
    # none falls back to Portlin.
    test -f "$MNT/usr/share/icons/hicolor/scalable/apps/portlin.svg" \
        && pass "the mark is installed under its own icon name" \
        || fail "hicolor apps/portlin.svg is missing (About Portlin has no icon)"

    # A wallpaper reaches a fresh account only by being the file xfdesktop
    # falls back to when no xfconf property names the monitor, and every step
    # of that takeover fails silently on its own: the account simply comes up
    # showing Debian's backdrop, which is also what a stick with no wallpaper
    # at all shows. Checked here rather than left to the eye, because the
    # difference is invisible to every other assertion in this file.
    BACKDROP="usr/share/backgrounds/xfce/xfce-x.svg"

    grep -qx "/$BACKDROP" "$MNT/var/lib/dpkg/diversions" 2>/dev/null \
        && pass "xfdesktop's default backdrop is diverted to portlin" \
        || fail "no diversion of /$BACKDROP (portlin-desktop's preinst did not run)"

    # By content, not by name. The path keeps its .svg suffix so that dpkg and
    # xfdesktop4-data still agree on what is being diverted, and xfdesktop
    # sniffs the bytes rather than the extension, so PNG magic is the only
    # honest evidence that the file there is portlin's render.
    head -c 8 "$MNT/$BACKDROP" 2>/dev/null | grep -qa PNG \
        && pass "portlin's render is installed as the default backdrop" \
        || fail "/$BACKDROP is not a PNG (portlin's render did not land there)"

    test -e "$MNT/$BACKDROP.distrib" \
        && pass "Debian's own backdrop is preserved beside it" \
        || fail "/$BACKDROP.distrib is missing (the diversion displaced nothing)"

    # The path portlin diverts is compiled into xfdesktop, not configurable,
    # so a Debian that rebuilds it against a different default would leave
    # every check above passing and every stick showing the wrong wallpaper.
    # This is the one assertion that notices.
    if test -x "$MNT/usr/bin/xfdesktop"; then
        grep -qa "/$BACKDROP" "$MNT/usr/bin/xfdesktop" \
            && pass "xfdesktop still falls back to the path portlin diverts" \
            || fail "xfdesktop no longer references /$BACKDROP (its built-in default moved)"
    fi
else
    echo "  (skip) portlin-desktop wallpapers: this is a headless image"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
    echo "All checks passed."
else
    echo "$FAILURES check(s) failed."
fi
exit "$FAILURES"
