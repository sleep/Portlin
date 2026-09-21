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

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from catalog import ENTRIES, installed, parse_dpkg_status

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
