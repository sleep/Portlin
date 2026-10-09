"""The HUD's readers, against real text from real kernels, and its packaging.

hud.py describes hardware the test machines do not have, so each parser is
fed captured output and each reader a /proc and /sys built under tmp_path.
The captured text is kept verbatim, trailing dots and padding included: the
kernel's spacing is part of what a parser has to survive.

The screen is curses, but everything up to the terminal is plain text: the
cards are rendered to lines of (text, role) here and checked for width,
layout and the "--" where nothing is known, and the packages are checked to
ship the tool beside the modules it imports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import load_tool
from portlin import package

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"


@pytest.fixture(scope="module")
def hud():
    return load_tool("hud.py")


@pytest.fixture(scope="module")
def tool():
    return load_tool("portlin-hud")


PROC_STAT = """cpu  512488 0 171193 54956660 10093 0 86644 0 0 0
cpu0 125234 0 30436 6787078 1733 0 37709 0 0 0
cpu1 54887 0 20492 6880120 1867 0 19410 0 0 0
intr 123456789 0 0 0
"""

MEMINFO = """MemTotal:       22509864 kB
MemFree:        16172644 kB
MemAvailable:   20640396 kB
Buffers:          699072 kB
Cached:          3772244 kB
SwapCached:            0 kB
SwapTotal:       1048572 kB
SwapFree:        1048572 kB
AnonPages:       1122548 kB
Shmem:              1108 kB
SReclaimable:     302064 kB
"""

NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:       0       0    0    0    0     0          0         0        0       0    0    0    0     0       0          0
  eth0:     302       3    0    0    0     0          0         0       42       1    0    0    0     0       0          0
 wlan0: 1234567890 1234    0    0    0     0          0         0 98765432    4321    0    0    0     0       0          0
"""

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\t010011AC\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""

DISKSTATS = """   1       0 ram0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
   8       0 sda 12345 678 2097152 4567 8910 1112 1048576 1314 0 1516 1718 0 0 0 0 0 0
   8       1 sda1 12000 600 2000000 4000 8000 1000 1000000 1300 0 1500 1700 0 0 0 0 0 0
 254       0 dm-0 100 0 2000 50 100 0 2000 50 0 100 100 0 0 0 0 0 0
"""

