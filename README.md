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

Boot the result anywhere and a first-run wizard sets it up: account, keyboard, language, time zone,
Wi-Fi, security, appearance, drivers, services, and growing the system to fill the drive.

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

The Xfce desktop is dark out of the box: Numix across GTK, window decorations, the LightDM greeter
and the terminal, with Papirus-Dark icons over it. First boot offers three widget themes and three
icon sets, all of them installed in the image, because first boot has no network. (Two further icon
sets, elementary-xfce and Numix, live in the Software app instead -- 138 MB is a lot to carry for a
menu entry.) The defaults live in `/etc/xdg/xdg-portlin`, which the session adds to `XDG_CONFIG_DIRS`,
so Settings > Appearance still changes them and the change sticks -- including a change made by the
wizard, since everything that names a theme is a conffile. They sit in a directory of their own
because dpkg lets only one installed package own a path, and Xfce's own packages already own the
canonical `/etc/xdg` locations.

The wallpaper draws the stick's own partition table, with root crimson and labelled `LUKS2` only
when root really is encrypted; on a plain stick it is white and labelled `ext4`. Which one shows
is decided on every boot, before the login screen, because a stick can be encrypted at any boot
after it was written.

One panel, along the top, with a searchable applications menu under the portlin mark. portlin's own
tools (Software, Drivers, Migrate, Caffeine and Portlin Settings) share a Portlin submenu beside Settings. At the right
end sits a readout of what the machine is doing:

```
cpu 14%  mem 3.1G/15.5G  gpu 22%  disk 6.1G/6.5G of 119.2G  ip 192.168.1.42  bat 87%
```

It is one program rather than a row of plugins, so it has one font and one spacing rule, and so it
can report things no stock plugin does. `disk` is the stick's own story: two capacities mean the
image has not been expanded into the drive yet, and the tooltip says so and names `portlin-expand`.
`luks` appears in the disk field on an encrypted stick, in crimson, which is the only crimson on
the desktop. Clicking anything in the line opens About Portlin, which names the machine the stick
is plugged into today: model, CPU, memory, graphics and whether it booted UEFI or BIOS.

The readout would rather say nothing than guess. `cpu --%` is the first couple of seconds of a
session, before there are two samples to compare; a machine with no battery has no `bat` field
rather than an empty one. Intel graphics expose a clock and no utilisation counter, so on those
machines the field reads `gpu 350MHz` and the tooltip says in words that it is a frequency.

A coffee cup sits in the panel: click it and the machine stops sleeping, blanking and dimming until
you click it again, or for a span you pick from `Activate for`. It holds a logind lock over
`idle:sleep:handle-lid-switch`, so a closed lid does not suspend either. Right-click for
preferences; untick it in Settings > Session and Startup to stop it appearing at all.

### Lite session

For older machines, first boot's Appearance step offers **Lite (labwc)** in place of Xfce. It is
labwc, a small Wayland compositor, with waybar along the top carrying the same readout, fuzzel as
the applications menu (also on Super+Space and Alt+F2), mako for notifications, and swayidle with
swaylock for the screen lock the wizard set. Right-click the desktop for everything else. The same
applications run in it, Thunar and the terminal included, in the same theme and icons.

The coffee cup is there too, beside the readout: click to toggle, right-click for `Activate for`
and the preferences. It takes the same logind lock as the Xfce one and shares its settings, and
since swayidle is the only thing that blanks or locks the screen under labwc, it keeps the screen
on by stopping swayidle until it is turned off.

What it leaves out: Xfce's settings app and desktop icons. The image carries
both desktops, since first boot has no network to fetch either with, and setup removes the one
not picked, along with whatever only it needed. Thunar, the terminal and the other applications
stay either way, because both sessions run them. The packages are the `lite` group, which
`--groups` can leave out; first boot then has nothing to choose between and keeps Xfce.

## Software

