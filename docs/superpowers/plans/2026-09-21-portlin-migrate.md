# Portlin Migrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring a chosen subset of one portlin stick's account, home, settings, software and network state onto the running stick, from the old drive or from an archive, at first boot or later.

**Architecture:** One privileged, stdlib-only CLI (`portlin-migrate`) plans and runs everything as an ordered list of `Step`s built by pure functions in a shared module (`/usr/lib/portlin/migrate.py`); rsync moves files from a stick, tar from an archive, and the CLI streams the `::step/::progress/::warn/::result` protocol `portlin-install` established. The first-boot wizard and a GTK window (`portlin-migration`) are two front ends over the same verbs, joined by a plan file.

**Tech Stack:** Python 3 stdlib on the stick (no third-party modules), whiptail, rsync, tar + zstd, GTK 3 through PyGObject for the window, pytest on the host, Docker loop-device harness.

**Spec:** `docs/superpowers/specs/2026-09-21-portlin-migrate-design.md`

## Global Constraints

- Everything that ships to the stick is stdlib-only Python and imports shared modules from `/usr/lib/portlin` (`sys.path.insert(0, "/usr/lib/portlin")`), exactly as `portlin-install` does.
- The source is never written to: plaintext mounts are `ro,noload`, LUKS mappings are `--readonly`.
- Passphrases are read from stdin, never argv.
- The tool deletes nothing on the target except by moving it into the backup tree.
- Protocol lines are `::step`, `::progress <0-100>`, `::warn`, `::result ok|failed [text]`. Exit codes: 0 ok, 1 failed, 2 usage, 3 privilege, 6 no space.
- Comments explain the why of the code, never the session. No dashes other than plain hyphens anywhere (no en or em dashes). Conventional commit messages, no AI trailers.
- New files carry the same GPL header the other runtime tools carry.
- Unit tests must pass on macOS with `make test`; nothing in them may need root, Linux, rsync, tar or GTK.
- Package versions in `packages.py` are Debian package names, unpinned, as the rest of that file.

---

## File structure

| File | Responsibility |
|---|---|
| `portlin/packages.py` | add `rsync` and `zstd` to the `SYSTEM` group |
| `portlin/package.py` | ship the new tool, module, polkit action, window and menu entry; `portlin-runtime` depends on `rsync`, `zstd` |
| `portlin/resources/runtime/migrate.py` | pure: candidates parsing, reading a source root, inventory, selection, step planning (rsync, tar, account, identity, theme, software), output parsers, byte formatting |
| `portlin/resources/runtime/portlin-migrate` | the CLI: open and close sources, run steps with the protocol, the verbs, the whiptail flow |
| `portlin/resources/runtime/org.portlin.migrate.policy` | polkit action for the window |
| `portlin/resources/runtime/portlin-migration` | the GTK window |
| `portlin/resources/runtime/portlin-migration.desktop` | menu entry |
| `portlin/resources/firstboot/portlin-firstboot` | the *Restore from another portlin* screen, prefilled steps |
| `tests/test_migrate.py` | unit tests for `migrate.py` |
| `tests/test_migrate_tool.py` | unit tests for the CLI's pure parts and the plan-to-run seam |
| `tests/test_migration_window.py` | unit tests for the window's pure parts, gi stubbed |
| `tests/test_package.py`, `tests/test_firstboot.py` | shipping and wizard assertions |
| `scripts/test-migrate.py` | Docker loop-device harness |
| `Makefile` | harness invocation |
| `README.md` | *Migrating* section |

Task order: 1 packages, 2-7 `migrate.py`, 8-10 the CLI, 11 packaging, 12 wizard, 13 window, 14 harness, 15 docs.

---

### Task 1: rsync and zstd on every stick

**Files:**
- Modify: `portlin/packages.py:52-69` (the `SYSTEM` list)
- Modify: `portlin/package.py:329-342` (`portlin-runtime` depends)
- Test: `tests/test_packages.py`, `tests/test_package.py`

**Interfaces:**
- Produces: nothing new; `packages.SYSTEM` contains `"rsync"` and `"zstd"`, `package.text_files("portlin-runtime")["DEBIAN/control"]` names both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_packages.py`:

```python
def test_every_stick_carries_the_migration_tools():
    # portlin-migrate copies with rsync and archives with zstd, and it ships
    # in portlin-runtime, which a --minimal stick installs. So both live in
    # the system group rather than in TOOLS, the way pciutils does.
    assert "rsync" in packages.SYSTEM
    assert "zstd" in packages.SYSTEM
```

Append to `tests/test_package.py`:

```python
def test_runtime_depends_on_what_the_migration_tool_shells_out_to():
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    depends = next(line for line in control.splitlines() if line.startswith("Depends:"))
    assert "rsync" in depends
    assert "zstd" in depends
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_packages.py::test_every_stick_carries_the_migration_tools tests/test_package.py::test_runtime_depends_on_what_the_migration_tool_shells_out_to -v`
Expected: both FAIL on the assertions.

- [ ] **Step 3: Add the packages**

In `portlin/packages.py`, inside `SYSTEM` after the `pciutils` entry:

```python
    # rsync copies and zstd compresses for portlin-migrate, which ships in
    # portlin-runtime. Here rather than in TOOLS for the same reason as
    # pciutils: a --minimal stick has the tool and must be able to run it.
    "rsync",
    "zstd",
```

Remove `"rsync"` from `TOOLS` in the same file (it would be listed twice otherwise).

In `portlin/package.py`, in the `portlin-runtime` `depends` list after `"pciutils"`:

```python
                    # portlin-migrate's own two: every copy it makes is an
                    # rsync and every archive it reads or writes goes through
                    # zstd. Neither shows in the file list.
                    "rsync",
                    "zstd",
```

- [ ] **Step 4: Run the whole suite**

Run: `make test`
Expected: PASS. If a test asserts the exact contents of `TOOLS`, update it to the list without `rsync`.

- [ ] **Step 5: Commit**

```bash
git add portlin/packages.py portlin/package.py tests/test_packages.py tests/test_package.py
git commit -m "build: carry rsync and zstd on every stick"
```

---

### Task 2: `migrate.py` skeleton, candidates and byte formatting

**Files:**
- Create: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Produces:
  - constants `BOOT_LABEL = "portlin-boot"`, `ROOT_LABEL = "portlin-root"`, `ARCHIVE_SUFFIX = ".portlin-backup.tar.zst"`, `MANIFEST = "manifest.json"`, `LSBLK_COLUMNS = "PATH,LABEL,FSTYPE,SIZE,MODEL,TRAN,PARTN"`
  - `@dataclass(frozen=True) Candidate(kind: str, path: str, model: str = "", size: str = "", encrypted: bool = False, version: str = "")` where `kind` is `"stick"` or `"archive"`
  - `lsblk_argv() -> list[str]`
  - `parse_lsblk(text: str, running_disk: str) -> list[Candidate]`
  - `archive_candidates(mounts: list[Path]) -> list[Candidate]`
  - `describe(candidate: Candidate) -> str`
  - `human(size: int) -> str` giving `"14.2 GB"`, `"512 MB"`, `"3 KB"`, `"0 B"` (decimal units like `portlin-info`)
  - `read_release(root: Path) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate.py`:

```python
"""The pure half of portlin-migrate: what it reads, what it plans, never what it runs.

Everything here runs with no root, no Linux and no rsync. The planners produce
ordered argument lists, and those lists are the artefact under test, because
that is where a migration goes wrong in a way nothing else notices: a backup
directory named relative to the wrong root, a chown on a system path, a
source directory copied without its trailing slash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


LSBLK = json.dumps({
    "blockdevices": [
        {"path": "/dev/sda", "label": None, "fstype": None, "size": "119.2G",
         "model": "Running Stick", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sda3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sda4", "label": "portlin-root", "fstype": "ext4", "size": "110G", "partn": 4},
         ]},
        {"path": "/dev/sdb", "label": None, "fstype": None, "size": "57.3G",
         "model": "SanDisk Ultra", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdb1", "label": None, "fstype": None, "size": "1M", "partn": 1},
             {"path": "/dev/sdb2", "label": "PORTLIN-ESP", "fstype": "vfat", "size": "512M", "partn": 2},
             {"path": "/dev/sdb3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sdb4", "label": None, "fstype": "crypto_LUKS", "size": "55.8G", "partn": 4},
         ]},
        {"path": "/dev/sdc", "label": None, "fstype": None, "size": "28.9G",
         "model": "Kingston", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdc3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sdc4", "label": "portlin-root", "fstype": "ext4", "size": "27G", "partn": 4},
         ]},
        {"path": "/dev/sdd", "label": None, "fstype": None, "size": "1.8T",
         "model": "WD Elements", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdd1", "label": "Backups", "fstype": "exfat", "size": "1.8T", "partn": 1},
         ]},
        {"path": "/dev/nvme0n1", "label": None, "fstype": None, "size": "931G",
         "model": "Samsung SSD", "tran": "nvme", "partn": None,
         "children": [
             {"path": "/dev/nvme0n1p3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/nvme0n1p4", "label": None, "fstype": "ext4", "size": "900G", "partn": 4},
         ]},
    ]
})


class TestCandidates:
    def test_a_stick_is_recognised_by_its_boot_label_and_root_partition(self, migrate):
        found = migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        assert [c.path for c in found] == ["/dev/sdb4", "/dev/sdc4"]

    def test_an_encrypted_root_is_flagged_and_a_plain_one_is_not(self, migrate):
        by_path = {c.path: c for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")}
        assert by_path["/dev/sdb4"].encrypted is True
        assert by_path["/dev/sdc4"].encrypted is False

    def test_the_running_disk_is_never_offered(self, migrate):
        assert all(
            not c.path.startswith("/dev/sda")
            for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        )

    def test_a_root_without_the_root_label_is_not_a_stick(self, migrate):
        # nvme0n1 has a portlin-boot label but its fourth partition is plain
        # ext4 with no portlin-root label: somebody's own layout, not ours.
        assert "/dev/nvme0n1p4" not in [
            c.path for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        ]

    def test_model_and_size_come_from_the_disk_not_the_partition(self, migrate):
        sdb = next(c for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda") if c.path == "/dev/sdb4")
        assert sdb.model == "SanDisk Ultra"
        assert sdb.size == "57.3G"
        assert sdb.kind == "stick"

    def test_empty_or_broken_lsblk_output_means_no_candidates(self, migrate):
        assert migrate.parse_lsblk("", running_disk="/dev/sda") == []
        assert migrate.parse_lsblk("not json", running_disk="/dev/sda") == []

    def test_lsblk_is_asked_for_json_with_the_columns_the_parser_reads(self, migrate):
        argv = migrate.lsblk_argv()
        assert argv[:2] == ["lsblk", "--json"]
        assert "-o" in argv and argv[argv.index("-o") + 1] == migrate.LSBLK_COLUMNS

    def test_archives_are_found_directly_under_each_mount(self, migrate, tmp_path):
        media = tmp_path / "media" / "somebody" / "MyDrive"
        media.mkdir(parents=True)
        wanted = media / "office-2026-09-14.portlin-backup.tar.zst"
        wanted.write_bytes(b"")
        (media / "notes.txt").write_text("no")
        deeper = media / "old" / "x.portlin-backup.tar.zst"
        deeper.parent.mkdir()
        deeper.write_bytes(b"")
        found = migrate.archive_candidates([media])
        assert [c.path for c in found] == [str(wanted)]
        assert found[0].kind == "archive"
        assert found[0].model == "MyDrive"

    def test_describe_names_the_drive_and_whether_it_is_encrypted(self, migrate):
        stick = migrate.Candidate("stick", "/dev/sdb4", "SanDisk Ultra", "57.3G", True, "0.1.2")
        assert migrate.describe(stick) == "SanDisk Ultra 57.3G   portlin 0.1.2, encrypted"
        plain = migrate.Candidate("stick", "/dev/sdc4", "Kingston", "28.9G", False, "")
        assert migrate.describe(plain) == "Kingston 28.9G   portlin"
        archive = migrate.Candidate("archive", "/media/x/MyDrive/a.portlin-backup.tar.zst", "MyDrive")
        assert migrate.describe(archive) == "backup on MyDrive   a.portlin-backup.tar.zst"


class TestHuman:
    @pytest.mark.parametrize("size,text", [
        (0, "0 B"),
        (999, "999 B"),
        (3_000, "3 KB"),
        (512_000_000, "512 MB"),
        (1_483_920, "1.5 MB"),
        (14_200_000_000, "14.2 GB"),
        (2_500_000_000_000, "2.5 TB"),
    ])
    def test_decimal_units_like_portlin_info(self, migrate, size, text):
        assert migrate.human(size) == text


class TestRelease:
    def test_reads_the_version_out_of_portlin_release(self, migrate, tmp_path):
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "portlin-release").write_text(
            "PORTLIN_VERSION=0.1.2\nPORTLIN_URL=https://github.com/sleep/Portlin\n"
        )
        assert migrate.read_release(tmp_path)["PORTLIN_VERSION"] == "0.1.2"

    def test_a_root_without_the_breadcrumb_is_not_portlin(self, migrate, tmp_path):
        assert migrate.read_release(tmp_path) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: FAIL at the fixture (no such file `migrate.py`).

- [ ] **Step 3: Create the module**

Create `portlin/resources/runtime/migrate.py`:

```python
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""What portlin-migrate reads and plans, kept apart from what it runs.

Every function here is pure over paths and strings: it reads a source root
that has already been mounted, and it produces ordered command lines rather
than running them. That is the shape build_rootfs and write_stick have, and
for the same reason: the ordered command list is what the unit tests can
assert on a machine with no root, no rsync and no block devices, and the
harness is what proves rsync did what the list says.

Stdlib-only, and imported with no dependency on the portlin package itself,
because this file ships onto a stick where only python3 is guaranteed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Labels write puts on partitions 3 and 4. The boot label is the one that
# identifies a stick, because it is readable whether or not the root is
# encrypted.
BOOT_LABEL = "portlin-boot"
ROOT_LABEL = "portlin-root"
ARCHIVE_SUFFIX = ".portlin-backup.tar.zst"
MANIFEST = "manifest.json"
RELEASE_FILE = "etc/portlin-release"

# PARTN rather than guessing from the name: sda4 and nvme0n1p4 spell the
# number differently, and lsblk knows it either way.
LSBLK_COLUMNS = "PATH,LABEL,FSTYPE,SIZE,MODEL,TRAN,PARTN"


@dataclass(frozen=True)
class Candidate:
    """A place a migration could come from."""

    kind: str  # "stick" or "archive"
    path: str  # the root partition, or the archive file
    model: str = ""  # the disk model, or the mount an archive was found on
    size: str = ""
    encrypted: bool = False
    version: str = ""


def lsblk_argv() -> list[str]:
    return ["lsblk", "--json", "-o", LSBLK_COLUMNS]


def parse_lsblk(text: str, running_disk: str) -> list[Candidate]:
    """Every portlin stick in lsblk's output, other than the one running.

    Excluded by disk rather than by partition, so the running stick's own
    /boot is never offered as somewhere to migrate from.
    """
    try:
        disks = json.loads(text).get("blockdevices", [])
    except (ValueError, AttributeError):
        return []
    found = []
    for disk in disks:
        if disk.get("path") == running_disk:
            continue
        parts = {
            child.get("partn"): child
            for child in disk.get("children", [])
            if child.get("partn") is not None
        }
        boot, root = parts.get(3), parts.get(4)
        if not boot or not root or boot.get("label") != BOOT_LABEL:
            continue
        fstype = root.get("fstype") or ""
        if fstype == "crypto_LUKS":
            encrypted = True
        elif fstype == "ext4" and root.get("label") == ROOT_LABEL:
            encrypted = False
        else:
            continue
        found.append(
            Candidate(
                "stick",
                root["path"],
                (disk.get("model") or "").strip(),
                disk.get("size") or "",
                encrypted,
            )
        )
    return found


def archive_candidates(mounts: list[Path]) -> list[Candidate]:
    """Archives sitting directly under each mount, and nowhere deeper.

    Directly under, because that is where export puts them and where a person
    dropping one onto a drive puts it. Walking a whole backup drive to find
    one file is the kind of scan that makes a menu take a minute to appear.
    """
    found = []
    for mount in mounts:
        try:
            entries = sorted(mount.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(ARCHIVE_SUFFIX) and entry.is_file():
                found.append(Candidate("archive", str(entry), mount.name))
    return found


def describe(candidate: Candidate) -> str:
    """One menu line per candidate, the same words in every front end."""
    if candidate.kind == "archive":
        return f"backup on {candidate.model}   {Path(candidate.path).name}"
    version = f"portlin {candidate.version}" if candidate.version else "portlin"
    detail = f"{version}, encrypted" if candidate.encrypted else version
    return f"{candidate.model} {candidate.size}   {detail}"


def human(size: int) -> str:
    """Decimal units, the way portlin-info reports the drive."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = 0
    while value >= 1000 and unit < len(units) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    text = f"{value:.1f}".removesuffix(".0")
    return f"{text} {units[unit]}"


def read_release(root: Path) -> dict[str, str]:
    """The KEY=value pairs of /etc/portlin-release, or nothing when absent."""
    try:
        text = (root / RELEASE_FILE).read_text()
    except OSError:
        return {}
    pairs = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            pairs[key.strip()] = value.strip().strip('"')
    return pairs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): find portlin sticks and archives to migrate from"
```

---

### Task 3: reading the account and identity out of a source root

**Files:**
- Modify: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Account(name, uid: int, gid: int, gecos: str, shell: str, home: str, password_hash: str, groups: tuple[str, ...], sudo_nopasswd: bool, autologin: bool)`; `home` is relative to the root, like `"home/ben"`
  - `@dataclass(frozen=True) Identity(hostname: str = "", locale: str = "", keyboard: str = "", timezone: str = "")`
  - `read_accounts(root: Path) -> list[Account]`
  - `read_identity(root: Path) -> Identity`
  - constants `FIRST_USER_UID = 1000`, `NOBODY_UID = 65534`, `SUDOERS_DROPIN = "etc/sudoers.d/50-portlin-nopasswd"`, `AUTOLOGIN_CONF = "etc/lightdm/lightdm.conf.d/10-portlin.conf"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
def make_source(root: Path, *, user: str = "olduser", uid: int = 1500, nopasswd: bool = True,
                autologin: bool = False) -> Path:
    """A portlin root as a stick leaves it: one account, its home, its answers."""
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "portlin-release").write_text("PORTLIN_VERSION=0.1.2\n")
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        f"{user}:x:{uid}:{uid}:Old User,,,:/home/{user}:/bin/bash\n"
    )
    (root / "etc" / "shadow").write_text(
        "root:*:19000:0:99999:7:::\n"
        f"{user}:$y$j9T$abc$def:19000:0:99999:7:::\n"
    )
    (root / "etc" / "group").write_text(
        "root:x:0:\n"
        f"sudo:x:27:{user}\n"
        f"audio:x:29:{user}\n"
        f"plugdev:x:46:{user}\n"
        "scanner:x:120:\n"
        f"{user}:x:{uid}:\n"
    )
    if nopasswd:
        (root / "etc" / "sudoers.d").mkdir(exist_ok=True)
        (root / "etc" / "sudoers.d" / "50-portlin-nopasswd").write_text(
            f"{user} ALL=(ALL) NOPASSWD: ALL\n"
        )
    if autologin:
        conf = root / "etc" / "lightdm" / "lightdm.conf.d" / "10-portlin.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(f"[Seat:*]\nautologin-user={user}\nautologin-user-timeout=0\n")
    (root / "etc" / "hostname").write_text("office\n")
    (root / "etc" / "default").mkdir(exist_ok=True)
    (root / "etc" / "default" / "locale").write_text('LANG="en_GB.UTF-8"\n')
    (root / "etc" / "default" / "keyboard").write_text(
        'XKBMODEL=pc105\nXKBLAYOUT="gb"\nXKBVARIANT=""\n'
    )
    (root / "etc" / "timezone").write_text("Europe/London\n")
    home = root / "home" / user
    home.mkdir(parents=True, exist_ok=True)
    return home


class TestReadAccount:
    def test_finds_the_one_real_account_and_its_hash(self, migrate, tmp_path):
        make_source(tmp_path)
        accounts = migrate.read_accounts(tmp_path)
        assert [a.name for a in accounts] == ["olduser"]
        account = accounts[0]
        assert account.uid == 1500 and account.gid == 1500
        assert account.gecos == "Old User,,,"
        assert account.shell == "/bin/bash"
        assert account.home == "home/olduser"
        assert account.password_hash == "$y$j9T$abc$def"

    def test_groups_are_the_ones_that_list_the_account(self, migrate, tmp_path):
        make_source(tmp_path)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.groups == ("sudo", "audio", "plugdev")

    def test_the_sudo_waiver_and_autologin_are_read_as_answers(self, migrate, tmp_path):
        make_source(tmp_path, nopasswd=True, autologin=True)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.sudo_nopasswd is True
        assert account.autologin is True

    def test_no_waiver_and_no_autologin_file_mean_no(self, migrate, tmp_path):
        make_source(tmp_path, nopasswd=False, autologin=False)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.sudo_nopasswd is False
        assert account.autologin is False

    def test_an_unreadable_shadow_leaves_the_hash_empty_rather_than_failing(self, migrate, tmp_path):
        make_source(tmp_path)
        (tmp_path / "etc" / "shadow").unlink()
        assert migrate.read_accounts(tmp_path)[0].password_hash == ""

    def test_a_root_with_no_passwd_has_no_accounts(self, migrate, tmp_path):
        assert migrate.read_accounts(tmp_path) == []


class TestReadIdentity:
    def test_reads_every_setting_the_wizard_wrote(self, migrate, tmp_path):
        make_source(tmp_path)
        identity = migrate.read_identity(tmp_path)
        assert identity == migrate.Identity(
            hostname="office", locale="en_GB.UTF-8", keyboard="gb", timezone="Europe/London"
        )

    def test_missing_files_leave_fields_empty(self, migrate, tmp_path):
        assert migrate.read_identity(tmp_path) == migrate.Identity()

    def test_the_timezone_falls_back_to_the_localtime_link(self, migrate, tmp_path):
        make_source(tmp_path)
        (tmp_path / "etc" / "timezone").unlink()
        (tmp_path / "etc" / "localtime").symlink_to("/usr/share/zoneinfo/Europe/Prague")
        assert migrate.read_identity(tmp_path).timezone == "Europe/Prague"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -k "ReadAccount or ReadIdentity" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'read_accounts'`.

- [ ] **Step 3: Implement the readers**

Append to `portlin/resources/runtime/migrate.py`:

```python
# Accounts below this are system accounts, and nobody is the one above it.
FIRST_USER_UID = 1000
NOBODY_UID = 65534

# Where the wizard records its two yes/no answers. Both are read as answers
# rather than copied as files, so first boot can put them through its own
# screens and the same visudo check it applies to a fresh answer.
SUDOERS_DROPIN = "etc/sudoers.d/50-portlin-nopasswd"
AUTOLOGIN_CONF = "etc/lightdm/lightdm.conf.d/10-portlin.conf"


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    gecos: str
    shell: str
    home: str  # relative to the root: "home/somebody"
    password_hash: str
    groups: tuple[str, ...]
    sudo_nopasswd: bool
    autologin: bool


@dataclass(frozen=True)
class Identity:
    hostname: str = ""
    locale: str = ""
    keyboard: str = ""
    timezone: str = ""


def _read(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text()
    except OSError:
        return ""


def _shadow_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for line in _read(root, "etc/shadow").splitlines():
        fields = line.split(":")
        if len(fields) >= 2:
            hashes[fields[0]] = fields[1]
    return hashes


def _group_memberships(root: Path) -> dict[str, list[str]]:
    """Group names by member, from the fourth field of /etc/group."""
    memberships: dict[str, list[str]] = {}
    for line in _read(root, "etc/group").splitlines():
        fields = line.split(":")
        if len(fields) < 4:
            continue
        for member in filter(None, fields[3].split(",")):
            memberships.setdefault(member, []).append(fields[0])
    return memberships


def read_accounts(root: Path) -> list[Account]:
    """Every person's account on the source: uid 1000 and up, nobody excluded."""
    hashes = _shadow_hashes(root)
    memberships = _group_memberships(root)
    waiver = _read(root, SUDOERS_DROPIN)
    autologin = _read(root, AUTOLOGIN_CONF)
    accounts = []
    for line in _read(root, "etc/passwd").splitlines():
        fields = line.split(":")
        if len(fields) < 7:
            continue
        name, _, uid, gid, gecos, home, shell = fields[:7]
        try:
            uid_number, gid_number = int(uid), int(gid)
        except ValueError:
            continue
        if uid_number < FIRST_USER_UID or uid_number == NOBODY_UID:
            continue
        accounts.append(
            Account(
                name=name,
                uid=uid_number,
                gid=gid_number,
                gecos=gecos,
                shell=shell,
                home=home.lstrip("/"),
                password_hash=hashes.get(name, ""),
                groups=tuple(memberships.get(name, [])),
                sudo_nopasswd=name in waiver.split(),
                autologin=f"autologin-user={name}" in autologin,
            )
        )
    return accounts


def _shell_value(text: str, key: str) -> str:
    """The value of KEY=value or KEY="value" in a file of shell assignments."""
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"')
    return ""


def read_identity(root: Path) -> Identity:
    timezone = _read(root, "etc/timezone").strip()
    if not timezone:
        try:
            target = os.readlink(root / "etc" / "localtime")
            timezone = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            timezone = ""
    return Identity(
        hostname=_read(root, "etc/hostname").strip(),
        locale=_shell_value(_read(root, "etc/default/locale"), "LANG"),
        keyboard=_shell_value(_read(root, "etc/default/keyboard"), "XKBLAYOUT"),
        timezone=timezone,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): read the account and identity off a portlin root"
```

