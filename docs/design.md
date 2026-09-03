# Portlin design

Portlin writes a portable, fully installed Debian + Xfce system onto a USB stick
that boots on any x86_64 machine, with optional LUKS2 encryption of the root
filesystem. It is the Linux answer to Rufus' "Windows To Go": not a live image
with an overlay, but a real, upgradable Debian install that happens to live on
removable media.

## Decisions

| Question | Decision |
|---|---|
| What goes on the stick | A real Debian install (debootstrap), not a live ISO with persistence |
| Where Portlin runs | Root CLI on x86_64 Debian/Ubuntu. Dev and unit tests run anywhere, including macOS |
| What LUKS covers | LUKS2 over the root filesystem. `/boot` and the ESP stay plaintext |
| Identity provisioning | Deferred to a first-boot wizard on tty1, mirroring Windows OOBE |

The LUKS passphrase is the one thing that cannot be deferred, since the container
must be formatted before anything is written into it. Portlin prompts for it at
build time, and the first-boot wizard offers to change it so the person holding
the stick can take ownership of the key.

## Command surface

```
portlin doctor                                  # preflight the host
portlin devices                                 # list candidate targets
portlin build --output rootfs.tar.zst           # slow half: debootstrap -> cached tarball
portlin write --target /dev/sdX --rootfs FILE   # fast half: partition, encrypt, install
portlin create --target /dev/sdX                # build + write
```

`build` produces a hardware-agnostic, identity-free rootfs. `write` applies it to
a target. Splitting them means a second stick costs minutes rather than a repeat
debootstrap, and lets the fast half be tested independently of the slow half.

`--target` accepts a block device or an image file path. Image files are
loop-mounted through the same interface, which is what makes development on a
non-Linux host possible.

## Disk layout

GPT, four partitions:

| # | Size | Type | Purpose |
|---|------|------|---------|
| 1 | 1 MiB | `EF02` | BIOS boot partition, holds GRUB `core.img`. Unformatted |
| 2 | 512 MiB | `EF00` | ESP, FAT32, mounted at `/boot/efi` |
| 3 | 1 GiB | `8300` | `/boot`, ext4, plaintext |
| 4 | rest | `8300` | root: ext4, or LUKS2 containing ext4 |

GRUB is installed twice against the same stick: `i386-pc` into the MBR gap for
legacy BIOS, and `x86_64-efi --removable --no-nvram` for UEFI. `--removable`
writes the `EFI/BOOT/BOOTX64.EFI` fallback path that all UEFI firmware probes,
so the stick boots on machines it has never seen. `--no-nvram` stops GRUB from
writing a boot entry into the *build machine's* firmware.

## The portability contract

Nothing host-specific may survive into the image:

- `GRUB_DISABLE_OS_PROBER=true`, so the menu is not polluted with the build
  host's operating systems and os-prober never mounts a stranger's internal disks.
- `MODULES=most` in the initramfs, so every storage and USB controller driver is
  present rather than only the build host's.
- `fstab` and `crypttab` reference UUIDs only, never `/dev/sdaN`, which renumbers
  on every machine.
- `RESUME=none`, so the initramfs does not stall probing for a hibernation image
  belonging to another computer.
- Emptied `/etc/machine-id` and deleted SSH host keys, regenerated on first boot.
- Broad firmware: `firmware-linux`, iwlwifi/realtek/atheros/brcm80211, and *both*
  `intel-microcode` and `amd-microcode`.
- The LUKS argon2id memory cost is capped so a low-RAM machine can still unlock a
  stick formatted on a workstation.
- `noatime,commit=120` and zram instead of swap, because USB flash has finite
  write cycles.

## The update tier rule

Every file portlin puts on a stick belongs to exactly one tier. This is the
governing rule of the whole design, and new features are expected to declare
their tier before they are written.

| | Frozen | Updatable |
|---|---|---|
| Written by | `write`, once | `portlin-runtime` and `portlin-desktop`, by apt |
| Holds | partition layout, `fstab`, `crypttab`, `/etc/default/grub`, the initramfs scripts, the bootloader, the first-boot wizard, the encryption finaliser | desktop theme, icon theme, panel layout, wallpaper, branding, the software catalog and the Software app, the `portlin-*` commands |
| Failure mode | a stick that will not boot or will not unlock | a desktop that looks wrong, or a command that refuses to run |