**Software**, in the Portlin menu, installs the things people go looking for on a fresh
system: Chrome, Brave, Chromium, Tor Browser, Pale Moon, Signal, Telegram, Discord, VLC, OBS,
LibreOffice, GIMP, VS Code, Zed, Cursor, Claude Desktop, Claude Code, Kimi Code, Docker,
RustDesk, AnyDesk, Mullvad, Tailscale, qBittorrent, Deluge and more. Each entry says where it
comes from, because they are not all the same kind of thing: Debian's archive, the vendor's apt
repository, a `.deb` the vendor publishes, a tarball unpacked into `/opt`, or an installer that
runs as you, under your own home directory.

Drivers are not in it: they have their own app, below, and searching Software for one points
there.

Everything privileged goes through one command, `portlin-install`, which the window runs under
`pkexec`, or under `sudo` if first boot was told sudo needs no password. So the program that can
be asked to act as root is one command with a fixed set of verbs rather than a window, and the
same verbs work from a terminal:

```
portlin-install list                 # the catalog, and what is already installed
portlin-install install mullvad      # or remove, or status
portlin-install upgrade              # what Update everything runs
```

**Update everything** in the window's header is that last one: `apt full-upgrade` with its
output in the log pane, for the system and everything installed from here alike. A full upgrade
is occasionally allowed to remove a package to resolve a transition, so it asks apt what it
would do first and stops with the list in front of you rather than removing anything from a
button press.

Entries that need Debian's `non-free` component get it through a drop-in under
`/etc/apt/sources.list.d/`, so sticks written before that component was enabled by default are
not left out. Deleting that file takes it away again.

## Drivers

**Drivers**, in the Portlin menu, looks at the machine the stick is plugged into, for after
setup: the stick moved to a new machine, setup ran with no network, or a driver was skipped then.
It names the machine, its graphics and its wifi hardware, and offers what fits, with the same
explanations first boot gives: NVIDIA's proprietary driver, picked for that exact card by
`nvidia-detect`, video acceleration and Vulkan for Intel and AMD, the Broadcom STA driver for the
chips the open ones do not cover, TLP for ThinkPads (power tuning and battery charge limits,
suggested when the firmware or the `thinkpad_acpi` driver says the machine is one), and printing
and scanning. TLP's USB autosuspend stays off, because the system itself runs from a USB drive. It marks which are already installed, and installs
or removes them with live progress and apt's output a click away. A stick travels, so the NVIDIA
entry says plainly what installing it does to the next machine, and how to undo it from a text
console. When a driver builds a kernel module, as NVIDIA's and Broadcom's do, it says a restart
is needed and offers one.

Installing needs a network, and the window says so rather than starting a download that cannot
finish: the Install buttons wait, and come back by themselves once NetworkManager reports a
connection. Removing works offline. It is another reader of `portlin-install`, under `pkexec` like
Software, so it knows nothing about drivers the command line does not:

```
portlin-install scan --json                      # what the window shows about this machine
portlin-install list --json --category Drivers   # every driver, and whether it is installed
portlin-install install nvidia-driver            # what its Install buttons run
```

## Migrating

Plug your old portlin into a machine booted from a new one, and **Migrate** in the
Portlin menu brings across what you tick: the account with its password, the
home directory folder by folder (`Documents` but not `Downloads`, Firefox but not
`.ssh`), what Software installed, saved wifi passwords, Bluetooth pairings,
printers and the desktop theme. First boot offers the same thing before it asks
for an account, so a new stick can start out as the old one. The same verbs work
from a terminal:

```
sudo portlin-migrate restore              # pick a source and tick what to bring
sudo portlin-migrate export /media/me/Backups   # this stick, everything, to an archive
```

The old drive is opened read-only and never written to. Files already on the new
stick with the same names are moved aside into `~/.portlin-migrate-backup/<date>`
rather than deleted, and a directory that exists on both sides is merged. Software
is brought back by running the installer again rather than by copying files, so it
needs network and fits the machine the stick is in now. An archive holds the
password hash and every saved wifi password, so `export` writes it `0600` and
says so.