---

### Task 4: the inventory and the selection

**Files:**
- Modify: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `read_accounts`, `read_identity`, `read_release` from Tasks 2-3; `catalog.Entry`, `catalog.installed`, `catalog.parse_dpkg_status`, `catalog.ENTRIES` from the existing `catalog.py`.
- Produces:
  - `@dataclass(frozen=True) Item(id, category, label, bytes: int = 0, default: bool = True, note: str = "", paths: tuple[str, ...] = (), value: str = "")`
  - `@dataclass(frozen=True) Inventory(version: str, hostname: str, home: str, items: tuple[Item, ...], accounts: tuple[Account, ...] = (), identity: Identity = Identity())`
  - `CATEGORIES: list[tuple[str, str]]` = `[("account", "Account"), ("identity", "Machine identity"), ("home.files", "Home: files"), ("home.settings", "Home: app settings"), ("software", "Software"), ("network", "Network"), ("extras", "System extras")]`
  - constants `SOFTWARE_STATE = "var/lib/portlin/software"`, `CONNECTIONS = "etc/NetworkManager/system-connections"`, `BLUETOOTH = "var/lib/bluetooth"`, `CUPS_PATHS = ("etc/cups/printers.conf", "etc/cups/ppd")`, `XSETTINGS = "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"`, `BACKUP_DIRNAME = ".portlin-migrate-backup"`, `UNTICKED_SETTINGS = frozenset({".cache"})`, `DRIVERS_CATEGORY = "Drivers"`
  - `tree_size(path: Path) -> int`
  - `read_theme_names(root: Path) -> dict[str, str]` with keys `"theme"` and `"icons"` when found
  - `build_inventory(root: Path, *, entries=None, dpkg_status: str = "", firstboot: bool = False) -> Inventory`
  - `to_json(inventory: Inventory) -> str`, `from_json(text: str) -> Inventory`
  - `choose_ids(inventory: Inventory, only: list[str] | None = None, skip: list[str] | None = None) -> list[str]`
  - `selected(inventory: Inventory, ids: list[str]) -> list[Item]`
  - `selected_bytes(inventory: Inventory, ids: list[str]) -> int`
  - `category_label(category: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
def populate_home(home: Path) -> None:
    (home / "Documents").mkdir()
    (home / "Documents" / "notes.txt").write_text("x" * 1000)
    (home / "Downloads").mkdir()
    (home / "Downloads" / "big.iso").write_text("y" * 5000)
    (home / ".config").mkdir()
    (home / ".config" / "app.ini").write_text("z" * 100)
    (home / ".cache").mkdir()
    (home / ".cache" / "junk").write_text("w" * 300)
    (home / ".bashrc").write_text("# rc\n")
    (home / ".portlin-migrate-backup").mkdir()
    (home / ".portlin-migrate-backup" / "old").write_text("gone")
    (home / "link-to-docs").symlink_to("Documents")


class TestInventory:
    @pytest.fixture
    def source(self, tmp_path):
        home = make_source(tmp_path)
        populate_home(home)
        return tmp_path

    def test_home_entries_split_into_files_and_settings(self, migrate, source):
        inventory = migrate.build_inventory(source)
        by_id = {item.id: item for item in inventory.items}
        assert by_id["home.files.Documents"].category == "home.files"
        assert by_id["home.files.Downloads"].paths == ("home/olduser/Downloads",)
        assert by_id["home.settings..config"].category == "home.settings"
        assert by_id["home.settings..bashrc"].paths == ("home/olduser/.bashrc",)

    def test_sizes_are_the_tree_totals(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.files.Documents"].bytes == 1000
        assert by_id["home.files.Downloads"].bytes == 5000
        assert by_id["home.settings..config"].bytes == 100

    def test_a_symlink_counts_as_itself_not_its_target(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.files.link-to-docs"].bytes < 1000

    def test_cache_is_listed_but_unticked_and_the_backup_tree_is_not_listed(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.settings..cache"].default is False
        assert "home.settings..portlin-migrate-backup" not in by_id

    def test_the_account_and_identity_become_items(self, migrate, source):
        inventory = migrate.build_inventory(source)
        by_id = {item.id: item for item in inventory.items}
        assert by_id["account.olduser"].value == "olduser"
        assert by_id["identity.hostname"].value == "office"
        assert by_id["identity.locale"].value == "en_GB.UTF-8"
        assert by_id["identity.keyboard"].value == "gb"
        assert by_id["identity.timezone"].value == "Europe/London"
        assert inventory.accounts[0].name == "olduser"
        assert inventory.identity.hostname == "office"
        assert inventory.version == "0.1.2"
        assert inventory.hostname == "office"
        assert inventory.home == "home/olduser"

    def test_network_and_extras_appear_only_when_present(self, migrate, source):
        assert not [i for i in migrate.build_inventory(source).items if i.category in ("network", "extras")]
        connections = source / "etc/NetworkManager/system-connections"
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        bluetooth = source / "var/lib/bluetooth/AA:BB"
        bluetooth.mkdir(parents=True)
        (bluetooth / "settings").write_text("x")
        (source / "etc/cups").mkdir(parents=True)
        (source / "etc/cups/printers.conf").write_text("p")
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["network"].paths == ("etc/NetworkManager/system-connections",)
        assert by_id["extras.bluetooth"].paths == ("var/lib/bluetooth",)
        # ppd/ does not exist here, so only the file that does is planned.
        assert by_id["extras.printers"].paths == ("etc/cups/printers.conf",)

    def test_theme_names_are_read_out_of_the_overlay(self, migrate, source):
        xsettings = source / migrate.XSETTINGS
        xsettings.parent.mkdir(parents=True)
        xsettings.write_text(
            '<channel name="xsettings">\n'
            '  <property name="ThemeName" type="string" value="Greybird-dark"/>\n'
            '  <property name="IconThemeName" type="string" value="Papirus"/>\n'
            "</channel>\n"
        )
        assert migrate.read_theme_names(source) == {"theme": "Greybird-dark", "icons": "Papirus"}
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["extras.theme"].value == '{"theme": "Greybird-dark", "icons": "Papirus"}'
        assert "Greybird-dark" in by_id["extras.theme"].label

    def test_software_comes_from_records_then_from_the_catalog_check(self, migrate, source):
        catalog = load_tool("catalog.py")
        state = source / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text('{"packages": ["mullvad-vpn"]}')
        # A driver entry recorded on the old machine must be listed but off.
        nvidia = next(e for e in catalog.ENTRIES if e.category == "Drivers")
        (state / f"{nvidia.id}.json").write_text("{}")
        # An entry with no record but a dpkg check that matches.
        by_dpkg = next(e for e in catalog.ENTRIES if e.check.kind == "dpkg" and e.category != "Drivers"
                       and e.id != "mullvad")
        dpkg_status = f"{by_dpkg.check.values[0]} installed\n"
        by_id = {
            item.id: item
            for item in migrate.build_inventory(source, dpkg_status=dpkg_status).items
        }
        assert by_id["software.mullvad"].default is True
        assert by_id[f"software.{nvidia.id}"].default is False
        assert by_id[f"software.{by_dpkg.id}"].value == by_dpkg.id
        assert by_id["software.mullvad"].category == "software"

    def test_first_boot_leaves_software_unticked_because_there_is_no_network(self, migrate, source):
        state = source / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text("{}")
        by_id = {item.id: item for item in migrate.build_inventory(source, firstboot=True).items}
        assert by_id["software.mullvad"].default is False
        assert "network" in by_id["software.mullvad"].note

    def test_items_come_out_in_category_order(self, migrate, source):
        order = [category for category, _ in migrate.CATEGORIES]
        seen = [item.category for item in migrate.build_inventory(source).items]
        assert seen == sorted(seen, key=order.index)

    def test_json_round_trips(self, migrate, source):
        inventory = migrate.build_inventory(source)
        assert migrate.from_json(migrate.to_json(inventory)) == inventory


class TestSelection:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        populate_home(make_source(tmp_path))
        return migrate.build_inventory(tmp_path)

    def test_the_default_plan_is_everything_ticked(self, migrate, inventory):
        ids = migrate.choose_ids(inventory)
        assert "home.files.Documents" in ids
        assert "home.settings..cache" not in ids

    def test_only_narrows_by_id_or_category(self, migrate, inventory):
        assert migrate.choose_ids(inventory, only=["home.files"]) == [
            i.id for i in inventory.items if i.category == "home.files"
        ]
        assert migrate.choose_ids(inventory, only=["home.settings..cache"]) == ["home.settings..cache"]

    def test_skip_removes_by_id_or_category(self, migrate, inventory):
        ids = migrate.choose_ids(inventory, skip=["account", "home.files.Downloads"])
        assert "account.olduser" not in ids
        assert "home.files.Downloads" not in ids
        assert "home.files.Documents" in ids

    def test_selected_bytes_sums_only_what_was_chosen(self, migrate, inventory):
        assert migrate.selected_bytes(inventory, ["home.files.Documents", "identity.hostname"]) == 1000

    def test_selected_keeps_inventory_order(self, migrate, inventory):
        chosen = migrate.selected(inventory, ["home.files.Downloads", "account.olduser"])
        assert [i.id for i in chosen] == ["account.olduser", "home.files.Downloads"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -k "Inventory or Selection" -v`
Expected: FAIL with `AttributeError` on `build_inventory`.

- [ ] **Step 3: Implement the inventory**

Append to `portlin/resources/runtime/migrate.py` (add `import re` and `import dataclasses` at the top with the other imports, and `from catalog import ENTRIES, installed, parse_dpkg_status` after the stdlib imports; the tool puts `/usr/lib/portlin` on `sys.path` before importing this module, and the test loader puts the runtime directory there):

```python
# The order categories are shown in, and their headings.
CATEGORIES = [
    ("account", "Account"),
    ("identity", "Machine identity"),
    ("home.files", "Home: files"),
    ("home.settings", "Home: app settings"),
    ("software", "Software"),
    ("network", "Network"),
    ("extras", "System extras"),
]

SOFTWARE_STATE = "var/lib/portlin/software"
CONNECTIONS = "etc/NetworkManager/system-connections"
BLUETOOTH = "var/lib/bluetooth"
CUPS_PATHS = ("etc/cups/printers.conf", "etc/cups/ppd")
XSETTINGS = "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
DRIVERS_CATEGORY = "Drivers"

# Where replaced files go, and the one home entry never offered: copying it
# would be copying a previous migration's leftovers onto this one's.
BACKUP_DIRNAME = ".portlin-migrate-backup"

# Listed, because it can be large enough to matter, but off by default:
# browsers rebuild it and it is the one part of a home nobody misses.
UNTICKED_SETTINGS = frozenset({".cache"})

FIRSTBOOT_SOFTWARE_NOTE = "needs network; install later from Software"

_XSETTINGS_NAME = re.compile(r'name="(ThemeName|IconThemeName)"[^>]*value="([^"]*)"')


@dataclass(frozen=True)
class Item:
    """One tickable thing. paths are relative to the root, source and target alike."""

    id: str
    category: str
    label: str
    bytes: int = 0
    default: bool = True
    note: str = ""
    paths: tuple[str, ...] = ()
    value: str = ""


@dataclass(frozen=True)
class Inventory:
    version: str
    hostname: str
    home: str
    items: tuple[Item, ...]
    accounts: tuple[Account, ...] = ()
    identity: Identity = field(default_factory=Identity)


def category_label(category: str) -> str:
    return dict(CATEGORIES).get(category, category)


def tree_size(path: Path) -> int:
    """Bytes under a path, links counted as themselves and never followed."""
    try:
        if path.is_symlink() or not path.is_dir():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + dirnames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def read_theme_names(root: Path) -> dict[str, str]:
    names = {}
    for key, value in _XSETTINGS_NAME.findall(_read(root, XSETTINGS)):
        names["theme" if key == "ThemeName" else "icons"] = value
    return names


def _home_items(root: Path, home: str) -> list[Item]:
    try:
        # Plain entries before dot entries, so the items come out in the
        # order the categories are shown.
        entries = sorted((root / home).iterdir(), key=lambda p: (p.name.startswith("."), p.name.lower()))
    except OSError:
        return []
    items = []
    for entry in entries:
        if entry.name == BACKUP_DIRNAME:
            continue
        hidden = entry.name.startswith(".")
        category = "home.settings" if hidden else "home.files"
        items.append(
            Item(
                id=f"{category}.{entry.name}",
                category=category,
                label=entry.name,
                bytes=tree_size(entry),
                default=entry.name not in UNTICKED_SETTINGS,
                paths=(f"{home}/{entry.name}",),
            )
        )
    return items


def _software_items(root: Path, home: str, entries, dpkg_status: str, firstboot: bool) -> list[Item]:
    recorded = {
        path.stem for path in sorted((root / SOFTWARE_STATE).glob("*.json"))
    } if (root / SOFTWARE_STATE).is_dir() else set()
    dpkg = parse_dpkg_status(dpkg_status)
    items = []
    for entry in entries:
        if entry.id not in recorded and not installed(entry, dpkg, root / home):
            continue
        driver = entry.category == DRIVERS_CATEGORY
        items.append(
            Item(
                id=f"software.{entry.id}",
                category="software",
                label=entry.name,
                default=not driver and not firstboot,
                note=(
                    "describes the old machine" if driver
                    else FIRSTBOOT_SOFTWARE_NOTE if firstboot
                    else ""
                ),
                value=entry.id,
            )
        )
    return items


def _system_items(root: Path) -> list[Item]:
    items = []
    connections = root / CONNECTIONS
    if connections.is_dir() and any(connections.iterdir()):
        items.append(Item("network", "network", "Saved network connections",
                          tree_size(connections), paths=(CONNECTIONS,)))
    bluetooth = root / BLUETOOTH
    if bluetooth.is_dir() and any(bluetooth.iterdir()):
        items.append(Item("extras.bluetooth", "extras", "Bluetooth pairings",
                          tree_size(bluetooth), paths=(BLUETOOTH,)))
    cups = tuple(p for p in CUPS_PATHS if (root / p).exists())
    if cups:
        items.append(Item("extras.printers", "extras", "Printers",
                          sum(tree_size(root / p) for p in cups), paths=cups))
    names = read_theme_names(root)
    if names:
        described = ", ".join(filter(None, (names.get("theme"), names.get("icons"))))
        items.append(Item("extras.theme", "extras", f"Desktop theme: {described}",
                          value=json.dumps(names)))
    return items


def build_inventory(root: Path, *, entries=None, dpkg_status: str = "", firstboot: bool = False) -> Inventory:
    """Everything a source has to offer, in the order the checklist shows it."""
    entries = ENTRIES if entries is None else entries
    accounts = read_accounts(root)
    identity = read_identity(root)
    home = accounts[0].home if accounts else ""
    items: list[Item] = [
        Item(f"account.{a.name}", "account", f"{a.name} ({a.gecos.split(',')[0] or a.name})", value=a.name)
        for a in accounts
    ]
    for key, label in (("hostname", "Computer name"), ("locale", "Language"),
                       ("keyboard", "Keyboard"), ("timezone", "Time zone")):
        value = getattr(identity, key)
        if value:
            items.append(Item(f"identity.{key}", "identity", f"{label}: {value}", value=value))
    if home:
        items += _home_items(root, home)
    items += _software_items(root, home, entries, dpkg_status, firstboot)
    items += _system_items(root)
    return Inventory(
        version=read_release(root).get("PORTLIN_VERSION", ""),
        hostname=identity.hostname,
        home=home,
        items=tuple(items),
        accounts=tuple(accounts),
        identity=identity,
    )


def to_json(inventory: Inventory) -> str:
    return json.dumps(dataclasses.asdict(inventory), indent=2) + "\n"


def from_json(text: str) -> Inventory:
    data = json.loads(text)
    return Inventory(
        version=data["version"],
        hostname=data["hostname"],
        home=data["home"],
        items=tuple(Item(**{**item, "paths": tuple(item["paths"])}) for item in data["items"]),
        accounts=tuple(Account(**{**a, "groups": tuple(a["groups"])}) for a in data["accounts"]),
        identity=Identity(**data["identity"]),
    )


def _matches(item: Item, names: list[str]) -> bool:
    return item.id in names or item.category in names


def choose_ids(inventory: Inventory, only: list[str] | None = None, skip: list[str] | None = None) -> list[str]:
    """The ids a plan holds: the defaults, narrowed by --only, thinned by --skip."""
    chosen = []
    for item in inventory.items:
        if only:
            if not _matches(item, only):
                continue
        elif not item.default:
            continue
        if skip and _matches(item, skip):
            continue
        chosen.append(item.id)
    return chosen


def selected(inventory: Inventory, ids: list[str]) -> list[Item]:
    wanted = set(ids)
    return [item for item in inventory.items if item.id in wanted]


def selected_bytes(inventory: Inventory, ids: list[str]) -> int:
    return sum(item.bytes for item in selected(inventory, ids))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): inventory what a portlin root has to offer"
```

---

### Task 5: steps, the rsync plan, progress and the free-space refusal

**Files:**
- Modify: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `Inventory`, `Item`, `selected`, `selected_bytes`, `human`, `BACKUP_DIRNAME` from Task 4; `UNCLAIMED_ADVICE` from the existing `devices.py`.
- Produces:
  - `@dataclass(frozen=True) Step(text: str, argv: tuple[str, ...] | None = None, stdin: str | None = None, progress: str = "", weight: int = 0, move_aside: tuple[tuple[str, str], ...] = (), mkdir: tuple[str, ...] = (), write: tuple[tuple[str, str], ...] = (), passthrough: bool = False, tolerate: tuple[int, ...] = (), optional: bool = False, warn: str | None = None)`
  - `@dataclass(frozen=True) Target(root: Path = Path("/"), user: str = "", uid: int = 0, gid: int = 0, home: str = "")`
  - `SYSTEM_BACKUP_ROOT = "var/backups/portlin-migrate"`, `RSYNC_PARTIAL = (23, 24)`, `EXIT_NO_SPACE = 6`, `SPACE_RESERVE = 512 * 1024**2`
  - `is_home(path: str, home: str) -> bool`, `target_path(path: str, source_home: str, target_home: str) -> str`
  - `backup_dir(target: Target, path: str, stamp: str) -> Path`
  - `rsync_argv(source: Path, target: Path, *, backup_dir: Path, chown: tuple[int, int] | None) -> tuple[str, ...]`
  - `parse_rsync_progress(line: str) -> int | None`
  - `overall_percent(done: int, weight: int, step_percent: int, total: int) -> int`
  - `plan_stick(inventory: Inventory, ids: list[str], source_root: Path, target: Target, stamp: str) -> list[Step]`
  - `shortfall(needed: int, free: int) -> int`, `no_space_message(short: int, unclaimed: int) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
STAMP = "2026-09-21-101500"


class TestPaths:
    def test_target_path_moves_home_entries_into_the_local_home(self, migrate):
        assert migrate.target_path("home/olduser/Documents", "home/olduser", "home/alice") == "home/alice/Documents"
        assert migrate.target_path("home/olduser", "home/olduser", "home/alice") == "home/alice"
        assert migrate.target_path("etc/NetworkManager/system-connections", "home/olduser", "home/alice") == "etc/NetworkManager/system-connections"
        # A sibling that merely starts with the same letters is not inside the home.
        assert migrate.target_path("home/olduser2/x", "home/olduser", "home/alice") == "home/olduser2/x"

    def test_backup_dir_is_under_the_home_for_home_paths_and_var_backups_otherwise(self, migrate, tmp_path):
        target = migrate.Target(root=tmp_path, user="alice", uid=1000, gid=1000, home="home/alice")
        assert migrate.backup_dir(target, "home/alice/.config", STAMP) == (
            tmp_path / "home/alice" / migrate.BACKUP_DIRNAME / STAMP / ".config"
        )
        assert migrate.backup_dir(target, "etc/cups/ppd", STAMP) == (
            tmp_path / migrate.SYSTEM_BACKUP_ROOT / STAMP / "etc/cups/ppd"
        )


class TestRsync:
    def test_a_directory_is_copied_with_trailing_slashes_and_a_backup_dir(self, migrate, tmp_path):
        source = tmp_path / "src" / "Documents"
        source.mkdir(parents=True)
        argv = migrate.rsync_argv(source, tmp_path / "dst" / "Documents",
                                  backup_dir=tmp_path / "bak", chown=(1000, 1000))
        assert argv[:2] == ("rsync", "-a")
        assert "--backup" in argv
        assert f"--backup-dir={tmp_path / 'bak'}" in argv
        assert "--info=progress2" in argv and "--no-inc-recursive" in argv
        assert "--chown=1000:1000" in argv
        assert argv[-2:] == (f"{source}/", f"{tmp_path / 'dst' / 'Documents'}/")
        assert "--delete" not in argv

    def test_a_file_and_a_symlink_are_copied_as_themselves(self, migrate, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text("# rc\n")
        assert migrate.rsync_argv(rc, tmp_path / "out" / ".bashrc", backup_dir=tmp_path / "b", chown=None)[-2:] == (
            str(rc), str(tmp_path / "out" / ".bashrc")
        )
        (tmp_path / "real").mkdir()
        link = tmp_path / "link"
        link.symlink_to("real")
        argv = migrate.rsync_argv(link, tmp_path / "out" / "link", backup_dir=tmp_path / "b", chown=None)
        assert argv[-2:] == (str(link), str(tmp_path / "out" / "link"))
        assert not any(a.startswith("--chown") for a in argv)

    @pytest.mark.parametrize("line,percent", [
        ("      1,234,567  45%   12.34MB/s    0:00:01 (xfr#12, to-chk=34/56)", 45),
        ("  5,000  100%    4.77MB/s    0:00:00 (xfr#3, to-chk=0/4)", 100),
        ("sending incremental file list", None),
        ("rsync: [sender] read errors mapping \"/x\": Permission denied (13)", None),
    ])
    def test_progress_is_read_off_rsyncs_summary_line(self, migrate, line, percent):
        assert migrate.parse_rsync_progress(line) == percent

    def test_overall_percent_weights_the_running_step_by_its_bytes(self, migrate):
        assert migrate.overall_percent(done=0, weight=1000, step_percent=50, total=4000) == 12
        assert migrate.overall_percent(done=3000, weight=1000, step_percent=100, total=4000) == 100
        assert migrate.overall_percent(done=0, weight=0, step_percent=0, total=0) == 100


class TestPlanStick:
    @pytest.fixture
    def parts(self, migrate, tmp_path):
        source = tmp_path / "source"
        home = make_source(source)
        populate_home(home)
        connections = source / migrate.CONNECTIONS
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        inventory = migrate.build_inventory(source)
        target = migrate.Target(root=tmp_path / "target", user="alice", uid=1000, gid=1000, home="home/alice")
        return source, inventory, target

    def test_one_rsync_per_selected_path_in_inventory_order(self, migrate, parts):
        source, inventory, target = parts
        ids = ["network", "home.files.Documents", "home.settings..config"]
        steps = migrate.plan_stick(inventory, ids, source, target, STAMP)
        assert [s.argv[0] for s in steps] == ["rsync", "rsync", "rsync"]
        assert [s.text for s in steps] == [
            "Copying Documents", "Copying .config", "Copying Saved network connections",
        ]

    def test_home_items_land_in_the_local_home_owned_by_the_local_account(self, migrate, parts):
        source, inventory, target = parts
        step = migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)[0]
        assert step.argv[-2:] == (f"{source}/home/olduser/Documents/", f"{target.root}/home/alice/Documents/")
        assert "--chown=1000:1000" in step.argv
        assert f"--backup-dir={target.root}/home/alice/{migrate.BACKUP_DIRNAME}/{STAMP}/Documents" in step.argv
        assert step.mkdir == (f"{target.root}/home/alice",)
        assert step.progress == "rsync"
        assert step.weight == 1000
        assert step.tolerate == migrate.RSYNC_PARTIAL

    def test_system_items_keep_their_path_and_their_owner(self, migrate, parts):
        source, inventory, target = parts
        step = migrate.plan_stick(inventory, ["network"], source, target, STAMP)[0]
        assert step.argv[-1] == f"{target.root}/etc/NetworkManager/system-connections/"
        assert not any(a.startswith("--chown") for a in step.argv)
        assert f"--backup-dir={target.root}/{migrate.SYSTEM_BACKUP_ROOT}/{STAMP}/etc/NetworkManager/system-connections" in step.argv

    def test_items_without_paths_plan_no_rsync(self, migrate, parts):
        source, inventory, target = parts
        assert migrate.plan_stick(inventory, ["identity.hostname", "account.olduser"], source, target, STAMP) == []


class TestSpace:
    def test_shortfall_keeps_a_reserve_for_the_system(self, migrate):
        assert migrate.shortfall(needed=1000, free=1000 + migrate.SPACE_RESERVE) == 0
        assert migrate.shortfall(needed=1000, free=1000) == migrate.SPACE_RESERVE
        assert migrate.shortfall(needed=5_000_000_000, free=1_000_000_000) == 4_000_000_000 + migrate.SPACE_RESERVE

    def test_the_refusal_names_the_gap_and_portlin_expand_when_it_would_help(self, migrate):
        devices = load_tool("devices.py")
        text = migrate.no_space_message(4_000_000_000, unclaimed=20_000_000_000)
        assert "4 GB" in text
        assert devices.UNCLAIMED_ADVICE.format(gb=20.0) in text
        assert "portlin-expand" not in migrate.no_space_message(4_000_000_000, unclaimed=0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -k "Paths or Rsync or PlanStick or Space" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the planner**

Append to `portlin/resources/runtime/migrate.py` (add `from devices import UNCLAIMED_ADVICE` beside the catalog import):

```python
# Replaced system files go here; replaced home files go under the home.
SYSTEM_BACKUP_ROOT = "var/backups/portlin-migrate"

