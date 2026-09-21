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
from typing import Callable

from catalog import ENTRIES, expand_home, parse_dpkg_status
from devices import UNCLAIMED_ADVICE

# Labels write puts on partitions 3 and 4. The boot label is the one that
# identifies a stick, because it is readable whether or not the root is
# encrypted.
BOOT_LABEL = "portlin-boot"
ROOT_LABEL = "portlin-root"
ARCHIVE_SUFFIX = ".portlin-backup.tar.zst"
MANIFEST = "manifest.json"
RELEASE_FILE = "etc/portlin-release"

# PARTN rather than guessing from the name: sda4 and nvme0n1p4 spell the
# number differently, and lsblk knows it either way. NAME is requested and
# never read: current util-linux only nests a disk's partitions under it as
# "children" in --json output when NAME is among the requested columns, and
# parse_lsblk's walk of disk["children"] finds nothing without it.
LSBLK_COLUMNS = "NAME,PATH,LABEL,FSTYPE,SIZE,MODEL,TRAN,PARTN"


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


def _partition_number(child: dict) -> int | None:
    """child["partn"] as an int. PARTN can come back as either a JSON string
    or a number depending on what printed it, and the parser should not
    depend on which: a string key here would never match the int lookups
    below, and every candidate would be silently dropped."""
    try:
        return int(child.get("partn"))
    except (TypeError, ValueError):
        return None


def parse_blkid_export(text: str) -> dict[str, str]:
    """blkid -o export's KEY=value lines, as a dict."""
    pairs = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            pairs[key] = value
    return pairs


def _probed(child: dict, probe: Callable[[str], dict[str, str]]) -> dict:
    """child with a missing fstype or label filled in from blkid.

    lsblk reads both from the udev database, which nothing populates in a
    freshly started container and which can lag behind a drive that was just
    plugged into real hardware. blkid probes the device itself rather than
    reading that cache, so it is the fallback for whichever of the two
    lsblk left blank -- never for a value lsblk already gave.
    """
    if child.get("fstype") and child.get("label"):
        return child
    info = probe(child["path"])
    filled = dict(child)
    if not filled.get("fstype"):
        filled["fstype"] = info.get("TYPE") or filled.get("fstype")
    if not filled.get("label"):
        filled["label"] = info.get("LABEL") or filled.get("label")
    return filled


def parse_lsblk(
    text: str, running_disk: str, *, probe: Callable[[str], dict[str, str]] | None = None
) -> list[Candidate]:
    """Every portlin stick in lsblk's output, other than the one running.

    Excluded by disk rather than by partition, so the running stick's own
    /boot is never offered as somewhere to migrate from. ``probe`` is asked
    about a partition only when lsblk left its fstype or label blank; see
    ``_probed`` for why that happens at all.
    """
    try:
        disks = json.loads(text).get("blockdevices", [])
    except (ValueError, AttributeError):
        return []
    found = []
    for disk in disks:
        if disk.get("path") == running_disk:
            continue
        parts = {}
        for child in disk.get("children", []):
            number = _partition_number(child)
            if number is not None:
                parts[number] = child
        boot, root = parts.get(3), parts.get(4)
        if not boot or not root:
            continue
        if probe:
            boot, root = _probed(boot, probe), _probed(root, probe)
        if boot.get("label") != BOOT_LABEL:
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
    # Kept out of the repr so a logged or printed Account never shows it.
    password_hash: str = field(repr=False)
    groups: tuple[str, ...]
    sudo_nopasswd: bool
    autologin: bool


@dataclass(frozen=True)
class Identity:
    hostname: str = ""
    locale: str = ""
    keyboard: str = ""
    timezone: str = ""


# What a value read off a source may look like before it is written into a
# file the system sources as root, spliced into /etc/hosts or handed to a
# command. The source is somebody else's stick or archive, so each value is
# checked here rather than trusted because it once passed the wizard's
# screens on another machine. The name and hostname rules are the wizard's;
# a test keeps the literals equal, since the wizard cannot import this file.
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
LOCALE_RE = re.compile(r"[A-Za-z0-9_.@-]+")
KEYBOARD_RE = re.compile(r"[a-z0-9_,+-]+")
TIMEZONE_RE = re.compile(r"[A-Za-z0-9_+/-]+")
THEME_NAME_RE = re.compile(r"[A-Za-z0-9 ._-]+")


