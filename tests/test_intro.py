"""Checks on the intro film and the autostart entry that plays it once.

Nothing here draws a frame: that needs Pango, cairo and the fonts a stick
ships, and scripts/render-intro.py does it in a container. What can be
checked anywhere is every decision made before drawing -- which language comes
last, which word never repeats, how the drive is read and measured, when a
login plays it and when it stays quiet -- and that the packages ship what the
entry and the program name.
"""

from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from portlin import package, packages

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
INTRO = RUNTIME / "portlin-intro"
AUTOSTART_ENTRY = RUNTIME / "portlin-intro-autostart.desktop"


def _stub_graphics() -> None:
    """Fake gi and cairo, so the film's module imports on a machine with neither.

    Adds to a gi stub another test file already installed rather than
    replacing it, because those tests hold references into theirs.
    """
    gi = sys.modules.get("gi")
    if not getattr(gi, "_portlin_stub", False):
        try:
            import gi as real_gi  # noqa: F401
            import cairo  # noqa: F401
            return
        except ImportError:
            pass
        gi = types.ModuleType("gi")
        gi._portlin_stub = True
        gi.require_version = lambda *args, **kwargs: None
        gi.repository = types.ModuleType("gi.repository")
        sys.modules["gi"] = gi
        sys.modules["gi.repository"] = gi.repository
    for name in ("Gtk", "Gdk", "GLib", "Pango", "PangoCairo"):
        if not hasattr(gi.repository, name):
            module = MagicMock(name=name)
            setattr(gi.repository, name, module)
            sys.modules[f"gi.repository.{name}"] = module
    sys.modules.setdefault("cairo", MagicMock(name="cairo"))


