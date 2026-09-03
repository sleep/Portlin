"""What the machine under this stick is, and what it is doing right now.

A stick travels. Every answer here changes with the laptop it was plugged into
this morning, so none of it can be recorded at write time and all of it has to
come from asking the running kernel. Three surfaces want the same answers -- the
portlin-info report, the About dialog that prints it, and the panel readout --
so the parsing lives here once rather than three times.

Stdlib-only, and imported with no dependency on the portlin package itself,
because this file ships onto a stick where only python3 is guaranteed.

Every reader is split in two: a pure function of the text a file holds, and a
thin reader that finds the file. The split is what lets the whole of this be
tested on a machine with no /proc and no /sys at all, which is where portlin's
unit tests run. The readers take a ``root`` for the same reason -- a test can
build a fake /proc and /sys under tmp_path and exercise the real reader rather
than only the parser underneath it.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Where DMI puts the machine's own description. Not every machine fills these
# in: virtual machines and whitebox desktops commonly leave "To Be Filled By
# O.E.M." or "System Product Name" behind, which is a placeholder rather than an
# answer and is filtered out below.
DMI = "sys/devices/virtual/dmi/id"

# Strings vendors leave in DMI when nobody filled the field in. Matched
# case-insensitively and in full, because a real product genuinely called
# "System" should not be thrown away by a substring test.
DMI_PLACEHOLDERS = {
    "to be filled by o.e.m.",
    "system product name",
    "system manufacturer",
    "default string",
    "not specified",
    "not applicable",
    "none",
    "o.e.m.",
    "unknown",
}

# An address in TEST-NET-1, which RFC 5737 reserves for documentation and which
# is therefore guaranteed to belong to nobody. Nothing is ever sent to it: a
# connect() on a UDP socket only asks the kernel to pick a route.
ROUTE_PROBE = ("192.0.2.1", 9)

# A battery, as opposed to everything else /sys/class/power_supply holds. The
# directory also carries the AC adapter, USB-C source ports, and -- genuinely --
# hidpp_battery_0 for a wireless mouse. Matching on the type alone would put a
# stranger's mouse in the panel, so the kernel name has to look like a battery
# too. BAT is the ACPI convention and CMB is what a few older machines use.
BATTERY_NAME = re.compile(r"^(BAT|CMB)\d*$")

# nvidia-smi is slow on a hybrid laptop whose discrete card is runtime
# suspended, and asking it can wake the card. That is a battery cost the stick
# would be blamed for, so the answer is held between queries.
NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=utilization.gpu",
    "--format=csv,noheader,nounits",
]
NVIDIA_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class CpuSample:
    """One reading of the aggregate CPU counters, in jiffies."""

    total: int
    idle: int


@dataclass(frozen=True)
class Memory:
    used_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class Battery:
    percent: int
    charging: bool


@dataclass(frozen=True)
class Gpu:
    """What can be said about the graphics hardware, and how sure it is.

    ``kind`` is the honest part. "percent" is real utilisation. "frequency" is
    a clock speed, which is what i915 exposes and which is emphatically not a
    utilisation figure. "name" means no counter could be read at all and only
    the hardware's identity is known.
    """

    kind: str
    value: int | None
    label: str
    detail: str


@dataclass(frozen=True)
class Host:
    model: str
    cpu: str
    threads: int
    memory_bytes: int
    firmware: str


def _clean(value: str) -> str:
    """A DMI string, or "" if the vendor left a placeholder in the field."""
    value = value.strip()
    return "" if value.lower() in DMI_PLACEHOLDERS else value


def parse_proc_stat(text: str) -> CpuSample | None:
    """The aggregate "cpu " line of /proc/stat, as a total and an idle count.

    Only the aggregate line, never the per-core ones: the readout has one
    number in it. idle is the fourth and fifth fields together, because iowait
    is time the CPU also spent doing nothing and counting it as busy makes a
    machine copying to a slow USB stick look pegged when it is only waiting.
    """
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            try:
                values = [int(field) for field in fields[1:]]
            except ValueError:
                return None
            if len(values) < 5:
                return None
            return CpuSample(total=sum(values), idle=values[3] + values[4])
    return None


def cpu_percent(previous: CpuSample | None, current: CpuSample | None) -> float | None:
    """Busy percentage between two samples, or None when it cannot be known.

    Returns None rather than a number in every case where the honest answer is
    "not yet": no previous sample, no elapsed jiffies, or counters that went
    backwards. That last one is not hypothetical -- a resumed machine and a
    discarded state file both produce it -- and the alternative to None is a
    since-boot average presented as though it were current, which is the one
    thing this must never do.
    """
    if previous is None or current is None:
        return None
    total = current.total - previous.total
    idle = current.idle - previous.idle
    if total <= 0 or idle < 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total - idle) / total))


def parse_meminfo(text: str) -> Memory | None:
    """Used and total memory from /proc/meminfo.

    Used is MemTotal minus MemAvailable, never minus MemFree. MemFree counts
    the page cache as used, so a machine that has been up an hour reports
    almost all of its memory in use, which is true of the kernel's bookkeeping
    and meaningless to the person reading the panel.
    """
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key] = int(fields[0]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return Memory(used_bytes=max(0, total - available), total_bytes=total)


def parse_cpu_model(text: str) -> str:
    """The first "model name" in /proc/cpuinfo.

    Every core repeats it, and on a big.LITTLE-style machine they can differ.
    The first is the one the firmware lists first, which is the description a
    person would recognise.
    """
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name":
            return value.strip()
    return ""


def parse_cpu_threads(text: str) -> int:
    """How many logical CPUs /proc/cpuinfo describes."""
    return sum(1 for line in text.splitlines() if line.startswith("processor"))


def parse_dmi_model(vendor: str, name: str, version: str) -> str:
    """The machine's description, from the three DMI fields that carry it.

    Vendors spread it across all three inconsistently: Lenovo puts "ThinkPad X1
    Carbon Gen 11" in version and a bare model code in product_name, while Dell
    puts the whole thing in product_name and leaves version empty. Taking
    whichever fields are real and joining them is the only rule that reads
    correctly on both.
    """
    vendor, name, version = _clean(vendor), _clean(name), _clean(version)
    # Lenovo's version field is the human-readable name and its product_name is
    # a code; preferring the longer of the two picks the readable one either way.
    model = max((name, version), key=len) if name or version else ""
    if vendor and model and not model.lower().startswith(vendor.lower().split()[0]):
        return f"{vendor} {model}"
    return model or vendor


def parse_firmware_mode(efi_exists: bool, bits: str) -> str:
    """Whether the machine booted UEFI or BIOS, and how wide the firmware is.

    Portlin installs GRUB twice so the stick boots either way, which makes
    "which one happened here" a question worth being able to answer out loud
    when a stick boots on one machine and not another.
    """
    if not efi_exists:
        return "BIOS"
    bits = bits.strip()
    return f"UEFI, {bits}-bit" if bits.isdigit() else "UEFI"


def parse_gpu_busy(text: str) -> int | None:
    """amdgpu's gpu_busy_percent, the one real utilisation counter in sysfs."""
    text = text.strip()
    if not text.isdigit():
        return None
    return max(0, min(100, int(text)))


