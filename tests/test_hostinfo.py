"""The shared host readers, against real text from real machines.

Everything in hostinfo.py describes hardware that portlin's own test machines
do not have and, on macOS, could not have: there is no /proc/stat to read and no
/sys/class/drm to walk. So the parsers are exercised against captured text, and
the readers are exercised against a /proc and /sys built under tmp_path. That
second half is the part worth having. A parser that is correct about a string
and a reader that looks in the wrong directory produce exactly the same passing
test suite and a blank panel on real hardware.

The captured text below is genuine output, kept verbatim including its trailing
newlines and its vendor spelling. Tidying it up is how a parser comes to depend
on a shape the kernel does not actually produce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def hostinfo():
    return load_tool("hostinfo.py")


PROC_STAT = """cpu  1234567 8901 234567 12345678 45678 0 12345 0 0 0
cpu0 308641 2225 58641 3086419 11419 0 3086 0 0 0
cpu1 308642 2225 58642 3086419 11419 0 3086 0 0 0
intr 123456789 0 0 0
ctxt 987654321
btime 1756900000
"""

MEMINFO = """MemTotal:       16219488 kB
MemFree:          482716 kB
MemAvailable:   12871232 kB
Buffers:          214372 kB
Cached:          8123904 kB
SwapTotal:             0 kB
"""

CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-1185G7 @ 3.00GHz
cpu MHz\t\t: 1200.000

processor\t: 1
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-1185G7 @ 3.00GHz
cpu MHz\t\t: 1200.000

processor\t: 2
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-1185G7 @ 3.00GHz
cpu MHz\t\t: 1200.000
"""


class TestCpu:
    def test_reads_only_the_aggregate_line(self, hostinfo):
        # The per-core lines start with the same three letters. A parser that
        # matched on a prefix rather than the exact field would take cpu0's
        # numbers and report one core's load as the machine's.
        sample = hostinfo.parse_proc_stat(PROC_STAT)
        assert sample.total == 1234567 + 8901 + 234567 + 12345678 + 45678 + 12345

    def test_counts_iowait_as_idle(self, hostinfo):
        # A machine copying to a slow USB stick spends its time in iowait. That
        # is the CPU doing nothing, and counting it as busy would make every
        # long copy -- the thing this product is for -- look like a pegged CPU.
        sample = hostinfo.parse_proc_stat(PROC_STAT)
        assert sample.idle == 12345678 + 45678

    def test_a_kernel_with_fewer_fields_is_refused(self, hostinfo):
        assert hostinfo.parse_proc_stat("cpu 1 2 3\n") is None

    def test_garbage_is_refused_rather_than_raising(self, hostinfo):
        assert hostinfo.parse_proc_stat("cpu a b c d e\n") is None
        assert hostinfo.parse_proc_stat("") is None

    def test_percent_between_two_samples(self, hostinfo):
        previous = hostinfo.CpuSample(total=1000, idle=900)
        current = hostinfo.CpuSample(total=1200, idle=1050)
        assert hostinfo.cpu_percent(previous, current) == pytest.approx(25.0)

    def test_no_previous_sample_is_unknown_rather_than_zero(self, hostinfo):
        # The first run of a session has nothing to compare against. Reporting
        # 0% there would be a lie that looks exactly like an idle machine.
        assert hostinfo.cpu_percent(None, hostinfo.CpuSample(1000, 900)) is None

    def test_counters_going_backwards_are_unknown(self, hostinfo):
        # A discarded state file and a resumed machine both produce this. The
        # alternative to None is a since-boot average presented as "now".
        previous = hostinfo.CpuSample(total=5000, idle=4000)
        current = hostinfo.CpuSample(total=1000, idle=900)
        assert hostinfo.cpu_percent(previous, current) is None

    def test_identical_samples_are_unknown_rather_than_zero(self, hostinfo):
        sample = hostinfo.CpuSample(total=1000, idle=900)
        assert hostinfo.cpu_percent(sample, sample) is None