# rsync's "some files could not be transferred" and "some files vanished".
# Both leave everything else copied, and a stick that went through a bad
# shutdown produces one or two. A warning, not a failed migration.
RSYNC_PARTIAL = (23, 24)

EXIT_NO_SPACE = 6
# Left free after the copy, so a full root does not stop apt or the desktop.
SPACE_RESERVE = 512 * 1024**2

_RSYNC_PERCENT = re.compile(r"\s(\d{1,3})%\s")


@dataclass(frozen=True)
class Step:
    """One thing apply does, in order. Data, so a plan can be asserted whole.

    ``move_aside`` renames happen first, then ``mkdir`` and ``write``, then
    ``argv``. ``progress`` names the parser that reads argv's output, and
    ``weight`` is how many of the plan's bytes it accounts for.
    ``passthrough`` means argv speaks the protocol itself and its lines are
    forwarded; ``tolerate`` lists exit codes that are a warning rather than
    a failure, and ``optional`` makes every non-zero exit a warning.
    """

    text: str
    argv: tuple[str, ...] | None = None
    stdin: str | None = None
    progress: str = ""
    weight: int = 0
    move_aside: tuple[tuple[str, str], ...] = ()
    mkdir: tuple[str, ...] = ()
    write: tuple[tuple[str, str], ...] = ()
    passthrough: bool = False
    tolerate: tuple[int, ...] = ()
    optional: bool = False
    warn: str | None = None


@dataclass(frozen=True)
class Target:
    """The running system, as the planner needs to know it."""

    root: Path = Path("/")
    user: str = ""
    uid: int = 0
    gid: int = 0
    home: str = ""  # relative: "home/somebody"


def is_home(path: str, home: str) -> bool:
    return bool(home) and (path == home or path.startswith(home + "/"))


def target_path(path: str, source_home: str, target_home: str) -> str:
    """Where a source path lands: the same place, unless it is under the home."""
    if is_home(path, source_home):
        return target_home + path[len(source_home):]
    return path


def backup_dir(target: Target, path: str, stamp: str) -> Path:
    if is_home(path, target.home):
        relative = path[len(target.home):].lstrip("/")
        return target.root / target.home / BACKUP_DIRNAME / stamp / relative
    return target.root / SYSTEM_BACKUP_ROOT / stamp / path


def rsync_argv(source: Path, target: Path, *, backup_dir: Path, chown: tuple[int, int] | None) -> tuple[str, ...]:
    """One item's copy.

    --backup with --backup-dir is the move-aside: rsync renames a file it is
    about to replace into that tree at the same relative path, which costs
    no space and no second pass. No --delete, so a file that exists only on
    the target survives and a directory on both sides is merged. The
    trailing slashes make rsync copy a directory's contents into the target
    rather than nesting it one level down; a file or a symlink is named as
    itself, and -a keeps a symlink a symlink.
    """
    argv = [
        "rsync", "-a", "--backup", f"--backup-dir={backup_dir}",
        "--info=progress2", "--no-inc-recursive",
    ]
    if chown:
        argv.append(f"--chown={chown[0]}:{chown[1]}")
    src, dst = str(source), str(target)
    if source.is_dir() and not source.is_symlink():
        src += "/"
        dst += "/"
    return (*argv, src, dst)


def parse_rsync_progress(line: str) -> int | None:
    match = _RSYNC_PERCENT.search(line)
    return int(match.group(1)) if match else None


def overall_percent(done: int, weight: int, step_percent: int, total: int) -> int:
    if total <= 0:
        return 100
    return min(100, int((done + weight * step_percent / 100) * 100 / total))


def plan_stick(inventory: Inventory, ids: list[str], source_root: Path, target: Target, stamp: str) -> list[Step]:
    """The rsync per path, in inventory order. Items without paths plan nothing here."""
    steps = []
    for item in selected(inventory, ids):
        for position, path in enumerate(item.paths):
            destination = target_path(path, inventory.home, target.home)
            chown = (target.uid, target.gid) if is_home(destination, target.home) else None
            steps.append(
                Step(
                    text=f"Copying {item.label}",
                    argv=rsync_argv(
                        source_root / path,
                        target.root / destination,
                        backup_dir=backup_dir(target, destination, stamp),
                        chown=chown,
                    ),
                    progress="rsync",
                    # The item's bytes count once, on its first path.
                    weight=item.bytes if position == 0 else 0,
                    mkdir=(str((target.root / destination).parent),),
                    tolerate=RSYNC_PARTIAL,
                )
            )
    return steps


def shortfall(needed: int, free: int) -> int:
    return max(0, needed + SPACE_RESERVE - free)


def no_space_message(short: int, unclaimed: int) -> str:
    text = f"This selection needs {human(short)} more than the drive has free."
    if unclaimed:
        text += " " + UNCLAIMED_ADVICE.format(gb=unclaimed / 1_000_000_000)
    return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): plan a stick-to-stick migration as rsync steps"
```

---

### Task 6: archives: manifest, export and import plans

**Files:**
- Modify: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `Inventory`, `to_json`, `from_json`, `Step`, `Target`, `selected`, `target_path`, `backup_dir`, `is_home` from earlier tasks.
- Produces:
  - `TAR_RECORD_BYTES = 10240`
  - `manifest_text(inventory: Inventory, stamp: str) -> str`, `read_manifest(text: str) -> tuple[Inventory, dict]` (the dict has `"exported"` and `"bytes"`)
  - `archive_name(hostname: str, stamp: str) -> str` giving `office-2026-09-21.portlin-backup.tar.zst`
  - `export_members(inventory: Inventory) -> list[str]`
  - `tar_create_argv(archive: Path, manifest_dir: Path, root: Path, members: list[str]) -> tuple[str, ...]`
  - `tar_list_argv(archive: Path)`, `tar_manifest_argv(archive: Path)`, `tar_extract_argv(archive: Path, root: Path, paths: list[str], *, source_home: str, target_home: str)`
  - `parse_tar_checkpoint(line: str) -> int | None`, `checkpoint_bytes(records: int) -> int`
  - `chosen_members(listing: str, paths: list[str]) -> list[str]`
  - `collisions(members: list[str], source_home: str, target: Target, stamp: str) -> list[tuple[str, str]]`
  - `plan_export(inventory: Inventory, root: Path, archive: Path, manifest_dir: Path, stamp: str) -> list[Step]`
  - `plan_archive(inventory: Inventory, ids: list[str], archive: Path, listing: str, target: Target, stamp: str) -> list[Step]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
class TestArchive:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        source = tmp_path / "source"
        populate_home(make_source(source))
        return migrate.build_inventory(source)

    def test_the_manifest_carries_the_inventory_and_round_trips(self, migrate, inventory):
        text = migrate.manifest_text(inventory, STAMP)
        restored, meta = migrate.read_manifest(text)
        assert restored == inventory
        assert meta["exported"] == STAMP
        assert meta["bytes"] == sum(i.bytes for i in inventory.items)

    def test_the_archive_is_named_for_the_host_and_the_day(self, migrate):
        assert migrate.archive_name("office", STAMP) == "office-2026-09-21.portlin-backup.tar.zst"

    def test_export_takes_every_path_including_the_unticked_ones(self, migrate, inventory):
        members = migrate.export_members(inventory)
        assert "home/olduser/.cache" in members
        assert "home/olduser/Documents" in members
        assert all("/" in m for m in members)

    def test_tar_create_writes_the_manifest_first_from_its_own_directory(self, migrate, tmp_path):
        argv = migrate.tar_create_argv(tmp_path / "a.tar.zst", tmp_path / "m", Path("/"), ["home/x/Documents"])
        assert argv[:3] == ("tar", "-I", "zstd -T0 -3")
        assert "--checkpoint-action=echo" in argv
        first = argv.index("-C")
        assert argv[first:first + 5] == ("-C", str(tmp_path / "m"), migrate.MANIFEST, "-C", "/")
        assert argv[-1] == "home/x/Documents"

    def test_plan_export_writes_the_manifest_then_archives(self, migrate, inventory, tmp_path):
        steps = migrate.plan_export(inventory, Path("/"), tmp_path / "out.tar.zst", tmp_path / "m", STAMP)
        assert steps[0].write[0][0] == str(tmp_path / "m" / migrate.MANIFEST)
        assert steps[0].mkdir == (str(tmp_path / "m"),)
        assert steps[1].argv[0] == "tar" and steps[1].progress == "tar"
        assert steps[1].weight == sum(i.bytes for i in inventory.items)
        assert steps[1].tolerate == (1,)

    @pytest.mark.parametrize("line,records", [
        ("tar: Write checkpoint 2000", 2000),
        ("tar: Read checkpoint 4000", 4000),
        ("home/x/Documents/notes.txt", None),
    ])
    def test_checkpoints_are_read_as_the_build_reads_them(self, migrate, line, records):
        assert migrate.parse_tar_checkpoint(line) == records
        assert migrate.checkpoint_bytes(2000) == 2000 * migrate.TAR_RECORD_BYTES

    def test_reading_the_manifest_extracts_that_member_to_stdout(self, migrate):
        assert migrate.tar_manifest_argv(Path("/a.tar.zst")) == ("tar", "-I", "zstd", "-xOf", "/a.tar.zst", "manifest.json")
        assert migrate.tar_list_argv(Path("/a.tar.zst")) == ("tar", "-I", "zstd", "-tf", "/a.tar.zst")


LISTING = "\n".join([
    "manifest.json",
    "home/olduser/Documents/",
    "home/olduser/Documents/notes.txt",
    "home/olduser/Downloads/",
    "home/olduser/Downloads/big.iso",
    "home/olduser/.config/",
    "home/olduser/.config/app.ini",
    "home/olduser/.bashrc",
    "etc/NetworkManager/system-connections/",
    "etc/NetworkManager/system-connections/cafe.nmconnection",
    "",
])


class TestPlanArchive:
    def test_chosen_members_are_the_files_under_the_chosen_paths(self, migrate):
        members = migrate.chosen_members(LISTING, ["home/olduser/Documents", "home/olduser/.bashrc"])
        assert members == ["home/olduser/Documents/notes.txt", "home/olduser/.bashrc"]

    def test_collisions_are_the_members_that_already_exist_on_the_target(self, migrate, tmp_path):
        target = migrate.Target(root=tmp_path, user="alice", uid=1000, gid=1000, home="home/alice")
        (tmp_path / "home/alice/Documents").mkdir(parents=True)
        (tmp_path / "home/alice/Documents/notes.txt").write_text("mine")
        (tmp_path / "etc/NetworkManager/system-connections").mkdir(parents=True)
        (tmp_path / "etc/NetworkManager/system-connections/cafe.nmconnection").write_text("old")
        members = [
            "home/olduser/Documents/notes.txt", "home/olduser/.bashrc",
            "etc/NetworkManager/system-connections/cafe.nmconnection",
        ]
        moves = migrate.collisions(members, "home/olduser", target, STAMP)
        assert moves == [
            (str(tmp_path / "home/alice/Documents/notes.txt"),
             str(tmp_path / "home/alice" / migrate.BACKUP_DIRNAME / STAMP / "Documents/notes.txt")),
            (str(tmp_path / "etc/NetworkManager/system-connections/cafe.nmconnection"),
             str(tmp_path / migrate.SYSTEM_BACKUP_ROOT / STAMP / "etc/NetworkManager/system-connections/cafe.nmconnection")),
        ]

    def test_extract_renames_the_home_and_never_keeps_the_archives_owners(self, migrate):
        argv = migrate.tar_extract_argv(
            Path("/a.tar.zst"), Path("/"), ["home/olduser/Documents", "etc/NetworkManager/system-connections"],
            source_home="home/olduser", target_home="home/alice",
        )
        assert "--no-same-owner" in argv
        assert "--transform=s|^home/olduser/|home/alice/|" in argv
        assert argv[argv.index("-C") + 1] == "/"
        assert argv[-2:] == ("home/olduser/Documents", "etc/NetworkManager/system-connections")
        same = migrate.tar_extract_argv(Path("/a.tar.zst"), Path("/"), ["home/x/D"], source_home="home/x", target_home="home/x")
        assert not any(a.startswith("--transform") for a in same)

    def test_the_plan_moves_aside_then_extracts_then_chowns_each_home_item(self, migrate, tmp_path):
        inventory, _ = migrate.read_manifest(migrate.manifest_text(
            migrate.build_inventory(_archive_source(migrate, tmp_path / "source")), STAMP
        ))
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        (tmp_path / "t/home/alice/Documents").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents/notes.txt").write_text("mine")
        steps = migrate.plan_archive(
            inventory, ["home.files.Documents", "home.settings..bashrc", "network"],
            tmp_path / "a.tar.zst", LISTING, target, STAMP,
        )
        extract, chown_docs, chown_rc = steps
        assert extract.argv[0] == "tar" and extract.progress == "tar"
        assert extract.move_aside == (
            (str(tmp_path / "t/home/alice/Documents/notes.txt"),
             str(tmp_path / "t/home/alice" / migrate.BACKUP_DIRNAME / STAMP / "Documents/notes.txt")),
        )
        assert extract.weight == sum(i.bytes for i in migrate.selected(inventory, ["home.files.Documents", "home.settings..bashrc", "network"]))
        assert extract.argv[-3:] == ("home/olduser/Documents", "home/olduser/.bashrc", "etc/NetworkManager/system-connections")
        assert chown_docs.argv == ("chown", "-R", "-h", "1000:1000", str(tmp_path / "t/home/alice/Documents"))
        assert chown_rc.argv == ("chown", "-R", "-h", "1000:1000", str(tmp_path / "t/home/alice/.bashrc"))


def _archive_source(migrate, root: Path) -> Path:
    populate_home(make_source(root))
    connections = root / migrate.CONNECTIONS
    connections.mkdir(parents=True)
    (connections / "cafe.nmconnection").write_text("[wifi]\n")
    return root
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -k "Archive" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the archive planners**

Append to `portlin/resources/runtime/migrate.py`:

```python
# tar's default blocking factor: 20 records of 512 bytes per checkpoint unit.
TAR_RECORD_BYTES = 10240
_TAR_CHECKPOINT = re.compile(r"^tar: (?:Write|Read) checkpoint (\d+)")
# Every 2000 records is every 20 MB, the cadence build_rootfs uses.
_TAR_PROGRESS = ("--checkpoint=2000", "--checkpoint-action=echo")


def manifest_text(inventory: Inventory, stamp: str) -> str:
    return json.dumps(
        {
            "inventory": dataclasses.asdict(inventory),
            "exported": stamp,
            "bytes": sum(item.bytes for item in inventory.items),
        },
        indent=2,
    ) + "\n"


def read_manifest(text: str) -> tuple[Inventory, dict]:
    data = json.loads(text)
    inventory = from_json(json.dumps(data["inventory"]))
    return inventory, {"exported": data.get("exported", ""), "bytes": data.get("bytes", 0)}


def archive_name(hostname: str, stamp: str) -> str:
    return f"{hostname or 'portlin'}-{stamp[:10]}{ARCHIVE_SUFFIX}"


def export_members(inventory: Inventory) -> list[str]:
    """Everything with a path. An export is a backup, so nothing is left out."""
    return [path for item in inventory.items for path in item.paths]


def tar_create_argv(archive: Path, manifest_dir: Path, root: Path, members: list[str]) -> tuple[str, ...]:
    """Level 3 rather than the rootfs's 6: a home is mostly media and documents
    that are compressed already, where a higher level costs time and saves
    nothing. The manifest is named first, from its own directory, so it is
    member 0 and can be read without unpacking the rest."""
    return (
        "tar", "-I", "zstd -T0 -3", *_TAR_PROGRESS,
        "-cf", str(archive),
        "-C", str(manifest_dir), MANIFEST,
        "-C", str(root), *members,
    )


def tar_list_argv(archive: Path) -> tuple[str, ...]:
    return ("tar", "-I", "zstd", "-tf", str(archive))


def tar_manifest_argv(archive: Path) -> tuple[str, ...]:
    return ("tar", "-I", "zstd", "-xOf", str(archive), MANIFEST)


def tar_extract_argv(archive: Path, root: Path, paths: list[str], *, source_home: str, target_home: str) -> tuple[str, ...]:
    """--no-same-owner because the archive's uids belong to another stick;
    a chown step gives home items to the local account afterwards."""
    argv = ["tar", "-I", "zstd", *_TAR_PROGRESS, "--no-same-owner", "-xf", str(archive), "-C", str(root)]
    if source_home and source_home != target_home:
        argv.append(f"--transform=s|^{source_home}/|{target_home}/|")
    return (*argv, *paths)


def parse_tar_checkpoint(line: str) -> int | None:
    match = _TAR_CHECKPOINT.match(line.strip())
    return int(match.group(1)) if match else None


def checkpoint_bytes(records: int) -> int:
    return records * TAR_RECORD_BYTES


def chosen_members(listing: str, paths: list[str]) -> list[str]:
    """The file members under any chosen path. Directories end in a slash in
    tar's listing and are left out: they merge rather than collide."""
    members = []
    for name in listing.splitlines():
        if not name or name.endswith("/"):
            continue
        if any(name == path or name.startswith(path + "/") for path in paths):
            members.append(name)
    return members


def collisions(members: list[str], source_home: str, target: Target, stamp: str) -> list[tuple[str, str]]:
    """(existing, backup) for every member that would land on a file already there."""
    moves = []
    for member in members:
        destination = target_path(member, source_home, target.home)
        existing = target.root / destination
        if existing.is_symlink() or existing.exists():
            backup = backup_dir(target, destination, stamp)
            moves.append((str(existing), str(backup)))
    return moves


def plan_export(inventory: Inventory, root: Path, archive: Path, manifest_dir: Path, stamp: str) -> list[Step]:
    return [
        Step(
            "Writing the manifest",
            mkdir=(str(manifest_dir),),
            write=((str(manifest_dir / MANIFEST), manifest_text(inventory, stamp)),),
        ),
        Step(
            f"Archiving to {archive.name}",
            argv=tar_create_argv(archive, manifest_dir, root, export_members(inventory)),
            progress="tar",
            weight=sum(item.bytes for item in inventory.items),
            # 1 is "some files changed while being read", which on a running
            # system a browser's cache does constantly. The archive is whole.
            tolerate=(1,),
        ),
    ]


def plan_archive(inventory: Inventory, ids: list[str], archive: Path, listing: str, target: Target, stamp: str) -> list[Step]:
    """Move collisions aside, extract everything chosen in one pass, then
    give each home item to the local account. Bytes are written once."""
    items = [item for item in selected(inventory, ids) if item.paths]
    paths = [path for item in items for path in item.paths]
    if not paths:
        return []
    steps = [
        Step(
            "Restoring from the archive",
            argv=tar_extract_argv(archive, target.root, paths,
                                  source_home=inventory.home, target_home=target.home),
            progress="tar",
            weight=sum(item.bytes for item in items),
            move_aside=tuple(collisions(chosen_members(listing, paths), inventory.home, target, stamp)),
        )
    ]
    for item in items:
        for path in item.paths:
            destination = target_path(path, inventory.home, target.home)
            if is_home(destination, target.home):
                steps.append(
                    Step(
                        f"Setting the owner of {item.label}",
                        argv=("chown", "-R", "-h", f"{target.uid}:{target.gid}", str(target.root / destination)),
                    )
                )
    return steps
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): plan exports and archive restores as tar steps"
```

---

### Task 7: account, identity, theme and software steps

**Files:**
- Modify: `portlin/resources/runtime/migrate.py`
- Test: `tests/test_migrate.py`, `tests/test_firstboot.py`

**Interfaces:**
- Consumes: `Account`, `Identity`, `Step`, `Target` from earlier tasks.
- Produces:
  - `INSTALLER = "/usr/bin/portlin-install"`
  - `THEME_TARGETS`, `ICON_THEME_TARGETS`: dicts identical to the wizard's
  - `account_steps(account: Account, *, existing_groups: set[str], uid_free: bool) -> list[Step]`
  - `identity_steps(identity: Identity, ids: list[str], *, hosts_text: str) -> list[Step]`
  - `rewrite_theme(text: str, pattern: str, replacement: str, theme: str) -> str | None`
  - `theme_steps(names: dict[str, str], target: Target, *, icon_theme_installed: bool) -> list[Step]`
  - `software_steps(ids: list[str]) -> list[Step]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
class TestAccountSteps:
    def account(self, migrate, **overrides):
        base = dict(name="olduser", uid=1500, gid=1500, gecos="Old User,,,", shell="/bin/bash",
                    home="home/olduser", password_hash="$y$j9T$abc$def",
                    groups=("sudo", "audio", "scanner", "lpadmin"), sudo_nopasswd=True, autologin=False)
        return migrate.Account(**{**base, **overrides})

    def test_the_account_is_recreated_with_its_ids_and_the_groups_that_exist_here(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups={"sudo", "audio", "video"}, uid_free=True)
        assert steps[0].argv == ("groupadd", "-f", "-g", "1500", "olduser")
        assert steps[1].argv == (
            "useradd", "--create-home", "--shell", "/bin/bash", "--comment", "Old User,,,",
            "--groups", "sudo,audio", "--uid", "1500", "--gid", "1500", "olduser",
        )

    def test_the_password_moves_as_its_hash_on_stdin(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups={"sudo"}, uid_free=True)
        last = steps[-1]
        assert last.argv == ("chpasswd", "-e")
        assert last.stdin == "olduser:$y$j9T$abc$def\n"
        assert "$y$" not in last.text

    def test_a_taken_uid_lets_useradd_pick_and_says_so(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups=set(), uid_free=False)
        assert steps[0].argv[0] == "useradd"
        assert "--uid" not in steps[0].argv and "--user-group" in steps[0].argv
        assert steps[0].warn is None
        assert any("1500" in (s.warn or "") for s in steps)

    def test_no_hash_means_no_password_step_and_a_warning(self, migrate):
        steps = migrate.account_steps(self.account(migrate, password_hash=""), existing_groups=set(), uid_free=True)
        assert not any(s.argv and s.argv[0] == "chpasswd" for s in steps)
        assert any("password" in (s.warn or "") for s in steps)


class TestIdentitySteps:
    def test_hostname_rewrites_hosts_and_asks_hostnamectl_optionally(self, migrate):
        hosts = "127.0.0.1\tlocalhost\n127.0.1.1\tportlin\n::1\tlocalhost ip6-localhost\n"
        steps = migrate.identity_steps(migrate.Identity(hostname="office"), ["identity.hostname"], hosts_text=hosts)
        step = steps[0]
        assert ("/etc/hostname", "office\n") in step.write
        assert ("/etc/hosts", "127.0.0.1\tlocalhost\n127.0.1.1\toffice\n::1\tlocalhost ip6-localhost\n") in step.write
        assert step.argv == ("hostnamectl", "set-hostname", "office")
        assert step.optional is True

    def test_locale_generates_then_records(self, migrate):
        steps = migrate.identity_steps(migrate.Identity(locale="en_GB.UTF-8"), ["identity.locale"], hosts_text="")
        gen, record = steps
        assert gen.write == (("/etc/locale.gen", "en_GB.UTF-8 UTF-8\nC.UTF-8 UTF-8\n"),)
        assert gen.argv == ("locale-gen",)
        assert record.write == (("/etc/default/locale", 'LANG="en_GB.UTF-8"\n'),)
        assert record.argv == ("localectl", "set-locale", "LANG=en_GB.UTF-8") and record.optional

    def test_keyboard_and_timezone(self, migrate):
        steps = migrate.identity_steps(
            migrate.Identity(keyboard="gb", timezone="Europe/London"),
            ["identity.keyboard", "identity.timezone"], hosts_text="",
        )
        keyboard, setupcon, link, tz = steps
        assert keyboard.write[0][0] == "/etc/default/keyboard"
        assert 'XKBLAYOUT="gb"' in keyboard.write[0][1]
        assert keyboard.argv == ("setupcon", "--save") and keyboard.optional
        assert setupcon.argv == ("localectl", "set-x11-keymap", "gb") and setupcon.optional
        assert link.write == (("/etc/timezone", "Europe/London\n"),)
        assert link.argv == ("ln", "-sf", "/usr/share/zoneinfo/Europe/London", "/etc/localtime")
        assert tz.argv == ("timedatectl", "set-timezone", "Europe/London") and tz.optional

    def test_only_the_chosen_settings_are_applied(self, migrate):
        identity = migrate.Identity(hostname="office", locale="en_GB.UTF-8", keyboard="gb", timezone="Europe/London")
        assert migrate.identity_steps(identity, ["identity.keyboard"], hosts_text="")[0].write[0][0] == "/etc/default/keyboard"
        assert migrate.identity_steps(identity, [], hosts_text="") == []


class TestThemeSteps:
    def test_targets_match_the_wizards(self, migrate):
        from test_firstboot import WIZARD, module_constant
        assert migrate.THEME_TARGETS == module_constant(WIZARD, "THEME_TARGETS")
        assert migrate.ICON_THEME_TARGETS == module_constant(WIZARD, "ICON_THEME_TARGETS")

    def test_rewrite_returns_none_when_the_file_does_not_take_the_name(self, migrate):
        pattern, replacement = migrate.THEME_TARGETS["/etc/xdg/xdg-portlin/gtk-3.0/settings.ini"]
        assert migrate.rewrite_theme("[Settings]\ngtk-theme-name=Numix\n", pattern, replacement, "Greybird-dark") == (
            "[Settings]\ngtk-theme-name=Greybird-dark\n"
        )
        assert migrate.rewrite_theme("[Settings]\n", pattern, replacement, "Greybird-dark") is None

    def test_every_target_file_is_rewritten_or_none_is(self, migrate, tmp_path):
        for path in {**migrate.THEME_TARGETS, **migrate.ICON_THEME_TARGETS}:
            file = tmp_path / path.lstrip("/")
            file.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml").write_text(
            '<property name="ThemeName" type="string" value="Numix"/>\n'
            '<property name="IconThemeName" type="string" value="Papirus-Dark"/>\n'
        )
        (tmp_path / "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml").write_text(
            '<property name="theme" type="string" value="Numix"/>\n'
        )
        (tmp_path / "etc/xdg/xdg-portlin/gtk-3.0/settings.ini").write_text(
            "gtk-theme-name=Numix\ngtk-icon-theme-name=Papirus-Dark\n"
        )
        (tmp_path / "etc/xdg/xdg-portlin/gtk-4.0/settings.ini").write_text("gtk-icon-theme-name=Papirus-Dark\n")
        (tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf").write_text(
            "theme-name=Numix\nicon-theme-name=Papirus-Dark\n"
        )
        target = migrate.Target(root=tmp_path)
        steps = migrate.theme_steps({"theme": "Greybird-dark", "icons": "Papirus"}, target, icon_theme_installed=True)
        written = {path: text for step in steps for path, text in step.write}
        assert written[str(tmp_path / "etc/xdg/xdg-portlin/gtk-3.0/settings.ini")] == (
            "gtk-theme-name=Greybird-dark\ngtk-icon-theme-name=Papirus\n"
        )
        assert "Greybird-dark" in written[str(tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf")]
        assert "Papirus\n" in written[str(tmp_path / "etc/xdg/xdg-portlin/gtk-4.0/settings.ini")]
        # A greeter file that does not take the name means the widget theme is
        # not applied anywhere: three of four is worse than none.
        (tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf").write_text("nothing\n")
        steps = migrate.theme_steps({"theme": "Greybird-dark"}, target, icon_theme_installed=True)
        assert [s.write for s in steps] == [()]
        assert "Greybird-dark" in steps[0].warn

    def test_an_icon_theme_not_installed_here_is_left_alone(self, migrate, tmp_path):
        steps = migrate.theme_steps({"icons": "Numix-Circle"}, migrate.Target(root=tmp_path), icon_theme_installed=False)
        assert [s.write for s in steps] == [()]
        assert "Numix-Circle" in steps[0].warn


class TestSoftwareSteps:
    def test_each_entry_is_installed_through_the_installer_and_may_fail(self, migrate):
        steps = migrate.software_steps(["mullvad", "vlc"])
        assert [s.argv for s in steps] == [
            (migrate.INSTALLER, "install", "mullvad"),
            (migrate.INSTALLER, "install", "vlc"),
        ]
        assert all(s.passthrough and s.optional for s in steps)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -k "AccountSteps or IdentitySteps or ThemeSteps or SoftwareSteps" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the appliers**

Append to `portlin/resources/runtime/migrate.py`:

```python
INSTALLER = "/usr/bin/portlin-install"