def parse_i915_freq(text: str) -> int | None:
    """Intel's current graphics clock, in MHz.

    This is a frequency and not a utilisation, and the distinction is the whole
    reason it has its own parser and its own label. i915 exposes no busy-percent
    counter, and dividing the current clock by the maximum would produce a
    number that looks exactly like the other two vendors' percentages while
    meaning something entirely different.
    """
    text = text.strip()
    if not text.isdigit():
        return None
    return int(text)


def parse_nvidia_smi(text: str) -> int | None:
    """The utilisation percentage nvidia-smi prints, or None.

    It answers "N/A" rather than failing when the card cannot report -- an
    older card, or one in a state where the counter is unavailable -- so a
    plain int() here would raise on a perfectly healthy machine.
    """
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first.isdigit():
        return None
    return max(0, min(100, int(first)))


def parse_battery(capacity: str, status: str) -> Battery | None:
    """One battery's charge and whether it is filling."""
    capacity = capacity.strip()
    if not capacity.isdigit():
        return None
    return Battery(
        percent=max(0, min(100, int(capacity))),
        charging=status.strip().lower() in {"charging", "full"},
    )


def format_bytes(count: int) -> str:
    """A byte count in the casing df -h uses: 31.0G, 512M, 4.1G.

    Powers of 1024 and a single suffix letter, because that is what df, lsblk
    and free print, and the brand rule for portlin's own labels is that they
    quote real system text rather than inventing a prettier form of it.
    """
    if count <= 0:
        return "0"
    for suffix, size in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if count >= size:
            value = count / size
            return f"{value:.1f}{suffix}" if value < 100 else f"{value:.0f}{suffix}"
    return f"{count}"


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def read_cpu_sample(root: Path = Path("/")) -> CpuSample | None:
    return parse_proc_stat(_read(root / "proc/stat"))


def read_memory(root: Path = Path("/")) -> Memory | None:
    return parse_meminfo(_read(root / "proc/meminfo"))


def read_battery(root: Path = Path("/")) -> Battery | None:
    """The machine's battery charge, summed across packs where there are two.

    ThinkPads have BAT0 and BAT1 and either can be absent or empty, so one
    reading is not the machine's answer. Percentages cannot simply be averaged
    when the packs are different sizes, but capacity is all most machines
    expose, so this averages what it has and says so by rounding rather than
    pretending to a precision it does not have.
    """
    supply = root / "sys/class/power_supply"
    try:
        entries = sorted(supply.iterdir())
    except OSError:
        return None
    found = []
    for entry in entries:
        if not BATTERY_NAME.match(entry.name):
            continue
        if _read(entry / "type").strip() != "Battery":
            continue
        battery = parse_battery(_read(entry / "capacity"), _read(entry / "status"))
        if battery is not None:
            found.append(battery)
    if not found:
        return None
    return Battery(
        percent=round(sum(b.percent for b in found) / len(found)),
        charging=any(b.charging for b in found),
    )


