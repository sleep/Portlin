# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Everything the HUD shows about the machine that the panel readout does not.

The readout answers six questions in one line. The HUD is the page behind it:
every filesystem rather than the root one, every network link rather than one
address, the memory broken down rather than summed, each core rather than the
aggregate, and the drive's throughput rather than its fullness. hostinfo.py
already parses the files both share, so this imports it rather than carrying
a second copy of /proc/stat or /proc/meminfo.

The split is the same as hostinfo's: a pure function of a file's text, and a
thin reader that finds the file under a ``root``. The parsers are what the
unit suite exercises, on a machine with no /proc at all; the readers are what
the harness runs against a real kernel.

Stdlib-only, because it ships onto a stick where only python3 is guaranteed.
Rates need two samples, so a ``Sampler`` holds the previous counters and
answers None until it has them, in the readout's tradition: a number that
cannot yet be known is not shown as one.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import hostinfo
from devices import (
    UNCLAIMED_ADVICE,
    backing_disk,
    backing_partition,
    command_output,
    disk_tail_bytes,
    root_source,
    unclaimed_bytes,
)

# Where the public address is asked for. Asking at all tells that service
# the address, which is unavoidable: nothing on this side of the router knows
# it. The HUD asks once every PUBLIC_ADDRESS_INTERVAL, not every refresh, and
# never without a route.
PUBLIC_ADDRESS_URL = "https://api.ipify.org"
PUBLIC_ADDRESS_INTERVAL_SECONDS = 600
PUBLIC_ADDRESS_TIMEOUT_SECONDS = 6

# nvidia-smi can wake a runtime-suspended card and take seconds doing it, so it
# is asked rarely and its answer held, success or failure alike.
NVIDIA_INTERVAL_SECONDS = 5
NVIDIA_DETAIL_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
    "--format=csv,noheader,nounits",
]

# `iw dev X link` is a netlink round trip per wireless interface. The SSID
# and signal change slowly, so they are refreshed less often than the counters.
WIFI_INTERVAL_SECONDS = 10

# Longer than this between samples and a rate describes a suspend, not traffic.
MAX_SAMPLE_GAP_SECONDS = 60

# The kernel's ARP hardware types, from if_arp.h, for the ones that tell a
# link's kind without reading anything else.
ARPHRD_ETHER = 1
ARPHRD_PPP = 512
ARPHRD_TUNNEL = 768
ARPHRD_TUNNEL6 = 769
ARPHRD_SIT = 776
ARPHRD_IPGRE = 778
ARPHRD_LOOPBACK = 772
ARPHRD_IP6GRE = 823
ARPHRD_NONE = 0xFFFE
TUNNEL_TYPES = {ARPHRD_TUNNEL, ARPHRD_TUNNEL6, ARPHRD_SIT, ARPHRD_IPGRE, ARPHRD_IP6GRE}

# Filesystems worth a row: anything backed by a block device, plus the
# network ones, which have no /dev source but hold real files.
NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p", "ceph", "glusterfs"}

# Block devices whose throughput is nobody's question: RAM disks, the loop
# devices under a mounted image, and zram, which is memory wearing a disk's
# name. dm-* is left out too, because its traffic is the underlying disk's
# counted twice.
DISK_NAME_SKIP = re.compile(r"^(ram|loop|zram|dm-|md)\d*")

# hwmon drivers that report the CPU's own temperature, in the order of how
# directly they do. acpitz is a board sensor that is often the CPU and
# sometimes is not, so it comes last.
CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz")

# Vendor strings the PCI database prints, shortened to what a person says.
PCI_VENDORS = {
    "Intel Corporation": "Intel",
    "Advanced Micro Devices, Inc. [AMD/ATI]": "AMD",
    "Advanced Micro Devices, Inc. [AMD]": "AMD",
    "NVIDIA Corporation": "NVIDIA",
}


@dataclass(frozen=True)
class MemoryBreakdown:
    """Where the memory went, in bytes, all straight from /proc/meminfo."""

    total: int
    available: int
    free: int
    buffers: int
    cached: int
    shmem: int
    anon: int
    reclaimable: int
    swap_total: int
    swap_free: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.available)

    @property
    def page_cache(self) -> int:
        """File cache that is not shared memory: Cached counts tmpfs too."""
        return max(0, self.cached - self.shmem)

    @property
    def swap_used(self) -> int:
        return max(0, self.swap_total - self.swap_free)


@dataclass(frozen=True)
class Zram:
    name: str
    disksize: int
    original: int
    compressed: int
    memory_used: int


