# Portlin runtime packages implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move portlin's desktop theming, branding and command-line tools out of
the frozen, write-once tier and into three Debian packages that `write` builds
and installs locally, so that a later signed archive can update them on a stick
already in use.

**Architecture:** Three `Architecture: all` packages (`portlin-archive-keyring`,
`portlin-runtime`, `portlin-desktop`) are assembled from files under
`portlin/resources/` by a new pure module, built inside the chroot with
`dpkg-deb --build` at write time, and installed with `apt-get install` against
local files. Everything boot-critical stays exactly where it is. One frozen-tier
change is a prerequisite: the encryption finaliser is extracted out of the
first-boot wizard so it runs on every boot.

**Tech Stack:** Python 3.11+ stdlib only, pytest 8.3.4, dpkg-deb, apt-get,
debhelper-free hand-assembled packages, Chrome headless for wallpaper rendering.

**Spec:** `docs/superpowers/specs/2026-08-31-portlin-runtime-updates-design.md`

## Global Constraints

- Python 3.11+, standard library only. `pyproject.toml` declares
  `dependencies = []` and that must stay true.
- Every external command goes through `Runner`, so that `write` remains
  replayable as an ordered command list by unit tests on macOS with no root.
- Unit tests must keep running with no root, no Linux and no block devices.
  `make test` currently passes 292 tests in about one second.
- Never use en-dash or em-dash characters in any file, including comments,
  documentation and commit messages. Use a plain hyphen, comma, colon or
  parentheses.
- Commit messages follow Conventional Commits: `<type>(<scope>): <description>`.
- No AI or assistant attribution trailers in commit messages.
- Comments explain the code, not the history of how it was written.
- The tier rule governs every file placement decision: if a broken version of a
  file can stop a stick booting or unlocking, it is frozen tier and belongs in
  `write`, not in a package.
- Package versions built locally carry the `~local` suffix so they sort below
  any published release of the same version.

---

### Task 1: Single-source the portlin version and stamp it at write time

`/etc/portlin-release` is currently written during `build` from a `VERSION`
constant in `rootfs.py`, which duplicates `__version__` in `__init__.py`.
Because `build` and `write` are deliberately separable, a stick written from an
old cached tarball reports the tarball's version rather than what is installed.
Every later task depends on that stamp being trustworthy.

**Files:**
- Modify: `portlin/rootfs.py:30` (remove `VERSION`), `portlin/rootfs.py:115`
- Modify: `portlin/install.py` (add the stamp to `_write_target_config`)
- Test: `tests/test_rootfs.py`, `tests/test_install.py`

**Interfaces:**
- Consumes: `portlin.__version__`
- Produces: `/etc/portlin-release` on the target containing
  `PORTLIN_VERSION=<portlin.__version__>`, written during `write`. Task 9 reads
  the same `__version__` for package versions.

- [ ] **Step 1: Write the failing tests**

In `tests/test_rootfs.py`, add to the existing build-stage test class:

```python
def test_does_not_stamp_the_version_at_build_time(self):
    # The tarball is reusable for months, so a version stamped into it would
    # describe the tarball rather than the stick written from it.
    assert not self.t.has("write-file", "etc/portlin-release")
```

In `tests/test_install.py`, inside `class TestUnencryptedWrite`:

```python
def test_stamps_the_portlin_version_onto_the_target(self):
    assert self.t.has("write-file", "etc/portlin-release")

def test_stamps_the_version_before_entering_the_chroot(self):
    assert self.t.before(
        ("write-file", "etc/portlin-release"), ("chroot", "update-initramfs")
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rootfs.py tests/test_install.py -v -k "portlin_release or version"`

Expected: the `test_install.py` cases FAIL with `no command matched
('write-file', 'etc/portlin-release')`. The `test_rootfs.py` case FAILS because
the build stage currently does write it.

- [ ] **Step 3: Remove the duplicate constant**

In `portlin/rootfs.py`, delete the `VERSION = "0.1.0"` line at line 30, and
delete this line from `_configure_system`:

```python
    chroot.write_file("etc/portlin-release", templates.render_os_release_extra(VERSION))
```

- [ ] **Step 4: Stamp it during write instead**

In `portlin/install.py`, add the import at the top of the file:

```python
from . import __version__
```

Then in `_write_target_config`, after the `/etc/default/grub` write at line 361,
add:

```python
    # Stamped here rather than during build: the rootfs tarball is reusable for
    # months, so a version baked into it would describe the tarball rather than
    # the stick, and the update channel needs a version it can trust.
    runner.write_file(
        mountpoint / "etc/portlin-release",
        templates.render_os_release_extra(__version__),
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rootfs.py tests/test_install.py -v`

Expected: PASS.

- [ ] **Step 6: Run the whole suite**

Run: `make test`

Expected: all tests pass. Any other test asserting on `rootfs.VERSION` needs
updating to `portlin.__version__`.

- [ ] **Step 7: Commit**

```bash
git add portlin/rootfs.py portlin/install.py tests/test_rootfs.py tests/test_install.py
git commit -m "fix: stamp the portlin version at write time from a single source

The version was stamped into the rootfs during build from a constant that
duplicated __version__, so a stick written from a cached tarball reported
the tarball's version rather than its own."
```

---

### Task 2: Extract the encryption finaliser into its own frozen script

`finalise_encryption()` currently lives inside the first-boot wizard, and the
wizard disables itself once setup completes. Any later encryption therefore has
nothing to make it permanent. This is the frozen-tier prerequisite for
`portlin-encrypt` in Task 6, and it independently repairs a stick whose wizard
crashed after the initramfs had already encrypted the drive.

**Files:**
- Create: `portlin/resources/firstboot/portlin-finalise-encryption`
- Create: `portlin/resources/firstboot/portlin-finalise-encryption.service`
- Modify: `portlin/resources/firstboot/portlin-firstboot` (`finalise_encryption`)
- Modify: `portlin/install.py:388` (`_install_firstboot`)
- Modify: `portlin/rootfs.py:25-28` (path constants)
- Test: `tests/test_install.py`, `tests/test_firstboot.py`

**Interfaces:**
- Produces: `/usr/local/sbin/portlin-finalise-encryption`, exit code 10 when it
  finalised an encryption and 0 when there was nothing to do. Touches
  `/run/portlin/finalised` when it acts. Task 6's `portlin-encrypt` tests for
  the presence of this file to decide whether it may safely arm encryption.

- [ ] **Step 1: Write the failing tests**

In `tests/test_install.py`, inside `class TestUnencryptedWrite`:

```python
def test_installs_the_encryption_finaliser(self):
    assert self.t.has("write-file", "usr/local/sbin/portlin-finalise-encryption")

def test_enables_the_encryption_finaliser(self):
    assert self.t.has_tokens("systemctl", "enable", "portlin-finalise-encryption.service")

def test_installs_the_finaliser_on_unencrypted_sticks_too(self):
    # An unencrypted stick is exactly the one that may be encrypted later, so
    # it is the stick that needs the finaliser most.
    assert self.t.has("write-file", "usr/local/sbin/portlin-finalise-encryption")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_install.py -v -k finalise`

Expected: FAIL with `no command matched ('write-file',
'usr/local/sbin/portlin-finalise-encryption')`.

- [ ] **Step 3: Create the finaliser script**

Create `portlin/resources/firstboot/portlin-finalise-encryption`. Move the body
of `finalise_encryption()` from the wizard, along with the `_backing_partition`
and `_sysfs_node` helpers it depends on, and a minimal `log`/`run` pair:

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Make an encryption performed by the initramfs permanent.

The initramfs can create the container and unlock it for one boot, but it
cannot make the system boot that way again: crypttab lives on the root
filesystem, the initramfs has to be rebuilt to contain the unlock logic, and
the kernel command line still carries the flag that offers encryption. All
three are userspace jobs.

Runs on every boot rather than only during first-boot setup, because a stick
can be encrypted long after the wizard has disabled itself. Keying off
observable state rather than a flag is what makes that safe: it asks whether
the root is a mapper device with no crypttab entry, so a repeat run is a no-op
and a run after a crashed setup is a repair.

Exits 10 when it finalised an encryption, 0 when there was nothing to do.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LOG = Path("/var/log/portlin-firstboot.log")
BREADCRUMB = Path("/run/portlin/finalised")
FINALISED = 10