MOUNTS = """sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/mapper/portlin-root / ext4 rw,noatime,commit=120 0 0
tmpfs /run tmpfs rw,nosuid,nodev,size=1593584k,mode=755 0 0
/dev/sdb3 /boot ext4 rw,relatime 0 0
/dev/sdb2 /boot/efi vfat rw,relatime,fmask=0077 0 0
/dev/sdb3 /mnt/boot-again ext4 rw,relatime 0 0
/dev/sdc1 /media/me/My\\040Passport exfat rw,nosuid,nodev 0 0
nas:/export /mnt/nas nfs4 rw,relatime 0 0
"""

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   70.  -40.  -256        0      0      0      0      0        0
"""

IW_LINK = """Connected to 9c:3d:cf:aa:bb:cc (on wlan0)
\tSSID: attic
\tfreq: 5180
\tRX: 123456 bytes (789 packets)
\tTX: 4567 bytes (42 packets)
\tsignal: -52 dBm
\trx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\ttx bitrate: 780.0 MBit/s VHT-MCS 8 80MHz short GI VHT-NSS 2
"""

IP_ADDR = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}]},
    {"ifname": "eth0", "addr_info": [
        {"family": "inet", "local": "192.168.1.42", "prefixlen": 24, "scope": "global"},
        {"family": "inet6", "local": "fe80::1", "prefixlen": 64, "scope": "link"},
        {"family": "inet6", "local": "2001:db8::42", "prefixlen": 64, "scope": "global"},
    ]},
])

LSPCI = '''00:02.0 "VGA compatible controller" "Intel Corporation" "Alder Lake-P GT2 [Iris Xe Graphics]" -r0c "Lenovo" "Device 22e4"
01:00.0 "3D controller" "NVIDIA Corporation" "GA107M [GeForce RTX 3050 Mobile]" -ra1 "Lenovo" "Device 3a5c"
00:14.0 "USB controller" "Intel Corporation" "Alder Lake PCH USB 3.2 xHCI Host Controller" -r01 "Lenovo" "Device 22e4"
'''


class TestCpuParsers:
    def test_per_core_lines_are_read_in_order(self, hud):
        cores = hud.parse_proc_stat_cores(PROC_STAT)
        assert len(cores) == 2
        assert cores[0].idle == 6787078 + 1733

    def test_the_aggregate_line_is_not_a_core(self, hud):
        assert len(hud.parse_proc_stat_cores("cpu  1 2 3 4 5 6 7\n")) == 0

    def test_the_clock_is_the_mean_of_every_core(self, hud):
        assert hud.parse_cpu_mhz("cpu MHz\t\t: 1200.000\ncpu MHz\t\t: 3400.000\n") == 2300

    def test_no_clock_lines_is_none_rather_than_zero(self, hud):
        assert hud.parse_cpu_mhz("model name : x\n") is None

    def test_loadavg_and_uptime(self, hud):
        assert hud.parse_loadavg("0.06 0.60 0.67 5/468 9\n") == (0.06, 0.60, 0.67)
        assert hud.parse_uptime("69818.63 549566.14\n") == 69818.63
        assert hud.parse_loadavg("") is None

    def test_the_package_sensor_beats_the_cores(self, hud):
        sensors = [
            ("acpitz", {"temp1_input": 27800}),
            ("coretemp", {"Package id 0": 61000, "Core 0": 64000, "Core 1": 58000}),
        ]
        assert hud.pick_cpu_temperature(sensors) == 61

    def test_amd_names_its_sensor_tctl(self, hud):
        assert hud.pick_cpu_temperature([("k10temp", {"Tctl": 45125, "Tccd1": 41000})]) == 45

    def test_a_driver_with_no_package_reading_reports_its_hottest(self, hud):
        assert hud.pick_cpu_temperature([("coretemp", {"Core 0": 64000, "Core 1": 58000})]) == 64

    def test_no_cpu_sensor_is_none_not_the_board(self, hud):
        assert hud.pick_cpu_temperature([("nvme", {"Composite": 40000})]) is None


class TestMemoryParsers:
    def test_the_breakdown_is_in_bytes_and_used_excludes_cache(self, hud):
        memory = hud.parse_meminfo_breakdown(MEMINFO)
        assert memory.total == 22509864 * 1024
        assert memory.used == (22509864 - 20640396) * 1024
        assert memory.page_cache == (3772244 - 1108) * 1024
        assert memory.swap_used == 0

    def test_a_meminfo_without_available_is_rejected(self, hud):
        assert hud.parse_meminfo_breakdown("MemTotal: 10 kB\n") is None

    def test_zram_mm_stat(self, hud):
        assert hud.parse_zram_mm_stat("1073741824 268435456 301989888 0 301989888 1 0 0 0\n") == (
            1073741824, 268435456, 301989888)
        assert hud.parse_zram_mm_stat("") is None


class TestFilesystemParsers:
    def test_mounts_unescape_spaces(self, hud):
        mounts = hud.parse_mounts(MOUNTS)
        assert ("/dev/sdc1", "/media/me/My Passport", "exfat") in mounts

    def test_only_real_filesystems_and_each_device_once(self, hud):
        kept = hud.real_filesystems(hud.parse_mounts(MOUNTS))
        assert [mountpoint for _, mountpoint, _ in kept] == ["/", "/boot", "/boot/efi", "/media/me/My Passport", "/mnt/nas"]

    def test_diskstats_are_bytes_from_512_byte_sectors(self, hud):
        counters = hud.parse_diskstats(DISKSTATS)
        assert counters["sda"] == (2097152 * 512, 1048576 * 512)

    def test_the_disk_reader_keeps_whole_disks_only(self, hud, tmp_path):
        (tmp_path / "proc").mkdir()
        (tmp_path / "proc/diskstats").write_text(DISKSTATS)
        for name, size in (("sda", "250069680"), ("dm-0", "1000"), ("ram0", "1000"), ("nbd0", "0")):
            (tmp_path / "sys/block" / name).mkdir(parents=True)
            (tmp_path / "sys/block" / name / "size").write_text(f"{size}\n")
        (tmp_path / "proc/diskstats").write_text(DISKSTATS + "  43       0 nbd0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        assert list(hud.read_disk_counters(tmp_path)) == ["sda"]


class TestNetworkParsers:
    def test_net_dev_counters(self, hud):
        assert hud.parse_net_dev(NET_DEV)["wlan0"] == (1234567890, 98765432)

    def test_the_default_route_with_the_lowest_metric_wins(self, hud):
        assert hud.parse_proc_route(ROUTE) == "eth0"
        assert hud.parse_proc_route(ROUTE.splitlines()[0] + "\n") == ""

    @pytest.mark.parametrize("name, arp, devtype, wireless, device, kind", [
        ("lo", 772, "", False, False, "loopback"),
        ("wlan0", 1, "wlan", True, True, "wifi"),
        ("wlp3s0", 1, "", True, True, "wifi"),
        ("eth0", 1, "", False, True, "ethernet"),
        ("enx00e04c", 1, "", False, True, "ethernet"),
        ("docker0", 1, "bridge", False, False, "bridge"),
        ("veth1a2b", 1, "", False, False, "virtual"),
        ("wg0", 65534, "wireguard", False, False, "vpn"),
        ("tun0", 65534, "", False, False, "tunnel"),
        ("sit0", 776, "", False, False, "tunnel"),
        ("ppp0", 512, "", False, False, "ppp"),
        ("wwan0", 1, "wwan", False, True, "mobile"),
    ])
    def test_link_kinds(self, hud, name, arp, devtype, wireless, device, kind):
        assert hud.interface_kind(name, arp_type=arp, devtype=devtype, wireless=wireless, has_device=device) == kind

    def test_state_falls_back_to_carrier_when_operstate_is_unknown(self, hud):
        assert hud.interface_state("unknown\n", "1\n") == "up"
        assert hud.interface_state("down\n", "") == "down"

    def test_wireless_level_loses_its_trailing_dot(self, hud):
        assert hud.parse_proc_wireless(WIRELESS) == {"wlan0": -40}

    def test_iw_link(self, hud):
        link = hud.parse_iw_link(IW_LINK)
        assert link == {"ssid": "attic", "freq_mhz": 5180, "signal_dbm": -52, "bitrate_mbps": 866.7}
        assert hud.parse_iw_link("Not connected.\n") == {}

    def test_ip_addr_keeps_global_scope_only(self, hud):
        addresses = hud.parse_ip_addr(IP_ADDR)
        assert addresses["eth0"] == ("192.168.1.42/24", "2001:db8::42/64")
        assert addresses["lo"] == ()
        assert hud.parse_ip_addr("not json") == {}

    def test_the_reader_drops_the_kernels_dead_tunnels_and_loopback(self, hud, tmp_path):
        net = tmp_path / "sys/class/net"
        for name, arp, operstate in (("lo", 772, "unknown"), ("sit0", 776, "down"), ("erspan0", 1, "down"),
                                     ("eth1", 1, "down"), ("eth0", 1, "up"), ("wlan0", 1, "up")):
            entry = net / name
            entry.mkdir(parents=True)
            (entry / "type").write_text(f"{arp}\n")
            (entry / "operstate").write_text(f"{operstate}\n")
            (entry / "address").write_text("00:11:22:33:44:55\n")
            (entry / "uevent").write_text(f"INTERFACE={name}\n")
        (net / "eth0/speed").write_text("1000\n")
        (net / "eth0/carrier").write_text("1\n")
        os.symlink("../../../pci0000:00/0000:00:1f.6", net / "eth0/device")
        os.symlink("../../../pci0000:00/0000:00:1f.7", net / "eth1/device")
        (net / "wlan0/wireless").mkdir()
        os.symlink("../../../usb1/1-3/1-3:1.0", net / "wlan0/device")
        (tmp_path / "proc/net").mkdir(parents=True)
        (tmp_path / "proc/net/dev").write_text(NET_DEV)
        (tmp_path / "proc/net/route").write_text(ROUTE)
        (tmp_path / "proc/net/wireless").write_text(WIRELESS)

        links = hud.read_interfaces(
            tmp_path,
            addresses={"eth0": ("192.168.1.42/24",)},
            wifi_links={"wlan0": {"ssid": "attic", "signal_dbm": -52, "bitrate_mbps": 866.7}},
        )
        # An empty Ethernet port stays; the module's own dead devices go.
        assert [link.name for link in links] == ["eth0", "wlan0", "eth1"]
        eth, wlan = links[:2]
        assert links[2].state == "down"
        assert (eth.kind, eth.bus, eth.state, eth.speed_mbps, eth.addresses) == (
            "ethernet", "pci", "up", 1000, ("192.168.1.42/24",))
        assert (wlan.kind, wlan.bus, wlan.ssid, wlan.signal_dbm, wlan.rx_bytes) == (
            "wifi", "usb", "attic", -52, 1234567890)

    def test_the_default_route_sorts_first(self, hud, tmp_path):
        net = tmp_path / "sys/class/net"
        for name in ("eth0", "eth1"):
            (net / name).mkdir(parents=True)
            (net / name / "type").write_text("1\n")
            (net / name / "operstate").write_text("up\n")
            os.symlink("../../../pci", net / name / "device")
        (tmp_path / "proc/net").mkdir(parents=True)
        (tmp_path / "proc/net/route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth1\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\n")
        assert [link.name for link in hud.read_interfaces(tmp_path)] == ["eth1", "eth0"]


class TestGpuParsers:
    def test_nvidia_detail_line(self, hud):
        detail = hud.parse_nvidia_detail("NVIDIA GeForce RTX 3060, 12, 1024, 12288, 45, 30.50\n")
        assert detail["name"] == "NVIDIA GeForce RTX 3060"
        assert detail["percent"] == 12
        assert detail["memory_total"] == 12288 * 1024**2
        assert detail["power_w"] == 30.5

    def test_nvidia_fields_it_cannot_report_are_absent_not_fatal(self, hud):
        detail = hud.parse_nvidia_detail("Quadro K620, [N/A], 100, 2048, [N/A], [N/A]\n")
        assert detail["percent"] is None and detail["power_w"] is None
        assert detail["memory_used"] == 100 * 1024**2
        assert hud.parse_nvidia_detail("") is None

    def test_lspci_names_display_controllers_the_way_people_do(self, hud):
        assert hud.parse_lspci_mm(LSPCI) == ["Intel Iris Xe Graphics", "NVIDIA GeForce RTX 3050 Mobile"]

    def test_amd_detail_from_sysfs(self, hud, tmp_path):
        card = tmp_path / "card0"
        (card / "device/hwmon/hwmon3").mkdir(parents=True)
        (card / "device/mem_info_vram_used").write_text("536870912\n")
        (card / "device/mem_info_vram_total").write_text("8589934592\n")
        (card / "device/hwmon/hwmon3/temp1_input").write_text("51000\n")
        assert hud.read_amd_detail(card) == {
            "memory_used": 536870912, "memory_total": 8589934592, "temperature_c": 51}


class TestSmallParsers:
    def test_dpkg_versions(self, hud):
        assert hud.parse_dpkg_versions("portlin-runtime 0.1.2~local\nportlin-desktop 0.1.2~local\n") == {
            "portlin-runtime": "0.1.2~local", "portlin-desktop": "0.1.2~local"}

    def test_a_public_address_has_to_be_an_address(self, hud):
        assert hud.parse_public_address("203.0.113.9\n") == "203.0.113.9"
        assert hud.parse_public_address("2001:db8::1") == "2001:db8::1"
        # A captive portal answers every URL with its login page.
        assert hud.parse_public_address("<html><body>Sign in</body></html>") == ""

    def test_env_files(self, hud):
        assert hud.parse_env_file('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n# x\nPORTLIN_VERSION=0.1.2\n') == {
            "PRETTY_NAME": "Debian GNU/Linux 13 (trixie)", "PORTLIN_VERSION": "0.1.2"}


class TestRates:
    def test_rates_are_per_second(self, hud):
        before = {"eth0": (1000, 500)}
        after = {"eth0": (3000, 500), "wlan0": (10, 10)}
        result = hud.rates(before, after, 2.0)
        assert result["eth0"] == (1000.0, 0.0)
        # No previous sample for wlan0: not known rather than zero.
        assert result["wlan0"] is None

    def test_a_counter_going_backwards_is_unknown(self, hud):
        assert hud.rates({"sda": (900, 0)}, {"sda": (100, 0)}, 2.0)["sda"] is None

    def test_a_gap_spanning_a_suspend_is_unknown(self, hud):
        assert hud.rates({"sda": (0, 0)}, {"sda": (100, 0)}, hud.MAX_SAMPLE_GAP_SECONDS + 1)["sda"] is None
        assert hud.rates(None, {"sda": (100, 0)}, 2.0)["sda"] is None

    def test_formatting_says_dashes_for_the_unknown(self, hud):
        assert hud.format_rate(None) == "--"
        assert hud.format_rate(1536.0) == "1.5K/s"
        assert hud.format_duration(None) == "--"
        assert hud.format_duration(3 * 86400 + 4 * 3600) == "3d 4h"
        assert hud.format_duration(2 * 3600 + 15 * 60) == "2h 15m"
        assert hud.format_duration(59) == "0m"


class TestReaders:
    """The readers against a /proc and /sys built under tmp_path."""

    def test_cpu_temperature_prefers_hwmon_then_the_thermal_zone(self, hud, tmp_path):
        zone = tmp_path / "sys/class/thermal/thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "type").write_text("x86_pkg_temp\n")
        (zone / "temp").write_text("55000\n")
        assert hud.read_cpu_temperature(tmp_path) == 55
        hwmon = tmp_path / "sys/class/hwmon/hwmon1"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("coretemp\n")
        (hwmon / "temp1_input").write_text("61000\n")
        (hwmon / "temp1_label").write_text("Package id 0\n")
        assert hud.read_cpu_temperature(tmp_path) == 61

    def test_cpu_mhz_prefers_cpufreq(self, hud, tmp_path):
        for index, khz in enumerate((1200000, 3400000)):
            cpufreq = tmp_path / f"sys/devices/system/cpu/cpu{index}/cpufreq"
            cpufreq.mkdir(parents=True)
            (cpufreq / "scaling_cur_freq").write_text(f"{khz}\n")
        (tmp_path / "proc").mkdir()
        (tmp_path / "proc/cpuinfo").write_text("cpu MHz\t\t: 800.000\n")
        assert hud.read_cpu_mhz(tmp_path) == 2300

    def test_zram_devices_with_a_size(self, hud, tmp_path):
        for name, size in (("zram0", "1073741824"), ("zram1", "0")):
            device = tmp_path / "sys/block" / name
            device.mkdir(parents=True)
            (device / "disksize").write_text(f"{size}\n")
            (device / "mm_stat").write_text("268435456 67108864 75497472 0 75497472 1 0 0 0\n")
        devices = hud.read_zram(tmp_path)
        assert [device.name for device in devices] == ["zram0"]
        assert devices[0].compressed == 67108864

    def test_filesystems_are_measured_where_they_are_mounted(self, hud, tmp_path):
        (tmp_path / "proc").mkdir()
        (tmp_path / "proc/mounts").write_text(f"/dev/sdz1 {tmp_path} ext4 rw 0 0\n/dev/sdz2 /nonexistent-mount ext4 rw 0 0\n")
        filesystems = hud.read_filesystems(tmp_path)
        assert [fs.mountpoint for fs in filesystems] == [str(tmp_path)]
        assert filesystems[0].total_bytes > 0

    def test_release_reads_the_sticks_own_files(self, hud, tmp_path):
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc/portlin-release").write_text("PORTLIN_VERSION=0.1.2\nPORTLIN_COMMIT=1c5301a\n")
        (tmp_path / "etc/os-release").write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
        release = hud.read_release(
            tmp_path,
            topology={"source": "/dev/mapper/portlin-root", "encrypted": True, "disk": "sdb",
                      "partition_bytes": 0, "drive_bytes": 64 * 10**9, "tail_bytes": 0},
            dpkg_versions={"portlin-runtime": "0.1.2"},
        )
        assert release["version"] == "0.1.2" and release["commit"] == "1c5301a"
        assert release["encrypted"] and release["drive"] == "/dev/sdb"
        assert release["runtime_version"] == "0.1.2" and release["desktop_version"] == ""

    def test_a_sampler_against_an_empty_root_answers_without_raising(self, hud, tmp_path, monkeypatch):
        # No /proc, no /sys, no lsblk output: every field has to come back
        # as "not known" rather than as a traceback, because that is what a
        # reader hitting a file the kernel did not provide looks like.
        monkeypatch.setattr(hud, "command_output", lambda argv: "")
        monkeypatch.setattr(hud, "read_nvidia_detail", lambda: None)
        monkeypatch.setattr(hud.hostinfo, "local_address", lambda: "")
        sampler = hud.Sampler(tmp_path)
        sampler.lookup_public = False
        first = sampler.sample()
        second = sampler.sample()
        assert first["cpu"]["percent"] is None and second["cpu"]["percent"] is None
        assert second["memory"] is None and second["interfaces"] == []
        assert second["addresses"]["public_state"] == "off"
        json.dumps(second)

    def test_the_public_address_is_not_asked_for_without_a_route(self, hud, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(hud, "command_output", lambda argv: "")
        monkeypatch.setattr(hud, "read_nvidia_detail", lambda: None)
        monkeypatch.setattr(hud, "fetch_public_address", lambda *a, **k: asked.append(1) or "203.0.113.9")
        monkeypatch.setattr(hud.hostinfo, "local_address", lambda: "")
        sampler = hud.Sampler(tmp_path)
        assert sampler.sample()["addresses"]["public_state"] == "no route"
        assert asked == []
        monkeypatch.setattr(hud.hostinfo, "local_address", lambda: "192.168.1.42")
        sampler.sample()
        sampler._public_thread.join(5)
        addresses = sampler.sample()["addresses"]
        assert asked == [1]
        assert (addresses["public"], addresses["public_state"]) == ("203.0.113.9", "known")


class TestTheTerminalProgram:
    def test_it_draws_nothing_graphical(self, tool):
        source = (RUNTIME / "portlin-hud").read_text()
        assert "import gi" not in source
        assert tool.DEFAULT_INTERVAL >= 1

    def test_unknowns_are_dashes_not_numbers(self, tool):
        assert tool.format_tokens(None) == "--"
        assert tool.format_percent(None) == "--%"
        assert tool.format_bytes(None) == "--"
        assert tool.session_context({"context_percent": None}) == "context --"
        assert tool.fraction(None, 100) is None and tool.fraction(50, 0) is None

    def test_token_and_speed_formats(self, tool):
        assert tool.format_tokens(999) == "999"
        assert tool.format_tokens(12_345) == "12.3k"
        assert tool.format_tokens(2_500_000) == "2.5M"
        assert tool.format_speed(1000) == "1 Gb/s"
        assert tool.format_speed(866.7) == "866.7 Mb/s"

    def test_an_interface_is_summarised_in_words(self, tool):
        interface = {"kind": "ethernet", "bus": "usb", "state": "up", "speed_mbps": 1000, "default": True}
        assert tool.interface_summary(interface) == "ethernet, usb, up, 1 Gb/s, default route"
        assert tool.wifi_summary({"kind": "wifi"}) == "not associated"
        assert tool.wifi_summary({"kind": "wifi", "ssid": "attic", "signal_dbm": -52}) == "attic, -52 dBm"

    def test_the_agents_headline(self, tool):
        names = {"claude": "Claude Code", "codex": "Codex CLI"}
        assert tool.agents_headline({"processes": [], "names": names}) == "none running"
        assert tool.agents_headline({"processes": [{"agent": "claude"}, {"agent": "claude"}], "names": names}) == "2 Claude Code"
        assert tool.agents_headline({"processes": [{"agent": "claude"}, {"agent": "codex"}], "names": names}) == "2 running"

    def test_codex_limits_read_as_windows(self, tool):
        session = {"limits": {
            "primary": {"used_percent": 29.0, "window_minutes": 300},
            "secondary": {"used_percent": 11.0, "window_minutes": 10080},
        }}
        assert tool.session_limits(session) == "5h 29% used, 7d 11% used"
        assert tool.session_limits({}) == ""
        # Claude Code's come from abtop's file and carry its age; without
        # the file the row says what would provide it.
        claude = {"agent": "claude", "limits": {
            "primary": {"used_percent": 1.0, "window_minutes": 300}, "updated_at": 1000.0}}
        assert tool.session_limits(claude, now=1000.0 + 5 * 60) == "5h 1% used, as of 5m ago"
        assert tool.session_limits({"agent": "claude"}) == tool.LIMITS_ADVICE
        assert tool.session_limits({"agent": "opencode"}) == ""

    def test_the_memory_rows_cover_the_breakdown(self, tool):
        memory = {"anon": 1, "page_cache": 2, "buffers": 3, "shmem": 4, "reclaimable": 5, "free": 6}
        assert [name for name, _, _ in tool.memory_rows(memory)] == [
            "applications", "file cache", "buffers", "shared", "kernel, reclaimable", "free"]

    def test_arguments(self, tool):
        args = tool.parse_args(["--json", "--no-public", "--interval", "5"])
        assert args.json and args.no_public and args.interval == 5


SNAPSHOT = {
    "sampled_at": 1000.0,
    "host": {"hostname": "attic", "model": "ThinkPad X1 Carbon Gen 9",
             "cpu": "11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz", "threads": 8,
             "uptime_seconds": 9000, "load": [0.5, 0.4, 0.3]},
    "release": {"version": "0.9.0", "root": "/dev/mapper/root", "encrypted": True},
    "cpu": {"percent": 14.2, "cores": [10, 90, 30, 4, 55, 12, 0, 100]},
    "memory": {"total": 16 << 30, "used": 3 << 30, "anon": 2 << 30, "page_cache": 5 << 30,
               "buffers": 1 << 28, "shmem": 1 << 26, "reclaimable": 1 << 28, "free": 8 << 30},
    "gpu": None,
    "addresses": {"private": "192.168.1.42", "public_state": "off"},
    "filesystems": [{"mountpoint": "/", "used_bytes": 6 << 30, "total_bytes": 119 << 30,
                     "source": "/dev/mapper/root", "fstype": "ext4"}],
    "disks": [],
    "interfaces": [],
    "agents": {"processes": [{"agent": "claude", "pid": 1}], "names": {"claude": "Claude Code"},
               "sessions": [{"agent": "claude", "pid": 1, "cwd": "/src/portlin", "project": "portlin",
                             "status": "running Bash", "context_percent": 92, "context_window": 200_000}]},
}

EMPTY = {"host": {}, "release": {}, "cpu": {}, "memory": None, "gpu": None, "addresses": {},
         "filesystems": [], "disks": [], "interfaces": [], "agents": {}}


def text_of(line) -> str:
    return "".join(text for text, _ in line)


class TestTheCards:
    @pytest.mark.parametrize("width", [40, 80, 120, 200])
    def test_every_line_fills_the_width_exactly(self, tool, width):
        for snapshot in (SNAPSHOT, EMPTY):
            assert {tool.line_width(line) for line in tool.render(snapshot, width, now=1000.0)} == {width}

    def test_cards_sit_side_by_side_as_the_width_allows(self, tool):
        assert tool.column_count(40) == 1
        assert tool.column_count(80) == 2
        assert tool.column_count(120) == 3
        first = tool.render_text(SNAPSHOT, 80, now=1000.0).splitlines()[0]
        assert first.startswith("SYSTEM") and "RELEASE" in first

    def test_an_empty_snapshot_says_dashes_and_words(self, tool):
        text = tool.render_text(EMPTY, 120)
        assert "--%" in text
        assert "No graphics card reported" in text
        assert "No AI agents running." in text

    def test_a_long_value_wraps_under_its_column(self, tool):
        lines = tool.render_text(SNAPSHOT, 40, now=1000.0).splitlines()
        start = next(index for index, line in enumerate(lines) if "processor" in line)
        assert lines[start + 1].startswith(" " * (tool.NAME_WIDTH + 2))
        assert "2.80GHz" in "".join(lines[start:start + 3])

    def test_luks_is_the_only_crimson(self, tool):
        lines = tool.render(SNAPSHOT, 120, now=1000.0)
        accented = [text for line in lines for text, role in line if role == "accent"]
        assert accented == ["luks"]

    def test_a_nearly_full_context_draws_amber(self, tool):
        roles = {role for line in tool.render(SNAPSHOT, 120, now=1000.0)
                 for text, role in line if tool.GLYPHS_UTF8["full"] in text}
        assert "amber" in roles

    def test_an_ascii_terminal_gets_ascii_bars(self, tool):
        text = tool.render_text(SNAPSHOT, 80, glyphs=tool.GLYPHS_ASCII, now=1000.0)
        assert text.isascii() and "#" in text

    def test_clipping_marks_what_it_cut(self, tool):
        assert tool.clip([("abcdef", "text")], 4, "~") == [("abc", "text"), ("~", "text")]
        assert tool.clip([("abc", "text")], 4, "~") == [("abc", "text")]


class TestPackaging:
    def test_runtime_ships_the_hud_and_its_modules(self):
        # A terminal program, so a --minimal stick with no desktop has it too.
        files = package.text_files("portlin-runtime")
        assert files["usr/bin/portlin-hud"].startswith("#!/usr/bin/env python3")
        assert "usr/bin/portlin-hud" in package.executable_paths("portlin-runtime")
        for module in ("hud.py", "agents.py", "hostinfo.py", "devices.py", "catalog.py"):
            assert f"usr/lib/portlin/{module}" in files
            assert f"usr/lib/portlin/{module}" not in package.executable_paths("portlin-runtime")

    def test_it_has_no_menu_entry(self):
        desktop = package.text_files("portlin-desktop")
        assert "usr/bin/portlin-hud" not in desktop
        assert not any(path.endswith("portlin-hud.desktop") for path in desktop)
        assert "portlin-hud" not in (RUNTIME / "theme" / "labwc-menu.xml").read_text()