# Copies of the wizard's THEME_TARGETS and ICON_THEME_TARGETS. The wizard is
# frozen at write time and cannot import this module, so both hold the same
# tables and a unit test keeps them equal. See the tier rule in the runtime
# updates design for why that duplication is accepted.
THEME_TARGETS = {
    "/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml": (
        r'(<property name="ThemeName" type="string" value=")[^"]*"',
        r'\g<1>{theme}"',
    ),
    "/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml": (
        r'(<property name="theme" type="string" value=")[^"]*"',
        r'\g<1>{theme}"',
    ),
    "/etc/xdg/xdg-portlin/gtk-3.0/settings.ini": (
        r"(?m)^(gtk-theme-name=).*$",
        r"\g<1>{theme}",
    ),
    "/etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf": (
        r"(?m)^(theme-name=).*$",
        r"\g<1>{theme}",
    ),
}

ICON_THEME_TARGETS = {
    "/etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml": (
        r'(<property name="IconThemeName" type="string" value=")[^"]*"',
        r'\g<1>{theme}"',
    ),
    "/etc/xdg/xdg-portlin/gtk-3.0/settings.ini": (
        r"(?m)^(gtk-icon-theme-name=).*$",
        r"\g<1>{theme}",
    ),
    "/etc/xdg/xdg-portlin/gtk-4.0/settings.ini": (
        r"(?m)^(gtk-icon-theme-name=).*$",
        r"\g<1>{theme}",
    ),
    "/etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf": (
        r"(?m)^(icon-theme-name=).*$",
        r"\g<1>{theme}",
    ),
}


def account_steps(account: Account, *, existing_groups: set[str], uid_free: bool) -> list[Step]:
    """Recreate the account. The password travels as its hash through
    chpasswd -e, so nothing here ever holds or shows it in clear."""
    groups = [g for g in account.groups if g in existing_groups]
    steps: list[Step] = []
    useradd = [
        "useradd", "--create-home", "--shell", account.shell, "--comment", account.gecos,
        "--groups", ",".join(groups),
    ]
    if uid_free:
        steps.append(Step(f"Creating the group {account.name}",
                          argv=("groupadd", "-f", "-g", str(account.gid), account.name)))
        useradd += ["--uid", str(account.uid), "--gid", str(account.gid)]
    else:
        useradd.append("--user-group")
    steps.append(Step(f"Creating the account {account.name}", argv=(*useradd, account.name)))
    if not uid_free:
        steps.append(Step("", warn=f"uid {account.uid} is already in use here, so {account.name} was given a new one"))
    if account.password_hash:
        steps.append(Step(f"Setting the password for {account.name}", argv=("chpasswd", "-e"),
                          stdin=f"{account.name}:{account.password_hash}\n"))
    else:
        steps.append(Step("", warn=f"the source's password for {account.name} could not be read; set one with passwd"))
    return steps


def identity_steps(identity: Identity, ids: list[str], *, hosts_text: str) -> list[Step]:
    """The wizard's apply_hostname, apply_locale, apply_keyboard and
    apply_timezone as data. The systemd calls are optional because the
    files are what persist; the calls only make the change take effect now."""
    steps: list[Step] = []
    if "identity.hostname" in ids and identity.hostname:
        lines = [line for line in hosts_text.splitlines() if not line.startswith("127.0.1.1")]
        lines.insert(min(1, len(lines)), f"127.0.1.1\t{identity.hostname}")
        steps.append(Step(
            f"Naming this computer {identity.hostname}",
            write=(("/etc/hostname", f"{identity.hostname}\n"), ("/etc/hosts", "\n".join(lines) + "\n")),
            argv=("hostnamectl", "set-hostname", identity.hostname),
            optional=True,
        ))
    if "identity.locale" in ids and identity.locale:
        steps.append(Step(f"Generating the locale {identity.locale}",
                          write=(("/etc/locale.gen", f"{identity.locale} UTF-8\nC.UTF-8 UTF-8\n"),),
                          argv=("locale-gen",)))
        steps.append(Step("Recording the language",
                          write=(("/etc/default/locale", f'LANG="{identity.locale}"\n'),),
                          argv=("localectl", "set-locale", f"LANG={identity.locale}"), optional=True))
    if "identity.keyboard" in ids and identity.keyboard:
        keyboard = "\n".join([
            "XKBMODEL=pc105", f'XKBLAYOUT="{identity.keyboard}"', 'XKBVARIANT=""',
            'XKBOPTIONS=""', 'BACKSPACE="guess"', "",
        ])
        steps.append(Step(f"Setting the keyboard layout to {identity.keyboard}",
                          write=(("/etc/default/keyboard", keyboard),),
                          argv=("setupcon", "--save"), optional=True))
        steps.append(Step("Recording the keyboard layout for X",
                          argv=("localectl", "set-x11-keymap", identity.keyboard), optional=True))
    if "identity.timezone" in ids and identity.timezone:
        steps.append(Step(f"Setting the time zone to {identity.timezone}",
                          write=(("/etc/timezone", f"{identity.timezone}\n"),),
                          argv=("ln", "-sf", f"/usr/share/zoneinfo/{identity.timezone}", "/etc/localtime")))
        steps.append(Step("Telling systemd the time zone",
                          argv=("timedatectl", "set-timezone", identity.timezone), optional=True))
    return steps


def rewrite_theme(text: str, pattern: str, replacement: str, theme: str) -> str | None:
    rewritten = re.sub(pattern, replacement.format(theme=theme), text, count=1)
    return rewritten if theme in rewritten else None


def theme_steps(names: dict[str, str], target: Target, *, icon_theme_installed: bool) -> list[Step]:
    """Rewrite every file that names a theme, or none of them.

    Rewriting three of four is worse than rewriting none: the greeter or the
    window borders keep the old name and the desktop looks broken. Three of
    the files name both the widget theme and the icon theme, so both
    rewrites work on one set of contents and land in one write, rather than
    the second reading the file from disk and undoing the first.
    """
    contents: dict[str, str] = {}

    def rewrite_all(theme: str, targets: dict) -> bool:
        pending = {}
        for path, (pattern, replacement) in targets.items():
            file = target.root / path.lstrip("/")
            key = str(file)
            text = contents.get(key)
            if text is None:
                try:
                    text = file.read_text()
                except OSError:
                    return False
            rewritten = rewrite_theme(text, pattern, replacement, theme)
            if rewritten is None:
                return False
            pending[key] = rewritten
        contents.update(pending)
        return True

    steps = []
    applied = []
    theme = names.get("theme")
    if theme:
        if rewrite_all(theme, THEME_TARGETS):
            applied.append(f"the desktop theme {theme}")
        else:
            steps.append(Step("", warn=f"the desktop theme {theme} could not be applied here"))
    icons = names.get("icons")
    if icons:
        if icon_theme_installed and rewrite_all(icons, ICON_THEME_TARGETS):
            applied.append(f"the icon theme {icons}")
        else:
            steps.append(Step("", warn=f"the icon theme {icons} is not installed here, so the default stays"))
    if applied:
        steps.append(Step("Applying " + " and ".join(applied), write=tuple(contents.items())))
    return steps


def software_steps(ids: list[str]) -> list[Step]:
    """One installer run per entry. It speaks the protocol itself, and a
    failure is a warning: the summary says what to retry from Software."""
    return [
        Step(f"Installing {entry_id}", argv=(INSTALLER, "install", entry_id), passthrough=True, optional=True)
        for entry_id in ids
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate.py tests/test_firstboot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/migrate.py tests/test_migrate.py
git commit -m "feat(runtime): plan the account, identity, theme and software steps"
```

---

### Task 8: `portlin-migrate`: opening sources, running steps, the non-interactive verbs

**Files:**
- Create: `portlin/resources/runtime/portlin-migrate`
- Modify: `portlin/resources/runtime/migrate.py` (one helper, `dpkg_status_lines`)
- Test: `tests/test_migrate_tool.py`, `tests/test_migrate.py`

**Interfaces:**
- Consumes: everything `migrate.py` exports; `devices.command_output`, `devices.root_source`, `devices.backing_partition`, `devices.backing_disk`, `devices.disk_tail_bytes`, `devices.unclaimed_bytes`.
- Produces (in `migrate.py`): `dpkg_status_lines(status_file: str) -> str` turning `/var/lib/dpkg/status` text into the `"<package> installed"` lines `parse_dpkg_status` reads; `mark_existing_account(inventory: Inventory, local_user: str) -> Inventory` which, when `local_user` is set, unticks every account item and notes that the account exists.
- Produces (in the tool, importable through `load_tool("portlin-migrate")`):
  - constants `MOUNT = Path("/run/portlin/migrate/source")`, `MAPPING = "portlin-migrate-source"`, `PLAN_DIR = Path("/run/portlin/migrate")`, `LOG = Path("/var/log/portlin-migrate.log")`, `MEDIA_ROOTS = ("/media", "/run/media")`, `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_PRIVILEGE = 3`, `EXIT_NO_SPACE = 6`, `WRONG_PASSPHRASE = 2`, `PASSPHRASE_TRIES = 3`
  - `emit(kind, *parts, out=None)`, `format_event(kind, *parts) -> str`
  - `open_argvs(device: str, encrypted: bool) -> list[tuple[str, ...]]`, `close_argvs(encrypted: bool) -> list[tuple[str, ...]]`
  - `@dataclass Source(kind: str, path: str, encrypted: bool = False, root: Path | None = None, listing: str = "")`
  - `open_source(path: str, *, passphrase: Callable[[], str], run=subprocess.run) -> Source`, `close_source(run=subprocess.run) -> None`
  - `source_inventory(source: Source, *, firstboot: bool, run=subprocess.run) -> Inventory`
  - `@dataclass(frozen=True) RunResult(ok: bool, warnings: tuple[str, ...], failure: str = "")`
  - `move_aside(existing: str, backup: str) -> None`
  - `run_steps(steps, *, total: int, out=None, execute=None) -> RunResult` where `execute(step, on_line) -> int`
  - `apply_steps(inventory, ids, source: Source, target: Target, stamp: str, *, firstboot: bool, hosts_text: str, icon_theme_installed: Callable[[str], bool]) -> list[Step]`
  - `write_plan(path: Path, *, source: Source, ids: list[str], inventory, firstboot: bool, label: str) -> None`, `read_plan(path: Path) -> dict`
  - `local_target(environ=os.environ) -> Target`
  - `main(argv=None) -> int` with verbs `candidates`, `inventory`, `apply`, `export`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate.py`:

```python
class TestExistingAccount:
    def test_after_setup_the_account_item_is_off_and_says_why(self, migrate, tmp_path):
        inventory = migrate.build_inventory(make_source(tmp_path).parent.parent)
        marked = migrate.mark_existing_account(inventory, "alice")
        item = next(i for i in marked.items if i.category == "account")
        assert item.default is False
        assert "alice" in item.note and "already" in item.note
        assert migrate.mark_existing_account(inventory, "") == inventory


class TestDpkgStatus:
    def test_the_status_file_becomes_the_lines_the_catalog_check_reads(self, migrate):
        text = (
            "Package: vlc\nStatus: install ok installed\nVersion: 3\n\n"
            "Package: gone\nStatus: deinstall ok config-files\n\n"
            "Package: half\nStatus: install ok unpacked\n\n"
        )
        assert migrate.dpkg_status_lines(text) == "vlc installed\ngone config-files\nhalf unpacked\n"
```

Create `tests/test_migrate_tool.py`:

```python
"""portlin-migrate's own seams: how it opens a source, and how it runs a plan.

The planners are covered in test_migrate.py. What is left is the executor,
which is where the protocol is spoken, the backup renames happen, and a
failing command either stops the run or becomes a warning. All of it runs
here against a fake execute() that feeds canned rsync and tar output, on a
machine with neither.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def tool():
    return load_tool("portlin-migrate")


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


class TestOpening:
    def test_an_encrypted_root_is_opened_read_only_with_the_passphrase_on_stdin(self, tool):
        argvs = tool.open_argvs("/dev/sdb4", encrypted=True)
        assert argvs[0] == ("cryptsetup", "open", "--readonly", "--type", "luks",
                            "--key-file", "-", "/dev/sdb4", tool.MAPPING)
        assert argvs[1] == ("mount", "-o", "ro,noload", f"/dev/mapper/{tool.MAPPING}", str(tool.MOUNT))

    def test_a_plain_root_is_only_mounted(self, tool):
        assert tool.open_argvs("/dev/sdc4", encrypted=False) == [
            ("mount", "-o", "ro,noload", "/dev/sdc4", str(tool.MOUNT)),
        ]

    def test_closing_reverses_opening(self, tool):
        assert tool.close_argvs(encrypted=True) == [
            ("umount", str(tool.MOUNT)),
            ("cryptsetup", "close", tool.MAPPING),
        ]
        assert tool.close_argvs(encrypted=False) == [("umount", str(tool.MOUNT))]

    def test_open_source_retries_a_wrong_passphrase_three_times_then_gives_up(self, tool, monkeypatch, tmp_path):
        # The mount point lives under /run on a stick; here it has to be
        # somewhere this test may create.
        monkeypatch.setattr(tool, "MOUNT", tmp_path / "source")
        monkeypatch.setattr(tool, "running_disk", lambda: "/dev/sda")
        calls = []
        answers = iter(["bad", "worse", "worst"])

        class Result:
            def __init__(self, code, stdout=""):
                self.returncode, self.stdout, self.stderr = code, stdout, ""

        def run(argv, **kwargs):
            calls.append((tuple(argv), kwargs.get("input")))
            if argv[0] == "blkid":
                return Result(0, "crypto_LUKS\n")
            if argv[0] == "cryptsetup":
                return Result(tool.WRONG_PASSPHRASE)
            if argv[0] == "findmnt":
                return Result(1)
            return Result(0)

        with pytest.raises(tool.SourceError, match="passphrase"):
            tool.open_source("/dev/sdb4", passphrase=lambda: next(answers), run=run)
        tries = [c for c in calls if c[0][0] == "cryptsetup"]
        assert len(tries) == tool.PASSPHRASE_TRIES
        assert [c[1] for c in tries] == ["bad", "worse", "worst"]
        assert not any(c[0][0] == "mount" for c in calls)

    def test_the_passphrase_never_appears_in_an_argument(self, tool):
        for argv in tool.open_argvs("/dev/sdb4", encrypted=True):
            assert "hunter2" not in " ".join(argv)

    def test_the_running_drive_is_refused_by_name_not_silently(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "running_disk", lambda: "/dev/sda")
        with pytest.raises(tool.SourceError, match="running from"):
            tool.open_source("/dev/sda4", passphrase=lambda: "", run=lambda *a, **k: pytest.fail("nothing runs"))


class TestRunSteps:
    def test_progress_is_weighted_across_steps_and_the_result_is_ok(self, tool, migrate):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", weight=3000),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync", weight=1000),
        ]
        out = io.StringIO()

        def execute(step, on_line):
            on_line("sending incremental file list")
            on_line("      1,234  50%   1.00MB/s    0:00:01 (xfr#1, to-chk=1/2)")
            on_line("      2,468  100%   1.00MB/s    0:00:01 (xfr#2, to-chk=0/2)")
            return 0

        result = tool.run_steps(steps, total=4000, out=out, execute=execute)
        assert result.ok and result.warnings == ()
        lines = out.getvalue().splitlines()
        assert lines[0] == "::step Copying A"
        assert "::progress 37" in lines
        assert "::progress 75" in lines
        assert lines[-1] == "::progress 100"

    def test_tar_checkpoints_drive_progress_by_bytes(self, tool, migrate):
        step = migrate.Step("Restoring", argv=("tar", "x"), progress="tar", weight=4 * migrate.TAR_RECORD_BYTES * 1000)
        out = io.StringIO()

        def execute(step, on_line):
            on_line("tar: Read checkpoint 2000")
            return 0

        tool.run_steps([step], total=step.weight, out=out, execute=execute)
        assert "::progress 50" in out.getvalue().splitlines()

    def test_a_tolerated_exit_is_a_warning_and_the_run_goes_on(self, tool, migrate):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", tolerate=(23,)),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync"),
        ]
        out = io.StringIO()
        codes = iter([23, 0])
        result = tool.run_steps(steps, total=0, out=out, execute=lambda step, on_line: next(codes))
        assert result.ok
        assert len(result.warnings) == 1 and "23" in result.warnings[0]
        assert any(line.startswith("::warn") for line in out.getvalue().splitlines())
        assert "::step Copying B" in out.getvalue()

    def test_an_optional_step_may_fail_but_an_ordinary_one_stops_the_run(self, tool, migrate):
        steps = [
            migrate.Step("Telling systemd", argv=("timedatectl",), optional=True),
            migrate.Step("Creating the account", argv=("useradd",)),
            migrate.Step("Never reached", argv=("rsync",)),
        ]
        ran = []

        def execute(step, on_line):
            ran.append(step.argv[0])
            return 1

        result = tool.run_steps(steps, total=0, out=io.StringIO(), execute=execute)
        assert not result.ok
        assert "useradd exited with status 1" in result.failure
        assert ran == ["timedatectl", "useradd"]

    def test_move_aside_write_and_mkdir_happen_before_the_command(self, tool, migrate, tmp_path):
        existing = tmp_path / "home/alice/Documents/notes.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("mine")
        backup = tmp_path / "home/alice/.portlin-migrate-backup/stamp/Documents/notes.txt"
        step = migrate.Step(
            "Restoring",
            argv=("tar", "x"),
            move_aside=((str(existing), str(backup)),),
            mkdir=(str(tmp_path / "made"),),
            write=((str(tmp_path / "etc/hostname"), "office\n"),),
        )
        seen = {}

        def execute(step, on_line):
            seen["existing"] = existing.exists()
            seen["backup"] = backup.read_text()
            seen["made"] = (tmp_path / "made").is_dir()
            seen["hostname"] = (tmp_path / "etc/hostname").read_text()
            return 0

        assert tool.run_steps([step], total=0, out=io.StringIO(), execute=execute).ok
        assert seen == {"existing": False, "backup": "mine", "made": True, "hostname": "office\n"}

    def test_a_passthrough_step_forwards_the_installers_events_but_not_its_result(self, tool, migrate):
        step = migrate.Step("Installing vlc", argv=("/usr/bin/portlin-install", "install", "vlc"),
                            passthrough=True, optional=True)
        out = io.StringIO()

        def execute(step, on_line):
            on_line("::step Installing packages")
            on_line("::progress 40")
            on_line("Get:1 http://deb.debian.org ...")
            on_line("::result failed vlc apt-get exited with status 100")
            return 1

        result = tool.run_steps([step], total=0, out=out, execute=execute)
        lines = out.getvalue().splitlines()
        assert result.ok
        assert "::step Installing packages" in lines
        assert "Get:1 http://deb.debian.org ..." in lines
        assert not any(line.startswith("::result") for line in lines)
        assert any("vlc" in w for w in result.warnings)

    def test_a_warn_only_step_is_emitted_and_runs_nothing(self, tool, migrate):
        out = io.StringIO()
        result = tool.run_steps(
            [migrate.Step("", warn="uid 1500 is already in use")], total=0, out=out,
            execute=lambda *a: pytest.fail("nothing to execute"),
        )
        assert result.ok
        assert "::warn uid 1500 is already in use" in out.getvalue()


class TestApplySteps:
    @pytest.fixture
    def parts(self, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        source_root = tmp_path / "source"
        populate_home(make_source(source_root))
        state = source_root / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text("{}")
        inventory = migrate.build_inventory(source_root)
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        return source_root, inventory, target

    def test_the_order_is_identity_then_files_then_theme_then_software(self, tool, migrate, parts):
        source_root, inventory, target = parts
        source = tool.Source("stick", "/dev/sdb4", root=source_root)
        ids = ["identity.hostname", "home.files.Documents", "software.mullvad"]
        steps = tool.apply_steps(inventory, ids, source, target, "stamp", firstboot=False,
                                 hosts_text="127.0.0.1\tlocalhost\n", icon_theme_installed=lambda n: True)
        assert [s.argv[0] for s in steps] == ["hostnamectl", "rsync", migrate.INSTALLER]

    def test_first_boot_leaves_identity_to_the_wizard(self, tool, migrate, parts):
        source_root, inventory, target = parts
        source = tool.Source("stick", "/dev/sdb4", root=source_root)
        steps = tool.apply_steps(inventory, ["identity.hostname", "home.files.Documents"], source, target, "stamp",
                                 firstboot=True, hosts_text="", icon_theme_installed=lambda n: True)
        assert [s.argv[0] for s in steps] == ["rsync"]

    def test_an_archive_source_plans_a_tar_extract(self, tool, migrate, parts):
        from test_migrate import LISTING
        source_root, inventory, target = parts
        source = tool.Source("archive", "/media/x/a.portlin-backup.tar.zst", listing=LISTING)
        steps = tool.apply_steps(inventory, ["home.files.Documents"], source, target, "stamp",
                                 firstboot=False, hosts_text="", icon_theme_installed=lambda n: True)
        assert steps[0].argv[0] == "tar"
        assert steps[1].argv[0] == "chown"


class TestPlanFile:
    def test_round_trips_through_json(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan = tmp_path / "plan.json"
        source = tool.Source("stick", "/dev/sdb4", encrypted=True)
        tool.write_plan(plan, source=source, ids=["account.olduser"], inventory=inventory,
                        firstboot=True, label="SanDisk Ultra 57.3G")
        data = tool.read_plan(plan)
        assert data["source"] == "/dev/sdb4" and data["kind"] == "stick" and data["encrypted"] is True
        assert data["ids"] == ["account.olduser"]
        assert data["firstboot"] is True
        assert data["label"] == "SanDisk Ultra 57.3G"
        assert migrate.from_json(json.dumps(data["inventory"])) == inventory
        assert data["account"] == {"name": "olduser", "gecos": "Old User", "sudo_nopasswd": True, "autologin": False}
        assert data["identity"] == {"hostname": "office", "locale": "en_GB.UTF-8", "keyboard": "gb", "timezone": "Europe/London"}

    def test_a_plan_without_an_account_item_reports_no_account(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan = tmp_path / "plan.json"
        tool.write_plan(plan, source=tool.Source("stick", "/dev/sdb4"), ids=["identity.hostname"],
                        inventory=inventory, firstboot=True, label="x")
        assert tool.read_plan(plan)["account"] is None


class TestLocalTarget:
    def test_the_invoking_user_wins_over_the_first_account(self, tool, monkeypatch):
        import pwd

        class Record:
            pw_name, pw_uid, pw_gid, pw_dir = "alice", 1000, 1000, "/home/alice"

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: Record)
        target = tool.local_target({"PKEXEC_UID": "1000"})
        assert (target.user, target.uid, target.gid, target.home) == ("alice", 1000, 1000, "home/alice")

    def test_no_invoking_user_and_no_account_means_an_empty_target(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "LOCAL_ROOT", tmp_path)
        assert tool.local_target({}) == tool.Target(root=Path("/"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate_tool.py tests/test_migrate.py::TestDpkgStatus -v`
Expected: FAIL (no such tool; `dpkg_status_lines` missing).

- [ ] **Step 3: Add the helper to `migrate.py`**

Append to `portlin/resources/runtime/migrate.py`:

```python
def dpkg_status_lines(status_file: str) -> str:
    """/var/lib/dpkg/status as the "<package> <status>" lines dpkg-query prints.

    The source is not running, so dpkg-query cannot be asked; the file is
    read instead and rendered the way parse_dpkg_status already expects.
    """
    lines = []
    package = ""
    for line in status_file.splitlines():
        if line.startswith("Package: "):
            package = line[len("Package: "):].strip()
        elif line.startswith("Status: ") and package:
            lines.append(f"{package} {line.split()[-1]}")
            package = ""
    return "".join(f"{line}\n" for line in lines)


def mark_existing_account(inventory: Inventory, local_user: str) -> Inventory:
    """After setup the account exists, so the item is shown off and says so;
    the files go into the local account instead."""
    if not local_user:
        return inventory
    items = tuple(
        dataclasses.replace(item, default=False, note=f"{local_user} already exists here; files go into that account")
        if item.category == "account" else item
        for item in inventory.items
    )
    return dataclasses.replace(inventory, items=items)
```

- [ ] **Step 4: Write the tool**

Create `portlin/resources/runtime/portlin-migrate` (mode 755):

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""portlin-migrate: bring another portlin's files and settings onto this one.

    portlin-migrate candidates [--json]        portlin drives and archives in reach
    portlin-migrate inventory SOURCE [--json]  what SOURCE holds, by item, with sizes
    portlin-migrate plan [SOURCE] --out FILE   choose on the console, write a plan
    portlin-migrate apply --plan FILE          copy what the plan selects
    portlin-migrate export FILE                this stick, everything, to an archive
    portlin-migrate restore [SOURCE]           plan and apply, on the console
    portlin-migrate close                      release a source plan kept open

SOURCE is a root partition (/dev/sdb4), an archive, or "auto" for the one
candidate when there is exactly one. A LUKS source's passphrase is read from
stdin, never from an argument.

The source is never written to. It is mounted read-only and, when encrypted,
opened read-only, so even a bug here cannot touch it.

apply prints the line protocol portlin-install established, so the Migrate
window reads it with the same code Software uses:

    ::step <text>            a phase began
    ::progress <0-100>       bytes copied over bytes planned
    ::warn <text>            something was skipped and the run went on
    ::result ok|failed [text]

Everything else is rsync, tar and apt talking, passed through for the log.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, "/usr/lib/portlin")