def valid_identity_value(name: str, value: str) -> bool:
    # fullmatch throughout: match with a $ anchor would still let a value
    # through with a newline on the end, which is a second line in a file.
    if name == "hostname":
        return bool(HOSTNAME_RE.fullmatch(value))
    if name == "locale":
        return bool(LOCALE_RE.fullmatch(value))
    if name == "keyboard":
        return bool(KEYBOARD_RE.fullmatch(value))
    if name == "timezone":
        # The value becomes a path under /usr/share/zoneinfo, so it must not
        # be able to climb back out of it.
        return bool(TIMEZONE_RE.fullmatch(value)) and ".." not in value.split("/")
    return False


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


def _installed_on_source(entry, dpkg: set[str], root: Path, home: str) -> bool:
    """catalog.installed, with every path check rooted at the source.

    catalog.expand_home only prefixes a home onto ~/ paths and leaves an
    absolute one alone, which is right for the running system and wrong
    for a mounted one: /opt/palemoon would be this machine's, not the
    source's.
    """
    if entry.check.kind == "dpkg":
        return any(name in dpkg for name in entry.check.values)
    return any(
        (root / str(expand_home(value, Path("/") / home)).lstrip("/")).exists()
        for value in entry.check.values
    )


def _software_items(root: Path, home: str, entries, dpkg_status: str, firstboot: bool) -> list[Item]:
    recorded = {
        path.stem for path in sorted((root / SOFTWARE_STATE).glob("*.json"))
    } if (root / SOFTWARE_STATE).is_dir() else set()
    dpkg = parse_dpkg_status(dpkg_status)
    items = []
    for entry in entries:
        if entry.id not in recorded and not _installed_on_source(entry, dpkg, root, home):
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
    ``private`` creates every ``write`` readable by its owner alone.
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
    private: bool = False


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


# The fixed system paths a plan may touch, besides the home.
ALLOWED_SYSTEM_PATHS = (CONNECTIONS, BLUETOOTH, *CUPS_PATHS)


def _malformed(path: str) -> bool:
    """A relative path that names one thing and cannot climb or be an option."""
    parts = path.split("/")
    return (
        not path
        or path.startswith("/")
        or path.startswith("-")
        or ".." in parts
        or "" in parts
    )


def unsafe_paths(inventory: Inventory, ids: list[str]) -> list[str]:
    """Paths a plan may touch: the source home, and the fixed system paths the
    inventory knows about. A manifest is read from an archive somebody
    handed us and the plan runs as root, so anything else is refused by
    name rather than extracted."""
    bad = []
    for item in selected(inventory, ids):
        for path in item.paths:
            if _malformed(path) or not (is_home(path, inventory.home) or path in ALLOWED_SYSTEM_PATHS):
                bad.append(path)
    return bad


def confinement_root(destination: str, target: Target) -> Path | None:
    """The directory a target path is allowed to resolve into: the local
    home for home paths, /etc or /var for the system paths. None for a path
    that unsafe_paths would already have refused."""
    if is_home(destination, target.home):
        return target.root / target.home
    if any(destination == p or destination.startswith(p + "/") for p in ALLOWED_SYSTEM_PATHS):
        return target.root / destination.split("/", 1)[0]
    return None