@dataclass(frozen=True)
class Filesystem:
    mountpoint: str
    source: str
    fstype: str
    total_bytes: int
    used_bytes: int


@dataclass(frozen=True)
class Interface:
    name: str
    kind: str
    bus: str
    state: str
    speed_mbps: int | None
    mac: str
    addresses: tuple[str, ...]
    rx_bytes: int
    tx_bytes: int
    ssid: str = ""
    signal_dbm: int | None = None
    bitrate_mbps: float | None = None


@dataclass(frozen=True)
class GpuDetail:
    """hostinfo's Gpu, with what nvidia-smi and amdgpu's sysfs add to it.

    ``kind`` keeps hostinfo's honesty: "percent" is utilisation, "frequency"
    is a clock, "name" is a card with no counter at all.
    """

    name: str
    kind: str
    value: int | None
    label: str
    detail: str
    memory_used: int | None = None
    memory_total: int | None = None
    temperature_c: int | None = None
    power_w: float | None = None


# -- pure parsers --------------------------------------------------------------


def parse_proc_stat_cores(text: str) -> list[hostinfo.CpuSample]:
    """The per-core lines of /proc/stat, in core order."""
    cores = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"cpu\d+", fields[0]):
            continue
        try:
            values = [int(field) for field in fields[1:]]
        except ValueError:
            continue
        if len(values) >= 5:
            cores.append(hostinfo.CpuSample(total=sum(values), idle=values[3] + values[4]))
    return cores


def parse_cpu_mhz(text: str) -> int | None:
    """The mean of every "cpu MHz" line in /proc/cpuinfo, or None without one."""
    readings = []
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "cpu MHz":
            try:
                readings.append(float(value))
            except ValueError:
                continue
    return round(sum(readings) / len(readings)) if readings else None


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        return float(fields[0]), float(fields[1]), float(fields[2])
    except ValueError:
        return None


def parse_uptime(text: str) -> float | None:
    fields = text.split()
    try:
        return float(fields[0]) if fields else None
    except ValueError:
        return None


def pick_cpu_temperature(sensors: list[tuple[str, dict[str, int]]]) -> int | None:
    """The CPU's temperature in whole degrees, from hwmon readings.

    ``sensors`` is (driver name, {label: millidegrees}) per hwmon device. The
    package reading is preferred where a driver labels one, because the
    per-core readings around it are the same number plus noise, and a
    desktop HUD has no use for eight of them.
    """
    by_name = {}
    for name, readings in sensors:
        if readings and name not in by_name:
            by_name[name] = readings
    for name in CPU_SENSORS:
        readings = by_name.get(name)
        if not readings:
            continue
        for label, value in readings.items():
            if label.lower().startswith(("package", "tctl", "tdie")):
                return round(value / 1000)
        return round(max(readings.values()) / 1000)
    return None


def parse_meminfo_breakdown(text: str) -> MemoryBreakdown | None:
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key] = int(fields[0]) * 1024
    if not values.get("MemTotal") or "MemAvailable" not in values:
        return None
    return MemoryBreakdown(
        total=values["MemTotal"],
        available=values["MemAvailable"],
        free=values.get("MemFree", 0),
        buffers=values.get("Buffers", 0),
        cached=values.get("Cached", 0),
        shmem=values.get("Shmem", 0),
        anon=values.get("AnonPages", 0),
        reclaimable=values.get("SReclaimable", 0),
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
    )