from devices import (  # noqa: E402
    backing_disk,
    backing_partition,
    command_output,
    disk_tail_bytes,
    root_source,
    unclaimed_bytes,
)
from migrate import (  # noqa: E402
    ARCHIVE_SUFFIX,
    BACKUP_DIRNAME,
    EXIT_NO_SPACE,
    Inventory,
    Step,
    Target,
    account_steps,
    archive_candidates,
    build_inventory,
    checkpoint_bytes,
    describe,
    dpkg_status_lines,
    human,
    mark_existing_account,
    identity_steps,
    lsblk_argv,
    no_space_message,
    overall_percent,
    parse_lsblk,
    parse_rsync_progress,
    parse_tar_checkpoint,
    plan_archive,
    plan_export,
    plan_stick,
    read_accounts,
    read_manifest,
    selected,
    selected_bytes,
    shortfall,
    software_steps,
    tar_list_argv,
    tar_manifest_argv,
    theme_steps,
    to_json,
    from_json,
    archive_name,
    read_release,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PRIVILEGE = 3

MOUNT = Path("/run/portlin/migrate/source")
MAPPING = "portlin-migrate-source"
PLAN_DIR = Path("/run/portlin/migrate")
LOG = Path("/var/log/portlin-migrate.log")
MEDIA_ROOTS = ("/media", "/run/media")
LOCAL_ROOT = Path("/")

# cryptsetup's exit status for a passphrase that does not open the device.
WRONG_PASSPHRASE = 2
PASSPHRASE_TRIES = 3


class SourceError(Exception):
    """The source could not be opened or is not a portlin."""


@dataclass
class Source:
    kind: str  # "stick" or "archive"
    path: str
    encrypted: bool = False
    root: Path | None = None  # where a stick is mounted
    listing: str = ""  # an archive's member list, once read


@dataclass(frozen=True)
class RunResult:
    ok: bool
    warnings: tuple[str, ...]
    failure: str = ""


# -- protocol and log ---------------------------------------------------------


def format_event(kind: str, *parts: str) -> str:
    return " ".join(("::" + kind, *parts)).rstrip()


def emit(kind: str, *parts: str, out=None) -> None:
    out = out or sys.stdout
    print(format_event(kind, *parts), file=out, flush=True)


def log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def stamp_now() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S")


# -- finding and opening sources ------------------------------------------------


def running_disk() -> str:
    partition = backing_partition(root_source())
    return f"/dev/{backing_disk(partition)}" if partition else ""


def media_mounts() -> list[Path]:
    mounts = []
    for root in MEDIA_ROOTS:
        try:
            for owner in Path(root).iterdir():
                mounts += [p for p in owner.iterdir() if p.is_dir()]
        except OSError:
            continue
    return mounts


def list_candidates():
    sticks = parse_lsblk(command_output(lsblk_argv()), running_disk())
    return sticks + archive_candidates(media_mounts())


def open_argvs(device: str, encrypted: bool) -> list[tuple[str, ...]]:
    """Read-only at both layers, so nothing here can write to the source."""
    argvs = []
    node = device
    if encrypted:
        argvs.append(("cryptsetup", "open", "--readonly", "--type", "luks",
                      "--key-file", "-", device, MAPPING))
        node = f"/dev/mapper/{MAPPING}"
    argvs.append(("mount", "-o", "ro,noload", node, str(MOUNT)))
    return argvs


def close_argvs(encrypted: bool) -> list[tuple[str, ...]]:
    argvs = [("umount", str(MOUNT))]
    if encrypted:
        argvs.append(("cryptsetup", "close", MAPPING))
    return argvs


def is_luks(device: str, run=subprocess.run) -> bool:
    proc = run(["blkid", "-o", "value", "-s", "TYPE", device], capture_output=True, text=True, check=False)
    return proc.stdout.strip() == "crypto_LUKS"


def is_mounted(run=subprocess.run) -> bool:
    return run(["findmnt", str(MOUNT)], capture_output=True, text=True, check=False).returncode == 0


def open_source(path: str, *, passphrase: Callable[[], str], run=subprocess.run) -> Source:
    """Mount a stick or note an archive. A stick already mounted is reused,
    which is how a plan kept open by first boot reaches apply."""
    if path.endswith(ARCHIVE_SUFFIX):
        if not Path(path).is_file():
            raise SourceError(f"{path} does not exist")
        return Source("archive", path)
    if not path.startswith("/dev/"):
        raise SourceError(f"{path} is neither a device nor a {ARCHIVE_SUFFIX} archive")
    disk = running_disk()
    if disk and path.startswith(disk):
        raise SourceError(f"{path} is on the drive this system is running from")
    encrypted = is_luks(path, run)
    if is_mounted(run):
        return Source("stick", path, encrypted, MOUNT)
    MOUNT.mkdir(parents=True, exist_ok=True)
    argvs = open_argvs(path, encrypted)
    if encrypted:
        for _ in range(PASSPHRASE_TRIES):
            proc = run(list(argvs[0]), input=passphrase(), capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                break
            if proc.returncode != WRONG_PASSPHRASE:
                raise SourceError(f"could not open {path}: {proc.stderr.strip()}")
        else:
            raise SourceError("the passphrase did not open the drive")
        argvs = argvs[1:]
    for argv in argvs:
        proc = run(list(argv), input="", capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            close_source(run)
            raise SourceError(f"could not mount {path}: {proc.stderr.strip()}")
    if not read_release(MOUNT):
        close_source(run)
        raise SourceError(f"{path} is not a portlin drive")
    return Source("stick", path, encrypted, MOUNT)


def close_source(run=subprocess.run) -> None:
    """Unmount, close the mapping, remove the directory: exact reverse, always."""
    for argv in close_argvs(encrypted=True):
        run(list(argv), input="", capture_output=True, text=True, check=False)
    try:
        MOUNT.rmdir()
    except OSError:
        pass


def source_inventory(source: Source, *, firstboot: bool, run=subprocess.run) -> Inventory:
    if source.kind == "archive":
        proc = run(list(tar_manifest_argv(Path(source.path))), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise SourceError(f"{source.path} has no manifest: {proc.stderr.strip()}")
        inventory, _ = read_manifest(proc.stdout)
        listing = run(list(tar_list_argv(Path(source.path))), capture_output=True, text=True, check=False)
        source.listing = listing.stdout
        return inventory
    try:
        status = (source.root / "var/lib/dpkg/status").read_text()
    except OSError:
        status = ""
    return build_inventory(source.root, dpkg_status=dpkg_status_lines(status), firstboot=firstboot)


# -- the running system ---------------------------------------------------------


def local_target(environ=os.environ) -> Target:
    """The account files land in: whoever asked, else the first account here."""
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        value = environ.get(variable)
        if not value:
            continue
        try:
            record = pwd.getpwuid(int(value))
        except (ValueError, KeyError):
            continue
        return Target(Path("/"), record.pw_name, record.pw_uid, record.pw_gid, record.pw_dir.lstrip("/"))
    accounts = read_accounts(LOCAL_ROOT)
    if accounts:
        first = accounts[0]
        return Target(Path("/"), first.name, first.uid, first.gid, first.home)
    return Target(Path("/"))


def target_for(name: str) -> Target:
    record = pwd.getpwnam(name)
    return Target(Path("/"), record.pw_name, record.pw_uid, record.pw_gid, record.pw_dir.lstrip("/"))


def uid_in_use(uid: int) -> bool:
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def unclaimed_now() -> int:
    """portlin-info's arithmetic, so the no-space refusal gives the same advice."""
    partition = backing_partition(root_source())
    if not partition:
        return 0
    disk = backing_disk(partition)
    partition_out = command_output(["lsblk", "-bdno", "SIZE", f"/dev/{partition}"])
    if not partition_out.isdigit():
        return 0
    return unclaimed_bytes(shutil.disk_usage("/").total, int(partition_out), disk_tail_bytes(disk, partition))


# -- running a plan -------------------------------------------------------------


def move_aside(existing: str, backup: str) -> None:
    Path(backup).parent.mkdir(parents=True, exist_ok=True)
    os.rename(existing, backup)
    log(f"moved aside {existing} -> {backup}")


def _execute(step: Step, on_line: Callable[[str], None]) -> int:
    """Run one command, feeding stdin if the step has one and reading output
    a line at a time. rsync ends its progress lines with a carriage return
    rather than a newline, so both count as line ends."""
    log(f"run: {' '.join(step.argv)}")
    proc = subprocess.Popen(
        list(step.argv),
        stdin=subprocess.PIPE if step.stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if step.stdin is not None:
        proc.stdin.write(step.stdin.encode())
        proc.stdin.close()
    buffer = b""
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        buffer += chunk
        while True:
            cut = min((i for i in (buffer.find(b"\n"), buffer.find(b"\r")) if i >= 0), default=-1)
            if cut < 0:
                break
            on_line(buffer[:cut].decode("utf-8", "replace"))
            buffer = buffer[cut + 1:]
    if buffer:
        on_line(buffer.decode("utf-8", "replace"))
    return proc.wait()


def run_steps(steps, *, total: int, out=None, execute=None) -> RunResult:
    out = out or sys.stdout
    execute = execute or _execute
    warnings: list[str] = []
    done = 0
    for step in steps:
        if step.warn:
            emit("warn", step.warn, out=out)
            warnings.append(step.warn)
            continue
        if step.text:
            emit("step", step.text, out=out)
        try:
            for existing, backup in step.move_aside:
                move_aside(existing, backup)
            for directory in step.mkdir:
                Path(directory).mkdir(parents=True, exist_ok=True)
            for path, content in step.write:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(content)
        except OSError as exc:
            return RunResult(False, tuple(warnings), str(exc))
        if step.argv:
            def on_line(line: str, step=step, done=done) -> None:
                if step.passthrough and line.startswith("::"):
                    kind, _, rest = line[2:].partition(" ")
                    if kind == "result":
                        if rest.startswith("failed"):
                            warnings.append(f"{step.text} did not finish: {rest[len('failed'):].strip()}")
                    elif kind != "progress":
                        print(line, file=out, flush=True)
                    return
                if step.progress == "rsync":
                    percent = parse_rsync_progress(line)
                    if percent is not None:
                        emit("progress", str(overall_percent(done, step.weight, percent, total)), out=out)
                        return
                elif step.progress == "tar":
                    records = parse_tar_checkpoint(line)
                    if records is not None:
                        percent = min(100, checkpoint_bytes(records) * 100 // max(step.weight, 1))
                        emit("progress", str(overall_percent(done, step.weight, percent, total)), out=out)
                        return
                print(line, file=out, flush=True)
            code = execute(step, on_line)
            if code != 0:
                if step.optional or code in step.tolerate:
                    text = f"{step.text}: {step.argv[0]} exited with status {code}"
                    warnings.append(text)
                    emit("warn", text, out=out)
                else:
                    return RunResult(False, tuple(warnings), f"{step.argv[0]} exited with status {code}")
        done += step.weight
        emit("progress", str(overall_percent(done, 0, 0, total)), out=out)
    return RunResult(True, tuple(warnings))


def apply_steps(inventory: Inventory, ids: list[str], source: Source, target: Target, stamp: str, *,
                firstboot: bool, hosts_text: str, icon_theme_installed: Callable[[str], bool]) -> list[Step]:
    """Everything after the account exists: settings, files, theme, software.

    First boot applies the identity through its own screens, so those steps
    are left out there. Software goes last because it needs the network and
    can take a while, and everything else is already in place if it stalls.
    """
    steps: list[Step] = []
    if not firstboot:
        steps += identity_steps(inventory.identity, ids, hosts_text=hosts_text)
    if source.kind == "archive":
        steps += plan_archive(inventory, ids, Path(source.path), source.listing, target, stamp)
    else:
        steps += plan_stick(inventory, ids, source.root, target, stamp)
    for item in selected(inventory, ids):
        if item.id == "extras.theme":
            names = json.loads(item.value)
            steps += theme_steps(names, target, icon_theme_installed=icon_theme_installed(names.get("icons", "")))
    steps += software_steps([item.value for item in selected(inventory, ids) if item.category == "software"])
    return steps


# -- plan files -----------------------------------------------------------------


def write_plan(path: Path, *, source: Source, ids: list[str], inventory: Inventory, firstboot: bool, label: str) -> None:
    """What the wizard and the window hand to apply. The account and identity
    are repeated at the top level so the wizard can prefill its screens
    without understanding the inventory."""
    account = None
    for candidate in inventory.accounts:
        if f"account.{candidate.name}" in ids:
            account = {
                "name": candidate.name,
                "gecos": candidate.gecos.split(",")[0],
                "sudo_nopasswd": candidate.sudo_nopasswd,
                "autologin": candidate.autologin,
            }
            break
    data = {
        "source": source.path,
        "kind": source.kind,
        "encrypted": source.encrypted,
        "ids": ids,
        "firstboot": firstboot,
        "label": label,
        "bytes": selected_bytes(inventory, ids),
        "account": account,
        "identity": {
            key: getattr(inventory.identity, key)
            for key in ("hostname", "locale", "keyboard", "timezone")
        },
        "inventory": json.loads(to_json(inventory)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def read_plan(path: Path) -> dict:
    return json.loads(path.read_text())


# -- verbs ----------------------------------------------------------------------


def passphrase_from_stdin() -> str:
    return sys.stdin.readline().rstrip("\n")


def resolve_source_path(given: str | None) -> str:
    if given and given != "auto":
        return given
    found = list_candidates()
    if len(found) == 1:
        return found[0].path
    if not found:
        raise SourceError("no portlin drive or archive was found")
    raise SourceError("more than one candidate; name one: " + ", ".join(c.path for c in found))


def cmd_candidates(args) -> int:
    found = list_candidates()
    if args.json:
        print(json.dumps([c.__dict__ for c in found]))
    else:
        for candidate in found:
            print(f"{candidate.path:<20} {describe(candidate)}")
    return EXIT_OK


def cmd_inventory(args) -> int:
    source = open_source(resolve_source_path(args.source), passphrase=passphrase_from_stdin)
    try:
        inventory = source_inventory(source, firstboot=args.firstboot)
    finally:
        if source.kind == "stick":
            close_source()
    if not args.firstboot:
        inventory = mark_existing_account(inventory, local_target().user)
    if args.json:
        print(to_json(inventory), end="")
    else:
        for item in inventory.items:
            mark = "x" if item.default else " "
            size = human(item.bytes) if item.bytes else ""
            print(f"[{mark}] {item.id:<40} {size:>9}  {item.label}")
    return EXIT_OK


def refuse_if_too_big(inventory: Inventory, ids: list[str]) -> int:
    short = shortfall(selected_bytes(inventory, ids), shutil.disk_usage("/").free)
    if short:
        emit("result", "failed", no_space_message(short, unclaimed_now()))
        return EXIT_NO_SPACE
    return EXIT_OK


def cmd_apply(args) -> int:
    plan = read_plan(Path(args.plan))
    inventory = from_json(json.dumps(plan["inventory"]))
    ids = list(plan["ids"])
    firstboot = bool(plan.get("firstboot"))
    stamp = stamp_now()
    source = open_source(plan["source"], passphrase=passphrase_from_stdin)
    try:
        if source.kind == "archive":
            source_inventory(source, firstboot=firstboot)  # fills the listing
        code = refuse_if_too_big(inventory, ids)
        if code:
            return code
        target = local_target()
        warnings: list[str] = []
        account = next((a for a in inventory.accounts if f"account.{a.name}" in ids), None)
        if account and not target.user:
            result = run_steps(
                account_steps(account, existing_groups={g.gr_name for g in grp.getgrall()},
                              uid_free=not uid_in_use(account.uid)),
                total=0,
            )
            warnings += result.warnings
            if not result.ok:
                emit("result", "failed", result.failure)
                return EXIT_FAILED
            target = target_for(account.name)
        elif account and target.user:
            emit("warn", f"the account {target.user} already exists here, so {account.name}'s files go into it")
        steps = apply_steps(
            inventory, ids, source, target, stamp, firstboot=firstboot,
            hosts_text=Path("/etc/hosts").read_text() if Path("/etc/hosts").exists() else "",
            icon_theme_installed=lambda name: Path(f"/usr/share/icons/{name}/index.theme").exists(),
        )
        result = run_steps(steps, total=sum(s.weight for s in steps))
        warnings += result.warnings
        if not result.ok:
            emit("result", "failed", result.failure)
            return EXIT_FAILED
        emit("result", "ok", summary(len(selected(inventory, ids)), target, stamp, warnings))
        return EXIT_OK
    finally:
        if source.kind == "stick":
            close_source()


def summary(count: int, target: Target, stamp: str, warnings: list[str]) -> str:
    text = f"Brought over {count} item{'s' if count != 1 else ''}."
    backup = Path("/") / target.home / BACKUP_DIRNAME / stamp if target.home else None
    if backup and backup.exists():
        text += f" Files that were replaced are under {backup}."
    if warnings:
        text += f" {len(warnings)} warning{'s' if len(warnings) != 1 else ''}; see {LOG}."
    return text


def cmd_export(args) -> int:
    target = local_target()
    inventory = build_inventory(Path("/"), dpkg_status=command_output(
        ["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\n"]
    ))
    stamp = stamp_now()
    destination = Path(args.file)
    if destination.is_dir():
        destination = destination / archive_name(inventory.hostname, stamp)
    steps = plan_export(inventory, Path("/"), destination, PLAN_DIR / "export", stamp)
    result = run_steps(steps, total=sum(s.weight for s in steps))
    if not result.ok:
        emit("result", "failed", result.failure)
        return EXIT_FAILED
    try:
        destination.chmod(0o600)
        if target.user:
            os.chown(destination, target.uid, target.gid)
    except OSError:
        pass
    emit("result", "ok", f"Wrote {destination}. It holds the password hash and every saved "
                         "network password, so keep it somewhere only you can read.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portlin-migrate")
    verbs = parser.add_subparsers(dest="verb", required=True)
    candidates = verbs.add_parser("candidates")
    candidates.add_argument("--json", action="store_true")
    inventory = verbs.add_parser("inventory")
    inventory.add_argument("source", nargs="?")
    inventory.add_argument("--json", action="store_true")
    inventory.add_argument("--firstboot", action="store_true")
    apply = verbs.add_parser("apply")
    apply.add_argument("--plan", required=True)
    export = verbs.add_parser("export")
    export.add_argument("file")
    return parser


VERBS = {
    "candidates": cmd_candidates,
    "inventory": cmd_inventory,
    "apply": cmd_apply,
    "export": cmd_export,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if os.geteuid() != 0:
        print("portlin-migrate must run as root", file=sys.stderr)
        return EXIT_PRIVILEGE
    try:
        return VERBS[args.verb](args)
    except SourceError as exc:
        emit("result", "failed", str(exc))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
```

Then `chmod 755 portlin/resources/runtime/portlin-migrate`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate_tool.py tests/test_migrate.py -v`
Expected: PASS. If `TestOpening::test_open_source_retries...` fails because `is_luks` is called with `check=False` as a keyword the fake does not expect, the fake already accepts `**kwargs`; check the fake's `blkid` branch returns `crypto_LUKS`.

- [ ] **Step 6: Add the tool to the runtime import test**

In `tests/test_runtime_tools.py`, change `TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt", "portlin-install"]` to include `"portlin-migrate"`, and add beside `test_devices_module_compiles`:

```python
    def test_migrate_module_compiles(self):
        source = (RUNTIME / "migrate.py").read_text()
        compile(source, str(RUNTIME / "migrate.py"), "exec")
```

Run: `.venv/bin/python -m pytest tests/test_runtime_tools.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add portlin/resources/runtime/portlin-migrate portlin/resources/runtime/migrate.py tests/test_migrate_tool.py tests/test_migrate.py tests/test_runtime_tools.py
git commit -m "feat(runtime): add portlin-migrate with candidates, inventory, apply and export"
```

---

### Task 9: the console flow: `plan`, `restore`, `close`, `apply --gauge`

**Files:**
- Modify: `portlin/resources/runtime/portlin-migrate`
- Test: `tests/test_migrate_tool.py`

**Interfaces:**
- Consumes: Task 8's tool, `human`, `category_label`, `CATEGORIES`, `describe`.
- Produces (in the tool):
  - `BACKTITLE = "Portlin migration"`, `HEADING_PREFIX = "--"`
  - `class Cancelled(Exception)`
  - `checklist_rows(inventory: Inventory) -> list[tuple[str, str, bool]]` as `(tag, text, on)`
  - `checklist_argv(title: str, text: str, rows) -> list[str]`
  - `parse_checklist(output: str) -> list[str]`
  - `plan_summary(inventory, ids: list[str], label: str, free: int) -> str`
  - `class Gauge` with `write(text: str)`, `flush()`, `close()`, and `gauge_feed(kind: str, rest: str, percent: int) -> str | None`
  - in `migrate.py`: `newer_version(source: str, local: str) -> bool`, true only when both parse as dotted integers and the source is greater
  - `apply_plan(path: Path, *, out) -> tuple[int, str]` (Task 8's `cmd_apply` body, returning the code and the result text instead of emitting `::result`)
  - `narrow(inventory, ids, only, skip) -> list[str]`
  - verbs `plan [SOURCE] --out FILE [--firstboot] [--keep-open]`, `restore [SOURCE]`, `close`, and `apply --plan FILE [--gauge] [--only ID...] [--skip ID...]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate_tool.py`:

```python
class TestChecklist:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        return migrate.build_inventory(tmp_path)

    def test_rows_group_items_under_unselectable_headings(self, tool, inventory):
        rows = tool.checklist_rows(inventory)
        tags = [tag for tag, _, _ in rows]
        assert tags[0] == f"{tool.HEADING_PREFIX}account"
        assert rows[0][1] == "-- Account --" and rows[0][2] is False
        assert "home.files.Documents" in tags
        docs = next(row for row in rows if row[0] == "home.files.Documents")
        assert docs[1].startswith("  Documents") and "1 KB" in docs[1] and docs[2] is True
        cache = next(row for row in rows if row[0] == "home.settings..cache")
        assert cache[2] is False
        # Headings only for categories that have something in them.
        assert f"{tool.HEADING_PREFIX}network" not in tags

    def test_a_note_is_shown_after_the_label(self, tool, migrate):
        inventory = migrate.Inventory("0.1.2", "office", "home/x", (
            migrate.Item("software.nvidia", "software", "NVIDIA driver", note="describes the old machine", default=False),
        ))
        row = tool.checklist_rows(inventory)[1]
        assert "NVIDIA driver" in row[1] and "(describes the old machine)" in row[1]

    def test_the_checklist_asks_for_one_tag_per_line(self, tool):
        argv = tool.checklist_argv("What to bring over", "Tick what you want.", [("a", "A", True), ("b", "B", False)])
        assert argv[0] == "whiptail"
        assert "--separate-output" in argv and "--checklist" in argv
        assert argv[-6:] == ["a", "A", "on", "b", "B", "off"]

    def test_parsing_drops_headings_and_blank_lines(self, tool):
        assert tool.parse_checklist("--account\naccount.olduser\n\nhome.files.Documents\n") == [
            "account.olduser", "home.files.Documents",
        ]

    def test_the_summary_names_the_count_size_source_and_free_space(self, tool, inventory):
        text = tool.plan_summary(inventory, ["home.files.Documents", "home.files.Downloads"], "SanDisk Ultra 57.3G", free=50_000_000_000)
        assert "2 items, 6 KB from SanDisk Ultra 57.3G" in text
        assert "50 GB" in text
        assert "moved aside" in text


class TestNarrow:
    def test_only_and_skip_take_ids_or_categories(self, tool, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        inventory = migrate.build_inventory(tmp_path)
        ids = ["account.olduser", "home.files.Documents", "home.files.Downloads", "home.settings..config"]
        assert tool.narrow(inventory, ids, ["home.files"], None) == ["home.files.Documents", "home.files.Downloads"]
        assert tool.narrow(inventory, ids, None, ["home.files.Downloads", "account"]) == [
            "home.files.Documents", "home.settings..config",
        ]
        assert tool.narrow(inventory, ids, None, None) == ids


class TestVersions:
    def test_a_newer_source_is_noticed_and_nothing_else_is(self, migrate):
        assert migrate.newer_version("0.2.0", "0.1.2") is True
        assert migrate.newer_version("0.1.10", "0.1.2") is True
        assert migrate.newer_version("0.1.2", "0.1.2") is False
        assert migrate.newer_version("0.1.1", "0.1.2") is False
        assert migrate.newer_version("", "0.1.2") is False
        assert migrate.newer_version("0.1.2~local", "0.1.2") is False


class TestGauge:
    def test_protocol_lines_become_gauge_updates(self, tool):
        assert tool.gauge_feed("progress", "42", 10) == "XXX\n42\n\nXXX\n"
        assert tool.gauge_feed("step", "Copying Documents", 42) == "XXX\n42\nCopying Documents\nXXX\n"
        assert tool.gauge_feed("warn", "something", 42) is None

    def test_the_writer_tracks_the_percent_and_feeds_the_process(self, tool):
        feed = io.StringIO()
        gauge = tool.Gauge(feed)
        gauge.write("::step Copying Documents\n")
        gauge.write("::progress 30\n")
        gauge.write("sending incremental file list\n")
        gauge.write("::step Copying Downloads\n")
        assert feed.getvalue() == (
            "XXX\n0\nCopying Documents\nXXX\n"
            "XXX\n30\n\nXXX\n"
            "XXX\n30\nCopying Downloads\nXXX\n"
        )
        assert gauge.percent == 30
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migrate_tool.py -k "Checklist or Gauge" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the version comparison to `migrate.py`**

Append to `portlin/resources/runtime/migrate.py`:

```python
def newer_version(source: str, local: str) -> bool:
    """Whether the source ran a newer portlin, the direction in which
    configuration formats diverge. Anything that is not plain dotted
    integers on both sides is not compared, rather than guessed at."""
    try:
        return tuple(int(p) for p in source.split(".")) > tuple(int(p) for p in local.split("."))
    except ValueError:
        return False
```

- [ ] **Step 3b: Add the console flow to the tool**

In `portlin/resources/runtime/portlin-migrate`, add `import dataclasses` beside the other imports, `CATEGORIES` and `newer_version` to the `migrate` import, and insert the following before the `# -- verbs` section:

```python
# -- the console ----------------------------------------------------------------

BACKTITLE = "Portlin migration"
# Whiptail checklists are flat, so category headings are rows too: tagged
# with this prefix so they can be told apart from items when read back.
HEADING_PREFIX = "--"
LABEL_WIDTH = 44


class Cancelled(Exception):
    """The user pressed Cancel or Esc."""


def _whiptail(args: list[str], *, capture: bool = False) -> str:
    """Selections come back on stderr, which is whiptail's convention. stdin is
    the console on purpose: whiptail needs it, unlike everything else run here."""
    proc = subprocess.run(["whiptail", "--backtitle", BACKTITLE, *args], stderr=subprocess.PIPE, text=True)
    if proc.returncode == 255 or (proc.returncode != 0 and capture):
        raise Cancelled()
    return (proc.stderr or "").strip()


def _height(text: str, minimum: int = 12) -> int:
    return max(minimum, min(text.count("\n") + 9, 22))


def message(title: str, text: str) -> None:
    _whiptail(["--title", title, "--msgbox", text, str(_height(text, 14)), "76"])


def confirm(title: str, text: str) -> bool:
    argv = ["whiptail", "--backtitle", BACKTITLE, "--title", title, "--yesno", text, str(_height(text)), "76"]
    return subprocess.run(argv).returncode == 0


def ask(title: str, text: str, default: str = "") -> str:
    return _whiptail(["--title", title, "--inputbox", text, "12", "76", default], capture=True)


def ask_password(title: str, text: str) -> str:
    return _whiptail(["--title", title, "--passwordbox", text, "12", "76"], capture=True)


def choose(title: str, text: str, entries: list[tuple[str, str]]) -> str:
    rows = [part for tag, label in entries for part in (tag, label)]
    return _whiptail(
        ["--title", title, "--menu", text, "20", "76", str(min(len(entries), 12)), *rows], capture=True
    )


def checklist_rows(inventory: Inventory) -> list[tuple[str, str, bool]]:
    rows = []
    for category, heading in CATEGORIES:
        items = [item for item in inventory.items if item.category == category]
        if not items:
            continue
        rows.append((f"{HEADING_PREFIX}{category}", f"-- {heading} --", False))
        for item in items:
            label = item.label[:LABEL_WIDTH]
            if item.note:
                label += f" ({item.note})"
            size = human(item.bytes) if item.bytes else ""
            rows.append((item.id, f"  {label:<{LABEL_WIDTH + 2}} {size:>9}".rstrip(), item.default))
    return rows


def checklist_argv(title: str, text: str, rows) -> list[str]:
    flat = [part for tag, label, on in rows for part in (tag, label, "on" if on else "off")]
    return ["whiptail", "--backtitle", BACKTITLE, "--separate-output", "--title", title,
            "--checklist", text, "22", "78", "14", *flat]


def parse_checklist(output: str) -> list[str]:
    return [line for line in output.splitlines() if line and not line.startswith(HEADING_PREFIX)]


def checklist(title: str, text: str, rows) -> list[str]:
    proc = subprocess.run(checklist_argv(title, text, rows), stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise Cancelled()
    return parse_checklist(proc.stderr or "")


def plan_summary(inventory: Inventory, ids: list[str], label: str, free: int) -> str:
    count = len(selected(inventory, ids))
    size = selected_bytes(inventory, ids)
    return (
        f"\n{count} item{'s' if count != 1 else ''}, {human(size)} from {label}\n"
        f"Free space after: {human(max(0, free - size))}\n\n"
        "Files already here with the same names are moved aside, not deleted.\n\n"
        "Bring these over now?"
    )


def gauge_feed(kind: str, rest: str, percent: int) -> str | None:
    """What a protocol line means to whiptail --gauge: a new percentage, or a
    new caption at the current one. XXX blocks are its update syntax."""
    if kind == "progress":
        return f"XXX\n{rest}\n\nXXX\n"
    if kind == "step":
        return f"XXX\n{percent}\n{rest}\nXXX\n"
    return None


class Gauge:
    """An ``out`` for run_steps that draws a whiptail gauge instead of printing.

    Ordinary command output goes to the log only; the gauge shows the step
    name and the percentage, which is what a person at a console wants to
    see while a home directory copies.
    """

    def __init__(self, feed=None) -> None:
        self.percent = 0
        self.process = None
        if feed is None:
            self.process = subprocess.Popen(
                ["whiptail", "--backtitle", BACKTITLE, "--title", "Bringing things over",
                 "--gauge", "Starting...", "8", "76", "0"],
                stdin=subprocess.PIPE, text=True,
            )
            feed = self.process.stdin
        self.feed = feed
        self.buffer = ""

    def write(self, text: str) -> None:
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._line(line)

    def _line(self, line: str) -> None:
        if not line.startswith("::"):
            log(line)
            return
        kind, _, rest = line[2:].partition(" ")
        if kind == "progress" and rest.isdigit():
            self.percent = int(rest)
        update = gauge_feed(kind, rest, self.percent)
        if update:
            self.feed.write(update)
            self.feed.flush()
        else:
            log(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self.process:
            self.process.stdin.close()
            self.process.wait()


def interactive_plan(source_path: str | None, *, firstboot: bool, keep_open: bool, out_path: Path) -> int:
    """Steps 1 to 4 of the console flow: source, passphrase, checklist, confirm."""
    candidate = None
    if source_path and source_path != "auto":
        path = source_path
    else:
        found = list_candidates()
        if not found:
            message("Nothing to migrate from",
                    "\nNo other portlin drive and no backup archive was found.\n\n"
                    "Plug the old drive in, or put the archive on a drive that is plugged in, and try again.")
            return EXIT_FAILED
        entries = [(c.path, describe(c)) for c in found] + [("other", "Somewhere else (type a path)")]
        path = choose("Migrate from", "Where should files and settings come from?", entries)
        if path == "other":
            path = ask("Migrate from", "Enter the device or archive path:")
        candidate = next((c for c in found if c.path == path), None)
    source = open_source(path, passphrase=lambda: ask_password(
        "Unlock the old drive", "\nEnter the passphrase for the old drive:"))
    try:
        inventory = source_inventory(source, firstboot=firstboot)
        if not firstboot:
            inventory = mark_existing_account(inventory, local_target().user)
        if candidate:
            candidate = dataclasses.replace(candidate, version=inventory.version)
        label = describe(candidate) if candidate else Path(path).name
        local_version = read_release(LOCAL_ROOT).get("PORTLIN_VERSION", "")
        if newer_version(inventory.version, local_version):
            message(
                "Newer than this stick",
                f"\nThe old drive runs portlin {inventory.version} and this one runs "
                f"{local_version}.\n\nSettings written by a newer version may not be "
                "understood by an older one. Everything is still copied as it is.",
            )
        ids = checklist(
            "What to bring over",
            "Space ticks and unticks, Tab reaches the buttons. Headings do nothing.",
            checklist_rows(inventory),
        )
        if not ids:
            message("Nothing chosen", "\nNothing was ticked, so nothing will be brought over.")
            return EXIT_FAILED
        if not confirm("Ready", plan_summary(inventory, ids, label, shutil.disk_usage("/").free)):
            return EXIT_FAILED
        write_plan(out_path, source=source, ids=ids, inventory=inventory, firstboot=firstboot, label=label)
        return EXIT_OK
    finally:
        if source.kind == "stick" and not (keep_open and out_path.exists()):
            close_source()
```

Now restructure `cmd_apply` into `apply_plan` and the verb, replacing the `cmd_apply` from Task 8:

```python
def narrow(inventory: Inventory, ids: list[str], only, skip) -> list[str]:
    """--only keeps ids or whole categories; --skip drops them."""
    kept = []
    for item in selected(inventory, ids):
        names = (item.id, item.category)
        if only and not any(n in only for n in names):
            continue
        if skip and any(n in skip for n in names):
            continue
        kept.append(item.id)
    return kept


def apply_plan(path: Path, *, out, only=None, skip=None) -> tuple[int, str]:
    """Run a plan. Returns the exit code and the text for ::result or a dialog."""
    plan = read_plan(path)
    inventory = from_json(json.dumps(plan["inventory"]))
    ids = narrow(inventory, list(plan["ids"]), only, skip)
    firstboot = bool(plan.get("firstboot"))
    stamp = stamp_now()
    source = open_source(plan["source"], passphrase=passphrase_from_stdin)
    try:
        if source.kind == "archive":
            source_inventory(source, firstboot=firstboot)  # fills the listing
        short = shortfall(selected_bytes(inventory, ids), shutil.disk_usage("/").free)
        if short:
            return EXIT_NO_SPACE, no_space_message(short, unclaimed_now())
        target = local_target()
        warnings: list[str] = []
        account = next((a for a in inventory.accounts if f"account.{a.name}" in ids), None)
        if account and not target.user:
            result = run_steps(
                account_steps(account, existing_groups={g.gr_name for g in grp.getgrall()},
                              uid_free=not uid_in_use(account.uid)),
                total=0, out=out,
            )
            warnings += result.warnings
            if not result.ok:
                return EXIT_FAILED, result.failure
            target = target_for(account.name)
        elif account and target.user:
            emit("warn", f"the account {target.user} already exists here, so {account.name}'s files go into it", out=out)
        steps = apply_steps(
            inventory, ids, source, target, stamp, firstboot=firstboot,
            hosts_text=Path("/etc/hosts").read_text() if Path("/etc/hosts").exists() else "",
            icon_theme_installed=lambda name: Path(f"/usr/share/icons/{name}/index.theme").exists(),
        )
        result = run_steps(steps, total=sum(s.weight for s in steps), out=out)
        warnings += result.warnings
        if not result.ok:
            return EXIT_FAILED, result.failure
        return EXIT_OK, summary(len(selected(inventory, ids)), target, stamp, warnings)
    finally:
        if source.kind == "stick":
            close_source()


def cmd_apply(args) -> int:
    if args.gauge:
        gauge = Gauge()
        try:
            code, text = apply_plan(Path(args.plan), out=gauge)
        finally:
            gauge.close()
        message("Done" if code == EXIT_OK else "Not finished", "\n" + text)
        return code
    code, text = apply_plan(Path(args.plan), out=sys.stdout, only=args.only, skip=args.skip)
    emit("result", "ok" if code == EXIT_OK else "failed", text)
    return code


def cmd_plan(args) -> int:
    try:
        return interactive_plan(args.source, firstboot=args.firstboot, keep_open=args.keep_open,
                                out_path=Path(args.out))
    except Cancelled:
        close_source()
        return EXIT_FAILED


def cmd_restore(args) -> int:
    plan_path = PLAN_DIR / "plan.json"
    try:
        code = interactive_plan(args.source, firstboot=False, keep_open=True, out_path=plan_path)
    except Cancelled:
        close_source()
        return EXIT_FAILED
    if code != EXIT_OK:
        return code
    gauge = Gauge()
    try:
        code, text = apply_plan(plan_path, out=gauge)
    finally:
        gauge.close()
        plan_path.unlink(missing_ok=True)
    message("Done" if code == EXIT_OK else "Not finished", "\n" + text)
    return code


def cmd_close(args) -> int:
    close_source()
    return EXIT_OK
```

Extend `build_parser` and `VERBS`:

```python
    apply.add_argument("--gauge", action="store_true", help="draw a whiptail gauge instead of printing")
    apply.add_argument("--only", nargs="+", metavar="ID", help="ids or categories to keep from the plan")
    apply.add_argument("--skip", nargs="+", metavar="ID", help="ids or categories to drop from the plan")
    plan = verbs.add_parser("plan")
    plan.add_argument("source", nargs="?")
    plan.add_argument("--out", required=True)
    plan.add_argument("--firstboot", action="store_true")
    plan.add_argument("--keep-open", action="store_true")
    restore = verbs.add_parser("restore")
    restore.add_argument("source", nargs="?")
    verbs.add_parser("close")
```

```python
VERBS = {
    "candidates": cmd_candidates,
    "inventory": cmd_inventory,
    "apply": cmd_apply,
    "export": cmd_export,
    "plan": cmd_plan,
    "restore": cmd_restore,
    "close": cmd_close,
}
```

In `main`, catch `Cancelled` too and return `EXIT_FAILED` after `close_source()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_migrate_tool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/portlin-migrate tests/test_migrate_tool.py
git commit -m "feat(runtime): give portlin-migrate a console flow with a gauge"
```

---

### Task 10: ship the tool, the module and the polkit action

**Files:**
- Create: `portlin/resources/runtime/org.portlin.migrate.policy`
- Modify: `portlin/package.py:35-49` (`TOOLS`, `SHARED_MODULES`, `POLKIT_ACTIONS`)
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: `usr/bin/portlin-migrate`, `usr/lib/portlin/migrate.py`, `usr/share/polkit-1/actions/org.portlin.migrate.policy` in `portlin-runtime`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package.py`:

```python
def test_runtime_ships_the_migration_tool_and_its_module():
    files = package.text_files("portlin-runtime")
    assert "usr/bin/portlin-migrate" in files
    assert "usr/lib/portlin/migrate.py" in files
    assert "usr/bin/portlin-migrate" in package.executable_paths("portlin-runtime")
    assert "usr/lib/portlin/migrate.py" not in package.executable_paths("portlin-runtime")


def test_runtime_ships_the_migrate_polkit_action_beside_the_program_it_names():
    files = package.text_files("portlin-runtime")
    policy = files["usr/share/polkit-1/actions/org.portlin.migrate.policy"]
    granted = re.search(r'exec\.path">([^<]+)<', policy).group(1)
    assert granted == "/usr/bin/portlin-migrate"
    assert granted.lstrip("/") in files
    assert 'id="org.portlin.migrate"' in policy
    assert "auth_admin_keep" in policy
```

(`import re` at the top of the file if it is not there already.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_package.py -k migrat -v`
Expected: FAIL with `KeyError`.

- [ ] **Step 3: Create the policy and register the files**

Create `portlin/resources/runtime/org.portlin.migrate.policy`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<!--
Part of portlin. Lets the Migrate window run portlin-migrate as root.

Same shape as org.portlin.install: the exec.path annotation names one
program, so the right granted is to run that program and nothing else.
auth_admin_keep, because a migration is a candidates call, an inventory
call and an apply call in a row, and three password dialogs for one job
teaches people to type the password without reading the dialog.
-->
<policyconfig>
  <vendor>Portlin</vendor>
  <vendor_url>https://github.com/sleep/Portlin</vendor_url>
  <action id="org.portlin.migrate">
    <description>Bring files and settings over from another portlin</description>
    <message>Authentication is required to read another portlin drive and copy from it</message>
    <icon_name>portlin</icon_name>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/bin/portlin-migrate</annotate>
  </action>
</policyconfig>
```

In `portlin/package.py`:

```python
TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt", "portlin-install", "portlin-migrate"]
```

```python
SHARED_MODULES = ["devices.py", "catalog.py", "hostinfo.py", "migrate.py"]
```

Update the comment above `SHARED_MODULES` to mention that `migrate.py` is shared because the window reads inventories through the tool but the tool and the tests both import the module.

```python
POLKIT_ACTIONS = {
    "org.portlin.install.policy": "usr/share/polkit-1/actions/org.portlin.install.policy",
    "org.portlin.migrate.policy": "usr/share/polkit-1/actions/org.portlin.migrate.policy",
}
```

- [ ] **Step 4: Run the whole suite**

Run: `make test`
Expected: PASS. `test_tools_use_the_shared_device_module_not_a_hand_copy` may assert something about every tool in `TOOLS`; read it and make `portlin-migrate` satisfy it (it imports `from devices import ...`, which is what that test looks for).

- [ ] **Step 5: Commit**

```bash
git add portlin/package.py portlin/resources/runtime/org.portlin.migrate.policy tests/test_package.py
git commit -m "feat(package): ship portlin-migrate, its module and its polkit action"
```

---

### Task 11: the first-boot screen

**Files:**
- Modify: `portlin/resources/firstboot/portlin-firstboot` (constants near line 44, `choose` at line 308, `step_keyboard`/`step_locale`/`step_timezone`/`step_hostname` at lines 340-410, `step_autologin`/`step_sudo_password` at lines 832-873, `wizard()` at line 1115, `main()` at line 1206)
- Test: `tests/test_firstboot.py`

**Interfaces:**
- Consumes: `portlin-migrate candidates --json`, `plan --firstboot --keep-open --out FILE`, `apply --plan FILE --gauge`, `close` from Tasks 8-9; the plan file's `account`, `identity`, `ids`, `label` keys from `write_plan`.
- Produces (in the wizard): `MIGRATE_TOOL = "/usr/bin/portlin-migrate"`, `MIGRATION_PLAN = Path("/run/portlin/migrate/plan.json")`, `migration_candidates() -> list[dict]`, `step_migration(candidates) -> dict | None`, `apply_migration() -> bool`, `close_migration() -> None`; `choose(..., default: str = "")`; `step_keyboard(default="")`, `step_locale(default="")`, `step_timezone(default="")`, `step_hostname(default="portlin")`, `step_autologin(..., default_yes=False)`, `step_sudo_password(..., default_yes=True)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_firstboot.py`, inside `class TestWizardScript` (add `import json` at the top):

```python
    def test_it_asks_the_packaged_tool_for_candidates_and_survives_its_absence(self):
        seen = []

        class Proc:
            returncode = 0
            stdout = json.dumps([{"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk", "size": "57.3G"}])

        class FakeSubprocess:
            SubprocessError = Exception

            @staticmethod
            def run(argv, **kwargs):
                seen.append((argv, kwargs.get("input")))
                return Proc()

        class Exists:
            def __init__(self, path):
                self.path = path

            def exists(self):
                return self.path == "/usr/bin/portlin-migrate"

        namespace = {"Path": Exists, "subprocess": FakeSubprocess, "json": json,
                     "MIGRATE_TOOL": "/usr/bin/portlin-migrate"}
        candidates = load_function(WIZARD, "migration_candidates", namespace)
        assert candidates() == [{"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk", "size": "57.3G"}]
        assert seen[0][0] == ["/usr/bin/portlin-migrate", "candidates", "--json"]
        # stdin is a pipe, as for every other command: tty1 is not to be handed out.
        assert seen[0][1] == ""

        namespace["MIGRATE_TOOL"] = "/usr/bin/absent"
        assert load_function(WIZARD, "migration_candidates", namespace)() == []

    def test_nonsense_from_the_tool_is_no_candidates(self):
        class Proc:
            returncode = 0
            stdout = '{"not": "a list"}'

        class FakeSubprocess:
            SubprocessError = Exception

            @staticmethod
            def run(argv, **kwargs):
                return Proc()

        class Exists:
            def __init__(self, path):
                pass

            def exists(self):
                return True

        namespace = {"Path": Exists, "subprocess": FakeSubprocess, "json": json,
                     "MIGRATE_TOOL": "/usr/bin/portlin-migrate"}
        assert load_function(WIZARD, "migration_candidates", namespace)() == []
        Proc.stdout = "garbage"
        assert load_function(WIZARD, "migration_candidates", namespace)() == []

    def test_choose_preselects_a_default_when_given_one(self):
        calls = []
        namespace = {"_whiptail": lambda args, capture=False: calls.append(args) or "gb"}
        choose = load_function(WIZARD, "choose", namespace)
        choose("Keyboard", "Which?", [("us", "US"), ("gb", "UK")], default="gb")
        assert "--default-item" in calls[0] and calls[0][calls[0].index("--default-item") + 1] == "gb"
        choose("Keyboard", "Which?", [("us", "US"), ("gb", "UK")])
        assert "--default-item" not in calls[1]

    def test_the_migration_screen_comes_after_welcome_and_before_the_keyboard(self):
        body = WIZARD.read_text()
        wizard = body[body.index("def wizard("):body.index("def main(")]
        assert wizard.index("step_welcome()") < wizard.index("migration_candidates()") < wizard.index("step_keyboard(")

    def test_the_copy_runs_right_after_the_account_exists(self):
        body = WIZARD.read_text()
        wizard = body[body.index("def wizard("):body.index("def main(")]
        assert wizard.index("apply_account(") < wizard.index("apply_migration()") < wizard.index("apply_sudo_password(")

    def test_a_migrated_account_is_not_asked_for_a_password(self):
        body = WIZARD.read_text()
        wizard = body[body.index("def wizard("):body.index("def main(")]
        assert wizard.index("account_plan") < wizard.index("step_account()")
        assert "if password is not None:" in wizard

    def test_the_tools_source_is_released_on_every_exit(self):
        body = WIZARD.read_text()
        main = body[body.index("def main("):]
        finally_block = main[main.index("finally:"):]
        assert "close_migration()" in finally_block

    def test_the_wizard_never_imports_the_tools_module(self):
        # The wizard is frozen; the package moves. The subprocess boundary is
        # what lets them drift, and an import would quietly undo it.
        assert "from migrate import" not in WIZARD.read_text()
        assert "import migrate" not in WIZARD.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_firstboot.py -k "migrat or choose_preselects or copy_runs or released" -v`
Expected: FAIL.

- [ ] **Step 3: Change the wizard**

Add `import json` to the imports. After `SUDOERS_NOPASSWD_RULE`, add:

```python
# The packaged migration tool. Probed rather than assumed, for the reason
# GNOME_KEYRING_DAEMON is: this wizard is frozen at write time, and the tool
# comes from a package that a stick written before it existed does not have.
# It is spoken to over argv and JSON and never imported, because that is the
# boundary that survives the two moving at different speeds.
MIGRATE_TOOL = "/usr/bin/portlin-migrate"
MIGRATION_PLAN = Path("/run/portlin/migrate/plan.json")
```

Change `choose`:

```python
def choose(title: str, text: str, entries: list[tuple[str, str]], height: int = 20, default: str = "") -> str:
    rows: list[str] = []
    for tag, label in entries:
        rows += [tag, label]
    listsize = min(len(entries), max(height - 8, 4))
    args = ["--title", title, "--menu", text, str(height), "72", str(listsize), *rows]
    if default:
        # Prefilled from another portlin, so the answer is the same one
        # keypress away as a fresh answer would be.
        args = ["--default-item", default, *args]
    return _whiptail(args, capture=True)
```

Change the question steps to take defaults:

```python
def step_keyboard(default: str = "") -> tuple[str, str]:
    known = dict(COMMON_KEYMAPS)
    layout = choose(
        "Keyboard layout",
        "Which keyboard layout does this machine have?\n"
        "You can pick a different one later from the Xfce settings.",
        COMMON_KEYMAPS + [("other", "Something else (type the code)")],
        default=default if default in known else ("other" if default else ""),
    )
    if layout == "other":
        layout = ask("Keyboard layout", "Enter an X11 layout code, for example 'cz' or 'br':", default or "us")
    return layout, known.get(layout, layout)


def step_locale(default: str = "") -> str:
    known = dict(COMMON_LOCALES)
    locale = choose(
        "Language",
        "Which language and regional formats should the desktop use?",
        COMMON_LOCALES + [("other", "Something else (type the name)")],
        default=default if default in known else ("other" if default else ""),
    )
    if locale == "other":
        locale = ask("Language", "Enter a locale name, for example 'cs_CZ.UTF-8':", default or "en_US.UTF-8")
    return locale
```

In `step_timezone(default: str = "")`, pass `default=default.split("/", 1)[0]` to the region `choose` and `default=default` to the city `choose`. In `step_hostname(default: str = "portlin")`, pass `default` as the third argument to `ask` instead of the literal `"portlin"`.

`step_autologin(username, *, encrypted, default_yes: bool = False)` passes `default_yes=default_yes` to `confirm`; `step_sudo_password(username, *, encrypted, autologin, default_yes: bool = True)` passes `default_yes=default_yes`.

Add the migration functions after `step_welcome`:

```python
def migration_candidates() -> list[dict]:
    """What portlin-migrate can see, or nothing when it is absent or confused.

    Nothing here may stop setup. A tool that is missing, that fails, or that
    prints something other than a list of candidates means there is simply
    no restore screen, and the wizard goes on as it did before the tool
    existed.
    """
    if not Path(MIGRATE_TOOL).exists():
        return []
    try:
        proc = subprocess.run(
            [MIGRATE_TOOL, "candidates", "--json"], input="", capture_output=True, text=True, timeout=60
        )
        found = json.loads(proc.stdout) if proc.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if not isinstance(found, list) or not all(isinstance(c, dict) and "path" in c for c in found):
        return []
    return found


def step_migration(candidates: list[dict]) -> dict | None:
    """Offer a restore, and hand the choosing to the tool's own screens.

    The tool is run with the console rather than through run(): its screens
    are whiptail too, and they need tty1 the way this wizard's do. It keeps
    the old drive open afterwards so apply can copy from it without asking
    for the passphrase twice.
    """
    names = "\n".join(
        f"  {c.get('model') or Path(c['path']).name} {c.get('size', '')}".rstrip() for c in candidates
    )
    if not confirm(
        "Restore from another portlin",
        f"\nAnother portlin was found:\n\n{names}\n\n"
        "Bring its account, files and settings over to this stick?\n\n"
        "You choose exactly what to bring on the next screen.",
    ):
        return None
    proc = subprocess.run([MIGRATE_TOOL, "plan", "--firstboot", "--keep-open", "--out", str(MIGRATION_PLAN)])
    if proc.returncode != 0:
        return None
    try:
        plan = json.loads(MIGRATION_PLAN.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("ids"), list):
        return None
    log(f"migration planned from {plan.get('source')}: {len(plan['ids'])} items")
    return plan


def apply_migration() -> bool:
    """The copy, drawn by the tool's own gauge on the console."""
    return subprocess.run([MIGRATE_TOOL, "apply", "--plan", str(MIGRATION_PLAN), "--gauge"]).returncode == 0


def close_migration() -> None:
    """Release the old drive and the plan, on every way out of setup."""
    if not MIGRATION_PLAN.exists():
        return
    if Path(MIGRATE_TOOL).exists():
        subprocess.run([MIGRATE_TOOL, "close"], input="", capture_output=True, text=True)
    MIGRATION_PLAN.unlink(missing_ok=True)
```

Rewrite `wizard()`:

```python
def wizard() -> None:
    step_welcome()

    migration = None
    candidates = migration_candidates()
    if candidates:
        migration = step_migration(candidates)
    prefill = (migration or {}).get("identity") or {}
    account_plan = (migration or {}).get("account")
    migrated_ids = set((migration or {}).get("ids") or [])

    layout, _ = step_keyboard(default=prefill.get("keyboard", ""))
    apply_keyboard(layout)

    locale = step_locale(default=prefill.get("locale", ""))
    timezone = step_timezone(default=prefill.get("timezone", ""))
    hostname = step_hostname(default=prefill.get("hostname") or "portlin")
    if account_plan:
        # The tool recreates the account with its password hash, so there is
        # no password to ask for and none to hold here.
        username, full_name, password = account_plan["name"], account_plan.get("gecos", ""), None
        message(
            "Your account",
            f"\nThe account '{username}' comes over from the old drive, with its password.",
        )
    else:
        username, full_name, password = step_account()

    luks_device = _luks_device()
    autologin = step_autologin(
        username, encrypted=luks_device is not None,
        default_yes=bool(account_plan and account_plan.get("autologin")),
    )
    sudo_password = step_sudo_password(
        username, encrypted=luks_device is not None, autologin=autologin,
        default_yes=not (account_plan and account_plan.get("sudo_nopasswd")),
    )
    has_desktop = _desktop_installed()
    # A migrated theme is applied by the tool; asking here would override it.
    theme = step_theme() if has_desktop and "extras.theme" not in migrated_ids else ""
    icon_theme = step_icon_theme() if has_desktop and "extras.theme" not in migrated_ids else ""
    expand = step_expand()

    if not confirm(
        "Ready to apply",
        f"\nAccount:   {username}\n"
        f"Computer:  {hostname}\n"
        f"Language:  {locale}\n"
        f"Keyboard:  {layout}\n"
        f"Time zone: {timezone}\n"
        f"Autologin: {'yes' if autologin else 'no'}\n"
        f"Sudo:      {'asks for a password' if sudo_password else 'no password'}\n"
        + (f"Theme:     {theme}\n" if theme else "")
        + (f"Icons:     {icon_theme}\n" if icon_theme else "")
        + (f"Restore:   {len(migrated_ids)} items from {migration['label']}\n" if migration else "")
        + f"Expand:    {'yes, use the whole drive' if expand else 'no'}\n\n"
        "Apply these settings now?",
    ):
        raise Cancelled()

    if finalise_encryption():
        info(
            "Encryption",
            "\nThis drive was encrypted during boot.\n\n"
            "Recording the settings so it unlocks normally from now on.",
        )

    if expand:
        info(
            "Expanding",
            "\nGrowing the system to fill the drive.\n\n"
            "This takes a few seconds. Do not remove the drive.",
        )
        apply_expand()

    apply_locale(locale)
    apply_timezone(timezone)
    apply_hostname(hostname)
    if password is not None:
        apply_account(username, full_name, password)
    if migration:
        # Right after the account exists, so files land under the right uid,
        # and before anything that reads the home directory.
        if not apply_migration():
            message(
                "Restore",
                "\nNot everything could be brought over. The details are in "
                "/var/log/portlin-migrate.log.\n\nSetup continues. You can try again "
                "later from Migrate in the applications menu.",
            )
        if password is None and not _user_exists(username):
            raise RuntimeError(f"the account {username} was not created by the restore")
    if not apply_sudo_password(username, sudo_password):
        message(
            "Administrator password",
            "\nThe rule that skips the sudo password could not be installed, "
            f"so sudo will ask '{username}' for one after all.",
        )
    if theme:
        apply_theme(theme)
    if icon_theme:
        apply_icon_theme(icon_theme)
    apply_autologin(username, autologin)
    if autologin and luks_device:
        apply_keyring_autounlock(username)
    drop_passphrase_stash()
    refresh_initramfs_for_keymap(luks_device is not None)

    if luks_device:
        step_change_passphrase(luks_device)

    SENTINEL.unlink(missing_ok=True)
    run(["systemctl", "disable", "portlin-firstboot.service"], check=False)
    message(
        "All set",
        f"\nWelcome, {full_name or username}.\n\n"
        "The desktop is starting now. Everything you change on this stick is "
        "saved to it, on whatever machine you plug it into next.",
    )
```

Keep the existing comments in the parts of `wizard()` that did not change (the finalise and expand blocks). In `main()`, add `close_migration()` as the first line of the `finally:` block, before `claim_console(False)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_firstboot.py -v`
Expected: PASS. `test_it_imports_nothing_outside_the_standard_library` must still pass (`json` is stdlib).

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/firstboot/portlin-firstboot tests/test_firstboot.py
git commit -m "feat(firstboot): offer to restore from another portlin"
```

---

### Task 12: the Migrate window

**Files:**
- Create: `portlin/resources/runtime/portlin-migration`
- Create: `portlin/resources/runtime/portlin-migration.desktop`
- Modify: `portlin/package.py:53-68` (`DESKTOP_TOOLS`, `MENU_ENTRIES`)
- Test: `tests/test_migration_window.py`, `tests/test_package.py`

**Interfaces:**
- Consumes: `portlin-migrate candidates --json`, `inventory SOURCE --json` (passphrase on stdin), `apply --plan FILE` (passphrase on stdin), `export FILE`; `migrate.CATEGORIES`, `migrate.human`, `migrate.from_json`, `migrate.describe`, `migrate.Candidate`; the plan keys `apply_plan` reads (`source`, `kind`, `encrypted`, `ids`, `firstboot`, `label`, `inventory`).
- Produces (in the window module): `TOOL = "/usr/bin/portlin-migrate"`, `PLAN_PATH` (under `$XDG_CACHE_HOME` or `~/.cache`, `portlin/migrate-plan.json`), `elevation_argv(passwordless_sudo)`, `candidates_argv(*, passwordless_sudo)`, `inventory_argv(source, *, passwordless_sudo)`, `apply_argv(plan, *, passwordless_sudo)`, `export_argv(destination, *, passwordless_sudo)`, `parse_event(line)`, `explain_exit(code)`, `tree_rows(inventory) -> list[tuple[str, str, list[tuple[str, str, str, bool]]]]`, `plan_document(candidate: dict, ids, inventory_data: dict) -> dict`, `class Job`, `class MigrationWindow`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migration_window.py`:

```python
"""Checks on the Migrate window, and the menu entry that opens it.

As with the Software window, nothing here draws anything. What is checked is
every decision the program makes before it draws: the command lines it runs
through pkexec, how an inventory becomes rows of tick boxes, and that the
plan it writes carries exactly the keys portlin-migrate apply reads.
"""

from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from conftest import load_tool
from test_software import _stub_gi

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
WINDOW = RUNTIME / "portlin-migration"
ENTRY = RUNTIME / "portlin-migration.desktop"
POLICY = RUNTIME / "org.portlin.migrate.policy"


@pytest.fixture(scope="module")
def window():
    _stub_gi()
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    loader = importlib.machinery.SourceFileLoader("portlin_migration", str(WINDOW))
    spec = importlib.util.spec_from_file_location(loader.name, WINDOW, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


class TestCommandLines:
    def test_everything_goes_through_the_one_tool_as_root(self, window):
        assert window.candidates_argv(passwordless_sudo=False) == ["pkexec", window.TOOL, "candidates", "--json"]
        assert window.inventory_argv("/dev/sdb4", passwordless_sudo=False) == [
            "pkexec", window.TOOL, "inventory", "/dev/sdb4", "--json",
        ]
        assert window.apply_argv("/home/x/.cache/portlin/migrate-plan.json", passwordless_sudo=True) == [
            "sudo", "-n", window.TOOL, "apply", "--plan", "/home/x/.cache/portlin/migrate-plan.json",
        ]
        assert window.export_argv("/media/x/D", passwordless_sudo=False) == ["pkexec", window.TOOL, "export", "/media/x/D"]

    def test_the_polkit_action_grants_exactly_that_tool(self, window):
        granted = re.search(r'exec\.path">([^<]+)<', POLICY.read_text()).group(1)
        assert granted == window.TOOL

    def test_exit_codes_are_explained_including_no_space(self, window):
        assert window.explain_exit(0) == ""
        assert "cancelled" in window.explain_exit(126).lower()
        assert "space" in window.explain_exit(6).lower()


class TestRows:
    def test_an_inventory_becomes_headed_groups_of_tickable_rows(self, window, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        inventory = migrate.build_inventory(tmp_path)
        groups = window.tree_rows(inventory)
        headings = [heading for _, heading, _ in groups]
        assert headings[:2] == ["Account", "Machine identity"]
        assert "Network" not in headings
        files = next(rows for category, _, rows in groups if category == "home.files")
        assert ("home.files.Documents", "Documents", "1 KB", True) in files
        settings = next(rows for category, _, rows in groups if category == "home.settings")
        assert ("home.settings..cache", ".cache", "300 B", False) in settings

    def test_a_note_is_part_of_the_label(self, window, migrate):
        inventory = migrate.Inventory("0.1.2", "office", "home/x", (
            migrate.Item("software.nvidia", "software", "NVIDIA driver", note="describes the old machine", default=False),
        ))
        rows = window.tree_rows(inventory)[0][2]
        assert rows[0][1] == "NVIDIA driver (describes the old machine)"
        assert rows[0][2] == ""


class TestPlan:
    def test_the_plan_carries_what_apply_reads_and_nothing_secret(self, window):
        candidate = {"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk Ultra", "size": "57.3G",
                     "encrypted": True, "version": ""}
        inventory_data = {"version": "0.1.2", "hostname": "office", "home": "home/olduser",
                          "items": [], "accounts": [], "identity": {}}
        plan = window.plan_document(candidate, ["home.files.Documents"], inventory_data)
        assert set(plan) >= {"source", "kind", "encrypted", "ids", "firstboot", "label", "inventory"}
        assert plan["source"] == "/dev/sdb4" and plan["kind"] == "stick" and plan["encrypted"] is True
        assert plan["firstboot"] is False
        assert plan["label"] == "SanDisk Ultra 57.3G   portlin, encrypted"
        assert "passphrase" not in json.dumps(plan)


class TestMenuEntry:
    def test_it_parses_and_opens_the_window_under_system(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        entry = parser["Desktop Entry"]
        assert entry["Exec"] == "portlin-migration"
        assert entry["Icon"] == "portlin"
        assert "System" in entry["Categories"]
        assert entry["Terminal"] == "false"
```

Append to `tests/test_package.py`:

```python
def test_desktop_ships_the_migration_app_and_its_menu_entry():
    files = package.text_files("portlin-desktop")
    assert "usr/bin/portlin-migration" in files
    assert "usr/bin/portlin-migration" in package.executable_paths("portlin-desktop")
    assert "usr/share/applications/portlin-migration.desktop" in files
    assert "usr/bin/portlin-migration" not in package.text_files("portlin-runtime")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_migration_window.py tests/test_package.py::test_desktop_ships_the_migration_app_and_its_menu_entry -v`
Expected: FAIL (no such file).

- [ ] **Step 3: Write the window**

Create `portlin/resources/runtime/portlin-migration` (mode 755):

```python
#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Migrate: bring files and settings over from another portlin.

This window runs as the user and touches no drive itself. Every action is
portlin-migrate, started as a child process through pkexec, and this is a
reader of what it prints: a JSON list of candidates, a JSON inventory, and
the ::step / ::progress / ::warn / ::result protocol while it copies. So
the program that can be asked to read another drive and write into this
one is one command with a fixed set of verbs, and the same verbs work from
a terminal.

Three pages: where from, what to bring, and the copy with its log.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, "/usr/lib/portlin")

from migrate import CATEGORIES, Candidate, Inventory, describe, from_json, human  # noqa: E402

APP_ID = "org.portlin.Migration"
TOOL = "/usr/bin/portlin-migrate"
ICON_NAME = "portlin"
PLAN_PATH = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "portlin" / "migrate-plan.json"

EXIT_OK = 0
EXIT_PRIVILEGE = 3
EXIT_NO_SPACE = 6
PKEXEC_REFUSED = 126
PKEXEC_NO_AGENT = 127


def elevation_argv(passwordless_sudo: bool) -> list[str]:
    """As in Software: pkexec draws a password dialog; someone who waived the
    sudo password at first boot has said they do not want one."""
    return ["sudo", "-n"] if passwordless_sudo else ["pkexec"]


def candidates_argv(*, passwordless_sudo: bool) -> list[str]:
    return [*elevation_argv(passwordless_sudo), TOOL, "candidates", "--json"]


def inventory_argv(source: str, *, passwordless_sudo: bool) -> list[str]:
    return [*elevation_argv(passwordless_sudo), TOOL, "inventory", source, "--json"]


def apply_argv(plan: str, *, passwordless_sudo: bool) -> list[str]:
    return [*elevation_argv(passwordless_sudo), TOOL, "apply", "--plan", plan]


def export_argv(destination: str, *, passwordless_sudo: bool) -> list[str]:
    return [*elevation_argv(passwordless_sudo), TOOL, "export", destination]


def parse_event(line: str) -> tuple[str, str] | None:
    if not line.startswith("::"):
        return None
    kind, _, rest = line[2:].partition(" ")
    return kind, rest.strip()


def explain_exit(code: int) -> str:
    if code == EXIT_OK:
        return ""
    if code == PKEXEC_REFUSED:
        return "Authentication was cancelled, so nothing was changed."
    if code == PKEXEC_NO_AGENT:
        return ("No authentication agent answered, so the password could not be "
                "asked for. Log out and in, or install mate-polkit.")
    if code == EXIT_PRIVILEGE:
        return "That was run with the wrong privileges. Nothing was changed."
    if code == EXIT_NO_SPACE:
        return "There is not enough free space on this drive for that selection. Nothing was copied."
    return f"It did not finish (exit status {code}). The log says where it stopped."


def tree_rows(inventory: Inventory) -> list[tuple[str, str, list[tuple[str, str, str, bool]]]]:
    """(category, heading, [(id, label, size, ticked)]) for every category with items."""
    groups = []
    for category, heading in CATEGORIES:
        rows = []
        for item in inventory.items:
            if item.category != category:
                continue
            label = f"{item.label} ({item.note})" if item.note else item.label
            rows.append((item.id, label, human(item.bytes) if item.bytes else "", item.default))
        if rows:
            groups.append((category, heading, rows))
    return groups


def plan_document(candidate: dict, ids: list[str], inventory_data: dict) -> dict:
    """The plan file apply reads. The passphrase is never in it: apply asks
    again on stdin, and this window hands over the one it already holds."""
    return {
        "source": candidate["path"],
        "kind": candidate["kind"],
        "encrypted": bool(candidate.get("encrypted")),
        "ids": list(ids),
        "firstboot": False,
        "label": describe(Candidate(**candidate)),
        "inventory": inventory_data,
    }


def has_passwordless_sudo(run=subprocess.run) -> bool:
    try:
        return run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Job:
    """One portlin-migrate run, read line by line without blocking the window.

    ``stdin_text`` is the passphrase for an encrypted source, written and
    closed before the first line is read, and held nowhere else.
    """

    def __init__(self, argv, *, on_event, on_output, on_done, stdin_text: str | None = None) -> None:
        self.argv = argv
        self.on_event = on_event
        self.on_output = on_output
        self.on_done = on_done
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if stdin_text is not None:
            self.proc.stdin.write(stdin_text + "\n")
            self.proc.stdin.close()
        GLib.io_add_watch(self.proc.stdout, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, self._on_readable)

    def _on_readable(self, source, condition) -> bool:
        line = source.readline()
        if line:
            line = line.rstrip("\n")
            event = parse_event(line)
            if event:
                self.on_event(*event)
            else:
                self.on_output(line)
            return True
        self.proc.wait()
        self.on_done(self.proc.returncode)
        return False


class MigrationWindow(Gtk.ApplicationWindow):
    def __init__(self, application=None, *, sudo=None) -> None:
        super().__init__(application=application, title="Migrate")
        self.set_default_size(820, 600)
        self.set_icon_name(ICON_NAME)
        self.passwordless_sudo = has_passwordless_sudo() if sudo is None else sudo
        self.candidates: list[dict] = []
        self.candidate: dict | None = None
        self.inventory_data: dict | None = None
        self.passphrase: str | None = None
        self.job: Job | None = None
        self.collected: list[str] = []
        self._build()
        self._refresh()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        header = Gtk.HeaderBar(title="Migrate", show_close_button=True)
        menu = Gtk.MenuButton()
        menu.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        export = Gtk.ModelButton(text="Back up this stick...")
        export.connect("clicked", lambda *_: self._export())
        box.pack_start(export, False, False, 0)
        box.show_all()
        popover.add(box)
        menu.set_popover(popover)
        header.pack_end(menu)
        self.set_titlebar(header)

        self.stack = Gtk.Stack()
        self.add(self.stack)
        self.stack.add_named(self._source_page(), "source")
        self.stack.add_named(self._choose_page(), "choose")
        self.stack.add_named(self._progress_page(), "progress")

    def _padded(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(12)
        return box

    def _source_page(self) -> Gtk.Widget:
        page = self._padded()
        intro = Gtk.Label(label="Where should files and settings come from?", xalign=0.0)
        page.pack_start(intro, False, False, 0)
        self.sources = Gtk.ListBox()
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.add(self.sources)
        page.pack_start(scroller, True, True, 0)
        self.sources_note = Gtk.Label(xalign=0.0)
        page.pack_start(self.sources_note, False, False, 0)
        buttons = Gtk.Box(spacing=6)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self._refresh())
        buttons.pack_start(refresh, False, False, 0)
        archive = Gtk.Button(label="Choose archive...")
        archive.connect("clicked", lambda *_: self._choose_archive())
        buttons.pack_start(archive, False, False, 0)
        self.next_button = Gtk.Button(label="Next")
        self.next_button.connect("clicked", lambda *_: self._open_selected())
        buttons.pack_end(self.next_button, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    def _choose_page(self) -> Gtk.Widget:
        page = self._padded()
        self.choose_title = Gtk.Label(xalign=0.0)
        page.pack_start(self.choose_title, False, False, 0)
        # id, label, size, ticked, inconsistent, is a heading
        self.store = Gtk.TreeStore(str, str, str, bool, bool, bool)
        self.tree = Gtk.TreeView(model=self.store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggled)
        column = Gtk.TreeViewColumn("", toggle, active=3, inconsistent=4)
        self.tree.append_column(column)
        self.tree.append_column(Gtk.TreeViewColumn("Bring over", Gtk.CellRendererText(), text=1))
        size = Gtk.CellRendererText(xalign=1.0)
        self.tree.append_column(Gtk.TreeViewColumn("Size", size, text=2))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.add(self.tree)
        page.pack_start(scroller, True, True, 0)
        self.choose_note = Gtk.Label(
            label="Files already here with the same names are moved aside, not deleted.", xalign=0.0
        )
        page.pack_start(self.choose_note, False, False, 0)
        buttons = Gtk.Box(spacing=6)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self.stack.set_visible_child_name("source"))
        buttons.pack_start(back, False, False, 0)
        start = Gtk.Button(label="Start")
        start.get_style_context().add_class("suggested-action")
        start.connect("clicked", lambda *_: self._start_apply())
        buttons.pack_end(start, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    def _progress_page(self) -> Gtk.Widget:
        page = self._padded()
        self.status = Gtk.Label(xalign=0.0)
        page.pack_start(self.status, False, False, 0)
        self.progress = Gtk.ProgressBar()
        page.pack_start(self.progress, False, False, 0)
        self.log = Gtk.TextView(editable=False, monospace=True)
        buffer = self.log.get_buffer()
        self.log_end = buffer.create_mark("end", buffer.get_end_iter(), False)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.add(self.log)
        page.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        self.again = Gtk.Button(label="Migrate something else")
        self.again.connect("clicked", lambda *_: self._refresh())
        buttons.pack_start(self.again, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        buttons.pack_end(close, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    # -- page one: where from ----------------------------------------------

    def _refresh(self) -> None:
        self.stack.set_visible_child_name("source")
        self.sources_note.set_text("Looking for portlin drives and backup archives...")
        self.collected = []
        self._run(candidates_argv(passwordless_sudo=self.passwordless_sudo), on_done=self._on_candidates)

    def _on_candidates(self, code: int) -> None:
        for child in self.sources.get_children():
            self.sources.remove(child)
        self.candidates = []
        if code != EXIT_OK:
            self.sources_note.set_text(explain_exit(code))
            return
        try:
            self.candidates = json.loads("\n".join(self.collected))
        except ValueError:
            self.sources_note.set_text("The tool did not answer with a list.")
            return
        for candidate in self.candidates:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=describe(Candidate(**candidate)), xalign=0.0)
            for side in ("start", "end", "top", "bottom"):
                getattr(label, f"set_margin_{side}")(8)
            row.add(label)
            row.candidate = candidate
            self.sources.add(row)
        self.sources.show_all()
        if self.candidates:
            self.sources.select_row(self.sources.get_row_at_index(0))
            self.sources_note.set_text("")
        else:
            self.sources_note.set_text(
                "No other portlin drive and no backup archive was found. Plug one in and refresh."
            )

    def _choose_archive(self) -> None:
        dialog = Gtk.FileChooserDialog(title="Choose a backup archive", parent=self,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        pattern = Gtk.FileFilter()
        pattern.set_name("portlin backups")
        pattern.add_pattern("*.portlin-backup.tar.zst")
        dialog.add_filter(pattern)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            self.candidate = {"kind": "archive", "path": path, "model": Path(path).parent.name,
                              "size": "", "encrypted": False, "version": ""}
            dialog.destroy()
            self._open(self.candidate)
            return
        dialog.destroy()

    def _open_selected(self) -> None:
        row = self.sources.get_selected_row()
        if row is None:
            return
        self._open(row.candidate)

    def _open(self, candidate: dict) -> None:
        self.candidate = candidate
        self.passphrase = None
        if candidate.get("encrypted"):
            self.passphrase = self._ask_passphrase()
            if self.passphrase is None:
                return
        self.sources_note.set_text("Reading what it holds...")
        self.collected = []
        self._run(
            inventory_argv(candidate["path"], passwordless_sudo=self.passwordless_sudo),
            on_done=self._on_inventory,
            stdin_text=self.passphrase,
        )

    def _ask_passphrase(self) -> str | None:
        dialog = Gtk.Dialog(title="Unlock the old drive", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Unlock", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(12)
        box.add(Gtk.Label(label="Enter the passphrase for the old drive:", xalign=0.0))
        entry = Gtk.Entry(visibility=False, activates_default=True)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        answer = entry.get_text() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        return answer

    def _on_inventory(self, code: int) -> None:
        if code != EXIT_OK:
            self.sources_note.set_text(explain_exit(code) or "The drive could not be read.")
            return
        try:
            text = "\n".join(self.collected)
            inventory = from_json(text)
            self.inventory_data = json.loads(text)
        except (ValueError, KeyError, TypeError):
            self.sources_note.set_text("The tool did not answer with an inventory.")
            return
        self.sources_note.set_text("")
        self.store.clear()
        for category, heading, rows in tree_rows(inventory):
            all_on = all(on for _, _, _, on in rows)
            any_on = any(on for _, _, _, on in rows)
            parent = self.store.append(None, [category, heading, "", all_on, any_on and not all_on, True])
            for item_id, label, size, on in rows:
                self.store.append(parent, [item_id, label, size, on, False, False])
        self.tree.expand_all()
        self.choose_title.set_text(f"From {describe(Candidate(**self.candidate))}")
        self.stack.set_visible_child_name("choose")

    # -- page two: what to bring -------------------------------------------

    def _on_toggled(self, renderer, path) -> None:
        row = self.store.get_iter(path)
        on = not self.store[row][3]
        self.store[row][3] = on
        self.store[row][4] = False
        if self.store[row][5]:
            child = self.store.iter_children(row)
            while child is not None:
                self.store[child][3] = on
                child = self.store.iter_next(child)
        else:
            parent = self.store.iter_parent(row)
            states = []
            child = self.store.iter_children(parent)
            while child is not None:
                states.append(self.store[child][3])
                child = self.store.iter_next(child)
            self.store[parent][3] = all(states)
            self.store[parent][4] = any(states) and not all(states)

    def _chosen_ids(self) -> list[str]:
        ids = []
        parent = self.store.get_iter_first()
        while parent is not None:
            child = self.store.iter_children(parent)
            while child is not None:
                if self.store[child][3]:
                    ids.append(self.store[child][0])
                child = self.store.iter_next(child)
            parent = self.store.iter_next(parent)
        return ids

    def _start_apply(self) -> None:
        ids = self._chosen_ids()
        if not ids:
            self.choose_note.set_text("Nothing is ticked.")
            return
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAN_PATH.write_text(json.dumps(plan_document(self.candidate, ids, self.inventory_data), indent=2))
        PLAN_PATH.chmod(0o600)
        self.stack.set_visible_child_name("progress")
        self.progress.set_fraction(0.0)
        self.status.set_text("Starting...")
        self.again.set_sensitive(False)
        argv = apply_argv(str(PLAN_PATH), passwordless_sudo=self.passwordless_sudo)
        self._append(f"$ {shlex.join(argv)}")
        self._run(argv, on_done=self._on_applied, stdin_text=self.passphrase, show=True)

    def _on_applied(self, code: int) -> None:
        self.passphrase = None
        self.again.set_sensitive(True)
        if code == EXIT_OK:
            self.status.set_text("Done.")
            self.progress.set_fraction(1.0)
        else:
            message = explain_exit(code)
            self.status.set_text(message)
            self._append(message)

    # -- backing up --------------------------------------------------------

    def _export(self) -> None:
        dialog = Gtk.FileChooserDialog(title="Back up this stick to", parent=self,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Back up here", Gtk.ResponseType.OK)
        if dialog.run() != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        folder = dialog.get_filename()
        dialog.destroy()
        self.stack.set_visible_child_name("progress")
        self.progress.set_fraction(0.0)
        self.status.set_text("Starting...")
        self.again.set_sensitive(False)
        argv = export_argv(folder, passwordless_sudo=self.passwordless_sudo)
        self._append(f"$ {shlex.join(argv)}")
        self._run(argv, on_done=self._on_applied, show=True)

    # -- running the tool --------------------------------------------------

    def _run(self, argv, *, on_done, stdin_text: str | None = None, show: bool = False) -> None:
        self.collected = []

        def on_output(line: str) -> None:
            if show:
                self._append(line)
            else:
                self.collected.append(line)

        try:
            self.job = Job(argv, on_event=self._on_event, on_output=on_output,
                           on_done=lambda code: (setattr(self, "job", None), on_done(code)),
                           stdin_text=stdin_text)
        except OSError as exc:
            self.job = None
            self.status.set_text(f"Could not start {argv[0]}: {exc}")
            self.sources_note.set_text(f"Could not start {argv[0]}: {exc}")

    def _append(self, line: str) -> None:
        buffer = self.log.get_buffer()
        buffer.insert(buffer.get_end_iter(), line + "\n")
        buffer.move_mark(self.log_end, buffer.get_end_iter())
        self.log.scroll_mark_onscreen(self.log_end)

    def _on_event(self, kind: str, rest: str) -> None:
        if kind == "step":
            self.status.set_text(rest)
        elif kind == "progress":
            try:
                self.progress.set_fraction(min(1.0, max(0.0, int(rest) / 100)))
            except ValueError:
                pass
        elif kind == "warn":
            self._append(rest)
        elif kind == "result":
            self._append(rest)
            if rest.startswith("ok "):
                self.status.set_text(rest[3:])


def main() -> int:
    application = Gtk.Application(application_id=APP_ID)
    windows: dict[str, MigrationWindow] = {}

    def on_activate(app) -> None:
        if "window" in windows:
            windows["window"].present()
            return
        Gtk.Window.set_default_icon_name(ICON_NAME)
        window = windows["window"] = MigrationWindow(application=app)
        window.show_all()

    application.connect("activate", on_activate)
    return application.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
```

Create `portlin/resources/runtime/portlin-migration.desktop`:

```ini
[Desktop Entry]
Version=1.0
Type=Application
Name=Migrate
GenericName=Migration from another portlin
Comment=Bring files and settings over from another portlin drive or a backup
Exec=portlin-migration
Icon=portlin
Terminal=false
StartupNotify=true
# System, beside Software: it is a program that changes the system, and the
# menu files Settings entries away where nobody looks for a program to run.
Categories=System;
Keywords=migrate;restore;backup;transfer;move;old drive;
```

In `portlin/package.py`, add `"portlin-migration",` to `DESKTOP_TOOLS` after `"portlin-software"`, and to `MENU_ENTRIES`:

```python
    "portlin-migration.desktop": "usr/share/applications/portlin-migration.desktop",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `make test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portlin/resources/runtime/portlin-migration portlin/resources/runtime/portlin-migration.desktop portlin/package.py tests/test_migration_window.py tests/test_package.py
git commit -m "feat(desktop): add the Migrate window"
```

---

### Task 13: the real-device harness

**Files:**
- Create: `scripts/test-migrate.py`
- Modify: `Makefile:52-70` (the `harness` target)
- Test: `tests/test_harness_scripts.py` (if it enumerates harnesses, add this one; read it first)

**Interfaces:**
- Consumes: the installed `/usr/bin/portlin-migrate` from the packages `python3 -m portlin package` builds; the plan file keys `apply_plan` reads.

- [ ] **Step 1: Write the harness**

Create `scripts/test-migrate.py` (mode 755):

```python
#!/usr/bin/env python3
"""Migrate from a real second stick, encrypted or not, and from an archive.

The unit tests prove portlin-migrate plans the right rsync and tar command
lines. What only a real device shows is whether those lines do what the plan
says: that rsync's --backup-dir really moves a replaced file aside, that a
merged directory keeps its local-only file, that the account comes back with
the same uid and password hash, that the source is untouched and its LUKS
mapping is gone afterwards, and that an archive written by export restores
through the same checks.

    python3 scripts/test-migrate.py [--encrypt]
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = "/usr/bin/portlin-migrate"
PASSPHRASE = "migrate-harness-passphrase"
DISK = Path("/tmp/portlin-migrate-test.img")
DISK_SIZE = 4 * 1024**3
MOUNT = Path("/mnt/old-stick")
MAPPING = "old_portlin_root"
USER = "olduser"
UID = 1500
PASSWORD = "old-password"
failures: list[str] = []


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str], **kwargs) -> str:
    result = run(argv, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def ok(message: str) -> None:
    print(f"ok: {message}", flush=True)


def bad(message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    failures.append(message)


def check(condition: bool, message: str) -> None:
    ok(message) if condition else bad(message)


def ensure_node(path: str) -> None:
    name = Path(path).name
    numbers = Path("/sys/class/block") / name / "dev"
    if not numbers.exists():
        raise SystemExit(f"kernel does not know about {path}")
    major, minor = numbers.read_text().strip().split(":")
    if Path(path).exists():
        os.unlink(path)
    must(["mknod", "-m", "0660", path, "b", major, minor])


def install_portlin_packages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / "packages"
        built.mkdir()
        result = run([sys.executable, "-m", "portlin", "package", "--output", str(built)], cwd=REPO)
        if result.returncode != 0:
            sys.exit(f"building portlin's packages failed:\n{result.stderr}")
        debs = sorted(str(deb) for deb in built.glob("*.deb"))
        result = run(["apt-get", "install", "-y", "-q", *debs],
                     env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
        if result.returncode != 0:
            sys.exit(f"installing portlin's packages failed:\n{result.stdout}")
    check(Path(TOOL).exists(), f"{TOOL} is installed")


def make_old_stick(encrypt: bool) -> tuple[str, str]:
    """A second portlin, as write and first boot would leave it: labels, a
    release file, one account, a home, a wifi password, a software record."""
    DISK.unlink(missing_ok=True)
    with open(DISK, "wb") as handle:
        handle.truncate(DISK_SIZE)
    must(["sgdisk", "-n1:0:+1M", "-t1:EF02", "-n2:0:+64M", "-t2:EF00",
          "-n3:0:+128M", "-t3:8300", "-n4:0:0", "-t4:8300", str(DISK)])
    loop = must(["losetup", "-P", "-f", "--show", str(DISK)])
    boot, root = f"{loop}p3", f"{loop}p4"
    ensure_node(boot)
    ensure_node(root)
    must(["mkfs.ext4", "-q", "-F", "-L", "portlin-boot", boot])
    device = root
    if encrypt:
        formatted = subprocess.run(
            ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--pbkdf", "argon2id",
             "--pbkdf-memory", "32768", "--key-file", "-", root],
            input=PASSPHRASE, capture_output=True, text=True)
        if formatted.returncode != 0:
            raise SystemExit(f"luksFormat failed: {formatted.stderr}")
        opened = subprocess.run(["cryptsetup", "open", "--key-file", "-", root, MAPPING],
                                input=PASSPHRASE, capture_output=True, text=True)
        if opened.returncode != 0:
            raise SystemExit(f"open failed: {opened.stderr}")
        device = f"/dev/mapper/{MAPPING}"
    must(["mkfs.ext4", "-q", "-F", "-L", "portlin-root", device])
    MOUNT.mkdir(parents=True, exist_ok=True)
    must(["mount", device, str(MOUNT)])

    password_hash = must(["openssl", "passwd", "-6", "-salt", "saltsalt", PASSWORD])
    (MOUNT / "etc").mkdir()
    (MOUNT / "etc/portlin-release").write_text("PORTLIN_VERSION=0.0.9\nPORTLIN_URL=x\n")
    (MOUNT / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        f"{USER}:x:{UID}:{UID}:Old User,,,:/home/{USER}:/bin/bash\n")
    (MOUNT / "etc/shadow").write_text(f"root:*:19000:0:99999:7:::\n{USER}:{password_hash}:19000:0:99999:7:::\n")
    (MOUNT / "etc/group").write_text(f"root:x:0:\nsudo:x:27:{USER}\naudio:x:29:{USER}\n{USER}:x:{UID}:\n")
    (MOUNT / "etc/sudoers.d").mkdir()
    (MOUNT / "etc/sudoers.d/50-portlin-nopasswd").write_text(f"{USER} ALL=(ALL) NOPASSWD: ALL\n")
    (MOUNT / "etc/hostname").write_text("oldbox\n")
    connections = MOUNT / "etc/NetworkManager/system-connections"
    connections.mkdir(parents=True)
    (connections / "cafe.nmconnection").write_text("[wifi-security]\npsk=secret\n")
    (connections / "cafe.nmconnection").chmod(0o600)
    state = MOUNT / "var/lib/portlin/software"
    state.mkdir(parents=True)
    (state / "mullvad.json").write_text("{}\n")
    home = MOUNT / "home" / USER
    (home / "Documents").mkdir(parents=True)
    (home / "Documents/canary.txt").write_text("from the old stick\n")
    (home / "Downloads").mkdir()
    (home / "Downloads/big.bin").write_bytes(os.urandom(1024 * 1024))
    (home / ".config/app").mkdir(parents=True)
    (home / ".config/app/settings.ini").write_text("[app]\ncolour=blue\n")
    (home / ".cache").mkdir()
    (home / ".cache/junk").write_text("junk\n")
    (home / "link-to-docs").symlink_to("Documents")
    must(["chown", "-R", f"{UID}:{UID}", str(home)])
    must(["umount", str(MOUNT)])
    if encrypt:
        must(["cryptsetup", "close", MAPPING])
    return loop, root


def shadow_hash(user: str) -> str:
    for line in Path("/etc/shadow").read_text().splitlines():
        fields = line.split(":")
        if fields[0] == user:
            return fields[1]
    return ""


def tool(*verb: str, stdin: str = "") -> subprocess.CompletedProcess:
    return run([TOOL, *verb], input=stdin)


def write_plan(path: Path, *, source: str, kind: str, encrypted: bool, ids: list[str], inventory: dict) -> None:
    path.write_text(json.dumps({
        "source": source, "kind": kind, "encrypted": encrypted, "ids": ids,
        "firstboot": False, "label": "harness", "inventory": inventory,
    }))


def source_mtime(root: str, encrypt: bool) -> float:
    device = root
    if encrypt:
        subprocess.run(["cryptsetup", "open", "--readonly", "--key-file", "-", root, MAPPING],
                       input=PASSPHRASE, capture_output=True, text=True)
        device = f"/dev/mapper/{MAPPING}"
    must(["mount", "-o", "ro,noload", device, str(MOUNT)])
    try:
        return (MOUNT / "home" / USER / "Documents/canary.txt").stat().st_mtime
    finally:
        must(["umount", str(MOUNT)])
        if encrypt:
            must(["cryptsetup", "close", MAPPING])


def check_stick_to_stick(root: str, encrypt: bool) -> dict:
    stdin = PASSPHRASE + "\n" if encrypt else ""
    listed = tool("candidates", "--json")
    candidates = json.loads(listed.stdout or "[]")
    check(any(c["path"] == root and c["encrypted"] == encrypt for c in candidates),
          f"candidates lists {root} (encrypted={encrypt})")

    before = source_mtime(root, encrypt)

    read = tool("inventory", root, "--json", stdin=stdin)
    check(read.returncode == 0, "inventory reads the old stick")
    inventory = json.loads(read.stdout)
    ids = {item["id"]: item for item in inventory["items"]}
    check(f"account.{USER}" in ids, "the old account is offered")
    check(ids["home.files.Documents"]["bytes"] > 0, "home entries carry sizes")
    check(ids["home.settings..cache"]["default"] is False, ".cache is offered but off")
    check("software.mullvad" in ids, "the software record is offered")

    plan = Path("/tmp/migrate-plan.json")
    chosen = [f"account.{USER}", "identity.hostname", "home.files.Documents", "home.files.Downloads",
              "home.files.link-to-docs", "home.settings..config", "network"]
    write_plan(plan, source=root, kind="stick", encrypted=encrypt, ids=chosen, inventory=inventory)

    applied = tool("apply", "--plan", str(plan), stdin=stdin)
    print(applied.stdout)
    check(applied.returncode == 0, "apply exits 0")
    check("::result ok" in applied.stdout, "apply reports ok")
    check(any(line.startswith("::progress 100") for line in applied.stdout.splitlines()), "progress reaches 100")

    record = pwd.getpwnam(USER)
    check(record.pw_uid == UID, f"the account was recreated with uid {UID}")
    check(shadow_hash(USER) == must(["openssl", "passwd", "-6", "-salt", "saltsalt", PASSWORD]),
          "the password hash came across")
    check(Path("/etc/hostname").read_text().strip() == "oldbox", "the hostname was applied")
    home = Path(record.pw_dir)
    canary = home / "Documents/canary.txt"
    check(canary.read_text() == "from the old stick\n", "the canary arrived")
    check(canary.stat().st_uid == UID, "files belong to the local account")
    check((home / "link-to-docs").is_symlink(), "a symlink is still a symlink")
    check((home / ".config/app/settings.ini").exists(), "dot directories arrived")
    check(not (home / ".cache").exists(), "the unticked .cache did not")
    connection = Path("/etc/NetworkManager/system-connections/cafe.nmconnection")
    check(connection.exists() and stat.S_IMODE(connection.stat().st_mode) == 0o600 and connection.stat().st_uid == 0,
          "the wifi connection is root-owned and 0600")
    check(not (home / ".portlin-migrate-backup").exists(), "nothing was moved aside on a fresh home")

    check(run(["findmnt", "/run/portlin/migrate/source"]).returncode != 0, "the source is unmounted afterwards")
    check("portlin-migrate-source" not in run(["dmsetup", "ls"]).stdout, "the mapping is closed afterwards")
    check(source_mtime(root, encrypt) == before, "the source was not touched")

    print("== second run: collision, merge, idempotence ==", flush=True)
    canary.write_text("local edit\n")
    (home / "Documents/only-here.txt").write_text("keep me\n")
    time.sleep(1.1)
    applied = tool("apply", "--plan", str(plan), stdin=stdin)
    check(applied.returncode == 0, "a second apply exits 0")
    check(canary.read_text() == "from the old stick\n", "the old stick's file wins")
    check((home / "Documents/only-here.txt").read_text() == "keep me\n", "a local-only file survives the merge")
    backups = sorted((home / ".portlin-migrate-backup").glob("*/Documents/canary.txt"))
    check(bool(backups) and backups[-1].read_text() == "local edit\n", "the replaced file is in the backup tree")
    check("Files that were replaced are under" in applied.stdout, "the summary names the backup tree")
    return inventory


def check_archive_round_trip() -> None:
    print("== export and restore from the archive ==", flush=True)
    folder = Path("/tmp/migrate-backups")
    folder.mkdir(exist_ok=True)
    exported = tool("export", str(folder))
    print(exported.stdout)
    check(exported.returncode == 0, "export exits 0")
    archives = sorted(folder.glob("*.portlin-backup.tar.zst"))
    check(bool(archives), "an archive was written")
    if not archives:
        return
    archive = archives[-1]
    check(stat.S_IMODE(archive.stat().st_mode) == 0o600, "the archive is 0600")
    listing = must(["tar", "-I", "zstd", "-tf", str(archive)]).splitlines()
    check(listing[0] == "manifest.json", "the manifest is member 0")

    read = tool("inventory", str(archive), "--json")
    check(read.returncode == 0, "inventory reads the archive")
    inventory = json.loads(read.stdout)
    home = Path(pwd.getpwnam(USER).pw_dir)
    canary = home / "Documents/canary.txt"
    canary.write_text("clobbered\n")
    plan = Path("/tmp/migrate-archive-plan.json")
    write_plan(plan, source=str(archive), kind="archive", encrypted=False,
               ids=["home.files.Documents"], inventory=inventory)
    applied = tool("apply", "--plan", str(plan))
    print(applied.stdout)
    check(applied.returncode == 0, "apply from the archive exits 0")
    check(canary.read_text() == "from the old stick\n", "the canary is back from the archive")
    check(canary.stat().st_uid == UID, "restored files belong to the local account")
    backups = sorted((home / ".portlin-migrate-backup").glob("*/Documents/canary.txt"))
    check(bool(backups) and backups[-1].read_text() == "clobbered\n", "the clobbered file was moved aside")


DISPLAY = ":94"
WINDOW = "/usr/bin/portlin-migration"


def start_xvfb() -> subprocess.Popen:
    server = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1024x768x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(50):
        if subprocess.run(["xset", "q"], capture_output=True).returncode == 0:
            return server
        time.sleep(0.2)
    raise SystemExit("Xvfb never came up")


def check_the_window(inventory: dict) -> None:
    """Build the real window against a real X server and feed it what the
    tool printed, without a second pkexec: the answers are handed to the
    same handlers the Job would call."""
    import importlib.machinery
    import importlib.util

    server = start_xvfb()
    try:
        sys.path.insert(0, "/usr/lib/portlin")
        loader = importlib.machinery.SourceFileLoader("portlin_migration", WINDOW)
        spec = importlib.util.spec_from_file_location(loader.name, WINDOW, loader=loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        spec.loader.exec_module(module)

        # The constructor asks the tool for candidates; answer it by hand so
        # the window is built without a password dialog.
        module.MigrationWindow._refresh = lambda self: None
        window = module.MigrationWindow(sudo=False)
        window.show_all()
        window.collected = [json.dumps([{"kind": "stick", "path": "/dev/loop0p4", "model": "",
                                         "size": "4G", "encrypted": False, "version": "0.0.9"}])]
        window._on_candidates(0)
        check(len(window.sources.get_children()) == 1, "the window lists the candidate it was given")
        window.candidate = window.sources.get_row_at_index(0).candidate
        window.collected = [json.dumps(inventory)]
        window._on_inventory(0)
        check(window.stack.get_visible_child_name() == "choose", "reading an inventory moves to the choose page")
        chosen = window._chosen_ids()
        check("home.files.Documents" in chosen and "home.settings..cache" not in chosen,
              "the tree starts from the inventory's defaults")
        # Untick the home files heading and every child follows.
        parent = window.store.get_iter_first()
        while window.store[parent][0] != "home.files":
            parent = window.store.iter_next(parent)
        window._on_toggled(None, window.store.get_path(parent))
        check("home.files.Documents" not in window._chosen_ids(), "a heading toggles its children")
        plan = module.plan_document(window.candidate, window._chosen_ids(), window.inventory_data)
        check(plan["source"] == "/dev/loop0p4" and plan["firstboot"] is False, "the plan names the source")
        window.destroy()
    finally:
        server.terminate()
        server.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--encrypt", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux":
        raise SystemExit("needs Linux and root; run inside the container harness")

    run(["umount", str(MOUNT)])
    run(["cryptsetup", "close", MAPPING])
    run(["userdel", "-r", USER])

    install_portlin_packages()
    loop, root = make_old_stick(args.encrypt)
    try:
        inventory = check_stick_to_stick(root, args.encrypt)
        check_archive_round_trip()
        if not args.encrypt:
            check_the_window(inventory)
    finally:
        run(["umount", str(MOUNT)])
        run(["cryptsetup", "close", "portlin-migrate-source"])
        run(["cryptsetup", "close", MAPPING])
        run(["losetup", "-d", loop])
        DISK.unlink(missing_ok=True)
        run(["userdel", "-r", USER])

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: migrated from a real stick and from an archive, source untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add it to the harness target**

In `Makefile`, add `rsync zstd openssl` to the `apt-get install` list in the `harness` target, and after the `test-software.py` line add:

```make
	  python3 -u scripts/test-migrate.py && \
	  python3 -u scripts/test-migrate.py --encrypt && \
```

Update the comment above the target: eight harnesses, twelve runs, and one line saying `test-migrate.py` goes twice because an encrypted source is opened through a different pair of commands than a plain one.

- [ ] **Step 3: Check the harness against the unit tests that know about harnesses**

Run: `.venv/bin/python -m pytest tests/test_harness_scripts.py -v`
Expected: PASS. If that file lists harness scripts by name, add `test-migrate.py` to the list.

- [ ] **Step 4: Run the harness**

Run: `make harness`
Expected: every harness passes, including both `test-migrate.py` runs. If Docker is unavailable on this machine, say so in the task report rather than claiming the harness passed; the next task does not depend on it.

- [ ] **Step 5: Commit**

```bash
git add scripts/test-migrate.py Makefile tests/test_harness_scripts.py
git commit -m "test: migrate from a real second stick and from an archive"
```

---

### Task 14: documentation

**Files:**
- Modify: `README.md` (after the `## Software` section, before `## Updates`)
- Modify: `docs/superpowers/specs/2026-09-21-portlin-migrate-design.md` (the `## Archives` section)

- [ ] **Step 1: Describe the feature in the README**

Insert after the Software section:

```markdown
## Migrating

Plug your old portlin into a machine booted from a new one, and **Migrate** in the
applications menu brings across what you tick: the account with its password, the
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
```

- [ ] **Step 2: Correct the spec's archive section**

In the spec's `## Archives` section, replace the paragraph beginning "`/etc/shadow` is in the archive" with:

```markdown
The manifest carries the account's password hash, because the account is part
of what an archive restores. The file is written `0600`, owned by whoever ran
the export, and the CLI says so on the way out. Exporting to a drive readable
by anyone is the user's call, made with that line in front of them.
```

- [ ] **Step 3: Run the tests one last time**

Run: `make check`
Expected: PASS, shellcheck included.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-21-portlin-migrate-design.md
git commit -m "docs: describe migrating between portlin sticks"
```
