<p align="center">
  <img src="https://github.com/sleep/Portlin/releases/download/v0.1.0/portlin-banner.png" alt="portlin" width="900">
</p>

Portlin writes a **real, upgradable Debian + Xfce install** onto a USB stick that boots on any
x86_64 machine, BIOS or UEFI, optionally with a LUKS2-encrypted root.

Not a live ISO with a persistence overlay. Kernel upgrades work. `apt full-upgrade` works.
Think Rufus' "Windows To Go", for Linux.

```
sudo portlin create --target /dev/sdb --encrypt
```

Boot the result anywhere and a first-run wizard asks for an account, keyboard, language and time
zone, then offers to grow the system to fill the drive.

## Requirements

x86_64 Linux host, root, and these packages:

```
sudo apt install debootstrap gdisk parted dosfstools e2fsprogs cryptsetup zstd
git clone https://github.com/sleep/Portlin && cd Portlin
sudo python3 -m portlin doctor
```

`doctor` reports everything missing at once, and names the package that provides it.

## Commands

```
portlin doctor                                   # check the host
portlin devices                                  # list candidate targets
portlin build -o rootfs.tar.zst                  # slow half, 20-40 min
portlin write -t /dev/sdb --rootfs rootfs.tar.zst --encrypt   # fast half, ~2 min
portlin create -t /dev/sdb --encrypt             # both, in one step
```

`build` produces a hardware-agnostic, identity-free tarball. Making a second stick, or redoing one
after a mistake, does not mean waiting for another debootstrap.

## Flags

| Flag | Effect |
|---|---|
| `--encrypt` | LUKS2 over root. Prompts for a passphrase on the terminal |
| `--minimal` | No desktop. Boot, system, storage and network only |
| `--groups desktop,apps` | Pick package groups explicitly |
| `--extra tmux --exclude firefox-esr` | Adjust the package set |
| `--suite bookworm` | Build a different Debian release |
| `--target stick.img` | Write to an image file instead of a device |
| `--dry-run` | Print every command that would run, and stop |

There is deliberately **no** `--passphrase` flag: it would be readable from `/proc` by every user on
the machine and would land in shell history.

## What lands on the stick

GPT, four partitions:

| # | Size | Type | Purpose |
|---|------|------|---------|
| 1 | 1 MiB | `EF02` | BIOS boot, holds GRUB's `core.img` |
| 2 | 512 MiB | `EF00` | ESP, FAT32, `/boot/efi` |
| 3 | 1 GiB | ext4 | `/boot`, plaintext |
| 4 | rest | ext4 or LUKS2 | root |

The image ships at 8 GB no matter how big the stick is, so one image fits every drive and the flash
is fast. On first boot it offers to expand into the rest.

The Xfce desktop is dark out of the box: Greybird-dark across GTK, window decorations, the LightDM
greeter and the terminal. The defaults live in `/etc/xdg`, so Settings > Appearance still changes
them and the change sticks.

## Updates

The Debian system updates itself: it is a real install, so `apt full-upgrade`
and kernel upgrades work.

Portlin's own contribution to the stick is split in two. The desktop theme,
the wallpapers and the `portlin-info`, `portlin-expand` and `portlin-encrypt`
commands are Debian packages, and will update from portlin's archive like
anything else once that archive is published; until then they stay at
whatever version the stick was written with. The bootloader, the initramfs,
`fstab` and `crypttab` are written once and stay put, because an update that
breaks one of those is a stick that will not boot. Moving those forward means
writing the stick again.

## Safety

`write` erases whatever you point it at. Before it does:

- non-removable devices are refused unless you pass `--force`
- devices under 8 GiB are refused outright, `--force` or not
- devices with mounted filesystems are refused outright
- the confirmation prompt requires typing the device path, not `y`

Any failure mid-write unwinds in exact reverse: unmount, close the LUKS mapping, detach the loop
device.

## Development

```
make image     # build a real image, with progress
make test      # 473 unit tests, no root, no Linux, ~1s
make dryrun    # print the full command plan
make check     # tests plus shellcheck
make harness   # shipped scripts against real loop devices, needs Docker
```

`make image` runs the whole pipeline and shows where it is: a bar per stage, the current package,
and an ETA. On a host that cannot build directly (anything but x86_64 Linux as root, which includes
every Mac) it re-runs itself inside a privileged `linux/amd64` container and renders the same
display from in there, so the same command works everywhere.

Every percentage comes from the tool doing the work rather than from a guess about phases: apt's
`APT::Status-Fd` stream, debootstrap's package lines, tar's checkpoints. The overall ETA is the one
estimate, weighted by how long each stage took on this machine last time; the first build has no
history and says so.

Unit tests run anywhere, including macOS, because every external command goes through one `Runner`
that can record instead of execute. That makes `build_rootfs` and `write_stick` assertable as
ordered command lists, which is where the real risk lives: a `crypttab` written after
`update-initramfs` produces a stick that cannot unlock itself, and no type checker finds that.

---

<details>
<summary><b>How it boots on machines it has never seen</b></summary>

- **Both firmware families.** GRUB is installed twice: `i386-pc` into the MBR gap, and
  `x86_64-efi --removable --no-nvram` for UEFI. `--removable` writes `EFI/BOOT/BOOTX64.EFI`, the
  fallback path every UEFI implementation probes on removable media. `--no-nvram` keeps
  grub-install out of the build machine's firmware.
- **`MODULES=most`** in the initramfs, so every storage and USB controller driver is present.
- **`GRUB_DISABLE_OS_PROBER=true`**, so the menu does not advertise the build host's operating
  systems and os-prober never mounts a stranger's internal disks.
