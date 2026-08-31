# Updating a deployed stick

Portlin writes a real Debian install, so `apt full-upgrade` already maintains the
operating system. Nothing maintains portlin's own contribution to the stick. The
wizard, the initramfs scripts, the desktop theme and the branding are written
once by `write` and are then frozen for the life of the drive.

This design adds a second, narrow update channel: a signed apt repository, hosted
on GitHub Pages, serving packages that carry the parts of portlin that are safe
to change on a drive already in someone's pocket.

## The tier rule

Every file portlin puts on a stick belongs to exactly one tier. This is the
governing rule of the whole design, and new features are expected to declare
their tier before they are written.

| | Frozen | Updatable |
|---|---|---|
| Written by | `write`, once | `portlin-runtime` and `portlin-desktop`, by apt |
| Holds | partition layout, `fstab`, `crypttab`, `/etc/default/grub`, the initramfs scripts, the bootloader, the first-boot wizard, the encryption finaliser | desktop theme, wallpaper, branding, the `portlin-*` commands |
| Failure mode | a stick that will not boot or will not unlock | a desktop that looks wrong, or a command that refuses to run |

**The test.** If a broken version of a file can stop a stick booting or
unlocking, it is frozen. Otherwise it is updatable. When the answer is unclear,
it is frozen.

Two consequences follow, and both are deliberate rather than accidental.

**The split causes duplication.** The wizard keeps its own `apply_expand()` even
once `portlin-expand` ships, because the wizard runs at first boot, when the
installed package is whatever `write` put there and no update has been possible
yet. The two implementations will drift. The harness tests both, and that is the
accepted price of the boundary.

**An updatable feature may depend on a frozen one.** When it does, it must detect
the prerequisite at runtime and refuse cleanly when it is absent, because the
frozen half of a stick written last year cannot be brought forward. `portlin-encrypt`
is the worked example, below.

## Packages

Three, all `Architecture: all`. Nothing portlin ships to a stick is compiled, so
one build serves every suite that portlin can target.

### `portlin-archive-keyring`

The trust root, kept separate from everything else so that the signing key can be
rotated later without stranding drives.

- `/usr/share/keyrings/portlin-archive-keyring.gpg`
- `/etc/apt/sources.list.d/portlin.sources`, deb822 format

```
Types: deb
URIs: https://sleep.github.io/Portlin/apt
Suites: portlin
Components: main
Architectures: all
Signed-By: /usr/share/keyrings/portlin-archive-keyring.gpg
```

`Suites: portlin` is one suite-neutral pocket rather than a pocket per Debian
release, which is sound because the packages are architecture-independent and
depend only on packages present in both bookworm and trixie.

`Architectures: all` is load-bearing rather than decorative. Without it, apt on
an amd64 system requests `binary-amd64/Packages`, which this archive will never
publish, and reports a fetch failure on every `apt update`.

### `portlin-runtime`

Depends on `portlin-archive-keyring`, `python3`, `cloud-guest-utils`,
`cryptsetup-bin`. Recommends `portlin-desktop`.

A recommendation rather than a dependency because of `--minimal`, which produces
a stick with no desktop at all. The three commands are useful there, and neither
the theme nor 11.9 MB of wallpaper is, so `write` installs `portlin-desktop` only
when the desktop package group is present.

- `/usr/bin/portlin-info`
- `/usr/bin/portlin-expand`
- `/usr/bin/portlin-encrypt`
- `/usr/lib/portlin/devices.py`
- `/usr/share/portlin/logo.svg`

### `portlin-desktop`

The Xfce theme conffiles, plus six renders of the wallpaper at 16:9, roughly
11.9 MB in total.

- `/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/{xsettings,xfwm4,xfce4-desktop}.xml`
- `/etc/xdg/gtk-3.0/settings.ini`, `/etc/xdg/gtk-4.0/settings.ini`
- `/etc/xdg/xfce4/terminal/terminalrc`
- `/etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf`

The files under `/etc` become dpkg conffiles, and that is the correct semantics
rather than a problem to engineer around. xfconf writes a user's own settings to
`~/.config/xfce4`, never to `/etc/xdg`, so these files are only ever modified by
someone who edited them deliberately. Preserving such an edit across an upgrade
is the desired behaviour, and the conffile prompt that accompanies it is the
honest signal that portlin's default and the local file have diverged.

