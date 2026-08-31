#!/usr/bin/env bash
# End-to-end test of the write path against sparse images, for real.
#
# Everything the unit tests deliberately stub out happens here: sgdisk on a real
# partition table, mkfs, losetup, LUKS, mount, chroot, grub-install. Both the
# plain and the encrypted layout are exercised, because they are genuinely
# different code paths and each has hidden a bug the other did not.
#
# Runs natively on x86_64 Linux as root, or inside a privileged linux/amd64
# container anywhere Docker runs, including an arm64 Mac.
set -euo pipefail

cd "$(dirname "$0")/.."

ROOTFS="${ROOTFS:-}"
SIZE="${SIZE:-16G}"
PASSPHRASE="${PASSPHRASE:-integration-test-passphrase}"
IMAGE_DIR="${IMAGE_DIR:-$PWD/out}"

if [[ -z "$ROOTFS" || ! -f "$ROOTFS" ]]; then
    cat >&2 <<'EOF'
Set ROOTFS to a tarball built by 'portlin build'.

    ROOTFS=out/rootfs.tar.zst scripts/integration-test.sh

To produce one on a machine that is not x86_64 Linux, see the container recipe
in the README. Building is the slow half; this script only tests the fast half,
which is the point of them being separate commands.
EOF
    exit 2
fi

ROOTFS="$(cd "$(dirname "$ROOTFS")" && pwd)/$(basename "$ROOTFS")"
mkdir -p "$IMAGE_DIR"

# The body runs identically whether it is executed directly or inside the
# container, so there is only one copy of it to keep correct.
read -r -d '' BODY <<'SCRIPT' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v sgdisk >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        python3 gdisk parted dosfstools e2fsprogs cryptsetup-bin util-linux \
        zstd tar mount coreutils >/dev/null
fi
cd /src

echo "=== plain write ==="
rm -f "$IMAGE_DIR/integration-plain.img"
python3 -m portlin write --target "$IMAGE_DIR/integration-plain.img" \
    --image-size "$SIZE" --rootfs "$ROOTFS" --yes
bash scripts/verify-image.sh "$IMAGE_DIR/integration-plain.img"

echo
echo "=== encrypted write ==="
rm -f "$IMAGE_DIR/integration-encrypted.img"
# The passphrase is prompted twice and read from stdin. It is never accepted as
# a flag, so this is the only way to drive it non-interactively.
printf '%s\n%s\n' "$PASSPHRASE" "$PASSPHRASE" | \
    python3 -m portlin write --target "$IMAGE_DIR/integration-encrypted.img" \
        --image-size "$SIZE" --rootfs "$ROOTFS" --encrypt --yes
echo "$PASSPHRASE" | bash scripts/verify-image.sh "$IMAGE_DIR/integration-encrypted.img"
SCRIPT

if [[ "$(uname -s)" == "Linux" && "$(id -u)" == "0" ]]; then
    echo "running natively"
    IMAGE_DIR="$IMAGE_DIR" ROOTFS="$ROOTFS" SIZE="$SIZE" PASSPHRASE="$PASSPHRASE" \
        bash -c "$BODY"
else
    command -v docker >/dev/null || {
        echo "needs either root on x86_64 Linux, or docker" >&2
        exit 2
    }
    echo "running in a privileged linux/amd64 container"
    # --privileged is required for loop devices and mounts. Note that /dev inside
    # is a plain tmpfs with no udev, so partition nodes are never created
    # automatically -- portlin handles that itself, which is precisely the
    # behaviour worth testing here.
    docker run --rm --privileged --platform linux/amd64 \
        -v "$PWD:/src" \
        -v "$IMAGE_DIR:/images" \
        -v "$(dirname "$ROOTFS"):/rootfs:ro" \
        -e IMAGE_DIR=/images \
        -e ROOTFS="/rootfs/$(basename "$ROOTFS")" \
        -e SIZE="$SIZE" \
        -e PASSPHRASE="$PASSPHRASE" \
        -w /src debian:trixie bash -c "$BODY"
fi

echo
echo "Both layouts written and verified. Boot them with:"
echo "    scripts/qemu-boot-test.sh $IMAGE_DIR/integration-plain.img"