def within(path: Path | str, root: Path | str) -> bool:
    """Whether path, with every symlink on the way resolved, is root or under it."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def entry_within(path: Path | str, root: Path | str) -> bool:
    """Like within, for an operation on the directory entry itself rather
    than on what it points at: a rename moves the entry, so it is the
    directory holding the entry that has to resolve inside root. The entry
    may itself be a symlink pointing anywhere, and moving that link aside
    touches nothing it points at."""
    return within(os.path.dirname(str(path)), root) and os.path.basename(str(path)) not in ("", ".", "..")


def _confined(destination: str, target: Target) -> None:
    """Raise unless the destination resolves inside the tree it belongs to.

    unsafe_paths keeps the path itself honest; this keeps the filesystem
    honest, where a symlink left at the destination by a previous run or by
    anyone with the account would otherwise have rsync or tar write on the
    far side of it.
    """
    root = confinement_root(destination, target)
    if root is None or not within(target.root / destination, root):
        raise ValueError(f"refusing to touch {destination}: it leads outside {root or 'the home'}")


def plan_stick(inventory: Inventory, ids: list[str], source_root: Path, target: Target, stamp: str) -> list[Step]:
    """The rsync per path, in inventory order. Items without paths plan nothing here."""
    bad = unsafe_paths(inventory, ids)
    if bad:
        raise ValueError("refusing to touch " + ", ".join(bad))
    steps = []
    for item in selected(inventory, ids):
        for position, path in enumerate(item.paths):
            destination = target_path(path, inventory.home, target.home)
            _confined(destination, target)
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
        "-C", str(root), "--", *members,
    )


def tar_list_argv(archive: Path) -> tuple[str, ...]:
    return ("tar", "-I", "zstd", "-tf", str(archive))


def tar_manifest_argv(archive: Path) -> tuple[str, ...]:
    return ("tar", "-I", "zstd", "-xOf", str(archive), MANIFEST)


def tar_extract_argv(archive: Path, root: Path, paths: list[str], *, source_home: str, target_home: str) -> tuple[str, ...]:
    """--no-same-owner because the archive's uids belong to another stick;
    a chown step gives home items to the local account afterwards."""
    if source_home and source_home != target_home:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", source_home):
            raise ValueError(f"unsafe home path: {source_home!r}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_home):
            raise ValueError(f"unsafe home path: {target_home!r}")
    argv = ["tar", "-I", "zstd", *_TAR_PROGRESS, "--no-same-owner", "-xf", str(archive), "-C", str(root)]
    if source_home and source_home != target_home:
        argv.append(f"--transform=s|^{source_home}/|{target_home}/|")
    return (*argv, "--", *paths)


def parse_tar_checkpoint(line: str) -> int | None:
    match = _TAR_CHECKPOINT.match(line.strip())
    return int(match.group(1)) if match else None


def checkpoint_bytes(records: int) -> int:
    return records * TAR_RECORD_BYTES


def _members_under(listing: str, paths: list[str]):
    """Every listed member under a chosen path, with whether its name is one
    tar could be allowed to write. The listing comes out of the archive, so a
    member only has to begin with a chosen path to be walked here, and the
    rest of its name is checked the way the manifest's paths were."""
    for name in listing.splitlines():
        if not name:
            continue
        if any(name == path or name.startswith(path + "/") for path in paths):
            yield name, not _malformed(name.removesuffix("/"))


def chosen_members(listing: str, paths: list[str]) -> list[str]:
    """The file members under any chosen path. Directories end in a slash in
    tar's listing and are left out: they merge rather than collide."""
    return [name for name, safe in _members_under(listing, paths) if safe and not name.endswith("/")]


def unsafe_members(listing: str, paths: list[str]) -> list[str]:
    """The members under a chosen path that climb, double a slash or start
    like an option. One of these means the archive was made to do harm, and
    the whole of it is refused rather than the rest extracted."""
    return [name for name, safe in _members_under(listing, paths) if not safe]


def collisions(members: list[str], source_home: str, target: Target, stamp: str) -> list[tuple[str, str]]:
    """(existing, backup) for every member that would land on a file already there.

    The rename that follows acts on the entry named here, so the directory
    holding that entry must resolve inside the home or the system tree the
    member belongs to: a directory on the way that is really a symlink out
    of it would otherwise have a file elsewhere renamed into the backup.
    """
    moves = []
    for member in members:
        destination = target_path(member, source_home, target.home)
        existing = target.root / destination
        if existing.is_symlink() or existing.exists():
            root = confinement_root(destination, target)
            if root is None or not entry_within(existing, root):
                raise ValueError(f"refusing to touch {destination}: it leads outside {root or 'the home'}")
            backup = backup_dir(target, destination, stamp)
            moves.append((str(existing), str(backup)))
    return moves