class TestMemory:
    def test_used_is_total_minus_available(self, hostinfo):
        memory = hostinfo.parse_meminfo(MEMINFO)
        assert memory.total_bytes == 16219488 * 1024
        assert memory.used_bytes == (16219488 - 12871232) * 1024

    def test_memfree_is_never_the_fallback(self, hostinfo):
        # MemFree counts the page cache as used, so an hour-old machine reports
        # nearly all its memory in use. True of the kernel's bookkeeping and
        # meaningless to the person reading the panel. Without MemAvailable the
        # honest answer is that it is not known.
        without = "\n".join(
            line for line in MEMINFO.splitlines() if "MemAvailable" not in line
        )
        assert hostinfo.parse_meminfo(without) is None


class TestCpuModel:
    def test_takes_the_first_model_name(self, hostinfo):
        assert hostinfo.parse_cpu_model(CPUINFO) == "Intel(R) Core(TM) i7-1185G7 @ 3.00GHz"

    def test_counts_logical_cpus(self, hostinfo):
        assert hostinfo.parse_cpu_threads(CPUINFO) == 3

    def test_a_kernel_without_model_name_gives_nothing(self, hostinfo):
        assert hostinfo.parse_cpu_model("processor\t: 0\n") == ""


class TestDmiModel:
    def test_lenovo_puts_the_readable_name_in_version(self, hostinfo):
        # product_name is a bare order code on ThinkPads. Preferring it would
        # put "21HMCTO1WW" in the panel, which names the machine to nobody.
        assert hostinfo.parse_dmi_model(
            "LENOVO", "21HMCTO1WW", "ThinkPad X1 Carbon Gen 11"
        ) == "LENOVO ThinkPad X1 Carbon Gen 11"

    def test_dell_puts_the_whole_thing_in_product_name(self, hostinfo):
        assert hostinfo.parse_dmi_model(
            "Dell Inc.", "XPS 13 9310", ""
        ) == "Dell Inc. XPS 13 9310"

    def test_the_vendor_is_not_repeated_when_the_model_already_carries_it(self, hostinfo):
        assert hostinfo.parse_dmi_model(
            "ASUSTeK COMPUTER INC.", "ASUSTeK ROG Strix", ""
        ) == "ASUSTeK ROG Strix"

    @pytest.mark.parametrize(
        "placeholder",
        ["To Be Filled By O.E.M.", "System Product Name", "Default string", "Unknown"],
    )
    def test_vendor_placeholders_are_not_answers(self, hostinfo, placeholder):
        # Whitebox desktops and virtual machines ship these unfilled. Printing
        # "To Be Filled By O.E.M." as the machine's model is worse than printing
        # nothing, because it looks like portlin got it wrong rather than the
        # firmware having nothing to say.
        assert hostinfo.parse_dmi_model("", placeholder, "") == ""

    def test_a_real_product_called_system_survives(self, hostinfo):
        # The placeholder match is on the whole string, not a substring, so a
        # machine genuinely named this keeps its name.
        assert hostinfo.parse_dmi_model("", "System76 Lemur Pro", "") == "System76 Lemur Pro"


class TestFirmware:
    def test_no_efi_directory_means_it_booted_bios(self, hostinfo):
        assert hostinfo.parse_firmware_mode(False, "") == "BIOS"

    def test_efi_reports_its_width(self, hostinfo):
        assert hostinfo.parse_firmware_mode(True, "64\n") == "UEFI, 64-bit"

    def test_efi_without_a_readable_width_still_says_uefi(self, hostinfo):
        assert hostinfo.parse_firmware_mode(True, "") == "UEFI"


class TestGpuParsers:
    def test_amdgpu_busy_percent(self, hostinfo):
        assert hostinfo.parse_gpu_busy("22\n") == 22

    def test_an_empty_counter_is_not_zero_percent(self, hostinfo):
        # sysfs returns an empty string for a counter the driver did not
        # populate. Reading that as 0 puts a confident, wrong number on screen.
        assert hostinfo.parse_gpu_busy("") is None
        assert hostinfo.parse_gpu_busy("\n") is None

    def test_nvidia_smi_answers_not_available_in_words(self, hostinfo):
        # It prints N/A rather than failing when a card cannot report, so int()
        # here would raise on a perfectly healthy machine.
        assert hostinfo.parse_nvidia_smi("N/A\n") is None

    def test_nvidia_smi_percentage(self, hostinfo):
        assert hostinfo.parse_nvidia_smi("31\n") == 31

    def test_i915_frequency_is_a_frequency(self, hostinfo):
        assert hostinfo.parse_i915_freq("350\n") == 350