| Scale of the authoring canvas | Output |
|---|---|
| 0.711x | 1365x768 |
| 1x | 1920x1080 |
| 4/3 | 2560x1440 |
| 2x | 3840x2160 |
| 8/3 | 5120x2880 |
| 4x | 7680x4320 |

Installed to `/usr/share/backgrounds/portlin/`.

Separate from `portlin-runtime` because the two change on completely different
schedules. A one-line fix to `portlin-info` should cost a stick a 30 KB download,
not 11.9 MB of unchanged PNGs.

Pre-rendering six sizes rather than scaling one is justified by this particular
composition: the background carries a one-pixel grid at 48 pixel spacing and
hard-edged partition bars, and both are exactly the features that resampling
destroys.

`wallpaper.html` is authored as a fixed 1920x1080 canvas with every element at an
absolute pixel offset, so all six renders are the same canvas rasterised at a
different device scale factor. Producing genuine 16:10 or ultrawide compositions
would require making that layout responsive, which is a redesign of the artwork
rather than a change to the render script. Out of scope here. Those displays get
a 16:9 render crop-fitted by xfdesktop's zoomed mode, which suits a composition
that already reserves its top-left quadrant for desktop icons.

## Reaching the stick

`_install_firstboot` in `install.py` becomes `_install_runtime`, and grows a
second responsibility: building the packages and installing them.

The packages are built inside the chroot at write time, from files in
`portlin/resources/runtime/`, rather than being committed to the repository as
binaries or fetched over the network:

1. assemble the package tree into a staging directory in the chroot
2. generate `DEBIAN/control`
3. `dpkg-deb --build`
4. `apt-get install ./portlin-*.deb`, which resolves the inter-package
   dependencies without a network
5. remove the staging directory

This keeps `write` offline, keeps binaries out of git history, and guarantees a
stick can never receive a package older than the checkout that wrote it. It also
preserves the property the rest of `write` already has: every step is a command
through `Runner`, so the whole sequence stays assertable as ordered data by unit
tests on a machine with no root and no Linux.

### Versioning

Packages built locally by `write` are versioned `<version>~local`. A tilde sorts
below the empty string in Debian version comparison, so `0.1.0~local` is strictly
older than the published `0.1.0`, and the first `apt upgrade` on a booted stick
replaces the locally built package with the signed one from the archive.

This is deliberate in both directions. An unsigned build from a working tree can
never present itself as a release, and a stick written from a modified checkout
converges on the published content rather than silently diverging from it.

### A prerequisite fix

`/etc/portlin-release` is currently stamped during `build` from a `VERSION`
constant in `rootfs.py`, which is a second hardcoded copy of `__version__` in
`__init__.py`. Because `build` and `write` are deliberately separated by design,
a stick written from a month-old cached tarball reports the tarball's version
rather than what is installed on it.

An update mechanism needs a version it can trust. The duplicate constant is
removed in favour of the single source in `__init__.py`, and the file is stamped
at write time.

## Tools

### `portlin-info`

Read-only. Reports what the stick is: portlin version, Debian suite, encrypted or
not, filesystem size against drive capacity, whether expansion is still
available, and which machine it is currently booted on. Sources are
`/etc/portlin-release`, `findmnt`, `lsblk` and `cryptsetup status`.

It carries no risk at all, and it makes the `/etc/portlin-release` breadcrumb
useful rather than decorative. It is also the natural place to report that an
update is available.

### `portlin-expand`

Grows the system into the rest of the drive, for a user who declined the offer at
first boot. The three operations run in the order the layout forces, because each
layer can only grow into space the layer beneath it has already claimed:
`growpart`, then `cryptsetup resize` where the stick is encrypted, then
`resize2fs`.

A deliberate second implementation of the wizard's `apply_expand()`, per the tier
rule.

### `portlin-encrypt`

Encrypts a stick that was written without `--encrypt`.

The tool itself does none of the encryption. All of that work, the fsck, the
shrink, the in-place `cryptsetup reencrypt` and the unlock, already lives in
`etc/initramfs-tools/scripts/local-top/portlin-encrypt`, which is frozen tier and
covered by `scripts/test-encrypt-hook.py` against real block devices. That script
gates itself on `portlin.encrypt=ask` appearing in `/proc/cmdline`.

So the packaged tool is a doorbell. It confirms the drive is not already
encrypted, warns, sets the flag, runs `update-grub`, and offers to reboot. The
dangerous machinery stays frozen and stays tested.

