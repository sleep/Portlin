#!/usr/bin/env bash
# Boot an image under qemu in both legacy BIOS and UEFI mode.
#
# This is the acceptance test for "boots on any x86_64 machine". The two firmware
# families are genuinely different code paths -- MBR boot code plus the BIOS boot
# partition on one side, the EFI/BOOT/BOOTX64.EFI fallback on the other -- and a
# stick can easily work on one and be invisible to the other.
#
# Success is defined as GRUB reaching its menu and printing the Portlin entry to
# the serial port. That covers firmware handoff, bootloader installation and
# grub.cfg generation, which is where portability actually breaks. Whether
# userspace then comes up is a property of the packages, not of the boot path.
set -euo pipefail

IMAGE="${1:?usage: qemu-boot-test.sh <image> [seconds]}"
TIMEOUT="${2:-90}"
QEMU="${QEMU:-qemu-system-x86_64}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v "$QEMU" >/dev/null || { echo "$QEMU not found (brew install qemu / apt install qemu-system-x86)" >&2; exit 2; }

# GRUB writes to the graphical console by default, which a headless test cannot
# read. custom.cfg is sourced by Debian's 41_custom at the end of grub.cfg, before
# the menu renders, so this redirects the menu to the serial port without
# regenerating or otherwise modifying the boot configuration.
inject_serial_output() {
    [[ "$(uname -s)" == "Linux" && "$(id -u)" == "0" ]] || {
        echo "note: not root on Linux, skipping serial injection." >&2
        echo "      The boot will run but cannot be asserted on automatically." >&2
        return 1
    }
    local dev mnt
    dev="$(losetup -P -f --show "$IMAGE")"
    mnt="$(mktemp -d)"
    mount "${dev}p3" "$mnt"
    cat > "$mnt/grub/custom.cfg" <<'EOF'
serial --unit=0 --speed=115200
terminal_output --append serial
terminal_input --append serial
EOF
    umount "$mnt"
    rmdir "$mnt"
    losetup -d "$dev"
    return 0
}

find_ovmf() {
    local candidates=(
        /usr/share/OVMF/OVMF_CODE.fd
        /usr/share/ovmf/OVMF.fd
        /usr/share/edk2/x64/OVMF_CODE.fd
        /opt/homebrew/share/qemu/edk2-x86_64-code.fd
        /usr/local/share/qemu/edk2-x86_64-code.fd
    )
    for path in "${candidates[@]}"; do
        [[ -f "$path" ]] && { echo "$path"; return 0; }
    done
    return 1
}

find_ovmf_vars() {
    # The writable variable store that pairs with the firmware image. Confusingly
    # the x86_64 code image ships alongside a varstore named for i386; both
    # architectures use the same 512 KiB layout.
    local candidates=(
        /usr/share/OVMF/OVMF_VARS.fd
        /usr/share/edk2/x64/OVMF_VARS.fd
        /opt/homebrew/share/qemu/edk2-i386-vars.fd
        /usr/local/share/qemu/edk2-i386-vars.fd
    )
    for path in "${candidates[@]}"; do
        [[ -f "$path" ]] && { echo "$path"; return 0; }
    done
    return 1
}

boot() { # $1 = label, remaining = extra qemu args
    local label="$1"; shift
    local log="$WORK/$label.log"
    echo "== $label =="
    # -no-reboot so a boot loop terminates instead of spinning for the whole
    # timeout; -display none because there is nobody to look at it.
    timeout "$TIMEOUT" "$QEMU" \
        -machine q35 \
        -m 2048 \
        -smp 2 \
        -drive "file=$IMAGE,format=raw,if=virtio" \
        -serial "file:$log" \
        -display none \
        -no-reboot \
        "$@" >/dev/null 2>&1 || true

    if grep -qi "portlin" "$log" 2>/dev/null; then
        echo "  ok   GRUB reached its menu and offered the Portlin entry"
        return 0
    fi
    echo "  FAIL no Portlin menu entry appeared on the serial console"
    echo "  ---- serial log ----"
    sed 's/^/  /' "$log" 2>/dev/null | head -40
    return 1
}

SERIAL_OK=0
inject_serial_output && SERIAL_OK=1

STATUS=0
boot bios || STATUS=1

if OVMF="$(find_ovmf)"; then
    cp "$OVMF" "$WORK/ovmf-code.fd"
    UEFI_ARGS=(-drive "if=pflash,format=raw,unit=0,readonly=on,file=$WORK/ovmf-code.fd")
    # A private, writable varstore. Without one, some OVMF builds refuse to
    # complete initialisation, and the run would fail for reasons that have
    # nothing to do with the stick.
    if VARS="$(find_ovmf_vars)"; then
        cp "$VARS" "$WORK/ovmf-vars.fd"
        UEFI_ARGS+=(-drive "if=pflash,format=raw,unit=1,file=$WORK/ovmf-vars.fd")
    fi
    boot uefi "${UEFI_ARGS[@]}" || STATUS=1
else
    echo "== uefi =="
    echo "  SKIP no OVMF firmware found. Install ovmf (apt) or qemu (brew) to test UEFI."
    STATUS=1
fi

# A pass is a pass regardless of who did the injection -- the image may already
# carry a serial stanza. The caveat only matters when something failed, where a
# missing serial console is the most likely innocent explanation.
if [[ "$SERIAL_OK" -eq 0 && "$STATUS" -ne 0 ]]; then
    echo
    echo "Serial injection was skipped, so a failure above may only mean GRUB's"
    echo "menu never reached the serial port rather than that the image is broken."
    echo "Re-run on Linux as root to be certain."
fi

exit "$STATUS"