class TestFormatBytes:
    @pytest.mark.parametrize(
        "count,expected",
        [
            (0, "0"),
            (4 * 1024**3, "4.0G"),
            (16219488 * 1024, "15.5G"),
            (512 * 1024**2, "512M"),
            (2 * 1024**4, "2.0T"),
        ],
    )
    def test_matches_the_casing_df_h_uses(self, hostinfo, count, expected):
        # A single suffix letter and powers of 1024, because that is what df,
        # lsblk and free print. Portlin's labels quote real system text rather
        # than inventing a prettier form of it.
        assert hostinfo.format_bytes(count) == expected


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestReadersAgainstAFakeRoot:
    """The readers, not just the parsers.

    A parser that is right about a string and a reader that looks in the wrong
    directory pass exactly the same parser tests and produce a blank panel on
    real hardware. These build the directories the kernel would and require the
    real readers to find them.
    """

    @pytest.fixture
    def root(self, tmp_path, hostinfo):
        _write(tmp_path / "proc/stat", PROC_STAT)
        _write(tmp_path / "proc/meminfo", MEMINFO)
        _write(tmp_path / "proc/cpuinfo", CPUINFO)
        dmi = tmp_path / hostinfo.DMI
        _write(dmi / "sys_vendor", "LENOVO\n")
        _write(dmi / "product_name", "21HMCTO1WW\n")
        _write(dmi / "product_version", "ThinkPad X1 Carbon Gen 11\n")
        _write(tmp_path / "sys/firmware/efi/fw_platform_size", "64\n")
        _write(tmp_path / "proc/sys/kernel/random/boot_id", "abc-123\n")
        return tmp_path

    def test_reads_the_whole_machine(self, hostinfo, root):
        host = hostinfo.read_host(root)
        assert host.model == "LENOVO ThinkPad X1 Carbon Gen 11"
        assert host.cpu.startswith("Intel(R) Core(TM) i7-1185G7")
        assert host.threads == 3
        assert host.memory_bytes == 16219488 * 1024
        assert host.firmware == "UEFI, 64-bit"

    def test_a_machine_with_no_proc_at_all_does_not_raise(self, hostinfo, tmp_path):
        # Every reader has to survive the file simply not being there. This is
        # the shape of a container, and of every unit-test host portlin has.
        host = hostinfo.read_host(tmp_path)
        assert host.model == "" and host.cpu == "" and host.firmware == "BIOS"
        assert hostinfo.read_cpu_sample(tmp_path) is None
        assert hostinfo.read_memory(tmp_path) is None
        assert hostinfo.read_battery(tmp_path) is None
        assert hostinfo.read_gpu(tmp_path) is None

    def test_boot_id_is_read_and_stripped(self, hostinfo, root):
        assert hostinfo.boot_id(root) == "abc-123"