def _drm_cards(root: Path) -> list[Path]:
    """Real graphics cards in sysfs, the one the firmware booted on first.

    The glob has to exclude renderD* and the card0-HDMI-A-1 connector entries,
    which share the directory. Sorting by boot_vga puts the display's own GPU
    first, which matters on a hybrid laptop: the discrete card is usually
    runtime suspended and will truthfully report 0% while the integrated one
    does all the work.
    """
    drm = root / "sys/class/drm"
    try:
        cards = [p for p in drm.iterdir() if re.fullmatch(r"card\d+", p.name)]
    except OSError:
        return []
    return sorted(cards, key=lambda p: (_read(p / "device/boot_vga").strip() != "1", p.name))


def read_gpu(root: Path = Path("/"), *, nvidia: int | None = None) -> Gpu | None:
    """What can be honestly said about the GPU right now.

    Tried in the order of how much they actually say: a real utilisation
    counter, then NVIDIA's equivalent, then Intel's clock -- which is labelled
    as a clock, not converted into a percentage it is not -- and finally
    nothing but the fact that a card is there. The caller supplies the NVIDIA
    reading rather than this triggering one, because asking costs a subprocess
    and can wake a sleeping card.
    """
    for card in _drm_cards(root):
        busy = parse_gpu_busy(_read(card / "device/gpu_busy_percent"))
        if busy is not None:
            return Gpu("percent", busy, f"{busy}%", f"{card.name} reports {busy}% busy")
        if nvidia is not None:
            return Gpu("percent", nvidia, f"{nvidia}%", f"nvidia-smi reports {nvidia}%")
        for field in ("gt_cur_freq_mhz", "gt/gt0/rps_cur_freq_mhz"):
            mhz = parse_i915_freq(_read(card / field))
            if mhz is not None:
                return Gpu(
                    "frequency",
                    mhz,
                    f"{mhz}MHz",
                    "i915 reports GPU frequency, not utilisation. "
                    "There is no busy-percent counter to read.",
                )
        return Gpu("name", None, "", f"{card.name}, no utilisation counter")
    if nvidia is not None:
        return Gpu("percent", nvidia, f"{nvidia}%", f"nvidia-smi reports {nvidia}%")
    return None


def read_nvidia_percent() -> int | None:
    """Ask nvidia-smi, if it is there at all.

    It only exists once the Drivers page has installed the proprietary driver,
    so its absence is the ordinary case rather than an error. The timeout is
    not defensive padding: on a hybrid laptop with the discrete card asleep,
    this call blocks for seconds while the card wakes up.
    """
    if shutil.which(NVIDIA_QUERY[0]) is None:
        return None
    try:
        result = subprocess.run(
            NVIDIA_QUERY,
            capture_output=True,
            text=True,
            check=False,
            timeout=NVIDIA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_nvidia_smi(result.stdout)


def read_host(root: Path = Path("/")) -> Host:
    """The machine's own description: model, CPU, memory, firmware."""
    dmi = root / DMI
    cpuinfo = _read(root / "proc/cpuinfo")
    memory = read_memory(root)
    return Host(
        model=parse_dmi_model(
            _read(dmi / "sys_vendor"),
            _read(dmi / "product_name"),
            _read(dmi / "product_version"),
        ),
        cpu=parse_cpu_model(cpuinfo),
        threads=parse_cpu_threads(cpuinfo),
        memory_bytes=memory.total_bytes if memory else 0,
        firmware=parse_firmware_mode(
            (root / "sys/firmware/efi").exists(),
            _read(root / "sys/firmware/efi/fw_platform_size"),
        ),
    )


def local_address() -> str:
    """The address this machine would reach the network on, or "".

    A UDP connect() sends no packet; it only asks the kernel to choose a route
    and bind a source address, which is exactly the question being asked. That
    makes this instant, unprivileged and correct on a machine with several
    interfaces, where parsing `ip addr` would have to invent a tiebreak. With no
    route at all the connect raises, and having no address is the true answer.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(ROUTE_PROBE)
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def boot_id(root: Path = Path("/")) -> str:
    """This boot's unique id, used to tell one machine's session from another.

    A stick suspended in one laptop and resumed in another keeps its filesystem
    and therefore keeps anything cached on it. Everything in this module
    describes hardware that just changed underneath, so a cache that survives a
    reboot is a cache that reports the previous machine.
    """
    return _read(root / "proc/sys/kernel/random/boot_id").strip()


def uid() -> int:
    return os.getuid()
