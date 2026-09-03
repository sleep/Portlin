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
TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt", "portlin-install"]


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

    @pytest.fixture(scope="class")
    def info(self):
        return _load_tool("portlin-info")

    def test_silent_when_the_filesystem_exactly_fills_a_full_size_partition(self, info):
        assert info._unclaimed_bytes(5_000_000_000, 5_000_000_000, 0) == 0

    def test_silent_on_a_fully_expanded_stick_at_real_ext4_overhead(self, info):
        # The regression the second attempt introduced: 300 MB of fixed slack is
        # about half the real overhead at this size, so the nag survived and
        # started quoting a figure at it.
        partition = 31_457_280_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        assert info._unclaimed_bytes(filesystem, partition, 0) == 0

    def test_silent_on_a_fully_expanded_large_drive(self, info):
        # At 62.9 GB the real gap is ~1311 MB, which clears a flat 1 GB floor on
        # its own. Only slack that scales with the partition keeps this quiet.
        partition = 62_914_560_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        assert info._unclaimed_bytes(filesystem, partition, 0) == 0

    def test_reports_the_drive_tail_on_an_unexpanded_stick(self, info):
        # The case the partition-only comparison went silent on: the image ships
        # at a fixed size, so a fresh stick on a 32 GB drive has ~24 GB sitting
        # after the root partition, untouched and invisible from inside it.
        partition = 6_400_000_000
        filesystem = int(partition * (1 - self.REAL_EXT4_OVERHEAD))
        tail = 24_000_000_000
        unclaimed = info._unclaimed_bytes(filesystem, partition, tail)
        assert unclaimed == pytest.approx(tail, rel=0.01)

    def test_reports_an_interrupted_expansion(self, info):
        # growpart succeeded and resize2fs did not: no tail left to find, and
        # all the unclaimed space is now inside a full-size partition.
        unclaimed = info._unclaimed_bytes(6_300_000_000, 30_000_000_000, 0)
        assert unclaimed == pytest.approx(22_800_000_000, abs=100_000_000)

    def test_stays_silent_below_the_reporting_floor(self, info):
        # Half a gigabyte is not worth interrupting anyone about.
        assert info._unclaimed_bytes(5_000_000_000, 5_000_000_000, 500_000_000) == 0

    def test_slack_scales_rather_than_sitting_at_a_constant(self, info):
        small = info._unused_inside_partition(0, 10_000_000_000)
        large = info._unused_inside_partition(0, 100_000_000_000)
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
