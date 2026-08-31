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

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt"]


def _load_tool(name: str):
    """Import a runtime tool as a real module, without running its main().

    The tool inserts /usr/lib/portlin -- its location on an installed stick,
    which does not exist here -- at the front of sys.path before importing
    devices. Putting the real runtime directory on sys.path first means that
    import still resolves: a nonexistent sys.path entry is silently skipped,
    and Python keeps searching the entries after it.
    """
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    path = RUNTIME / name
    # These tools ship with no .py extension, so spec_from_file_location
    # cannot infer a loader from the suffix; it has to be given one directly.
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_file_location(loader.name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """The arithmetic behind finding 1: nagging must track the partition, not
    the whole disk, or the ~1.6 GB of fixed partitions ahead of root (see
    layout.py) makes every stick, expanded or not, report unused space
    forever.
    """

    @pytest.fixture(scope="class")
    def info(self):
        return _load_tool("portlin-info")

    def test_zero_when_the_filesystem_exactly_fills_the_partition(self, info):
        assert info._unclaimed_bytes(5_000_000_000, 5_000_000_000) == 0

    def test_zero_within_slack_of_ordinary_filesystem_overhead(self, info):
        assert info._unclaimed_bytes(4_995_000_000, 5_000_000_000) == 0

    def test_a_fully_expanded_stick_reports_no_unclaimed_space(self, info):
        # This is finding 1's exact regression: the old code compared the
        # filesystem against the whole disk, which is bigger than the root
        # partition by the fixed partitions ahead of it on every stick, so it
        # nagged even once the partition itself was completely full.
        partition_bytes = 30_000_000_000  # a fully expanded partition
        filesystem_bytes = 29_950_000_000  # ext4 overhead only
        assert info._unclaimed_bytes(filesystem_bytes, partition_bytes) == 0

    def test_reports_a_real_unclaimed_gigabyte(self, info):
        unclaimed = info._unclaimed_bytes(5_000_000_000, 20_000_000_000)
        assert unclaimed > 0
        assert unclaimed == pytest.approx(14_700_000_000, abs=1_000_000)


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