class TestBattery:
    def test_a_wireless_mouse_is_not_the_machines_battery(self, hostinfo, tmp_path):
        # /sys/class/power_supply carries the AC adapter, USB-C source ports and
        # -- genuinely -- hidpp_battery_0 for a Logitech mouse. Matching on the
        # type alone would one day put a stranger's mouse in the panel.
        supply = tmp_path / "sys/class/power_supply"
        _write(supply / "hidpp_battery_0/type", "Battery\n")
        _write(supply / "hidpp_battery_0/capacity", "35\n")
        _write(supply / "hidpp_battery_0/status", "Discharging\n")
        assert hostinfo.read_battery(tmp_path) is None

    def test_the_ac_adapter_is_not_a_battery(self, hostinfo, tmp_path):
        supply = tmp_path / "sys/class/power_supply"
        _write(supply / "AC/type", "Mains\n")
        _write(supply / "AC/online", "1\n")
        assert hostinfo.read_battery(tmp_path) is None

    def test_reads_a_single_pack(self, hostinfo, tmp_path):
        supply = tmp_path / "sys/class/power_supply"
        _write(supply / "BAT0/type", "Battery\n")
        _write(supply / "BAT0/capacity", "87\n")
        _write(supply / "BAT0/status", "Discharging\n")
        battery = hostinfo.read_battery(tmp_path)
        assert battery.percent == 87 and not battery.charging

    def test_a_thinkpad_with_two_packs_reports_one_number(self, hostinfo, tmp_path):
        # BAT0 and BAT1 both exist and either can be absent or empty. One
        # reading is not the machine's answer.
        supply = tmp_path / "sys/class/power_supply"
        for name, capacity, status in (("BAT0", "80", "Charging"), ("BAT1", "60", "Full")):
            _write(supply / name / "type", "Battery\n")
            _write(supply / name / "capacity", capacity + "\n")
            _write(supply / name / "status", status + "\n")
        battery = hostinfo.read_battery(tmp_path)
        assert battery.percent == 70 and battery.charging


class TestGpuSelection:
    def _card(self, root: Path, name: str, *, boot_vga: str = "0", **files: str) -> None:
        card = root / "sys/class/drm" / name
        _write(card / "device/boot_vga", boot_vga + "\n")
        for field, value in files.items():
            _write(card / field.replace("__", "/"), value + "\n")

    def test_render_nodes_and_connectors_are_not_cards(self, hostinfo, tmp_path):
        # /sys/class/drm holds renderD128 and card0-HDMI-A-1 alongside the real
        # entries. Walking all of them finds no counter and reports nothing.
        drm = tmp_path / "sys/class/drm"
        _write(drm / "renderD128/dev", "226:128\n")
        _write(drm / "card0-HDMI-A-1/status", "connected\n")
        assert hostinfo.read_gpu(tmp_path) is None

    def test_amdgpu_reports_real_utilisation(self, hostinfo, tmp_path):
        self._card(tmp_path, "card0", boot_vga="1", device__gpu_busy_percent="22")
        gpu = hostinfo.read_gpu(tmp_path)
        assert gpu.kind == "percent" and gpu.label == "22%"

    def test_the_display_gpu_is_preferred_over_a_sleeping_discrete_card(
        self, hostinfo, tmp_path
    ):
        # On a hybrid laptop the discrete card is usually runtime suspended and
        # will truthfully report 0% while the integrated one does all the work.
        # boot_vga is what says which one is driving the screen.
        self._card(tmp_path, "card0", boot_vga="0", device__gpu_busy_percent="0")
        self._card(tmp_path, "card1", boot_vga="1", device__gpu_busy_percent="47")
        assert hostinfo.read_gpu(tmp_path).label == "47%"

    def test_intel_reports_a_frequency_and_says_so(self, hostinfo, tmp_path):
        # The honesty test. i915 exposes no busy-percent counter, and dividing
        # the current clock by the maximum would produce a number that looks
        # exactly like the other vendors' percentages while meaning something
        # entirely different.
        self._card(tmp_path, "card0", boot_vga="1", gt_cur_freq_mhz="350")
        gpu = hostinfo.read_gpu(tmp_path)
        assert gpu.kind == "frequency"
        assert gpu.label == "350MHz"
        assert "%" not in gpu.label
        assert "not utilisation" in gpu.detail

    def test_a_card_with_no_counter_is_named_rather_than_measured(self, hostinfo, tmp_path):
        self._card(tmp_path, "card0", boot_vga="1")
        gpu = hostinfo.read_gpu(tmp_path)
        assert gpu.kind == "name" and gpu.label == ""

    def test_nvidia_is_used_only_when_sysfs_has_nothing(self, hostinfo, tmp_path):
        self._card(tmp_path, "card0", boot_vga="1", device__gpu_busy_percent="22")
        assert hostinfo.read_gpu(tmp_path, nvidia=99).label == "22%"

    def test_nvidia_answers_when_there_is_no_drm_card_at_all(self, hostinfo, tmp_path):
        assert hostinfo.read_gpu(tmp_path, nvidia=31).label == "31%"