def plan_export(inventory: Inventory, root: Path, archive: Path, manifest_dir: Path, stamp: str) -> list[Step]:
    return [
        Step(
            "Writing the manifest",
            mkdir=(str(manifest_dir),),
            write=((str(manifest_dir / MANIFEST), manifest_text(inventory, stamp)),),
            # It carries the password hash, like the archive it goes into.
            private=True,
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
    bad = unsafe_paths(inventory, ids)
    if bad:
        raise ValueError("refusing to touch " + ", ".join(bad))
    items = [item for item in selected(inventory, ids) if item.paths]
    paths = [path for item in items for path in item.paths]
    if not paths:
        return []
    bad = unsafe_members(listing, paths)
    if bad:
        raise ValueError("refusing to touch " + ", ".join(bad))
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


DEFAULT_SHELL = "/bin/bash"


def account_steps(account: Account, *, existing_groups: set[str], uid_free: bool,
                  shells: set[str] = frozenset()) -> list[Step]:
    """Recreate the account. The password travels as its hash through
    chpasswd -e, so nothing here ever holds or shows it in clear.

    The account is read off the source, so its fields are checked before
    they become argv: the name is last on useradd's line and could be an
    option, the shell is only kept when this system lists it in
    ``shells`` (its /etc/shells), and the comment cannot span lines.
    """
    if not USERNAME_RE.fullmatch(account.name):
        return [Step("", warn=f"ignoring the source's account {account.name!r}: not a valid username")]
    groups = [g for g in account.groups if g in existing_groups]
    shell = account.shell if account.shell in shells else DEFAULT_SHELL
    gecos = " ".join(account.gecos.splitlines())
    steps: list[Step] = []
    useradd = [
        "useradd", "--create-home", "--shell", shell, "--comment", gecos,
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
    # chpasswd reads one "name:hash" per line, so a hash holding either
    # separator could set a second account's password; it is treated as
    # unreadable instead.
    if account.password_hash and not any(c in account.password_hash for c in ":\n\r"):
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
    for name in ("hostname", "locale", "keyboard", "timezone"):
        value = getattr(identity, name)
        if f"identity.{name}" in ids and value and not valid_identity_value(name, value):
            steps.append(Step("", warn=f"ignoring the source's {name} {value!r}: not a valid {name}"))
            ids = [i for i in ids if i != f"identity.{name}"]
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
    # The name is spliced into a replacement template, where a backslash
    # would start a group reference; doubling it keeps the name literal.
    rewritten = re.sub(pattern, replacement.format(theme=theme.replace("\\", "\\\\")), text, count=1)
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
        # The name is written into XML and ini files read by the desktop
        # and the greeter, so it is kept to the characters a theme is named
        # with rather than trusted from the source.
        if not THEME_NAME_RE.fullmatch(theme):
            steps.append(Step("", warn=f"ignoring the source's desktop theme {theme!r}: not a valid theme name"))
        elif rewrite_all(theme, THEME_TARGETS):
            applied.append(f"the desktop theme {theme}")
        else:
            steps.append(Step("", warn=f"the desktop theme {theme} could not be applied here"))
    icons = names.get("icons")
    if icons:
        if not THEME_NAME_RE.fullmatch(icons):
            steps.append(Step("", warn=f"ignoring the source's icon theme {icons!r}: not a valid theme name"))
        elif icon_theme_installed and rewrite_all(icons, ICON_THEME_TARGETS):
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


def newer_version(source: str, local: str) -> bool:
    """Whether the source ran a newer portlin, the direction in which
    configuration formats diverge. Anything that is not plain dotted
    integers on both sides is not compared, rather than guessed at."""
    try:
        return tuple(int(p) for p in source.split(".")) > tuple(int(p) for p in local.split("."))
    except ValueError:
        return False


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