def parse_zram_mm_stat(text: str) -> tuple[int, int, int] | None:
    """zram's mm_stat: original bytes, compressed bytes, memory used in all."""
    fields = text.split()
    if len(fields) < 3 or not all(field.isdigit() for field in fields[:3]):
        return None
    return int(fields[0]), int(fields[1]), int(fields[2])


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    """(source, mountpoint, fstype) per line of /proc/mounts.

    The kernel escapes spaces in a mountpoint as \\040, so a drive labelled
    "My Passport" arrives as /media/me/My\\040Passport and is unescaped here.
    """
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        source, mountpoint, fstype = (_unescape_mount(field) for field in fields[:3])
        mounts.append((source, mountpoint, fstype))
    return mounts


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def real_filesystems(mounts: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The mounts that hold files, one per device, in mount order.

    A device bind-mounted into several places is one filesystem, and the
    first place it appears is where it was mounted. Pseudo filesystems (proc,
    sysfs, cgroup, the tmpfs under /run) have nothing to say about space.
    """
    seen = set()
    kept = []
    for source, mountpoint, fstype in mounts:
        if not (source.startswith("/dev/") or fstype in NETWORK_FILESYSTEMS):
            continue
        if source in seen:
            continue
        seen.add(source)
        kept.append((source, mountpoint, fstype))
    return kept


def parse_diskstats(text: str) -> dict[str, tuple[int, int]]:
    """Bytes read and written per block device, from /proc/diskstats.

    Fields 6 and 10 are sectors, and a sector in this file is 512 bytes
    whatever the device's own sector size: the kernel documents the unit as
    fixed, which is the only reason a byte count can be made of it here.
    """
    counters = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        name = fields[2]
        try:
            counters[name] = (int(fields[5]) * 512, int(fields[9]) * 512)
        except ValueError:
            continue
    return counters


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Bytes received and sent per interface, from /proc/net/dev."""
    counters = {}
    for line in text.splitlines():
        name, colon, rest = line.partition(":")
        if not colon:
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            counters[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return counters


def parse_proc_route(text: str) -> str:
    """The interface the default route leaves by, or "" with no default route.

    Read from /proc/net/route rather than by running ip, because this is
    asked every refresh and a destination of all zeros is the whole test.
    The lowest metric wins where there are two, as it does in the kernel.
    """
    best: tuple[int, str] | None = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 7 or fields[1] != "00000000":
            continue
        try:
            metric = int(fields[6])
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, fields[0])
    return best[1] if best else ""


def interface_kind(
    name: str,
    *,
    arp_type: int,
    devtype: str,
    wireless: bool,
    has_device: bool,
) -> str:
    """What sort of link this is, from what sysfs says about it.

    The wireless directory and DEVTYPE are the kernel's own words, so they
    come first. The ARP type separates the tunnels and PPP from Ethernet.
    What is left is Ethernet-shaped, and whether a real device sits behind it
    is what tells a cable from a bridge or a veth pair, both of which claim
    to be Ethernet and are made of nothing.
    """
    if arp_type == ARPHRD_LOOPBACK:
        return "loopback"
    if wireless or devtype == "wlan":
        return "wifi"
    if devtype == "wwan":
        return "mobile"
    if devtype in ("bridge", "bond", "vlan", "wireguard", "vxlan", "macvlan"):
        return "vpn" if devtype == "wireguard" else devtype
    if arp_type == ARPHRD_PPP:
        return "ppp"
    if arp_type in TUNNEL_TYPES:
        return "tunnel"
    if arp_type == ARPHRD_NONE or name.startswith(("tun", "tap", "wg")):
        return "vpn" if name.startswith("wg") else "tunnel"
    if arp_type == ARPHRD_ETHER:
        if has_device:
            return "ethernet"
        if name.startswith(("docker", "virbr", "br-", "br")):
            return "bridge"
        return "virtual"
    return "other"


def interface_state(operstate: str, carrier: str) -> str:
    """"up", "down", or the kernel's word when it is neither.

    lo and some virtual links report operstate "unknown" forever while
    carrying traffic; the carrier file is the truth for those.
    """
    operstate = operstate.strip()
    if operstate in ("up", "down"):
        return operstate
    if carrier.strip() == "1":
        return "up"
    return operstate or "unknown"


def parse_proc_wireless(text: str) -> dict[str, int]:
    """Signal level in dBm per wireless interface, from /proc/net/wireless.

    The third number on a line is the level; the kernel prints it with a
    trailing dot ("-40.") that int() will not take.
    """
    levels = {}
    for line in text.splitlines()[2:]:
        name, colon, rest = line.partition(":")
        if not colon:
            continue
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            levels[name.strip()] = int(float(fields[2].rstrip(".")))
        except ValueError:
            continue
    return levels


def parse_iw_link(text: str) -> dict:
    """What `iw dev X link` says: the SSID, signal and bit rate, or {} when
    the interface is not associated."""
    link: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            link["ssid"] = line[5:].strip()
        elif line.startswith("signal:"):
            match = re.search(r"(-?\d+)\s*dBm", line)
            if match:
                link["signal_dbm"] = int(match.group(1))
        elif line.startswith(("tx bitrate:", "rx bitrate:")):
            match = re.search(r"([\d.]+)\s*MBit/s", line)
            if match and "bitrate_mbps" not in link:
                link["bitrate_mbps"] = float(match.group(1))
        elif line.startswith("freq:"):
            match = re.search(r"(\d+)", line)
            if match:
                link["freq_mhz"] = int(match.group(1))
    return link


def parse_ip_addr(text: str) -> dict[str, tuple[str, ...]]:
    """Global addresses per interface, from `ip -j addr`.

    Only scope global: every link has a fe80:: address and nobody types one.
    """
    try:
        entries = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(entries, list):
        return {}
    addresses: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ifname"), str):
            continue
        found = []
        for info in entry.get("addr_info") or []:
            if not isinstance(info, dict) or info.get("scope") != "global":
                continue
            local = info.get("local")
            if isinstance(local, str):
                prefix = info.get("prefixlen")
                found.append(f"{local}/{prefix}" if isinstance(prefix, int) else local)
        addresses[entry["ifname"]] = tuple(found)
    return addresses


def parse_nvidia_detail(text: str) -> dict | None:
    """One line of the six-field nvidia-smi query, or None.

    "[N/A]" stands in for any field the card cannot report, so each is
    parsed on its own and absent rather than the line being rejected.
    """
    first = text.strip().splitlines()[0] if text.strip() else ""
    fields = [field.strip() for field in first.split(",")]
    if len(fields) < 6:
        return None

    def number(field: str) -> float | None:
        try:
            return float(field)
        except ValueError:
            return None

    percent = number(fields[1])
    used = number(fields[2])
    total = number(fields[3])
    temperature = number(fields[4])
    power = number(fields[5])
    return {
        "name": fields[0],
        "percent": max(0, min(100, int(percent))) if percent is not None else None,
        "memory_used": int(used * 1024**2) if used is not None else None,
        "memory_total": int(total * 1024**2) if total is not None else None,
        "temperature_c": int(temperature) if temperature is not None else None,
        "power_w": round(power, 1) if power is not None else None,
    }


def parse_lspci_mm(text: str) -> list[str]:
    """Display controllers as "Vendor Model", from `lspci -mm`.

    The device field is "GA106 [GeForce RTX 3060]" for cards the database
    knows by a marketing name; the bracketed part is the one a person uses.
    """
    names = []
    for line in text.splitlines():
        fields = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
        if len(fields) < 3:
            continue
        kind, vendor, device = fields[0], fields[1], fields[2]
        if "VGA" not in kind and "3D" not in kind and "Display" not in kind:
            continue
        vendor = PCI_VENDORS.get(vendor, vendor)
        bracketed = re.search(r"\[([^\]]+)\]", device)
        if bracketed:
            device = bracketed.group(1)
        names.append(f"{vendor} {device}".strip())
    return names


def parse_dpkg_versions(text: str) -> dict[str, str]:
    """{package: version} from `dpkg-query -W -f='${Package} ${Version}\\n'`."""
    versions = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            versions[fields[0]] = fields[1]
    return versions


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=value lines, with os-release's double quotes stripped."""
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def parse_public_address(text: str) -> str:
    """The address a lookup service answered with, or "" if it is not one.

    Validated rather than trusted: a captive portal answers any URL with its
    own login page, and a page is not an address.
    """
    candidate = text.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def rates(
    previous: dict | None,
    current: dict,
    elapsed: float,
) -> dict[str, tuple[float, ...] | None]:
    """Per-second rates for each key's counters, or None where unknowable.

    None for a key with no previous sample, when the counters went
    backwards (an interface that was re-created, a disk unplugged and
    plugged back), or when too long passed between samples for the average
    to describe anything a person is looking at.
    """
    result: dict[str, tuple[float, ...] | None] = {}
    for key, counters in current.items():
        before = previous.get(key) if previous else None
        if (
            before is None
            or len(before) != len(counters)
            or not 0 < elapsed <= MAX_SAMPLE_GAP_SECONDS
            or any(now < then for now, then in zip(counters, before))
        ):
            result[key] = None
            continue
        result[key] = tuple((now - then) / elapsed for now, then in zip(counters, before))
    return result


def format_rate(bytes_per_second: float | None) -> str:
    if bytes_per_second is None:
        return "--"
    return f"{hostinfo.format_bytes(int(bytes_per_second))}/s"


def format_duration(seconds: float | None) -> str:
    """Uptime the way `uptime` says it: 3d 4h, 2h 15m, 12m."""
    if seconds is None:
        return "--"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# -- readers -------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def read_cpu_cores(root: Path = Path("/")) -> list[hostinfo.CpuSample]:
    return parse_proc_stat_cores(_read(root / "proc/stat"))


def read_cpu_mhz(root: Path = Path("/")) -> int | None:
    """The current clock: cpufreq's reading if the driver is loaded, since it
    is per core and current, else the mean /proc/cpuinfo reports."""
    frequencies = []
    for entry in sorted((root / "sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")):
        text = _read(entry).strip()
        if text.isdigit():
            frequencies.append(int(text) / 1000)
    if frequencies:
        return round(sum(frequencies) / len(frequencies))
    return parse_cpu_mhz(_read(root / "proc/cpuinfo"))


def read_cpu_temperature(root: Path = Path("/")) -> int | None:
    sensors: list[tuple[str, dict[str, int]]] = []
    hwmon = root / "sys/class/hwmon"
    try:
        devices = sorted(hwmon.iterdir())
    except OSError:
        devices = []
    for device in devices:
        name = _read(device / "name").strip()
        readings: dict[str, int] = {}
        for entry in sorted(device.glob("temp*_input")):
            value = _read(entry).strip()
            if not re.fullmatch(r"-?\d+", value):
                continue
            label = _read(device / entry.name.replace("_input", "_label")).strip() or entry.name
            readings[label] = int(value)
        sensors.append((name, readings))
    temperature = pick_cpu_temperature(sensors)
    if temperature is not None:
        return temperature
    # Machines with no hwmon driver for the CPU still usually have the ACPI
    # thermal zone, which names the package sensor on Intel.
    for zone in sorted((root / "sys/class/thermal").glob("thermal_zone*")):
        kind = _read(zone / "type").strip().lower()
        value = _read(zone / "temp").strip()
        if ("pkg" in kind or "cpu" in kind) and re.fullmatch(r"-?\d+", value):
            return round(int(value) / 1000)
    return None


def read_memory_breakdown(root: Path = Path("/")) -> MemoryBreakdown | None:
    return parse_meminfo_breakdown(_read(root / "proc/meminfo"))


def read_zram(root: Path = Path("/")) -> list[Zram]:
    devices = []
    for device in sorted((root / "sys/block").glob("zram*")):
        disksize = _read(device / "disksize").strip()
        stat = parse_zram_mm_stat(_read(device / "mm_stat"))
        if not disksize.isdigit() or stat is None or int(disksize) == 0:
            continue
        devices.append(Zram(device.name, int(disksize), *stat))
    return devices


def read_filesystems(root: Path = Path("/")) -> list[Filesystem]:
    found = []
    for source, mountpoint, fstype in real_filesystems(parse_mounts(_read(root / "proc/mounts"))):
        try:
            usage = shutil.disk_usage(mountpoint)
        except OSError:
            continue
        found.append(Filesystem(mountpoint, source, fstype, usage.total, usage.used))
    return found


def read_disk_counters(root: Path = Path("/")) -> dict[str, tuple[int, int]]:
    """Read and write bytes for every whole disk that has a size.

    A size of zero is a device node with nothing behind it: the sixteen
    nbd devices the module registers on load, a card reader with no card.
    """
    block = root / "sys/block"
    try:
        disks = {
            entry.name for entry in block.iterdir()
            if _read(entry / "size").strip() not in ("", "0")
        }
    except OSError:
        disks = set()
    return {
        name: counters
        for name, counters in parse_diskstats(_read(root / "proc/diskstats")).items()
        if name in disks and not DISK_NAME_SKIP.match(name)
    }


def read_net_counters(root: Path = Path("/")) -> dict[str, tuple[int, int]]:
    return parse_net_dev(_read(root / "proc/net/dev"))


def read_default_interface(root: Path = Path("/")) -> str:
    return parse_proc_route(_read(root / "proc/net/route"))


def read_addresses() -> dict[str, tuple[str, ...]]:
    return parse_ip_addr(command_output(["ip", "-j", "addr"]))


def read_wifi_link(name: str) -> dict:
    return parse_iw_link(command_output(["iw", "dev", name, "link"]))


def read_interfaces(
    root: Path = Path("/"),
    *,
    addresses: dict[str, tuple[str, ...]] | None = None,
    wifi_links: dict[str, dict] | None = None,
) -> list[Interface]:
    """Every link worth a row, the default route's first.

    Left out: loopback, and the devices the kernel's tunnel modules create
    on load (tunl0, sit0, gre0, gretap0, erspan0 and their IPv6 twins),
    which are down, addressless, have never carried a byte and are present
    on every machine. The rule is those three facts rather than a list of
    names, so a bridge or a tunnel somebody brought up stays in, and so
    does an Ethernet port with nothing plugged into it, because a port is
    hardware and its emptiness is news.
    """
    addresses = addresses or {}
    wifi_links = wifi_links or {}
    counters = read_net_counters(root)
    levels = parse_proc_wireless(_read(root / "proc/net/wireless"))
    default = read_default_interface(root)
    net = root / "sys/class/net"
    try:
        entries = sorted(entry for entry in net.iterdir() if entry.is_dir())
    except OSError:
        return []
    found = []
    for entry in entries:
        name = entry.name
        arp = _read(entry / "type").strip()
        uevent = parse_env_file(_read(entry / "uevent"))
        device = ""
        try:
            device = os.readlink(entry / "device")
        except OSError:
            pass
        kind = interface_kind(
            name,
            arp_type=int(arp) if arp.isdigit() else -1,
            devtype=uevent.get("DEVTYPE", ""),
            wireless=(entry / "wireless").is_dir() or (entry / "phy80211").exists(),
            has_device=bool(device),
        )
        if kind == "loopback":
            continue
        state = interface_state(_read(entry / "operstate"), _read(entry / "carrier"))
        own = addresses.get(name, ())
        rx, tx = counters.get(name, (0, 0))
        if state == "down" and not own and rx == 0 and tx == 0 and kind not in ("ethernet", "wifi", "mobile"):
            continue
        speed = _read(entry / "speed").strip()
        link = wifi_links.get(name, {}) if kind == "wifi" else {}
        found.append(Interface(
            name=name,
            kind=kind,
            bus="usb" if "/usb" in device else ("pci" if "/pci" in device else ""),
            state=state,
            speed_mbps=int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            mac=_read(entry / "address").strip(),
            addresses=own,
            rx_bytes=rx,
            tx_bytes=tx,
            ssid=link.get("ssid", ""),
            signal_dbm=link.get("signal_dbm", levels.get(name)),
            bitrate_mbps=link.get("bitrate_mbps"),
        ))
    rank = {"ethernet": 1, "wifi": 2, "mobile": 3, "vpn": 4, "ppp": 5}
    found.sort(key=lambda i: (i.name != default, i.state != "up", rank.get(i.kind, 9), i.name))
    return found


def read_gpu_names() -> list[str]:
    return parse_lspci_mm(command_output(["lspci", "-mm"]))


def read_nvidia_detail() -> dict | None:
    if shutil.which(NVIDIA_DETAIL_QUERY[0]) is None:
        return None
    try:
        result = subprocess.run(
            NVIDIA_DETAIL_QUERY,
            capture_output=True,
            text=True,
            check=False,
            timeout=hostinfo.NVIDIA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_nvidia_detail(result.stdout)


def read_amd_detail(card: Path) -> dict:
    """What amdgpu puts in sysfs beside the busy counter: VRAM and heat."""
    detail: dict = {}
    used = _read(card / "device/mem_info_vram_used").strip()
    total = _read(card / "device/mem_info_vram_total").strip()
    if used.isdigit() and total.isdigit():
        detail["memory_used"] = int(used)
        detail["memory_total"] = int(total)
    for sensor in sorted((card / "device/hwmon").glob("hwmon*/temp1_input")):
        value = _read(sensor).strip()
        if re.fullmatch(r"-?\d+", value):
            detail["temperature_c"] = round(int(value) / 1000)
            break
    return detail


def read_gpu_detail(
    root: Path = Path("/"),
    *,
    nvidia: dict | None = None,
    names: list[str] | None = None,
) -> GpuDetail | None:
    gpu = hostinfo.read_gpu(root, nvidia=nvidia.get("percent") if nvidia else None)
    if gpu is None:
        return None
    extra: dict = {}
    cards = hostinfo._drm_cards(root)
    if nvidia and (nvidia.get("memory_total") or nvidia.get("temperature_c") is not None):
        extra = {key: nvidia.get(key) for key in ("memory_used", "memory_total", "temperature_c", "power_w")}
    elif cards:
        extra = read_amd_detail(cards[0])
    name = (nvidia or {}).get("name") or (names[0] if names else "")
    return GpuDetail(
        name=name,
        kind=gpu.kind,
        value=gpu.value,
        label=gpu.label,
        detail=gpu.detail,
        **extra,
    )


def read_topology() -> dict:
    """The stick's geometry, as the panel readout caches it: read once a boot."""
    source = root_source()
    partition = backing_partition(source)
    disk = backing_disk(partition) if partition else ""
    partition_size = command_output(["lsblk", "-bdno", "SIZE", f"/dev/{partition}"]) if partition else ""
    drive_size = command_output(["lsblk", "-bdno", "SIZE", f"/dev/{disk}"]) if disk else ""
    return {
        "source": source,
        "encrypted": source.startswith("/dev/mapper/"),
        "disk": disk,
        "partition_bytes": int(partition_size) if partition_size.isdigit() else 0,
        "drive_bytes": int(drive_size) if drive_size.isdigit() else 0,
        "tail_bytes": disk_tail_bytes(disk, partition) if disk and partition else 0,
    }


def read_release(root: Path = Path("/"), *, topology: dict, dpkg_versions: dict[str, str]) -> dict:
    release = parse_env_file(_read(root / "etc/portlin-release"))
    debian = parse_env_file(_read(root / "etc/os-release"))
    unclaimed = 0
    try:
        usage = shutil.disk_usage("/")
        unclaimed = unclaimed_bytes(
            usage.total, topology.get("partition_bytes", 0), topology.get("tail_bytes", 0)
        )
    except OSError:
        pass
    return {
        "version": release.get("PORTLIN_VERSION", ""),
        "commit": release.get("PORTLIN_COMMIT", ""),
        "debian": debian.get("PRETTY_NAME", ""),
        "codename": debian.get("VERSION_CODENAME", ""),
        "root": topology.get("source", ""),
        "encrypted": bool(topology.get("encrypted")),
        "drive": f"/dev/{topology['disk']}" if topology.get("disk") else "",
        "drive_bytes": topology.get("drive_bytes", 0),
        "runtime_version": dpkg_versions.get("portlin-runtime", ""),
        "desktop_version": dpkg_versions.get("portlin-desktop", ""),
        "unclaimed_bytes": unclaimed,
        "advice": UNCLAIMED_ADVICE.format(gb=unclaimed / 1_000_000_000) if unclaimed else "",
    }


def read_dpkg_versions() -> dict[str, str]:
    return parse_dpkg_versions(
        command_output(["dpkg-query", "-W", "-f=${Package} ${Version}\n",
                        "portlin-runtime", "portlin-desktop"])
    )


def fetch_public_address(
    url: str = PUBLIC_ADDRESS_URL, timeout: float = PUBLIC_ADDRESS_TIMEOUT_SECONDS
) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return parse_public_address(response.read(64).decode("ascii", "replace"))
    except (OSError, ValueError):
        return ""


# -- the sampler ---------------------------------------------------------------


class Sampler:
    """Takes one snapshot of everything, holding what a rate needs between them.

    Everything slow or boot-constant is read once and kept: the drive
    geometry, the GPU names, the package versions. The public address is
    asked for in a thread of its own, so a lookup service that does not
    answer stalls nothing but its own row.
    """

    def __init__(self, root: Path = Path("/"), *, home: Path | None = None) -> None:
        self.root = root
        self.home = home or Path.home()
        self.previous_cpu: tuple[hostinfo.CpuSample | None, list[hostinfo.CpuSample]] | None = None
        self.previous_net: dict | None = None
        self.previous_disk: dict | None = None
        self.previous_at: float | None = None
        self.topology: dict | None = None
        self.gpu_names: list[str] | None = None
        self.dpkg_versions: dict[str, str] | None = None
        self.nvidia: dict | None = None
        self.nvidia_at: float | None = None
        self.wifi_links: dict[str, dict] = {}
        self.wifi_at: float | None = None
        self.public_address = ""
        self.public_state = "not asked"
        self.public_at: float | None = None
        self._public_lock = threading.Lock()
        self._public_thread: threading.Thread | None = None
        self.lookup_public = True

    def _nvidia(self, now: float) -> dict | None:
        if self.nvidia_at is None or now - self.nvidia_at >= NVIDIA_INTERVAL_SECONDS:
            self.nvidia = read_nvidia_detail()
            self.nvidia_at = now
        return self.nvidia

    def _wifi(self, now: float) -> dict[str, dict]:
        if self.wifi_at is None or now - self.wifi_at >= WIFI_INTERVAL_SECONDS:
            net = self.root / "sys/class/net"
            links = {}
            try:
                for entry in net.iterdir():
                    if (entry / "wireless").is_dir() or (entry / "phy80211").exists():
                        links[entry.name] = read_wifi_link(entry.name)
            except OSError:
                pass
            self.wifi_links = links
            self.wifi_at = now
        return self.wifi_links

    def _ask_public(self, now: float, has_route: bool) -> None:
        if not self.lookup_public:
            self.public_state = "off"
            return
        if not has_route:
            self.public_state = "no route"
            return
        if self._public_thread is not None and self._public_thread.is_alive():
            return
        if self.public_at is not None and now - self.public_at < PUBLIC_ADDRESS_INTERVAL_SECONDS:
            return
        self.public_at = now
        self.public_state = "asking"

        def ask() -> None:
            answer = fetch_public_address()
            with self._public_lock:
                self.public_address = answer
                self.public_state = "known" if answer else "no answer"

        self._public_thread = threading.Thread(target=ask, name="portlin-hud-public", daemon=True)
        self._public_thread.start()

    def sample(self) -> dict:
        now = time.monotonic()
        root = self.root
        elapsed = now - self.previous_at if self.previous_at is not None else 0.0

        if self.topology is None:
            self.topology = read_topology()
        if self.gpu_names is None:
            self.gpu_names = read_gpu_names()
        if self.dpkg_versions is None:
            self.dpkg_versions = read_dpkg_versions()

        aggregate = hostinfo.read_cpu_sample(root)
        cores = read_cpu_cores(root)
        busy = None
        core_busy: list[float | None] = [None] * len(cores)
        if self.previous_cpu is not None and 0 < elapsed <= MAX_SAMPLE_GAP_SECONDS:
            busy = hostinfo.cpu_percent(self.previous_cpu[0], aggregate)
            previous_cores = self.previous_cpu[1]
            if len(previous_cores) == len(cores):
                core_busy = [hostinfo.cpu_percent(before, after) for before, after in zip(previous_cores, cores)]
        self.previous_cpu = (aggregate, cores)

        net_counters = read_net_counters(root)
        disk_counters = read_disk_counters(root)
        net_rates = rates(self.previous_net, net_counters, elapsed)
        disk_rates = rates(self.previous_disk, disk_counters, elapsed)
        self.previous_net = net_counters
        self.previous_disk = disk_counters
        self.previous_at = now

        default = read_default_interface(root)
        private = hostinfo.local_address()
        self._ask_public(now, bool(private))
        with self._public_lock:
            public = self.public_address
            public_state = self.public_state

        interfaces = read_interfaces(root, addresses=read_addresses(), wifi_links=self._wifi(now))
        memory = read_memory_breakdown(root)
        host = hostinfo.read_host(root)
        gpu = read_gpu_detail(root, nvidia=self._nvidia(now), names=self.gpu_names)
        battery = hostinfo.read_battery(root)
        uname = os.uname()

        return {
            "sampled_at": time.time(),
            "host": {
                **asdict(host),
                "hostname": socket.gethostname(),
                "kernel": uname.release,
                "uptime_seconds": parse_uptime(_read(root / "proc/uptime")),
                "load": parse_loadavg(_read(root / "proc/loadavg")),
                "user": os.environ.get("USER", ""),
                "session": os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", ""),
                "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
                "battery": asdict(battery) if battery else None,
            },
            "release": read_release(root, topology=self.topology, dpkg_versions=self.dpkg_versions),
            "cpu": {
                "percent": busy,
                "cores": core_busy,
                "mhz": read_cpu_mhz(root),
                "temperature_c": read_cpu_temperature(root),
            },
            "memory": {
                **(asdict(memory) if memory else {}),
                "used": memory.used if memory else None,
                "page_cache": memory.page_cache if memory else None,
                "swap_used": memory.swap_used if memory else None,
                "zram": [asdict(device) for device in read_zram(root)],
            } if memory else None,
            "gpu": asdict(gpu) if gpu else None,
            "filesystems": [asdict(fs) for fs in read_filesystems(root)],
            "disks": [
                {
                    "name": name,
                    "read_bytes": counters[0],
                    "write_bytes": counters[1],
                    "read_rate": disk_rates[name][0] if disk_rates.get(name) else None,
                    "write_rate": disk_rates[name][1] if disk_rates.get(name) else None,
                }
                for name, counters in sorted(disk_counters.items())
            ],
            "interfaces": [
                {
                    **asdict(interface),
                    "default": interface.name == default,
                    "rx_rate": net_rates[interface.name][0] if net_rates.get(interface.name) else None,
                    "tx_rate": net_rates[interface.name][1] if net_rates.get(interface.name) else None,
                }
                for interface in interfaces
            ],
            "addresses": {
                "private": private,
                "public": public,
                "public_state": public_state,
                "public_source": PUBLIC_ADDRESS_URL,
                "default_interface": default,
            },
        }