A copy from a drive can be stopped with **Pause + Unmount**, which finishes the
file under way, unmounts the old drive and locks it again, so it is safe to
unplug. **Resume**, there or the next time Migrate opens, runs the same plan again:
rsync skips everything already copied, and the drive is found by its UUID even if
it comes back under another device name. A copy that fails part way, such as on a
drive with bad sectors, keeps its plan the same way and offers **Try again**. The
Details pane shows each file rsync writes.

To upgrade without going through setup, choose **Upgrade from an old portlin** on
first boot's welcome screen. Setup asks for the keyboard layout, grows the system
to fill the drive, and opens Migrate on its own in a bare X session. Bring the
account over with whatever else you want. When the copy finishes, setup applies
the old drive's sudo and automatic-login answers, rebuilds the boot files and
starts the desktop. Closing Migrate before then offers to open it again (a paused
copy resumes) or to set up normally.

## Updates

The Debian system updates itself: it is a real install, so `apt full-upgrade`
and kernel upgrades work.

Portlin's own contribution to the stick is split in two. The desktop theme,
the wallpapers, the caffeine applet, the Software app and its catalog, the Drivers app, the About Portlin menu
entry and the `portlin-info`, `portlin-expand`, `portlin-encrypt` and `portlin-install`
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
make test      # unit tests, no root, no Linux, ~1s
make dryrun    # print the full command plan
make check     # tests plus shellcheck
make harness   # shipped scripts against real loop devices, needs Docker
```

`make image` runs the whole pipeline and shows where it is: a bar per stage, the current package,
and an ETA. On a host that cannot build directly (anything but x86_64 Linux as root, which includes
every Mac) it re-runs itself inside a privileged `linux/amd64` container and renders the same
display from in there, so the same command works everywhere.

One stage cannot be drawn: the container has to install `python3` before anything can draw at all,
and on an emulated host that is among the slowest minutes of a build. Those steps narrate
themselves instead, with the same elapsed-time format the display uses (`  [4s] installing
python3`), and a cold image pull is announced before it starts rather than looking like a stall.
`make image ARGS=--verbose` gives back the full apt output for debugging the bootstrap itself.

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
- **Flash-aware defaults**: `noatime,commit=120`, and zram instead of swap. A swap file on the drive is
  offered during setup but off by default, for a system on a hard disk or SSD rather than flash.

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
`portlin-firstboot.service` runs on tty1 before LightDM. It is a full-screen curses program in
portlin's colours (it repaints the Linux console palette with the brand tokens), with a step list
down the side, Esc to go back to any earlier answer, and F10 to postpone. Every screen arrives with
an answer already chosen, so Enter alone gets through it. It asks for:

| Step | What it sets |
|---|---|
| Welcome | Start setup, or (on a stick with the desktop) **Upgrade from an old portlin**: keyboard, grow the drive, then Migrate alone in place of every step below |
| Restore | Only shown when another portlin is plugged in; hands over to `portlin-migrate` |
| Keyboard, Language | Searchable lists of every XKB layout and UTF-8 locale on the system |
| Time zone | Searchable zone list, then whether the hardware clock keeps UTC or local time (preselected to local when the machine boots Windows, so the stick never shifts a Windows PC's clock) |
| Network | Computer name; the hardware address networks see (random per network, random every time, or the real one); Wi-Fi, joined there and then with a profile kept in `/run` until the summary is accepted |
| Account | Full name, username and password on one form |
| Security | Automatic login, whether sudo asks for a password, screen lock delay and lock on suspend, a new LUKS passphrase (only when someone else chose the current one), and an "if found" message shown on the boot menu and above the passphrase prompt |
| Appearance | Theme, icons, and display scale: automatic picks 100% or 200% at every login for whatever screen the stick is plugged into |
| Hardware | Drivers `portlin-install scan` suggests for this machine, installed during setup when there is a network (or later from Drivers in the Portlin menu); compressed swap size; an optional swap file on the drive (2-32 GB, used after compressed swap fills, with a warning on USB flash) |
| Services | SSH server (off by default, host keys generated on first enable) and the ufw firewall (on by default, letting SSH through rate-limited when it is on) |
| Storage | Growing the system to fill the drive, and the storage-wear switches `portlin-wear` owns |

Nothing is written until the summary screen is accepted, apart from the keyboard layout, which
takes effect straight away so everything after it is typed on the right keys. The summary lists
every answer by section; choosing a line jumps to that step and straight back. Applying shows each
task as it runs. Optional tasks (drivers, firewall, scale and so on) that fail are reported at the
end without stopping setup; anything that would leave the stick without a working account stops it.

If the wizard is cancelled or crashes its sentinel stays in place and it runs again next boot,
rather than stranding you at a login screen with no accounts. On a stick encrypted during that boot
but never finished, the initramfs recognises the situation and asks for the passphrase itself.
Otherwise no `crypttab` would exist yet, nothing would unlock the root, and cancelling a wizard
would leave an unbootable drive.

The first time each account logs in, `portlin-intro` plays a short full-screen film, drawn live: the
mark draws itself, then its bars take this stick's real partition sizes beside the rows `lsblk`
would print for them, with any space `portlin-expand` could still claim drawn in outline. Root is
crimson only if it is encrypted. "Welcome" flies past in every language the installed fonts can
draw, the last word to arrive is in the language that account chose, and the machine the stick is
plugged into types itself in underneath. Any key or click skips it. It runs once per account
(a stamp in `~/.local/state/portlin`), stays quiet when animations are turned off in Appearance,
waits while the open nouveau driver runs the screen (it plays at the first login on the NVIDIA
driver instead), and replays any time by running `portlin-intro` by hand.

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

`make harness` earns its keep. It runs the *shipped* scripts and packages against real block
devices, a real dpkg and a real X server, in about three minutes. Seven harnesses, ten runs:

| Harness | What only a real device shows |
|---|---|
| `test-encrypt-hook.py` | the initramfs encryption script end to end: prompts, fsck, shrink, encrypt, unlock, mount, and a canary file proving the data survived |
| `test-expand.py` | the expansion path against a real mounted filesystem. Four runs: the wizard's `apply_expand` and the packaged `portlin-expand` are separate implementations that can drift, each tested encrypted and not |
| `test-stash-passphrase.py` | the crypttab keyscript, whose stdout *is* key material, and that the passphrase reaches the wizard without ever becoming a file on the stick |
| `test-package-conflicts.py` | portlin's packages installing where something else already owns `/etc/xdg` |
| `test-package-upgrade.py` | conffiles surviving an upgrade, against a real dpkg |
| `test-caffeine.py` | the applet actually moving the screen settings, against a real X server |
| `test-software.py` | `portlin-install` installing and removing real packages from Debian's archive and from a vendor repository, every package name in the catalog resolving, both privilege refusals, and the Software window against a real X server |

Between them they caught a malformed `partx` argument, `lsblk -o PKNAME` returning empty for
device-mapper volumes, an assumption that udev had created `/dev/mapper` symlinks, a filesystem
left larger than its container, and `cryptsetup resize` silently prompting for a passphrase with no
terminal. Every one had already reached a USB stick, because the only test covering them was a
fifteen-minute emulated boot.

That last one is now fixed rather than merely survived: the volume key is unreachable once the
initramfs has exited, so whatever saw the passphrase there leaves it in `/run` for the wizard to
finish the expansion with, and the wizard deletes it on first use. On an ordinary boot that is a
crypttab keyscript; on the boot that creates the container it is the encryption hook itself, which
runs before any crypttab exists to name a keyscript. The wizard also gives every command it runs a
pipe on stdin rather than the tty1 it was handed, so cryptsetup cannot take the console and ask for
the passphrase over the top of the dialogs.

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