- **UUIDs everywhere.** `/dev/sda4` is correct on exactly one machine.
- **`RESUME=none`**, so the initramfs does not stall hunting for someone else's hibernation image.
- **Firmware and microcode for everyone**: iwlwifi, realtek, atheros, brcm80211, plus both
  `intel-microcode` and `amd-microcode`.
- **A capped LUKS KDF.** cryptsetup sizes argon2id by benchmarking whichever machine formats the
  container, so a stick formatted on a workstation can be unopenable on a netbook. Portlin caps it
  at 256 MiB.
- **Flash-aware defaults**: `noatime,commit=120`, and zram instead of swap.

</details>

<details>
<summary><b>Why <code>/boot</code> is not encrypted</b></summary>

GRUB can only read LUKS2 with the old PBKDF2 derivation, and unlocking in GRUB means typing the
passphrase twice at every boot. The cost of a plaintext `/boot` is that someone with physical
access can tamper with your kernel. The benefit is one prompt, a modern KDF, and the same
arrangement the Debian installer itself produces.

</details>

<details>
<summary><b>How expansion works</b></summary>

Each layer can only grow into space the layer beneath it has already claimed, so the order is not a
preference:

1. `growpart` grows the last partition, via the kernel's live partition-resize ioctl.
2. `cryptsetup resize` grows the LUKS mapping into the enlarged partition.
3. `resize2fs` grows the filesystem into the enlarged mapping.

On an unencrypted stick, step 2 is skipped. Declining is safe and repeatable: nothing depends on
having expanded, and the same three commands work later by hand.

</details>

<details>
<summary><b>First boot in detail</b></summary>

The image ships with no user, an empty machine-id, no SSH host keys and a locked root account.
`portlin-firstboot.service` runs on tty1 before LightDM and collects the account, hostname, locale,
keyboard and time zone. On an encrypted stick it also offers to change the LUKS passphrase, so the
person holding the stick owns the key rather than whoever built it.

If the wizard is cancelled or crashes its sentinel stays in place and it runs again next boot,
rather than stranding you at a login screen with no accounts. On a stick encrypted during that boot
but never finished, the initramfs recognises the situation and asks for the passphrase itself.
Otherwise no `crypttab` would exist yet, nothing would unlock the root, and cancelling a wizard
would leave an unbootable drive.

</details>

<details>
<summary><b>Verification tiers</b></summary>

| Tier | Needs | Command |
|---|---|---|
| Unit | nothing | `make test` |
| Real-device harnesses | Docker, privileged | `make harness` |
| Structural | Linux, root | `sudo scripts/verify-image.sh stick.img` |
| Integration | Linux or Docker, privileged | `ROOTFS=... scripts/integration-test.sh` |
| End to end | qemu | `make prove` |

`make harness` earns its keep. It runs the *shipped* scripts against real block devices in about
three minutes: `test-encrypt-hook.py` drives the initramfs encryption script against a loop device
(prompts, fsck, shrink, encrypt, unlock, mount, and a canary file to prove the data survived), and
`test-expand.py` runs the wizard's own discovery and expansion code against a real mounted
filesystem, encrypted and not.

Between them they caught a malformed `partx` argument, `lsblk -o PKNAME` returning empty for
device-mapper volumes, an assumption that udev had created `/dev/mapper` symlinks, a filesystem
left larger than its container, and `cryptsetup resize` silently prompting for a passphrase with no
terminal. Every one had already reached a USB stick, because the only test covering them was a
fifteen-minute emulated boot.

`verify-image.sh` is the one to run after any change to the write path: it loop-mounts a finished
image and checks the things that only show up as "this machine won't boot it" - the UEFI fallback
file, GRUB in the MBR, the BIOS boot partition type, UUID-only fstab, an empty machine-id, an armed
wizard.

`qemu-boot-test.sh` boots the image under both legacy BIOS and UEFI/OVMF and asserts GRUB reaches
its menu on each. Those are genuinely different code paths and a stick can work on one while being
invisible to the other.

</details>

<details>
<summary><b>Building on an arm64 Mac</b></summary>

Not natively, since debootstrap runs amd64 maintainer scripts. A privileged `linux/amd64` container
works end to end, at emulation speed:

```
docker run --rm --privileged --platform linux/amd64 \
    -v "$PWD:/src" -v "$PWD/out:/out" -w /src debian:trixie bash -c '
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends python3 debootstrap gdisk \
            parted dosfstools e2fsprogs cryptsetup-bin util-linux zstd tar mount
        python3 -m portlin build --minimal -o /out/rootfs.tar.zst
        python3 -m portlin write -t /out/stick.img --image-size 12G \
            --rootfs /out/rootfs.tar.zst --yes
        bash scripts/verify-image.sh /out/stick.img'
```

A container has no udev and mounts `/dev` as a plain tmpfs, so partition nodes are never created
automatically. Portlin handles that itself: it waits for the nodes, nudges the kernel with `partx`,
and finally creates them from `/sys/class/block/<name>/dev` the way udev would.

Then boot the result natively, since qemu on the Mac emulates x86_64 fine:

```
brew install qemu
scripts/qemu-boot-test.sh out/stick.img
```

</details>

## Out of scope

A shared FAT32/exFAT partition readable from Windows, Secure Boot signing, and architectures other
than amd64.

## License

[GNU General Public License v3.0 or later](LICENSE). The Debian system portlin installs onto the
stick carries its own licenses, unaffected by this one.
