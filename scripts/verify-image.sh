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

for tool in portlin-info portlin-expand portlin-encrypt; do
    test -x "$MNT/usr/bin/$tool" \
        && pass "$tool is executable" \
        || fail "$tool is missing or not executable"
done

# portlin-desktop carries the theme and the wallpapers, and write installs it only
# when the rootfs actually has a desktop. Probing the same way install.py does,
# rather than asserting unconditionally, keeps a --minimal image from failing a
# check about software it was never meant to contain.
if test -x "$MNT/usr/bin/startxfce4"; then
    test -d "$MNT/usr/share/backgrounds/portlin" \
        && pass "portlin-desktop wallpapers are installed" \
        || fail "portlin-desktop wallpapers are missing (portlin-desktop did not install)"
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