def log(message: str) -> None:
    try:
        with LOG.open("a") as handle:
            handle.write(f"{message}\n")
    except OSError:
        pass


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    log(f"run: {' '.join(argv)}")
    proc = subprocess.run(argv, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(argv)}")
    return proc


def _sysfs_node(device: str) -> Path | None:
    """Sysfs directory for a device node, located by its device number.

    Resolving /dev/mapper/<name> as a symlink only works where udev created it
    that way. Without udev -- in an initramfs, a container, or a minimal system
    -- cryptsetup makes a real device node there instead, and resolve() returns
    the path unchanged, pointing at a sysfs entry that does not exist. The
    major:minor pair is how the kernel identifies the device either way.
    """
    try:
        status = os.stat(device)
    except OSError:
        return None
    path = Path(f"/sys/dev/block/{os.major(status.st_rdev)}:{os.minor(status.st_rdev)}")
    return path if path.exists() else None


def _backing_partition(source: str) -> str:
    """Kernel name of the partition holding ``source``.

    dm devices have no "device" link in sysfs, which is why `lsblk -o PKNAME`
    returns an empty string for them. The supported relationship is
    /sys/block/<dm>/slaves/, listing what the mapping is built on top of.
    """
    node = _sysfs_node(source)
    if node is None:
        return ""
    slaves = node / "slaves"
    if slaves.is_dir():
        entries = sorted(entry.name for entry in slaves.iterdir())
        if entries:
            return entries[0]
    return node.resolve().name


def finalise() -> bool:
    source = subprocess.run(
        ["findmnt", "-no", "SOURCE", "/"], capture_output=True, text=True
    ).stdout.strip()
    if not source.startswith("/dev/mapper/"):
        return False

    crypttab = Path("/etc/crypttab")
    name = Path(source).name
    if crypttab.exists() and name in crypttab.read_text():
        return False

    partition = _backing_partition(source)
    if not partition:
        log("encrypted root has no backing device, cannot finalise")
        return False
    device = f"/dev/{partition}"

    uuid = subprocess.run(
        ["cryptsetup", "luksUUID", device], capture_output=True, text=True
    ).stdout.strip()
    if not uuid:
        log(f"could not read the LUKS UUID of {device}")
        return False

    crypttab.write_text(
        "# Generated by portlin after boot-time encryption.\n"
        f"{name}\tUUID={uuid}\tnone\tluks\n"
    )
    run(["update-initramfs", "-u", "-k", "all"], check=False)

    # Drop the offer flag: this stick has made its choice, and asking again
    # would invite someone to encrypt an already-encrypted drive.
    default_grub = Path("/etc/default/grub")
    if default_grub.exists():
        default_grub.write_text(
            default_grub.read_text().replace(" portlin.encrypt=ask", "")
        )
        run(["update-grub"], check=False)
    return True


def main() -> int:
    try:
        if not finalise():
            return 0
    except Exception as exc:
        log(f"finalise failed: {exc}")
        return 0
    BREADCRUMB.parent.mkdir(parents=True, exist_ok=True)
    BREADCRUMB.touch()
    return FINALISED


if __name__ == "__main__":
    sys.exit(main())
```

Note the two deliberate differences from the wizard's version: it runs
`update-grub` after editing `/etc/default/grub`, which the wizard did not need
because it rebuilt the initramfs for other reasons anyway, and every failure
returns 0 rather than raising, because a boot-time unit must never block the
boot.

- [ ] **Step 4: Create the systemd unit**

Create `portlin/resources/firstboot/portlin-finalise-encryption.service`:

```ini
[Unit]
Description=Make portlin boot-time encryption permanent
Documentation=file:/etc/portlin-release
DefaultDependencies=no
After=local-fs.target
Before=portlin-firstboot.service display-manager.service
ConditionPathExists=/etc/portlin-release

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/portlin-finalise-encryption
SuccessExitStatus=0 10
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
```

`SuccessExitStatus=0 10` keeps the informative exit code without systemd
logging a failure for the ordinary case.

- [ ] **Step 5: Replace the wizard's copy**

In `portlin/resources/firstboot/portlin-firstboot`, replace the whole body of
`finalise_encryption()` with a breadcrumb check, keeping the same return
contract so its one call site is unchanged:

```python
def finalise_encryption() -> bool:
    """Whether the boot-time encryption finaliser acted earlier this boot.

    The work itself is done by portlin-finalise-encryption.service, which is
    ordered before this wizard and also runs on sticks whose wizard has long
    since disabled itself. This only reports it, so the wizard can explain to
    the user what happened.
    """
    return Path("/run/portlin/finalised").exists()
```

Delete `_backing_partition` and `_sysfs_node` from the wizard if nothing else
in it calls them. Check first:

```bash
grep -n "_backing_partition\|_sysfs_node" portlin/resources/firstboot/portlin-firstboot
```

- [ ] **Step 6: Install both files during write**

In `portlin/rootfs.py`, next to the existing constants at lines 25 to 28, add:

```python
FINALISE_SCRIPT = "usr/local/sbin/portlin-finalise-encryption"
FINALISE_UNIT = "etc/systemd/system/portlin-finalise-encryption.service"
```

In `portlin/install.py`, in `_install_firstboot`, extend the import and add the
two writes after the wizard's unit is written:

```python
    from .rootfs import (
        FINALISE_SCRIPT,
        FINALISE_UNIT,
        FIRSTBOOT_SCRIPT,
        FIRSTBOOT_SENTINEL,
        FIRSTBOOT_UNIT,
        RESOURCES,
    )
```

```python
    # Frozen tier, and installed on every stick including unencrypted ones: an
    # unencrypted stick is precisely the one that may be encrypted later, and
    # the finaliser is what makes that encryption survive a reboot.
    chroot.write_file(
        FINALISE_SCRIPT,
        (RESOURCES / "firstboot" / "portlin-finalise-encryption").read_text(),
        mode=0o755,
    )
    chroot.write_file(
        FINALISE_UNIT,
        (RESOURCES / "firstboot" / "portlin-finalise-encryption.service").read_text(),
    )
    chroot.run(["systemctl", "enable", "portlin-finalise-encryption.service"])
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_install.py -v -k finalise`

Expected: PASS.

- [ ] **Step 8: Run the whole suite and the shellcheck pass**

Run: `make check`

Expected: all tests pass, shellcheck clean.

- [ ] **Step 9: Commit**

```bash
git add portlin/resources/firstboot/portlin-finalise-encryption \
        portlin/resources/firstboot/portlin-finalise-encryption.service \
        portlin/resources/firstboot/portlin-firstboot \
        portlin/rootfs.py portlin/install.py tests/test_install.py
git commit -m "feat: finalise boot-time encryption on every boot

The finaliser lived inside the first-boot wizard, which disables itself
once setup completes, so a stick encrypted later had nothing to write its
crypttab or rebuild its initramfs. Extracting it into a oneshot unit also
repairs a stick whose wizard crashed after the initramfs had encrypted it."
```

---

### Task 3: A pure module describing the three packages

The package trees are a mapping from destination path to content. Keeping that
mapping pure and separate from the code that writes it is what lets the whole
thing be asserted by unit tests on a machine with no dpkg.

**Files:**
- Create: `portlin/package.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Produces:
  - `LOCAL_SUFFIX = "~local"`
  - `local_version() -> str` returning `f"{__version__}{LOCAL_SUFFIX}"`
  - `render_control(*, name: str, version: str, description: str, depends: list[str], recommends: list[str] | None = None) -> str`
  - `render_sources_entry() -> str`
  - `PACKAGES: list[str]`, the build order `["portlin-archive-keyring", "portlin-runtime", "portlin-desktop"]`
  - `text_files(package: str) -> dict[str, str]` mapping a path relative to the package root to its content
  - `binary_files(package: str) -> dict[str, Path]` mapping a path relative to the package root to a source file on the host
  - `executable_paths(package: str) -> set[str]`, the subset of `text_files` needing mode 0755

  Task 9 consumes all of these to build the packages in the chroot, and Task 10
  exposes them through a CLI subcommand.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_package.py`:

```python
"""The package descriptions are pure data, so they are asserted directly."""

from __future__ import annotations

import pytest

from portlin import __version__, package


