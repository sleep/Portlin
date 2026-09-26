"""Real execution coverage for the three shipped runtime commands.

Before this file, nothing ever imported, ran or even byte-compiled
portlin-info, portlin-expand or portlin-encrypt: test_package.py only asserts
things about their source text, such as "growpart" appearing before
"cryptsetup". A syntax error, or a NameError on the very first line, would
reach a stick undetected. This loads each tool as a real module -- resolving
its `from devices import ...` against the real shared module rather than the
/usr/lib/portlin path that only exists on an installed stick -- and exercises
the pure arithmetic and parsing functions directly, with no root and no Linux.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_tool

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt", "portlin-install", "portlin-migrate", "portlin-wear"]


def _load_tool(name: str):
    return load_tool(name)


class TestToolsAreValidPython:
    @pytest.mark.parametrize("name", TOOLS)
    def test_compiles(self, name):
        source = (RUNTIME / name).read_text()
        compile(source, str(RUNTIME / name), "exec")

    def test_devices_module_compiles(self):
        source = (RUNTIME / "devices.py").read_text()
        compile(source, str(RUNTIME / "devices.py"), "exec")

    def test_migrate_module_compiles(self):
        source = (RUNTIME / "migrate.py").read_text()
        compile(source, str(RUNTIME / "migrate.py"), "exec")

    @pytest.mark.parametrize("name", TOOLS)
    def test_imports_without_error(self, name):
        # Importing (rather than running main()) exercises every module-level
        # statement, including the `from devices import ...` that a source-text
        # assertion cannot prove actually resolves.
        _load_tool(name)


class TestInfoUnclaimedSpace:
    """The arithmetic behind finding 1, which has now been wrong twice in
    opposite directions.

    Comparing the filesystem against the whole disk counts the ~1.6 GB of fixed
    partitions ahead of root (see layout.py) and nags on every stick forever.
    Comparing it against only its own partition goes silent on the ordinary
    case, an unexpanded image sitting on a much larger drive, which is exactly
    what portlin-expand exists for. Both gaps have to be counted.

    The overhead figures below are measured, not assumed: statvfs already
    excludes ext4 metadata, and the real gap between a partition and the
    statvfs size of the filesystem filling it is a flat ~2.1%.
    """

    # Measured on debian:trixie with a default mkfs.ext4, mounted, via df -B1.
    REAL_EXT4_OVERHEAD = 0.021

    # The shared module rather than portlin-info, because that is where this
    # arithmetic now lives: portlin-stats needs the same two gaps for the panel
    # readout, and a second copy is exactly what
    # test_tools_use_the_shared_device_module_not_a_hand_copy forbids.
    @pytest.fixture(scope="class")
    def info(self):
        return _load_tool("devices.py")

    def test_silent_when_the_filesystem_exactly_fills_a_full_size_partition(self, info):
        assert info.unclaimed_bytes(5_000_000_000, 5_000_000_000, 0) == 0

    def test_silent_on_a_fully_expanded_stick_at_real_ext4_overhead(self, info):
        # The regression the second attempt introduced: 300 MB of fixed slack is
        # about half the real overhead at this size, so the nag survived and
        # started quoting a figure at it.
        partition = 31_457_280_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        assert info.unclaimed_bytes(filesystem, partition, 0) == 0

    def test_silent_on_a_fully_expanded_large_drive(self, info):
        # At 62.9 GB the real gap is ~1311 MB, which clears a flat 1 GB floor on
        # its own. Only slack that scales with the partition keeps this quiet.
        partition = 62_914_560_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        assert info.unclaimed_bytes(filesystem, partition, 0) == 0

    def test_reports_the_drive_tail_on_an_unexpanded_stick(self, info):
        # The case the partition-only comparison went silent on: the image ships
        # at a fixed size, so a fresh stick on a 32 GB drive has ~24 GB sitting
        # after the root partition, untouched and invisible from inside it.
        partition = 6_400_000_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        tail = 24_000_000_000
        unclaimed = info.unclaimed_bytes(filesystem, partition, tail)
        assert unclaimed == pytest.approx(tail, rel=0.01)

    def test_reports_an_interrupted_expansion(self, info):
        # growpart succeeded and resize2fs did not: no tail left to find, and
        # all the unclaimed space is now inside a full-size partition.
        unclaimed = info.unclaimed_bytes(6_300_000_000, 30_000_000_000, 0)
        assert unclaimed == pytest.approx(22_800_000_000, abs=100_000_000)

    def test_stays_silent_below_the_reporting_floor(self, info):
        # Half a gigabyte is not worth interrupting anyone about.
        assert info.unclaimed_bytes(5_000_000_000, 5_000_000_000, 500_000_000) == 0

    def test_slack_scales_rather_than_sitting_at_a_constant(self, info):
        small = info.unused_inside_partition(0, 10_000_000_000)
        large = info.unused_inside_partition(0, 100_000_000_000)
        assert large - small == pytest.approx(90_000_000_000 * 0.97, rel=0.01)


class TestReleaseParsing:
    """The parser behind finding 4: portlin-info must read /etc/os-release
    (always present) instead of shelling out to lsb_release (installed
    nowhere in packages.py), and the same parser has to handle both that
    file's quoted values and /etc/portlin-release's unquoted ones.
    """

    @pytest.fixture(scope="class")
    def info(self):
        return _load_tool("portlin-info")

    def test_strips_quotes_from_os_release_style_values(self, info, tmp_path):
        path = tmp_path / "os-release"
        path.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
        assert info._parse_env_file(path) == {
            "PRETTY_NAME": "Debian GNU/Linux 13 (trixie)",
            "ID": "debian",
        }

    def test_reads_unquoted_values_the_same_way(self, info, tmp_path):
        # /etc/portlin-release is not quoted, and the same function parses it.
        path = tmp_path / "portlin-release"
        path.write_text("PORTLIN_VERSION=0.4.0\n")
        assert info._parse_env_file(path) == {"PORTLIN_VERSION": "0.4.0"}

    def test_ignores_comments_and_blank_lines(self, info, tmp_path):
        path = tmp_path / "os-release"
        path.write_text("# generated\n\nPRETTY_NAME=Plain\n")
        assert info._parse_env_file(path) == {"PRETTY_NAME": "Plain"}

    def test_tolerates_a_missing_file(self, info, tmp_path):
        assert info._parse_env_file(tmp_path / "does-not-exist") == {}

    def test_debian_description_falls_back_when_pretty_name_is_absent(
        self, info, tmp_path, monkeypatch
    ):
        path = tmp_path / "os-release"
        path.write_text("ID=debian\n")
        monkeypatch.setattr(info, "OS_RELEASE", path)
        assert info._debian_description() == "unknown"

    def test_debian_description_reads_a_real_os_release_shape(
        self, info, tmp_path, monkeypatch
    ):
        path = tmp_path / "os-release"
        path.write_text(
            'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
            "ID=debian\n"
            'VERSION_ID="13"\n'
        )
        monkeypatch.setattr(info, "OS_RELEASE", path)
        assert info._debian_description() == "Debian GNU/Linux 13 (trixie)"


class TestWelcomeBanner:
    """The terminal welcome banner: the mark beside live system data.

    Same import coverage as the other tools -- a syntax error here reaches a
    stick undetected otherwise -- plus the pure layout functions, since the
    banner's shape is the brand's.
    """

    def test_compiles(self):
        source = (RUNTIME / "portlin-welcome").read_text()
        compile(source, str(RUNTIME / "portlin-welcome"), "exec")

    @pytest.fixture(scope="class")
    def welcome(self):
        return _load_tool("portlin-welcome")

    def _data(self, welcome, **overrides):
        data = {
            "version": "0.1.2",
            "encrypted": True,
            "os": "Debian GNU/Linux 13 (trixie)",
            "machine": "ThinkPad X270",
            "firmware": "UEFI, 64-bit",
            "cpu": "Intel Core i5-7300U",
            "threads": "4",
            "memory": "3.2G of 15.5G",
            "disk_free": "41.2G",
            "disk_total": "58.0G",
            "uptime": "2 hours, 14 mins",
            "shell": "bash 5.2.37",
        }
        data.update(overrides)
        return data

    def test_uptime_is_fastfetch_shaped(self, welcome):
        assert welcome.format_uptime(0) == "0 mins"
        assert welcome.format_uptime(5 * 60) == "5 mins"
        assert welcome.format_uptime(62 * 60) == "1 hour, 2 mins"
        assert welcome.format_uptime((2 * 3600) + (14 * 60)) == "2 hours, 14 mins"
        assert welcome.format_uptime(3 * 86400 + 2 * 3600) == "3 days, 2 hours"

    def test_every_mark_row_is_the_same_width(self, welcome):
        for colored in (True, False):
            for unicode_glyphs in (True, False):
                rows = welcome.mark_lines(colored=colored, unicode_glyphs=unicode_glyphs)
                widths = {sum(len(run[0]) for run in row) for row in rows}
                assert len(widths) == 1, (colored, unicode_glyphs)

    def test_the_ascii_fallback_draws_the_same_geometry(self, welcome):
        art = "\n".join(
            "".join(run[0] for run in row)
            for row in welcome.mark_lines(colored=False, unicode_glyphs=False)
        )
        assert "+" in art and "#" in art
        assert "╭" not in art and "█" not in art

    def test_the_fastfetch_logo_is_the_same_mark(self, welcome):
        # One geometry in two places: the banner's art and the logo fastfetch
        # substitutes colors into. A hand-edit to either fails here.
        import re

        logo = (RUNTIME / "theme" / "fastfetch-logo.txt").read_text()
        logo = re.sub(r"\{[12]\}", "", logo)
        art = "\n".join(
            "".join(run[0] for run in row)
            for row in welcome.mark_lines(colored=False, unicode_glyphs=True)
        )
        assert logo.rstrip("\n") == art

    def test_plain_output_carries_no_escape_codes(self, welcome):
        banner = welcome.render(self._data(welcome), colored=False, columns=100)
        assert "\033[" not in banner
        assert "╭" in banner  # NO_COLOR keeps the box-drawing art.
        assert "portlin 0.1.2" in banner

    def test_the_accent_only_appears_for_the_encrypted_root(self, welcome):
        encrypted = welcome.render(self._data(welcome), colored=True, columns=100)
        plain = welcome.render(
            self._data(welcome, encrypted=False), colored=True, columns=100
        )
        assert welcome.ACCENT in encrypted
        assert "LUKS2 encrypted" in encrypted
        assert welcome.ACCENT not in plain
        assert "not encrypted" in plain

    def test_the_disk_row_leads_with_free_space(self, welcome):
        rows = welcome.info_rows(self._data(welcome))
        disk = next(value for key, value in rows if key == "disk")
        first_text, first_color = disk[0]
        assert first_text == "41.2G "
        assert first_color == "green-bold"

    def test_narrow_terminals_get_one_line(self, welcome):
        banner = welcome.render(self._data(welcome), colored=False, columns=60)
        assert "\n" not in banner
        assert "41.2G free of 58.0G" in banner
        assert "LUKS2 encrypted" in banner

    def test_the_footer_points_at_the_full_report(self, welcome):
        banner = welcome.render(self._data(welcome), colored=False, columns=100)
        assert "portlin-info" in banner