@pytest.fixture(scope="module")
def intro():
    _stub_graphics()
    # devices and hostinfo come from /usr/lib/portlin on a stick.
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    loader = importlib.machinery.SourceFileLoader("portlin_intro", str(INTRO))
    spec = importlib.util.spec_from_file_location(loader.name, INTRO, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


class TestLanguage:
    @pytest.mark.parametrize("environ, expected", [
        ({"LANG": "de_DE.UTF-8"}, "de"),
        ({"LANG": "pt_BR.UTF-8"}, "pt"),
        ({"LANG": "sr_RS@latin"}, "sr"),
        # gettext's order: LANGUAGE beats LC_ALL beats LC_MESSAGES beats LANG.
        ({"LANG": "en_GB.UTF-8", "LC_MESSAGES": "fr_FR.UTF-8"}, "fr"),
        ({"LANG": "en_GB.UTF-8", "LANGUAGE": "nl:en"}, "nl"),
        # Unset, and the C locale, are not a language anyone chose.
        ({}, "en"),
        ({"LANG": "C.UTF-8"}, "en"),
        ({"LC_ALL": "POSIX"}, "en"),
    ])
    def test_reads_the_language_the_desktop_speaks(self, intro, environ, expected):
        assert intro.language_of(environ) == expected

    def test_the_last_word_is_the_accounts_own_language(self, intro):
        flyby, final = intro.order_words("de")
        assert final == "Willkommen"
        assert "Willkommen" not in flyby

    def test_a_language_with_no_entry_lands_on_english(self, intro):
        flyby, final = intro.order_words("xx")
        assert final == "Welcome"
        assert "Welcome" not in flyby

    def test_every_other_word_still_flies_past(self, intro):
        flyby, _ = intro.order_words("fr")
        assert len(flyby) == len(intro.WELCOMES) - 1

    def test_one_word_per_language(self, intro):
        codes = [code for code, _ in intro.WELCOMES]
        assert len(codes) == len(set(codes))
        assert "en" in codes

    def test_enough_words_survive_without_any_extra_fonts(self, intro):
        # DejaVu is all a stick is sure to have. The film drops what no font
        # can draw, and the fly-by should still be full after that.
        latin_greek_cyrillic = [
            word for _, word in intro.WELCOMES
            if all(ord(ch) < 0x0530 for ch in word)
        ]
        assert len(latin_greek_cyrillic) > intro.FLYBY_COUNT


class TestOnce:
    def test_stamp_follows_xdg_state_home(self, intro, tmp_path):
        path = intro.stamp_path({"XDG_STATE_HOME": str(tmp_path)})
        assert path == tmp_path / "portlin" / "intro-seen"

    def test_stamp_defaults_under_the_home_directory(self, intro):
        path = intro.stamp_path({})
        assert path == Path.home() / ".local" / "state" / "portlin" / "intro-seen"

    def test_a_seen_account_returns_before_touching_gtk(self, intro, tmp_path, monkeypatch):
        stamp = tmp_path / "portlin" / "intro-seen"
        stamp.parent.mkdir()
        stamp.touch()
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(intro, "play", lambda film: pytest.fail("played twice"))
        assert intro.main(["--once"]) == 0

    def test_the_stamp_is_written_before_playing(self, intro, tmp_path, monkeypatch):
        # So a film that crashes, or a machine that cannot draw it, costs one
        # login and not every one after.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        seen_when_played = []
        monkeypatch.setattr(intro, "make_film", lambda environ: object())
        monkeypatch.setattr(
            intro, "play",
            lambda film: seen_when_played.append(
                (tmp_path / "portlin" / "intro-seen").exists()) or 0,
        )
        monkeypatch.setattr(intro, "can_play", lambda once: True)
        intro.main(["--once"])
        assert seen_when_played == [True]


class TestAutostart:
    @pytest.fixture
    def entry(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(AUTOSTART_ENTRY.read_text())
        return parser["Desktop Entry"]

    def test_it_plays_once_rather_than_every_login(self, entry):
        assert entry["Exec"] == "portlin-intro --once"

    def test_it_ships_to_the_system_autostart_directory(self):
        destination = package.AUTOSTART_ENTRIES["portlin-intro-autostart.desktop"]
        assert destination == "etc/xdg/autostart/portlin-intro.desktop"
        files = package.text_files("portlin-desktop")
        assert files[destination] == AUTOSTART_ENTRY.read_text()

    def test_its_icon_is_one_the_packages_install(self, entry):
        assert entry["Icon"] == package.APP_ICON


class TestPackaging:
    def test_it_parses_and_has_a_python3_shebang(self):
        source = INTRO.read_text()
        assert source.startswith("#!/usr/bin/env python3")
        compile(source, str(INTRO), "exec")

    def test_it_ships_in_the_desktop_package_and_is_executable(self):
        assert "usr/bin/portlin-intro" in package.text_files("portlin-desktop")
        assert "usr/bin/portlin-intro" in package.executable_paths("portlin-desktop")
        assert "usr/bin/portlin-intro" not in package.text_files("portlin-runtime")

    def test_the_cairo_binding_is_a_dependency_and_in_the_image(self):
        # A GTK draw handler hands Python its cairo context through this
        # package; without it the window opens and stays black. It has to be
        # in the rootfs too, because write installs portlin-desktop offline.
        control = package.text_files("portlin-desktop")["DEBIAN/control"]
        assert "python3-gi-cairo" in control
        assert "python3-gi-cairo" in packages.DESKTOP

    def test_it_draws_in_the_brand_palette(self, intro):
        def hex_of(colour):
            return "#" + "".join(f"{round(c * 255):02X}" for c in colour)

        assert hex_of(intro.INK) == "#12161C"
        assert hex_of(intro.MUTED) == "#7C8B9E"
        assert hex_of(intro.ACCENT) == "#FF3355"


GIB, MIB = 1024**3, 1024**2

# lsblk -bnPo NAME,TYPE,SIZE,FSTYPE,FSVER,PARTTYPE,MOUNTPOINT on an encrypted,
# unexpanded stick: the column format as lsblk prints it, with the type
# columns udev fills in on a running desktop.
LSBLK_STICK = """\
NAME="sdb" TYPE="disk" SIZE="64023222272" FSTYPE="" FSVER="" PARTTYPE="" MOUNTPOINT=""
NAME="sdb1" TYPE="part" SIZE="1048576" FSTYPE="" FSVER="" PARTTYPE="21686148-6449-6e6f-744e-656564454649" MOUNTPOINT=""
NAME="sdb2" TYPE="part" SIZE="536870912" FSTYPE="vfat" FSVER="FAT32" PARTTYPE="c12a7328-f81f-11d2-ba4b-00a0c93ec93b" MOUNTPOINT="/boot/efi"
NAME="sdb3" TYPE="part" SIZE="1073741824" FSTYPE="ext4" FSVER="1.0" PARTTYPE="0fc63daf-8483-4772-8e79-3d69d8477de4" MOUNTPOINT="/boot"
NAME="sdb4" TYPE="part" SIZE="6979321856" FSTYPE="crypto_LUKS" FSVER="2" PARTTYPE="0fc63daf-8483-4772-8e79-3d69d8477de4" MOUNTPOINT=""
NAME="root" TYPE="crypt" SIZE="6962544640" FSTYPE="ext4" FSVER="1.0" PARTTYPE="" MOUNTPOINT="/"
"""


def _stick(intro, monkeypatch, *, source="/dev/mapper/root", lsblk=LSBLK_STICK):
    monkeypatch.setattr(intro, "root_source", lambda: source)
    monkeypatch.setattr(intro, "backing_partition", lambda src: "sdb4")
    monkeypatch.setattr(intro, "backing_disk", lambda part: "sdb")
    monkeypatch.setattr(intro, "command_output", lambda argv: lsblk)
    monkeypatch.setattr(intro, "sysfs_sectors", lambda name, field: 64023222272 // 512)
    monkeypatch.setattr(intro, "disk_tail_bytes", lambda disk, part: 55 * GIB)
    monkeypatch.setattr(intro.shutil, "disk_usage",
                        lambda path: types.SimpleNamespace(total=6 * GIB))


class TestDrive:
    def test_reads_the_four_partitions_as_lsblk_names_them(self, intro, monkeypatch):
        _stick(intro, monkeypatch)
        drive = intro.read_drive()
        assert [p.name for p in drive.partitions] == ["sdb1", "sdb2", "sdb3", "sdb4"]
        assert [p.kind for p in drive.partitions] == ["EF02", "vfat", "ext4", "LUKS2"]
        assert [p.mount for p in drive.partitions] == ["", "/boot/efi", "/boot", "/"]
        assert [p.root for p in drive.partitions] == [False, False, False, True]
        assert drive.encrypted
        assert drive.size == 64023222272

    def test_unexpanded_space_is_reported_with_portlins_own_arithmetic(self, intro, monkeypatch):
        _stick(intro, monkeypatch)
        assert intro.read_drive().unclaimed >= 55 * GIB

    def test_a_plain_root_is_not_called_encrypted(self, intro, monkeypatch):
        _stick(intro, monkeypatch, source="/dev/sdb4")
        assert not intro.read_drive().encrypted

    def test_a_layout_portlin_did_not_write_is_not_measured(self, intro, monkeypatch):
        three = "\n".join(LSBLK_STICK.splitlines()[:1] + LSBLK_STICK.splitlines()[2:])
        _stick(intro, monkeypatch, lsblk=three)
        assert intro.read_drive() is None

    def test_no_backing_disk_is_no_drive(self, intro, monkeypatch):
        # A container's overlay root, or a root that is a whole disk.
        _stick(intro, monkeypatch)
        monkeypatch.setattr(intro, "backing_disk", lambda part: part)
        assert intro.read_drive() is None

    def test_probing_that_raises_is_no_drive_rather_than_no_film(self, intro, monkeypatch):
        _stick(intro, monkeypatch)
        monkeypatch.setattr(intro, "sysfs_sectors", lambda *a: 1 / 0)
        assert intro.read_drive() is None

    @pytest.mark.parametrize("row, expected", [
        ({"FSTYPE": "crypto_LUKS", "FSVER": "2"}, "LUKS2"),
        ({"FSTYPE": "crypto_LUKS", "FSVER": ""}, "LUKS"),
        ({"FSTYPE": "vfat", "FSVER": "FAT32"}, "vfat"),
        ({"FSTYPE": "", "PARTTYPE": "21686148-6449-6E6F-744E-656564454649"}, "EF02"),
        # No udev data: lsblk knows nothing, and the row says so plainly.
        ({"FSTYPE": "", "PARTTYPE": ""}, "-"),
    ])
    def test_partition_kind(self, intro, row, expected):
        assert intro.partition_kind(row) == expected


class TestMeasuring:
    def _drive(self, intro, root, size=64023222272, encrypted=True):
        parts = (
            intro.Partition("sdb1", "EF02", 1 * MIB, "", False),
            intro.Partition("sdb2", "vfat", 512 * MIB, "/boot/efi", False),
            intro.Partition("sdb3", "ext4", 1 * GIB, "/boot", False),
            intro.Partition("sdb4", "LUKS2", root, "/", True),
        )
        return intro.Drive("sdb", size, parts, 0, encrypted)

    def test_bars_keep_the_partitions_rank_order(self, intro):
        widths = intro.measured_widths(self._drive(intro, 7 * GIB))
        assert widths == sorted(widths)
        assert len(set(widths)) == len(widths)

    def test_bars_stay_inside_the_enclosure_and_stay_visible(self, intro):
        widths = intro.measured_widths(self._drive(intro, 7 * GIB))
        assert min(widths) >= intro.MARK_MIN_BAR
        assert max(widths) <= intro.MARK_TRACK

    def test_an_expanded_root_reaches_the_end_of_the_drive(self, intro):
        expanded = intro.measured_widths(self._drive(intro, 62 * GIB))[-1]
        unexpanded = intro.measured_widths(self._drive(intro, 7 * GIB))[-1]
        assert expanded == pytest.approx(intro.MARK_TRACK, rel=0.01)
        assert unexpanded < expanded

    def test_rows_are_aligned_in_columns(self, intro):
        rows = intro.partition_rows(self._drive(intro, 7 * GIB))
        assert rows[0].startswith("sdb1  EF02 ")
        assert rows[3].endswith("  /")
        # Size column right-aligned, as lsblk prints it.
        size_ends = {row.index("M") if "M" in row else row.index("G") for row in rows}
        assert len(size_ends) == 1

    def test_rows_quote_sizes_the_way_the_other_tools_do(self, intro):
        rows = intro.partition_rows(self._drive(intro, 7 * GIB))
        assert "512M" in rows[1] and "1.0G" in rows[2] and "7.0G" in rows[3]


class TestFacts:
    def test_reads_the_machine_as_the_banner_keys_it(self, intro, monkeypatch):
        host = types.SimpleNamespace(model="LENOVO ThinkPad T14", cpu="AMD Ryzen 5 PRO 4650U",
                                     threads=12, memory_bytes=16 * GIB, firmware="UEFI 64-bit")
        monkeypatch.setattr(intro, "read_host", lambda: host)
        assert intro.read_facts() == [
            ("machine", "LENOVO ThinkPad T14"),
            ("cpu", "AMD Ryzen 5 PRO 4650U (12 threads)"),
            ("memory", "16.0G"),
            ("firmware", "UEFI 64-bit"),
        ]

    def test_what_the_machine_will_not_say_is_left_out(self, intro, monkeypatch):
        host = types.SimpleNamespace(model="", cpu="", threads=0, memory_bytes=0, firmware="BIOS")
        monkeypatch.setattr(intro, "read_host", lambda: host)
        assert intro.read_facts() == [("firmware", "BIOS")]

    def test_a_machine_that_cannot_be_read_has_no_facts(self, intro, monkeypatch):
        def broken():
            raise OSError("no /sys")
        monkeypatch.setattr(intro, "read_host", broken)
        assert intro.read_facts() == []