def test_local_builds_are_suffixed_so_they_sort_below_a_release():
    # A tilde sorts below the empty string in Debian version comparison, so
    # 0.1.0~local is older than 0.1.0 and the published build always wins.
    # Not asserted by comparing the strings here: Python orders them the other
    # way round, because the tilde rule is dpkg's, not Python's.
    assert package.local_version() == f"{__version__}~local"
    assert package.local_version().startswith(__version__)


def test_control_names_the_package_and_its_version():
    control = package.render_control(
        name="portlin-runtime",
        version="0.1.0~local",
        description="Portlin desktop integration and tools",
        depends=["python3"],
    )
    assert "Package: portlin-runtime" in control
    assert "Version: 0.1.0~local" in control
    assert "Architecture: all" in control
    assert "Depends: python3" in control
    assert control.endswith("\n")


def test_control_omits_recommends_when_there_are_none():
    control = package.render_control(
        name="portlin-archive-keyring",
        version="0.1.0~local",
        description="Portlin archive signing key",
        depends=[],
    )
    assert "Recommends:" not in control
    assert "Depends:" not in control


def test_runtime_recommends_rather_than_depends_on_wallpapers():
    # A --minimal stick has no desktop, so 14 MB of wallpaper must not be a
    # hard dependency of the tools.
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    assert "Recommends: portlin-desktop" in control
    assert "portlin-desktop" not in control.split("Depends:")[1].split("\n")[0]


def test_sources_entry_pins_the_architecture():
    # Without this apt requests binary-amd64/Packages, which this archive will
    # never publish, and reports a fetch failure on every apt update.
    entry = package.render_sources_entry()
    assert "Architectures: all" in entry
    assert "Signed-By: /usr/share/keyrings/portlin-archive-keyring.gpg" in entry


def test_keyring_package_ships_the_sources_entry():
    files = package.text_files("portlin-archive-keyring")
    assert "etc/apt/sources.list.d/portlin.sources" in files


def test_runtime_ships_the_three_tools_as_executables():
    executables = package.executable_paths("portlin-runtime")
    assert executables == {
        "usr/bin/portlin-info",
        "usr/bin/portlin-expand",
        "usr/bin/portlin-encrypt",
    }