**The frozen prerequisite.** The initramfs can create and unlock the container,
but it cannot make the system boot that way again: `crypttab` lives on the root
filesystem, the initramfs must be rebuilt to contain the unlock logic, and the
kernel command line still carries the offer flag. Those three are userspace jobs,
and today they are done by `finalise_encryption()` inside the first-boot wizard.

The wizard disables itself once setup completes. On a stick that has finished
setup, arming encryption would therefore encrypt the drive with nothing left to
finalise it.

`finalise_encryption()` is consequently extracted from the wizard into
`portlin-finalise-encryption.service`, a oneshot unit installed by `write`, in
the frozen tier, ordered before the display manager. The logic is already
idempotent and already keys off observable state rather than a flag, testing
whether the root is a mapper device with no matching `crypttab` entry, so running
it on every boot is both safe and an improvement: it repairs a stick whose wizard
crashed after the initramfs had encrypted the drive, which today leaves the drive
stranded.

Because the finaliser is frozen, it exists only on sticks written after this
change. `portlin-encrypt` therefore checks for the unit and refuses with an
explanatory message when it is missing, rather than arming an encryption that
nothing on that stick can complete. This is the tier rule's second consequence in
practice.

## Publishing

No CI exists in the repository today, so both workflows are new.

**On pull requests**: `make test`, plus `shellcheck` over `scripts/`.

**On tags matching `v*`**: build the three packages in a `debian:trixie`
container. CI and `write` share one implementation, exposed as a
`python -m portlin package` subcommand, so that a published package and a
locally built one can never be assembled by two drifting code paths. Then lay out
`pool/main/p/` and `dists/portlin/main/binary-all/`, generate `Packages` and
`Release` with `apt-ftparchive`, sign `InRelease` and `Release.gpg`, and publish
to the `apt/` directory of the `gh-pages` branch.

Two secrets: the armoured private signing key and its passphrase.

### The cost, stated plainly

This design places a long-lived GPG private key in GitHub Actions secrets, and
every stick portlin has ever written trusts it. That is the real commitment
being made, and it is larger than the packaging work.

Two things make it survivable. The keyring is a separate package from the first
release, so a rotation can be delivered through the archive itself. And the
archive can only ever install the updatable tier, so a compromised key reaches
the desktop theme and three commands, and cannot reach the bootloader, the
initramfs or `crypttab`.

Rotation on a schedule is explicitly not built now. The separate keyring package
is what keeps it possible later.

## Testing

Following the existing three tiers.

**Unit.** Package tree assembly is a pure mapping from path to content, tested
directly. The extended `write` sequence is replayed through the recording
`Runner` and asserted as an ordered command list, which is where an ordering
error such as installing a package before its dependency is available would show
up.

**Harness** (`make harness`, real devices in a container). Build all three
packages, install them into a chroot, and assert file placement. Then install a
second version over the first and assert that no conffile prompt appears, which
is the failure most likely to reach a user, since it surfaces in the middle of an
unrelated `apt full-upgrade`.

**Structural** (`scripts/verify-image.sh`). Assert on a finished image that the
sources entry and keyring are present, that `dpkg -l portlin-runtime` reports it
installed, that the three commands exist and are executable, and that
`portlin-finalise-encryption.service` is enabled.

## Sequencing

Three landable steps. Each is useful on its own, and nothing written at an
earlier step is stranded by a later one.

1. **Version stamping and the finaliser.** Remove the duplicate `VERSION`
   constant and stamp `/etc/portlin-release` at write time. Extract
   `finalise_encryption()` into a frozen oneshot unit. Both are improvements to
   the current product independent of anything below them.
2. **The packages.** Built and installed locally by `write`, with the sources
   entry and keyring in place. No archive yet, so nothing updates, but the
   structure is on the stick.
3. **The archive.** CI, signing, publication.

Sticks written after step 2 carry the sources entry and the keyring, so they
begin receiving updates the moment step 3 publishes, with no action required
from whoever is holding them.

## Out of scope

Updating the frozen tier. A stick's bootloader, initramfs and `crypttab` remain
fixed at the moment it was written, and the way to move them forward is to write
the stick again. A host-side `portlin refresh` that rewrites the frozen tier on
an existing stick is a coherent idea and a separate design.

A responsive wallpaper composition, and therefore native 16:10 and ultrawide
renders.

Automatic updates. The packages are ordinary apt packages and arrive with
whatever `apt full-upgrade` the user already runs.