**The test.** If a broken version of a file can stop a stick booting or
unlocking, it is frozen. Otherwise it is updatable. When the answer is unclear,
it is frozen.

Two consequences follow, and both are deliberate rather than accidental.

**The split causes duplication.** The wizard keeps its own `apply_expand()` even
once `portlin-expand` ships, because the wizard runs at first boot, when the
installed package is whatever `write` put there and no update has been possible
yet. The two implementations will drift, and that is the accepted price of the
boundary.

**An updatable feature may depend on a frozen one.** When it does, it must
detect the prerequisite at runtime and refuse cleanly when it is absent,
because the frozen half of a stick written last year cannot be brought
forward.

**Privilege lives in one place.** The Software app runs as the user and never
touches apt. Everything needing root is `portlin-install`, reached through a
polkit action whose `exec.path` annotation names that one program, so what
can be asked to act as root is a single command with a fixed set of verbs
rather than a window. It refuses in both directions: a system package
without root, and a vendor script that installs into a home directory with
it.

## Module decomposition

Each module has one job and a testable surface.

| Module | Responsibility |
|---|---|
| `runner.py` | Single subprocess chokepoint. Records every command, supports dry-run, redacts secret stdin |
| `layout.py` | Pure: target size -> partition plan, sgdisk argv, partition device paths |
| `templates.py` | Pure: renders fstab, crypttab, `/etc/default/grub`, initramfs conf, sources.list |
| `devices.py` | Enumerates block devices from `lsblk -J`, evaluates target safety |
| `target.py` | Uniform interface over a block device and a loop-mounted image file |
| `chroot.py` | Bind-mount lifecycle, `policy-rc.d`, resolv.conf, command execution |
| `crypto.py` | LUKS format/open/close, passphrase handling |
| `rootfs.py` | debootstrap + package installation -> cached tarball |
| `install.py` | Orchestrates `write`: partition, format, unpack, configure, GRUB |
| `packages.py` | The package set, grouped and overridable |
| `resources/runtime/catalog.py` | Pure: the software catalog both shipped programs read |
| `resources/runtime/hostinfo.py` | Pure parsers plus thin readers: what machine the stick is plugged into, and what it is doing |
| `package.py` | Pure: the three runtime packages as a path-to-content mapping |
| `progress.py` | Pure: command output -> progress events, stage weights, bars and ETAs |
| `cli.py` | Argument parsing, confirmation prompts, wiring |

The `Runner` chokepoint is the keystone. Because every external command flows
through it and it can record instead of execute, the entire orchestration is
unit-testable as pure data on a machine with no root, no loop devices, and no
Linux.

## Error handling

`write` builds its teardown as it goes on a `contextlib.ExitStack`, so any failure
unwinds in exact reverse order: unmount the ESP, unmount `/boot`, unmount root,
close the LUKS mapping, detach the loop device, remove the temp mountpoint. A
crashed run must never leave a mounted chroot or a dangling `/dev/mapper` entry.

`portlin doctor` fails fast on a non-x86_64 host, a non-root user, or a missing
binary, so a build dies in a second rather than twenty minutes in.

Destructive operations are gated: non-removable devices are refused without
`--force`, targets under 16 GiB are refused outright, and the confirmation prompt
requires typing the device path rather than pressing `y`.

The first-boot wizard leaves its sentinel file in place if it fails, so a failed
setup retries on the next boot instead of stranding the user with no account.

## Testing

Three tiers, deliberately separated by what they require:

1. **Unit** (`pytest`, runs on macOS, no root): pure layout and template functions,
   lsblk parsing, safety rules, and the full `build`/`write` orchestration replayed
   through a recording `Runner` to assert command ordering.
2. **Integration** (`scripts/integration-test.sh`, linux/amd64 container, privileged):
   real sgdisk, real LUKS, real loop devices against a sparse image.
3. **Boot** (`scripts/qemu-boot-test.sh`): boots the produced image under
   `qemu-system-x86_64` in both legacy BIOS and UEFI/OVMF modes and asserts the
   first-boot wizard is reached. This is the acceptance test for "boots anywhere".

## Out of scope

A shared FAT32/exFAT partition readable from Windows, secure boot signing, and
non-amd64 targets.