@pytest.mark.parametrize("name", package.PACKAGES)
def test_every_package_has_a_control_file(name):
    assert "DEBIAN/control" in package.text_files(name)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_package.py -v`

Expected: FAIL at import with `ImportError: cannot import name 'package'`.

- [ ] **Step 3: Write the module**

Create `portlin/package.py`:

```python
"""Descriptions of the Debian packages portlin installs onto a stick.

Portlin's contribution to a stick is split in two. Anything whose failure means
a drive that will not boot or will not unlock is written directly by ``write``
and is frozen for the life of that drive. Everything else lives in these
packages, so a signed archive can move it forward later.

Kept pure, as a mapping from destination path to content, so the whole package
layout is assertable by unit tests on a machine with no dpkg and no root. The
code that turns these into .deb files lives in install.py.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

RESOURCES = Path(__file__).parent / "resources"

# A tilde sorts below the empty string in Debian version comparison, so a
# package built here from a working tree is always superseded by the signed
# build of the same version from the archive. An unsigned local build can
# therefore never present itself as a release.
LOCAL_SUFFIX = "~local"

ARCHIVE_URI = "https://sleep.github.io/Portlin/apt"
ARCHIVE_SUITE = "portlin"
KEYRING_PATH = "/usr/share/keyrings/portlin-archive-keyring.gpg"

# Build order: the keyring first, because the others are installed alongside it
# in one apt transaction that has to resolve.
PACKAGES = ["portlin-archive-keyring", "portlin-runtime", "portlin-desktop"]

TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt"]

# Corrected after this plan was executed. The listing below originally put the
# theme defaults at the canonical /etc/xdg locations, and a real build then
# failed at unpack: dpkg permits exactly one installed package to own a path,
# xfce4-settings already ships xsettings.xml there, and the refusal aborted the
# whole apt transaction, taking all three packages down and leaving the stick
# unwritten. A conffiles declaration is no exemption, and --force-confnew
# governs conffile prompts rather than ownership. The defaults therefore live
# in an overlay directory portlin owns, which the session snippet puts on
# XDG_CONFIG_DIRS. scripts/test-package-conflicts.py holds that in place.
XDG_OVERLAY = "etc/xdg/xdg-portlin"

XSESSION_SNIPPET = "etc/X11/Xsession.d/40portlin-desktop_xdg-config-dirs"

# Keyed by path relative to a config root rather than by full destination, so
# these can only ever be written inside XDG_OVERLAY.
XDG_DEFAULTS = {
    "xfce4/xfconf/xfce-perchannel-xml/xsettings.xml": "xsettings.xml",
    "xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml": "xfwm4.xml",
    "xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml": "xfce4-desktop.xml",
    "gtk-3.0/settings.ini": "gtk-3.0-settings.ini",
    "gtk-4.0/settings.ini": "gtk-4.0-settings.ini",
    "xfce4/terminal/terminalrc": "terminalrc",
}

THEME_FILES = {
    **{f"{XDG_OVERLAY}/{relative}": source for relative, source in XDG_DEFAULTS.items()},
    "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf": "50-portlin.conf",
    XSESSION_SNIPPET: "xdg-config-dirs.sh",
}

WALLPAPER_SIZES = [
    "1366x768",
    "1920x1080",
    "2560x1440",
    "3840x2160",
    "5120x2880",
    "7680x4320",
]


def local_version() -> str:
    return f"{__version__}{LOCAL_SUFFIX}"


def render_control(
    *,
    name: str,
    version: str,
    description: str,
    depends: list[str],
    recommends: list[str] | None = None,
) -> str:
    lines = [
        f"Package: {name}",
        f"Version: {version}",
        "Section: utils",
        "Priority: optional",
        "Architecture: all",
        "Maintainer: The portlin authors <portlin@localhost>",
    ]
    if depends:
        lines.append(f"Depends: {', '.join(depends)}")
    if recommends:
        lines.append(f"Recommends: {', '.join(recommends)}")
    lines += [
        f"Description: {description}",
        " Installed by portlin onto the stick it writes. Portlin's own files",
        " are split so that the ones which cannot break a boot are carried by",
        " packages and can be updated from the portlin archive.",
        "",
    ]
    return "\n".join(lines)


def render_sources_entry() -> str:
    """Render the deb822 apt source for the portlin archive.

    Architectures is pinned rather than left to apt. Every portlin package is
    Architecture: all, so the archive publishes only binary-all; without the
    pin, apt on amd64 requests binary-amd64/Packages and reports a fetch
    failure on every apt update.
    """
    return "\n".join(
        [
            "Types: deb",
            f"URIs: {ARCHIVE_URI}",
            f"Suites: {ARCHIVE_SUITE}",
            "Components: main",
            "Architectures: all",
            f"Signed-By: {KEYRING_PATH}",
            "",
        ]
    )


def text_files(package: str) -> dict[str, str]:
    """Text members of a package, keyed by path relative to the package root."""
    version = local_version()
    if package == "portlin-archive-keyring":
        return {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin archive signing key and apt source",
                depends=[],
            ),
            "etc/apt/sources.list.d/portlin.sources": render_sources_entry(),
        }
    if package == "portlin-runtime":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop integration and tools",
                depends=[
                    "portlin-archive-keyring",
                    "python3",
                    "cloud-guest-utils",
                    "cryptsetup-bin",
                ],
                recommends=["portlin-desktop"],
            ),
        }
        for tool in TOOLS:
            files[f"usr/bin/{tool}"] = (RESOURCES / "runtime" / tool).read_text()
        for destination, source in THEME_FILES.items():
            files[destination] = (RESOURCES / "runtime" / "theme" / source).read_text()
        return files
    if package == "portlin-desktop":
        return {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop wallpapers",
                depends=[],
            ),
        }
    raise KeyError(package)


def binary_files(package: str) -> dict[str, Path]:
    """Binary members, keyed by path relative to the package root."""
    if package == "portlin-archive-keyring":
        # A dearmoured public key, so it is bytes rather than text.
        return {
            KEYRING_PATH.lstrip("/"):
                RESOURCES / "keyring" / "portlin-archive-keyring.gpg"
        }
    if package == "portlin-runtime":
        return {"usr/share/portlin/logo.svg": RESOURCES / "runtime" / "logo.svg"}
    if package == "portlin-desktop":
        return {
            f"usr/share/backgrounds/portlin/portlin-{size}.png":
                RESOURCES / "wallpapers" / f"portlin-{size}.png"
            for size in WALLPAPER_SIZES
        }
    raise KeyError(package)


Every branch above falls through to a shared tail that derives the conffiles
member, so it can never drift when files are added or moved:

```python
    # Hand-built packages get no conffiles for free. debhelper's dh_installdeb
    # is what normally registers /etc files, and dpkg-deb does not. Without
    # this member dpkg treats them as ordinary files and overwrites a user's
    # edits silently on every upgrade.
    conffiles = sorted(path for path in files if path.startswith("etc/"))
    if conffiles:
        files["DEBIAN/conffiles"] = "".join(f"/{path}\n" for path in conffiles)
    return files
```

def executable_paths(package: str) -> set[str]:
    if package == "portlin-runtime":
        return {f"usr/bin/{tool}" for tool in TOOLS}
    return set()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_package.py -v`

Expected: the control, version and sources tests PASS. The tests touching
`text_files("portlin-runtime")` FAIL with `FileNotFoundError`, because the tool
and theme resources arrive in Tasks 4 to 8. That is expected at this point.

- [ ] **Step 5: Commit**

```bash
git add portlin/package.py tests/test_package.py
git commit -m "feat(package): describe the three runtime packages as pure data"
```

---

### Task 4: `portlin-info`

The read-only tool. Written first of the three because it has no failure mode
worth worrying about, which makes it the right thing to prove the packaging on.

**Files:**
- Create: `portlin/resources/runtime/portlin-info`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: `/etc/portlin-release` from Task 1.
- Produces: a `/usr/bin/portlin-info` command. No other task depends on it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_package.py`:

```python
def test_info_tool_is_shipped_and_executable():
    files = package.text_files("portlin-runtime")
    assert files["usr/bin/portlin-info"].startswith("#!/usr/bin/env python3")
    assert "usr/bin/portlin-info" in package.executable_paths("portlin-runtime")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_package.py -v -k info`

Expected: FAIL with `FileNotFoundError` for
`portlin/resources/runtime/portlin-info`.

- [ ] **Step 3: Write the tool**

Create `portlin/resources/runtime/portlin-info`:

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Report what this portlin stick is.

Read-only. Everything here comes from asking the running system rather than
from recorded state, because a stick is portable and half of these answers
change with the machine it is plugged into.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/usr/lib/portlin")

from devices import backing_partition, command_output, root_source  # noqa: E402

RELEASE = Path("/etc/portlin-release")
OS_RELEASE = Path("/etc/os-release")

# ext4 metadata -- the superblock, the block and inode group descriptors, the
# inode tables and the journal -- claims roughly 2-3% of a filesystem's
# nominal size, and an encrypted root's partition additionally holds a 16 MiB
# LUKS header the filesystem never sees. 0.3 GB of slack comfortably covers
# both without masking a real unclaimed gigabyte.
PARTITION_SLACK_BYTES = 300_000_000


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=value file, tolerating os-release's quoted values."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def _release() -> dict[str, str]:
    return _parse_env_file(RELEASE)


def _debian_description() -> str:
    return _parse_env_file(OS_RELEASE).get("PRETTY_NAME", "unknown")


def _backing_disk(partition: str) -> str:
    """The whole disk behind ``partition``."""
    return command_output(["lsblk", "-dno", "PKNAME", f"/dev/{partition}"]) or partition


def _unclaimed_bytes(filesystem_bytes: int, partition_bytes: int) -> int:
    """Bytes inside the partition that the filesystem has not claimed.

    Compared against the partition rather than the whole disk: the fixed
    partitions ahead of root are always a gap between the disk and the
    partition, on every stick, expanded or not, and comparing against the
    disk would nag about that gap forever.
    """
    return max(0, partition_bytes - filesystem_bytes - PARTITION_SLACK_BYTES)


def main() -> int:
    release = _release()
    source = root_source()
    encrypted = source.startswith("/dev/mapper/")
    partition = backing_partition(source)
    disk = _backing_disk(partition) if partition else ""

    used = shutil.disk_usage("/")
    filesystem_gb = used.total / 1_000_000_000
    partition_bytes_out = command_output(["lsblk", "-bdno", "SIZE", f"/dev/{partition}"]) if partition else ""
    partition_bytes = int(partition_bytes_out) if partition_bytes_out.isdigit() else 0
    disk_bytes_out = command_output(["lsblk", "-bdno", "SIZE", f"/dev/{disk}"]) if disk else ""
    drive_gb = int(disk_bytes_out) / 1_000_000_000 if disk_bytes_out.isdigit() else 0.0

    print(f"portlin      {release.get('PORTLIN_VERSION', 'unknown')}")
    print(f"debian       {_debian_description()}")
    print(f"root         {source}")
    print(f"encrypted    {'yes' if encrypted else 'no'}")
    print(f"drive        /dev/{disk}" if disk else "drive        unknown")
    print(f"filesystem   {filesystem_gb:.1f} GB")
    if drive_gb:
        print(f"capacity     {drive_gb:.1f} GB")
    if partition_bytes:
        unclaimed = _unclaimed_bytes(used.total, partition_bytes)
        if unclaimed:
            print()
            print(
                f"There is {unclaimed / 1_000_000_000:.1f} GB of unused space on "
                "this drive. Run portlin-expand to claim it."
            )
    print(f"booted on    {command_output(['uname', '-n'])}, {command_output(['uname', '-r'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: this listing was corrected after a whole-branch review found three bugs
in the original version: it compared the filesystem against the whole disk
rather than the root partition, which nagged about unused space on every
stick, expanded or not; it shelled out to `lsb_release`, which nothing
installs, so `debian` always read `unknown`; and its `lsblk -no PKNAME` query
was missing `-d` (no-deps), which on the ordinary case this runs against -- a
live, mounted, encrypted stick, where an open LUKS mapping sits on top of the
very partition being asked about -- returns two rows glued into one string
instead of one. It now compares against the partition, reads `/etc/os-release`
directly, queries lsblk with `-d`, and shares `backing_partition`/
`command_output`/`root_source` with the other two tools via
`usr/lib/portlin/devices.py` instead of a hand-rolled `/sys/class/block`
lookup. Re-executing this task should start from that shared module and this
corrected arithmetic.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_package.py -v -k info`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/portlin-info tests/test_package.py
git commit -m "feat(runtime): add portlin-info"
```

---

### Task 5: `portlin-expand`

Grows the system into the rest of the drive for someone who declined the offer
at first boot. A deliberate second implementation of the wizard's
`apply_expand()`, per the tier rule: the wizard's copy is frozen because it runs
before any update is possible.

**Files:**
- Create: `portlin/resources/runtime/portlin-expand`
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: a `/usr/bin/portlin-expand` command. Task 11 exercises it against a
  real loop device in the harness.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_package.py`:

```python
def test_expand_tool_resizes_in_the_only_order_that_works():
    # Each layer can only grow into space the layer beneath it has claimed, so
    # the order is not a preference. Asserted on the source because the real
    # behaviour is covered by the harness against a live device.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-expand"]
    assert source.index("growpart") < source.index("cryptsetup")
    assert source.index("cryptsetup") < source.index("resize2fs")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_package.py -v -k expand`

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the tool**

Create `portlin/resources/runtime/portlin-expand`:

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Grow a portlin stick into the rest of its drive.

The image ships at a fixed size so that one image fits every drive and the
flash is quick, and the first-boot wizard offers to expand it. This is the
same operation for someone who declined then, or who moved the system to a
larger drive since.

All three resizes run online, on the mounted running system. Each layer can
only grow into space the layer beneath it has already claimed, so the order is
forced: partition, then LUKS mapping, then filesystem.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/usr/lib/portlin")

from devices import backing_partition, command_output, root_source  # noqa: E402


def run(argv: list[str], *, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, input=stdin)
    if check and proc.returncode != 0:
        raise SystemExit(
            f"portlin-expand: {' '.join(argv)} failed:\n{proc.stderr.strip()}"
        )
    return proc


def _split_partition(partition: str) -> tuple[str, str]:
    """Split a partition kernel name into its disk and its number."""
    disk = command_output(["lsblk", "-dno", "PKNAME", f"/dev/{partition}"])
    if not disk:
        raise SystemExit(f"portlin-expand: cannot find the disk behind {partition}")
    number = partition[len(disk):].lstrip("p")
    return f"/dev/{disk}", number


def main() -> int:
    if os.geteuid() != 0:
        print("portlin-expand must be run as root.", file=sys.stderr)
        return 1

    source = root_source()
    if not source:
        print("portlin-expand: cannot determine the root device.", file=sys.stderr)
        return 1

    encrypted = source.startswith("/dev/mapper/")
    partition = backing_partition(source)
    disk, number = _split_partition(partition)

    print(f"Growing {source} to fill {disk}.")
    try:
        answer = input("This cannot be undone, but it does not destroy data. [y/N] ")
    except EOFError:
        print("\nNo input available; nothing was changed.")
        return 0
    if answer.strip().lower() not in {"y", "yes"}:
        print("Nothing was changed.")
        return 0

    # growpart exits 1 with NOCHANGE when there is nothing to do, which is a
    # success for us: it means the partition already fills the drive.
    result = run(["growpart", disk, number], check=False)
    if result.returncode != 0 and "NOCHANGE" not in result.stdout:
        raise SystemExit(f"portlin-expand: growpart failed:\n{result.stderr.strip()}")

    # Unlike the wizard, which returns immediately when growpart reports
    # NOCHANGE, this keeps going: a partition that already fills the drive can
    # still have an unresized mapping or filesystem left over from an
    # interrupted expansion, and repairing that is exactly what this tool is
    # for.
    if encrypted:
        name = Path(source).name
        # cryptsetup normally takes the volume key from the kernel keyring,
        # but when it is not there it quietly falls back to prompting for the
        # passphrase. That prompt is legitimate here: unlike the wizard, which
        # draws whiptail dialogs on tty1 before a shell exists, this tool
        # always runs interactively at a terminal someone is sitting in front
        # of. Try the keyring first, and ask only if that fails.
        resize = run(["cryptsetup", "resize", name], check=False)
        if resize.returncode != 0:
            for _ in range(3):
                try:
                    passphrase = getpass.getpass(
                        "Enter this drive's passphrase to finish expanding it: "
                    )
                except EOFError:
                    break
                if not passphrase:
                    break
                resize = run(
                    ["cryptsetup", "resize", "--key-file", "-", name],
                    check=False,
                    stdin=passphrase,
                )
                if resize.returncode == 0:
                    break
                print("That passphrase was not accepted.")
            if resize.returncode != 0:
                raise SystemExit(
                    "portlin-expand: could not resize the encrypted mapping:\n"
                    f"{resize.stderr.strip()}"
                )

    run(["resize2fs", source])
    print("Done. Run portlin-info to see the new size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: this listing was corrected after a whole-branch review found the encrypted
path unreachable in practice. The original comment above claimed cryptsetup
"needs no key material" because the mapping was already open; in fact cryptsetup
takes the volume key from the kernel keyring when it can, but silently falls
back to prompting for the passphrase when it cannot, and the original code had
no fallback -- it simply failed after growpart had already grown the partition.
It now matches the wizard's `_resize_mapping`, tries the keyring first, and asks
for the passphrase up to three times. It also now continues past a growpart
NOCHANGE rather than returning early, so it can repair a stick whose partition
already fills the drive but whose mapping or filesystem does not; queries lsblk
with `-d` (no-deps) in `_split_partition`, without which an already-open LUKS
mapping on the partition made `lsblk -no PKNAME` return two rows glued into one
string, so growpart ran with an empty partition number; and shares
`backing_partition`/`command_output`/`root_source` with the other two tools via
`usr/lib/portlin/devices.py` instead of a hand-rolled `/sys/class/block` lookup.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_package.py -v -k expand`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/portlin-expand tests/test_package.py
git commit -m "feat(runtime): add portlin-expand"
```

---

### Task 6: `portlin-encrypt`

The doorbell. It performs no encryption itself: the fsck, the shrink, the
`cryptsetup reencrypt --encrypt` and the unlock all live in the frozen
initramfs script, which gates on `portlin.encrypt=ask` in `/proc/cmdline`. This
tool sets that flag, and refuses on any stick lacking the Task 2 finaliser.

**Files:**
- Create: `portlin/resources/runtime/portlin-encrypt`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: `/usr/local/sbin/portlin-finalise-encryption` from Task 2, as a
  runtime precondition.
- Produces: a `/usr/bin/portlin-encrypt` command.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package.py`:

```python
def test_encrypt_tool_refuses_without_the_frozen_finaliser():
    # The frozen tier of a stick written before the finaliser existed cannot be
    # brought forward, so the tool must detect its absence rather than arm an
    # encryption nothing on that stick can complete.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "/usr/local/sbin/portlin-finalise-encryption" in source


def test_encrypt_tool_does_not_encrypt_anything_itself():
    # All the dangerous work stays in the frozen, harness-tested initramfs
    # script. This tool only sets the flag that wakes it up.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "reencrypt" not in source
    assert "luksFormat" not in source
    assert "portlin.encrypt=ask" in source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_package.py -v -k encrypt`

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the tool**

Create `portlin/resources/runtime/portlin-encrypt`:

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Encrypt a portlin stick that was written without --encrypt.

This tool encrypts nothing. The work happens in the initramfs on the next
boot, before the root filesystem is mounted, because a filesystem cannot be
shrunk and re-encrypted underneath a running system. That script is installed
on every stick, is covered by scripts/test-encrypt-hook.py against real block
devices, and does nothing at all unless portlin.encrypt=ask is on the kernel
command line.

So all this does is set the flag, having first checked that the stick can
finish what it starts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FINALISER = Path("/usr/local/sbin/portlin-finalise-encryption")
DEFAULT_GRUB = Path("/etc/default/grub")
FLAG = "portlin.encrypt=ask"


def _out(argv: list[str]) -> str:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False
    ).stdout.strip()


def main() -> int:
    if os.geteuid() != 0:
        print("portlin-encrypt must be run as root.", file=sys.stderr)
        return 1

    source = _out(["findmnt", "-no", "SOURCE", "/"])
    if source.startswith("/dev/mapper/"):
        print("This stick is already encrypted.")
        return 0

    if not FINALISER.exists():
        # The finaliser is frozen tier: it is written by portlin at write time
        # and cannot be delivered by an update. A stick predating it would be
        # encrypted by the initramfs with nothing left to write its crypttab,
        # and would need its passphrase typed by hand on every boot.
        print(
            "This stick was written by a portlin too old to finish an\n"
            "encryption started later. Encrypting it now would leave it\n"
            "unable to record the result.\n\n"
            "Write the stick again with a current portlin and --encrypt.",
            file=sys.stderr,
        )
        return 1

    if not DEFAULT_GRUB.exists():
        print("portlin-encrypt: /etc/default/grub is missing.", file=sys.stderr)
        return 1

    text = DEFAULT_GRUB.read_text()
    if FLAG in text:
        print("Encryption is already armed. Reboot to begin.")
        return 0

    print(
        "This will encrypt the root filesystem of this stick during the next\n"
        "boot, before the desktop starts. You will be asked to choose a\n"
        "passphrase then, and for it on every boot afterwards.\n\n"
        "It takes a while and it must not be interrupted. Back up anything\n"
        "you cannot lose first, and do not do this on battery."
    )
    if input("Arm encryption for the next boot? [y/N] ").strip().lower() not in {
        "y",
        "yes",
    }:
        print("Nothing was changed.")
        return 0

    updated = []
    for line in text.splitlines():
        if line.startswith("GRUB_CMDLINE_LINUX_DEFAULT=") and FLAG not in line:
            head, _, tail = line.rpartition('"')
            line = f'{head} {FLAG}"{tail}' if head else line
        updated.append(line)
    DEFAULT_GRUB.write_text("\n".join(updated) + "\n")

    result = subprocess.run(["update-grub"], capture_output=True, text=True)
    if result.returncode != 0:
        DEFAULT_GRUB.write_text(text)
        print(
            f"portlin-encrypt: update-grub failed, nothing was armed:\n"
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    print("\nArmed. Reboot when you are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note the rollback: if `update-grub` fails the original `/etc/default/grub` is
restored, because a stick left with the flag set but an unregenerated
`grub.cfg` would be armed without the user knowing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_package.py -v -k encrypt`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/portlin-encrypt tests/test_package.py
git commit -m "feat(runtime): add portlin-encrypt

Sets the flag the frozen initramfs script waits for, and refuses on a
stick without the finaliser rather than arming an encryption that stick
cannot complete."
```

---

### Task 7: Move the desktop theme into the runtime package

The theme is currently rendered by eight `render_*` functions in `templates.py`
and written during `build`, which freezes it into cached tarballs. As package
payload it becomes updatable. The content does not change; only where it lives.

**Files:**
- Create: `portlin/resources/runtime/theme/` (seven files)
- Modify: `portlin/templates.py` (remove the theme renderers)
- Modify: `portlin/rootfs.py:183-193` (remove the theme writes)
- Modify: `tests/test_templates.py`, `tests/test_rootfs.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: the seven paths listed in `package.THEME_FILES`.

- [ ] **Step 1: Capture the current rendered output**

Before deleting anything, dump each renderer's output to the file it becomes,
so the move is provably content-preserving:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from portlin import templates

out = Path("portlin/resources/runtime/theme")
out.mkdir(parents=True, exist_ok=True)
for name, filename in [
    ("render_xsettings_channel", "xsettings.xml"),
    ("render_xfwm4_channel", "xfwm4.xml"),
    ("render_gtk3_settings", "gtk-3.0-settings.ini"),
    ("render_gtk4_settings", "gtk-4.0-settings.ini"),
    ("render_terminal_config", "terminalrc"),
    ("render_lightdm_greeter_conf", "50-portlin.conf"),
]:
    (out / filename).write_text(getattr(templates, name)())
    print("wrote", filename)
PY
```

- [ ] **Step 2: Write the xfce4-desktop channel by hand**

This one has no existing renderer, because the wallpaper is new. Create
`portlin/resources/runtime/theme/xfce4-desktop.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- System default only. xfconf writes a user's own choices to
     ~/.config/xfce4, so changing the wallpaper in Settings still works and
     still sticks. -->
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="last-image" type="string"
                    value="/usr/share/backgrounds/portlin/portlin-1920x1080.png"/>
          <!-- Zoomed rather than stretched: the artwork is 16:9, and zoomed
               crop-fits it onto 16:10 and ultrawide panels without distorting
               the partition bars. -->
          <property name="image-style" type="int" value="5"/>
        </property>
      </property>
    </property>
  </property>
</channel>
```

- [ ] **Step 3: Write the failing test**

Append to `tests/test_package.py`:

```python
def test_runtime_ships_every_theme_file():
    files = package.text_files("portlin-runtime")
    for destination in package.THEME_FILES:
        assert destination in files, destination
    assert "Greybird-dark" in files[
        f"{package.XDG_OVERLAY}/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
    ]


def test_theme_files_are_not_executable():
    assert not (package.executable_paths("portlin-runtime")
                & set(package.THEME_FILES))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_package.py -v -k theme`

Expected: PASS, since Step 1 already created the files.

- [ ] **Step 5: Remove the renderers and the build-time writes**

In `portlin/rootfs.py`, delete the theme block at lines 183 to 193 and the
now-unused `xfconf` local. In `portlin/templates.py`, delete
`render_xsettings_channel`, `render_xfwm4_channel`, `render_gtk3_settings`,
`render_gtk4_settings`, `render_lightdm_greeter_conf`, `render_terminal_config`
and the `GTK_THEME` constant.

Keep `greybird-gtk-theme` in `packages.py`: the theme files reference it, and
it must be installed for them to mean anything.

- [ ] **Step 6: Move the tests across**

Delete the theme tests from `tests/test_templates.py` and the theme assertions
from `tests/test_rootfs.py`. Their coverage now lives in `tests/test_package.py`.

- [ ] **Step 7: Run the whole suite**

Run: `make test`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add portlin/resources/runtime/theme portlin/templates.py portlin/rootfs.py \
        tests/test_templates.py tests/test_rootfs.py tests/test_package.py
git commit -m "refactor(theme): carry the desktop theme in the runtime package

Rendered at build time the theme was frozen into cached tarballs and into
every stick written from one. As package payload it can be updated."
```

---

### Task 8: Render and commit the wallpapers

**Files:**
- Modify: `out/brand/render.sh`
- Modify: `.gitignore`
- Create: `portlin/resources/wallpapers/portlin-<size>.png` (six files)
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: the six paths named by `package.binary_files("portlin-desktop")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_package.py`:

```python
def test_every_declared_binary_member_exists():
    for name in package.PACKAGES:
        for destination, source in package.binary_files(name).items():
            assert source.exists(), f"{source} is missing for {destination}"


def test_wallpapers_carry_every_declared_size():
    destinations = package.binary_files("portlin-desktop")
    assert len(destinations) == len(package.WALLPAPER_SIZES)
    assert all(d.startswith("usr/share/backgrounds/portlin/") for d in destinations)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_package.py -v -k wallpaper`

Expected: FAIL with a missing `portlin-1366x768.png`.

- [ ] **Step 3: Extend the render script**

`wallpaper.html` is a fixed 1920x1080 canvas with every element at an absolute
pixel offset, so each size is that same canvas rasterised at a different device
scale factor. Replace the wallpaper lines in `out/brand/render.sh` with:

```bash
# One canvas, six scale factors. The composition is authored at 1920x1080 with
# absolute offsets, so scaling the device pixel ratio is the only thing that
# keeps every element in proportion. A genuinely 16:10 or ultrawide render
# would need the layout made responsive first.
WALLPAPERS="$PWD/../../portlin/resources/wallpapers"
mkdir -p "$WALLPAPERS"
render_wallpaper() {  # scale WxH
    render wallpaper.html "$1" 1920,1080 "$WALLPAPERS/portlin-$2.png"
}
render_wallpaper 0.711 1366x768
render_wallpaper 1     1920x1080
render_wallpaper 1.333 2560x1440
render_wallpaper 2     3840x2160
render_wallpaper 2.667 5120x2880
render_wallpaper 4     7680x4320

render wallpaper-cmd.html 1 1920,1080 portlin-wallpaper-1920x1080-cmd.png
```

- [ ] **Step 4: Render them**

Run: `out/brand/render.sh`

Then confirm the sizes are what they claim:

```bash
.venv/bin/python -c "
import struct, pathlib
for p in sorted(pathlib.Path('portlin/resources/wallpapers').glob('*.png')):
    w, h = struct.unpack('>II', p.read_bytes()[16:24])
    print(f'{p.name}: {w}x{h}, {p.stat().st_size/1e6:.1f} MB')
"
```

Expected: six files whose pixel dimensions match their names, totalling roughly
14 MB. If a dimension is off by a pixel from the scale factor rounding, rename
the file to the true dimensions and update `package.WALLPAPER_SIZES` to match.

- [ ] **Step 5: Place the logo and the keyring placeholder**

`binary_files` declares two members beyond the wallpapers. The logo already
exists as `out/brand/mark.svg`:

```bash
cp out/brand/mark.svg portlin/resources/runtime/logo.svg
```

The signing key does not exist yet and is generated by the follow-up archive
plan. A zero-byte placeholder keeps the package layout complete and the tests
honest, and it is what the "not yet serving anything" note at the end of this
plan describes:

```bash
mkdir -p portlin/resources/keyring
: > portlin/resources/keyring/portlin-archive-keyring.gpg
```

Add a test to `tests/test_package.py` asserting the layout rather than the key,
because there is no key to validate yet:

```python
def test_keyring_package_carries_the_key_at_the_path_the_source_names():
    # The Signed-By path in the apt source and the path the package installs
    # the key to are the same string in two files, and apt fails silently on
    # every update if they drift apart.
    destinations = package.binary_files("portlin-archive-keyring")
    assert package.KEYRING_PATH.lstrip("/") in destinations
    assert package.KEYRING_PATH in package.render_sources_entry()
```

- [ ] **Step 6: Track them**

`out/` is gitignored, but the wallpapers now live under `portlin/resources/`,
which is not. Confirm nothing excludes them:

```bash
git check-ignore -v portlin/resources/wallpapers/portlin-1920x1080.png || echo "not ignored"
```

Expected: `not ignored`.

Then include them in the Python package data. In `pyproject.toml`, replace the
package-data line:

```toml
[tool.setuptools.package-data]
portlin = [
    "resources/firstboot/*",
    "resources/runtime/*",
    "resources/runtime/theme/*",
    "resources/wallpapers/*",
    "resources/keyring/*",
]
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_package.py -v -k wallpaper`

Expected: PASS.

- [ ] **Step 8: Commit**

`out/` is gitignored and has no tracked files, so the render script cannot be
committed and must not be staged. The generator stays a local tool; this task's
tracked deliverable is the rendered PNGs, the logo, the keyring placeholder and
the packaging metadata.

```bash
git add pyproject.toml portlin/resources/wallpapers portlin/resources/runtime/logo.svg \
        portlin/resources/keyring tests/test_package.py
git commit -m "feat(brand): render and ship six wallpaper sizes

One 1920x1080 canvas at six device scale factors, from 1366x768 to 8K.
Pre-rendered rather than scaled at display time because the composition
carries a one-pixel grid and hard-edged bars, which resampling destroys."
```

---

### Task 9: Build and install the packages during write

**Files:**
- Modify: `portlin/runner.py` (add `copy_file`)
- Modify: `portlin/install.py` (`_install_firstboot` becomes `_install_runtime`)
- Test: `tests/test_runner.py`, `tests/test_install.py`

**Interfaces:**
- Consumes: everything from `portlin/package.py`.
- Produces: `Runner.copy_file(source, destination)`, recorded as
  `["copy-file", src, dst]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_runner.py`:

```python
def test_copy_file_is_recorded_like_a_command(tmp_path):
    # Wallpapers are binary, so they cannot go through write_file, but a dry
    # run still has to show them.
    runner = Runner(dry_run=True)
    runner.copy_file(tmp_path / "a.png", tmp_path / "b.png")
    assert runner.rendered() == [f"copy-file {tmp_path / 'a.png'} {tmp_path / 'b.png'}"]


def test_copy_file_copies_bytes_when_not_dry_running(tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    runner = Runner()
    runner.copy_file(source, tmp_path / "nested" / "b.bin")
    assert (tmp_path / "nested" / "b.bin").read_bytes() == b"\x89PNG\r\n\x1a\n"
```

In `tests/test_install.py`, inside `class TestUnencryptedWrite`:

```python
def test_builds_every_package_before_installing_any(self):
    assert self.t.tokens_before(
        ("dpkg-deb", "--build"), ("apt-get", "install")
    )

def test_builds_all_three_packages(self):
    assert self.t.count("dpkg-deb", "--build") == 3

def test_installs_the_packages_in_one_transaction(self):
    # One apt call so the inter-package dependencies resolve against each
    # other rather than against an archive that is not reachable here.
    argv = self.t.command_at("apt-get", "install")
    assert sum(1 for a in argv if a.endswith(".deb")) == 3

def test_installs_packages_after_the_frozen_wizard(self):
    assert self.t.before(
        ("write-file", "usr/local/sbin/portlin-firstboot"),
        ("apt-get", "install"),
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py tests/test_install.py -v -k "copy_file or package or dpkg"`

Expected: FAIL with `AttributeError: 'Runner' object has no attribute 'copy_file'`
and `no command had tokens ('dpkg-deb', '--build')`.

- [ ] **Step 3: Add `Runner.copy_file`**

In `portlin/runner.py`, after `write_file`:

```python
    def copy_file(self, source: str | Path, destination: str | Path) -> None:
        """Copy a file, recorded like a command so dry runs stay inspectable.

        write_file handles text. Wallpapers are PNGs, and decoding them into a
        str to write them back out would corrupt them.
        """
        source, destination = Path(source), Path(destination)
        self.commands.append(["copy-file", str(source), str(destination)])
        if self.dry_run:
            log.debug("dry-run: copy %s to %s", source, destination)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
```

Add `import shutil` at the top of the file.

- [ ] **Step 4: Build and install the packages**

In `portlin/install.py`, add the import:

```python
from . import package as pkg
```

Rename `_install_firstboot` to `_install_runtime`, update its call site at line
116, and append the package work to it:

```python
def _install_runtime(chroot: Chroot) -> None:
    ...  # the existing frozen-tier writes stay exactly as they are

    _build_and_install_packages(chroot)


def _has_desktop(chroot: Chroot) -> bool:
    """Whether the unpacked rootfs contains an Xfce session.

    Asked of the filesystem rather than the config, because ``write`` consumes
    a prebuilt tarball and the package groups that produced it belong to
    ``BuildConfig``. A tarball built with --minimal on one machine can be
    written on another that never saw those arguments, so the rootfs itself is
    the only honest source.

    Routed through the runner so it is recorded, and so a dry run reports the
    full desktop plan rather than silently dropping the wallpapers.
    """
    return chroot.runner.exists(
        ["test", "-x", str(chroot.root / "usr/bin/startxfce4")]
    )


def _build_and_install_packages(chroot: Chroot) -> None:
    """Assemble portlin's own packages in the chroot and install them.

    Built here rather than committed as binaries or fetched over the network,
    so that write stays offline, the repository stays free of build products,
    and a stick can never receive a package older than the checkout that wrote
    it. The version carries a ~local suffix, which sorts below any published
    release, so the first apt upgrade replaces these with signed builds.
    """
    staging = "tmp/portlin-packages"
    names = list(pkg.PACKAGES)
    if not _has_desktop(chroot):
        # 14 MB of wallpaper on a --minimal stick with no desktop to show it.
        # Recommends rather than Depends is what makes leaving it out legal.
        names.remove("portlin-desktop")

    for name in names:
        root = f"{staging}/{name}"
        for relative, content in pkg.text_files(name).items():
            mode = 0o755 if relative in pkg.executable_paths(name) else 0o644
            chroot.write_file(f"{root}/{relative}", content, mode=mode)
        for relative, source in pkg.binary_files(name).items():
            chroot.runner.copy_file(source, chroot.root / root / relative)
        chroot.run(["dpkg-deb", "--build", f"/{root}", f"/{staging}/{name}.deb"])

    # One transaction, so the dependencies between these three resolve against
    # each other. There is no network here and no archive to fall back on.
    chroot.apt(["install", *[f"/{staging}/{name}.deb" for name in names]])
    chroot.run(["rm", "-rf", f"/{staging}"])
```

Update the call site at line 116:

```python
            _install_runtime(chroot)
```

`Runner.exists` returns True under `dry_run`, because a dry run has no rootfs
to probe and reporting the fuller plan is the more useful answer. That is why
the Step 1 test expects three packages rather than two.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runner.py tests/test_install.py -v`

Expected: PASS.

- [ ] **Step 6: Check the dry run reads correctly**

Run: `make dryrun`

Expected: the write plan shows three `dpkg-deb --build` calls, one `apt-get
install` naming three `.deb` paths, and the `copy-file` lines for the
wallpapers.

- [ ] **Step 7: Commit**

```bash
git add portlin/runner.py portlin/install.py tests/test_runner.py tests/test_install.py
git commit -m "feat(install): build and install the runtime packages during write"
```

---

### Task 10: A `package` subcommand

CI must build the same packages from the same code, or a published package and
a locally built one will drift apart.

**Files:**
- Modify: `portlin/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `python -m portlin package --output DIR [--version V]`, writing one
  `.deb` per package into `DIR`. The follow-up archive plan consumes this.

- [ ] **Step 1: Write the failing test**

In `tests/test_cli.py`:

`cli.main` takes only an argv list and constructs its own `Runner` from
`--dry-run`; it does not accept a runner argument. Tests in this file assert
against `capsys`, because a dry run prints its command plan to stdout. Follow
that existing idiom, visible in `TestMain` in the same file:

```python
def test_package_subcommand_builds_every_package(tmp_path, capsys):
    cli.main(["--dry-run", "package", "--output", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.count("dpkg-deb --build") == 3
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v -k package`

Expected: FAIL with a SystemExit from argparse, `invalid choice: 'package'`.

- [ ] **Step 3: Add the subcommand**

Follow the existing subparser pattern in `portlin/cli.py`. Name it `cmd_package` to match the existing `cmd_doctor`/`cmd_devices`/
`cmd_build` handlers, which carry no leading underscore. The handler
assembles each tree on the host with `runner.write_file` and
`runner.copy_file`, then runs `dpkg-deb --build` on the host rather than in a
chroot:

```python
def cmd_package(args: argparse.Namespace, runner: Runner) -> int:
    """Build the runtime packages without writing a stick.

    Shared with the write path through portlin.package, so that a package
    published by CI and one built locally can never be assembled by two
    different code paths.
    """
    from . import package as pkg

    output = Path(args.output)
    version = args.version or pkg.local_version()
    for name in pkg.PACKAGES:
        root = output / name
        for relative, content in pkg.text_files(name).items():
            mode = 0o755 if relative in pkg.executable_paths(name) else 0o644
            content = content.replace(pkg.local_version(), version)
            runner.write_file(root / relative, content, mode=mode)
        for relative, source in pkg.binary_files(name).items():
            runner.copy_file(source, root / relative)
        runner.run(["dpkg-deb", "--build", str(root), str(output / f"{name}.deb")])
    return 0
```

Register it alongside the other subparsers, with `--output` required and
`--version` optional.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v -k package`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/cli.py tests/test_cli.py
git commit -m "feat(cli): add a package subcommand"
```

---

### Task 11: Prove it on a real image

Unit tests assert the command order. These assert that the packages actually
install, land where they should, and upgrade without stopping to ask.

**Files:**
- Modify: `scripts/verify-image.sh`
- Create: `scripts/test-package-upgrade.py`
- Modify: `Makefile` (`harness` target)
- Modify: `README.md`, `docs/design.md`

- [ ] **Step 1: Extend the structural check**

In `scripts/verify-image.sh`, alongside the existing checks, assert against the
loop-mounted root:

Follow the script's existing idiom exactly. It has no `check` helper: assertions
are written as a test command chained to the `pass`/`fail` pair defined at
`scripts/verify-image.sh:14-15`, where `fail` increments the `FAILURES` counter
the script exits on. Read those two lines and a nearby assertion before writing
these, and match the surrounding style.

```bash
test -f "$MNT/var/lib/dpkg/info/portlin-runtime.list" \
    && pass "portlin-runtime is installed" \
    || fail "portlin-runtime is not installed (no updates will reach this stick)"

test -f "$MNT/etc/apt/sources.list.d/portlin.sources" \
    && pass "the portlin apt source is present" \
    || fail "the portlin apt source is missing"

test -f "$MNT/usr/share/keyrings/portlin-archive-keyring.gpg" \
    && pass "the archive keyring is present" \
    || fail "the archive keyring is missing (apt will reject the archive)"

test -L "$MNT/etc/systemd/system/multi-user.target.wants/portlin-finalise-encryption.service" \
    && pass "the encryption finaliser is enabled" \
    || fail "the encryption finaliser is not enabled"

for tool in portlin-info portlin-expand portlin-encrypt; do
    test -x "$MNT/usr/bin/$tool" \
        && pass "$tool is executable" \
        || fail "$tool is missing or not executable"
done
```

- [ ] **Step 2: Write the upgrade harness**

Create `scripts/test-package-upgrade.py`:

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Install the runtime packages, then upgrade them, and prove it stays quiet.

The assertion that matters is the second install: dpkg must not stop to ask
about a conffile. That prompt would surface in the middle of an unrelated
apt full-upgrade, which is where a user is least equipped to answer it, and no
unit test can see it because it only exists once dpkg is really running.

Needs root and a Debian userland. Run under `make harness`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THEME = "etc/xdg/xdg-portlin/xfce4/terminal/terminalrc"


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def build(output: Path, version: str) -> None:
    result = run(
        [sys.executable, "-m", "portlin", "package",
         "--output", str(output), "--version", version],
        cwd=REPO,
    )
    if result.returncode != 0:
        sys.exit(f"building {version} failed:\n{result.stderr}")


def install(debs: list[Path]) -> subprocess.CompletedProcess:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    return subprocess.run(
        ["apt-get", "install", "-y",
         "-o", "Dpkg::Options::=--force-confdef",
         *[str(d) for d in debs]],
        capture_output=True, text=True, env=env,
    )


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("needs root; run it under make harness")

    with tempfile.TemporaryDirectory() as tmp:
        first, second = Path(tmp) / "v1", Path(tmp) / "v2"
        first.mkdir()
        second.mkdir()

        build(first, "0.1.0~test")
        debs = sorted(first.glob("*.deb"))
        if len(debs) != 3:
            sys.exit(f"expected three packages, built {len(debs)}")

        result = install(debs)
        if result.returncode != 0:
            sys.exit(f"first install failed:\n{result.stderr}")

        installed = Path("/") / THEME
        if not installed.exists():
            sys.exit(f"{THEME} was not installed")
        print(f"ok: first install placed {THEME}")

        # Change the shipped content so the second build genuinely differs.
        # An upgrade whose files are byte-identical would never prompt, and
        # would prove nothing.
        source = REPO / "portlin/resources/runtime/theme/terminalrc"
        original = source.read_text()
        source.write_text(original + "\n# upgrade probe\n")
        try:
            build(second, "0.1.1~test")
        finally:
            source.write_text(original)

        result = install(sorted(second.glob("*.deb")))
        if result.returncode != 0:
            sys.exit(f"upgrade failed:\n{result.stderr}")

        output = result.stdout + result.stderr
        for phrase in ("Configuration file", "conffile", "What would you like"):
            if phrase in output:
                sys.exit(f"the upgrade prompted about a conffile:\n{output}")
        print("ok: the upgrade did not prompt")

        if "# upgrade probe" not in installed.read_text():
            sys.exit(f"{THEME} was not updated by the upgrade")
        print("ok: the upgrade replaced the unmodified conffile")

    print("\npackage upgrade harness passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The last check is the counterpart to the quiet-upgrade one: dpkg must replace a
conffile the user has not touched, or the update channel would deliver nothing.

- [ ] **Step 3: Add it to the harness target**

In the `Makefile`, extend the `harness` recipe's command list with
`python3 -u scripts/test-package-upgrade.py`, and add `dpkg-dev` to the
`apt-get install` line in that target.

- [ ] **Step 4: Run the harness**

Run: `make harness`

Expected: all three existing harnesses pass, plus the new one. This needs
Docker and takes roughly three minutes.

- [ ] **Step 5: Document the tier rule**

In `docs/design.md`, add a section after "The portability contract" containing
the tier rule table and its two consequences, copied from the spec. Add
`package.py` to the module decomposition table:

```
| `package.py` | Pure: the three runtime packages as a path-to-content mapping |
```

In `README.md`, add a short "Updates" section under "What lands on the stick":

```markdown
## Updates

The Debian system updates itself: it is a real install, so `apt full-upgrade`
and kernel upgrades work.

Portlin's own contribution to the stick is split in two. The desktop theme,
the wallpapers and the `portlin-info`, `portlin-expand` and `portlin-encrypt`
commands are Debian packages, and update from portlin's archive like anything
else. The bootloader, the initramfs, `fstab` and `crypttab` are written once
and stay put, because an update that breaks one of those is a stick that will
not boot. Moving those forward means writing the stick again.
```

- [ ] **Step 6: Run everything**

Run: `make check`

Expected: all tests pass, shellcheck clean.

- [ ] **Step 7: Commit**

```bash
git add scripts/verify-image.sh scripts/test-package-upgrade.py Makefile \
        README.md docs/design.md
git commit -m "test: prove the runtime packages install and upgrade cleanly

The upgrade harness exists for one assertion: that a second version
installs without a conffile prompt, which would otherwise surface in the
middle of an unrelated apt full-upgrade."
```

---

## What this plan does not cover

Spec step 3, the signed archive itself: the GPG key, the CI workflows,
`apt-ftparchive`, and publication to GitHub Pages. It needs key material that
does not exist yet and cannot be tested without publishing, so it gets its own
plan once this one has landed.

After this plan, a written stick carries the three packages, the apt source and
the keyring path, but the archive it points at is not yet serving anything. That
is inert rather than broken: `apt update` reports the source as unreachable
until the archive exists. Task 11's `verify-image.sh` checks confirm the
structure is in place and ready for it.

The keyring package ships `/usr/share/keyrings/portlin-archive-keyring.gpg`,
which does not exist until the key is generated. Until then, place a zero-byte
placeholder at `portlin/resources/keyring/portlin-archive-keyring.gpg` and add a
Task 3 test asserting the file is listed in the package, not that it is valid.
Generating the real key is the first task of the follow-up plan.
