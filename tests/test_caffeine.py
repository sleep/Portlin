"""Checks on the caffeine applet: what it blocks, what it restores, what it remembers.

The applet itself cannot run here -- it needs GTK, a panel to sit in, and a
logind to take a lock from. What can run is everything the applet decides
before it talks to any of them: which inhibitor it asks for, what it puts the
screen back to afterwards, how long it says it has left, and what state a
session restores into. Those are loaded out of the shipped file and called for
real, with gi stubbed out, so a mistake in any of them fails here rather than
on a machine that quietly went to sleep mid-download.
"""

from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from portlin import package

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
CAFFEINE = RUNTIME / "portlin-caffeine"
MENU_ENTRY = RUNTIME / "portlin-caffeine.desktop"
AUTOSTART_ENTRY = RUNTIME / "portlin-caffeine-autostart.desktop"


def _stub_gi() -> None:
    """Put a fake gi in sys.modules so the applet imports on a machine with no GTK.

    Everything worth testing here is a decision the applet makes before it
    draws anything, but it is all in one file with `from gi.repository import
    Gtk` at the top -- so importing that file at all means answering for gi.
    Stubbing it is what keeps the tests exercising the real shipped code
    rather than a second copy of these functions kept next to them.
    """
    if "gi" in sys.modules and isinstance(sys.modules["gi"], types.ModuleType):
        if getattr(sys.modules["gi"], "_portlin_stub", False):
            return
    gi = types.ModuleType("gi")
    gi._portlin_stub = True
    gi.require_version = lambda *args, **kwargs: None
    repository = types.ModuleType("gi.repository")
    for name in ("Gtk", "Gio", "GLib", "GdkPixbuf", "Gdk"):
        module = MagicMock(name=name)
        setattr(repository, name, module)
        sys.modules[f"gi.repository.{name}"] = module
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository


@pytest.fixture(scope="module")
def caffeine():
    """The applet, imported as a module, without running its main()."""
    _stub_gi()
    loader = importlib.machinery.SourceFileLoader("portlin_caffeine", str(CAFFEINE))
    spec = importlib.util.spec_from_file_location(loader.name, CAFFEINE, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: dataclasses resolves the annotations on
    # Settings by looking its defining module up in sys.modules, and a module
    # that is only a local variable is not there to be found.
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


class TestTheAppletIsValidPython:
    def test_it_compiles(self):
        compile(CAFFEINE.read_text(), str(CAFFEINE), "exec")

    def test_it_has_a_python3_shebang(self):
        assert CAFFEINE.read_text().startswith("#!/usr/bin/env python3")

    def test_it_imports_without_error(self, caffeine):
        assert caffeine is not None

    def test_it_needs_no_root(self):
        # It autostarts into the user's panel. Anything demanding root here
        # would put a password prompt in front of a tray icon at every login.
        assert "geteuid" not in CAFFEINE.read_text()


class TestTheInhibitorItTakes:
    def test_it_blocks_idle_sleep_and_the_lid_switch(self, caffeine):
        # All three, because each covers a route to sleep the others do not:
        # idle is the timer, sleep is an explicit suspend, and the lid switch
        # is logind acting on hardware without consulting either.
        what = caffeine.inhibit_argv("because")[1]
        assert what.startswith("--what=")
        assert set(what.partition("=")[2].split(":")) == {
            "idle",
            "sleep",
            "handle-lid-switch",
        }

    def test_it_holds_the_lock_rather_than_delaying_it(self, caffeine):
        # --mode=delay only postpones a suspend by a few seconds and then lets
        # it happen, which would look like caffeine working right up until the
        # machine slept anyway.
        assert "--mode=block" in caffeine.inhibit_argv("because")

    def test_it_says_who_is_holding_the_lock_and_why(self, caffeine):
        argv = caffeine.inhibit_argv("Keeping this machine awake")
        assert any(arg.startswith("--who=Portlin") for arg in argv)
        assert "--why=Keeping this machine awake" in argv

    def test_the_lock_lives_as_long_as_the_child_it_wraps(self, caffeine):
        # systemd-inhibit holds the lock for the lifetime of the command it
        # runs, so the command has to be one that never exits on its own.
        assert caffeine.inhibit_argv("because")[-2:] == ["sleep", "infinity"]

    def test_the_lock_dies_with_the_applet(self, caffeine):
        # An ordinary child outlives the parent that spawned it. A killed
        # applet would leave systemd-inhibit holding the machine awake with no
        # icon in the panel to explain it and nothing left to click.
        assert "PR_SET_PDEATHSIG" in CAFFEINE.read_text()
        assert "preexec_fn=die_with_parent" in CAFFEINE.read_text()

    def test_failing_to_arrange_that_still_leaves_the_lock_takeable(self, caffeine):
        # preexec_fn runs in the forked child, and an exception there is raised
        # back out of Popen: an unguarded prctl that failed would cost the lock
        # entirely, which is worse than the orphan it is there to prevent.
        # Called here on a machine with no prctl at all, which is the check.
        assert caffeine.die_with_parent() is None

    def test_it_asks_logind_rather_than_reimplementing_the_lock(self):
        # A hand-rolled D-Bus fd is a second implementation of something
        # systemd-inhibit already does, and an invisible one: the lock this
        # takes shows up in `systemd-inhibit --list` where anyone can see it.
        assert "systemd-inhibit" in CAFFEINE.read_text()


class TestTheScreenSettingsItRestores:
    XSET_Q = """Keyboard Control:
  auto repeat:  on    key click percent:  0
Screen Saver:
  prefer blanking:  yes    allow exposures:  yes
  timeout:  600    cycle:  600
Colors:
  default colormap:  0x20    BlackPixel:  0
DPMS (Display Power Management Signaling):
  Standby: 900    Suspend: 1200    Off: 1800
  DPMS is Enabled
  Monitor is On
"""

    def test_it_reads_back_what_it_is_about_to_change(self, caffeine):
        saved = caffeine.parse_xset(self.XSET_Q)
        assert saved["screensaver"] == (600, 600)
        assert saved["dpms"] == (900, 1200, 1800)
        assert saved["dpms_enabled"] is True

    def test_it_notices_dpms_that_was_already_disabled(self, caffeine):
        saved = caffeine.parse_xset(self.XSET_Q.replace("is Enabled", "is Disabled"))
        assert saved["dpms_enabled"] is False

    def test_it_puts_back_the_exact_timeouts_it_found(self, caffeine):
        # Not `xset s default`: the numbers a person chose in the power manager
        # are not the X server's compiled-in defaults, and turning caffeine off
        # is not a reason to quietly replace one with the other.
        argv = caffeine.restore_argv(caffeine.parse_xset(self.XSET_Q))
        assert argv == ["xset", "s", "600", "600", "dpms", "900", "1200", "1800", "+dpms"]

    def test_the_dpms_switch_comes_after_the_timeouts_it_qualifies(self, caffeine):
        # `xset dpms <standby> <suspend> <off>` turns DPMS on as a side effect
        # of setting the numbers, so a -dpms written before them is undone by
        # the very next argument on the same command line.
        saved = caffeine.parse_xset(self.XSET_Q.replace("is Enabled", "is Disabled"))
        assert caffeine.restore_argv(saved)[-1] == "-dpms"

    def test_it_leaves_dpms_off_if_it_found_it_off(self, caffeine):
        saved = caffeine.parse_xset(self.XSET_Q.replace("is Enabled", "is Disabled"))
        argv = caffeine.restore_argv(saved)
        assert "-dpms" in argv and "+dpms" not in argv

    def test_it_restores_nothing_it_never_managed_to_read(self, caffeine):
        # xset can be missing, or there can be no display. Restoring from an
        # empty reading would invent timeouts nobody set.
        assert caffeine.restore_argv(caffeine.parse_xset("")) == []

    def test_turning_it_on_stops_both_blanking_and_dpms(self, caffeine):
        assert caffeine.SUSPEND_BLANKING_ARGV == ["xset", "s", "off", "-dpms"]


class TestHowLongItSaysItHasLeft:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "less than a minute"),
            (59, "less than a minute"),
            (60, "1 minute"),
            (119, "1 minute"),
            (42 * 60, "42 minutes"),
            (3600, "1 hour"),
            (90 * 60, "1 hour 30 minutes"),
            (5 * 3600, "5 hours"),
            (-10, "less than a minute"),
        ],
    )
    def test_it_reads_as_a_person_would_say_it(self, caffeine, seconds, expected):
        assert caffeine.format_remaining(seconds) == expected

    def test_the_menu_header_says_it_is_off(self, caffeine):
        assert caffeine.status_text(False, None, now=1000.0) == "Caffeine is off"

    def test_the_menu_header_says_it_is_active(self, caffeine):
        assert caffeine.status_text(True, None, now=1000.0) == "Caffeine is active"

    def test_the_menu_header_counts_a_timed_session_down(self, caffeine):
        text = caffeine.status_text(True, 1000.0 + 42 * 60, now=1000.0)
        assert text == "Caffeine is active for another 42 minutes"


class TestWhatASessionRestoresInto:
    def test_it_remembers_that_it_was_left_on(self, caffeine):
        settings = caffeine.Settings(active=True, deadline=None)
        restored = caffeine.parse_settings(caffeine.render_settings(settings), now=1000.0)
        assert restored.active is True
        assert restored.deadline is None

    def test_it_remembers_how_much_of_a_timed_session_was_left(self, caffeine):
        settings = caffeine.Settings(active=True, deadline=2000.0)
        restored = caffeine.parse_settings(caffeine.render_settings(settings), now=1000.0)
        assert restored.active is True
        assert restored.deadline == 2000.0

    def test_a_deadline_that_has_already_passed_restores_as_off(self, caffeine):
        # The stick was shut down caffeinated with ten minutes left, and comes
        # back a week later. Honouring the stored flag would keep a machine
        # awake on the strength of a countdown that ended before it booted.
        settings = caffeine.Settings(active=True, deadline=500.0)
        restored = caffeine.parse_settings(caffeine.render_settings(settings), now=1000.0)
        assert restored.active is False
        assert restored.deadline is None

    def test_it_starts_off_when_asked_not_to_restore(self, caffeine):
        settings = caffeine.Settings(active=True, deadline=None, restore_at_login=False)
        restored = caffeine.parse_settings(caffeine.render_settings(settings), now=1000.0)
        assert restored.active is False
        assert restored.restore_at_login is False

    def test_it_starts_off_with_no_settings_at_all(self, caffeine):
        # First login on a fresh account. A stick that plugs into someone
        # else's hardware should never begin by disabling their power settings.
        fresh = caffeine.parse_settings("", now=1000.0)
        assert fresh.active is False
        assert fresh.restore_at_login is True
        assert fresh.notify_on_expiry is True
        assert fresh.default_duration is None

    def test_it_starts_off_when_the_file_is_unreadable(self, caffeine):
        assert caffeine.parse_settings("}{ not an ini file", now=1000.0).active is False

    def test_it_remembers_the_preferences(self, caffeine):
        settings = caffeine.Settings(
            default_duration=3600, notify_on_expiry=False, restore_at_login=False
        )
        restored = caffeine.parse_settings(caffeine.render_settings(settings), now=1000.0)
        assert restored.default_duration == 3600
        assert restored.notify_on_expiry is False
        assert restored.restore_at_login is False

    def test_it_writes_where_the_xdg_spec_says_to(self, caffeine, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/somewhere")
        assert caffeine.settings_path() == Path("/tmp/somewhere/portlin/caffeine.ini")

    def test_it_falls_back_to_dot_config(self, caffeine, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", "/home/someone")
        assert caffeine.settings_path() == Path(
            "/home/someone/.config/portlin/caffeine.ini"
        )


class TestTheDurationsOffered:
    def test_it_offers_the_spans_a_person_would_pick(self, caffeine):
        assert [seconds for seconds, _ in caffeine.DURATIONS] == [
            15 * 60,
            30 * 60,
            3600,
            2 * 3600,
            5 * 3600,
            None,
        ]

    def test_a_stored_duration_finds_its_place_in_the_list(self, caffeine):
        assert caffeine.duration_index(60 * 60) == 2
        assert caffeine.duration_index(None) == len(caffeine.DURATIONS) - 1

    def test_a_duration_nobody_offers_falls_back_rather_than_raising(self, caffeine):
        # The file is editable by hand, and a number that matches no entry
        # would otherwise be an exception thrown while opening Preferences --
        # which is the one dialog that could put it back.
        assert caffeine.duration_index(999) == len(caffeine.DURATIONS) - 1

    def test_the_open_ended_choice_is_last_and_says_so(self, caffeine):
        seconds, label = caffeine.DURATIONS[-1]
        assert seconds is None
        assert label == "Until turned off"


class TestTheSessionServicesItAsks:
    def test_it_inhibits_the_screensaver_and_the_power_manager(self, caffeine):
        # The logind lock covers suspend, not the session's own blanking: on
        # Xfce those are xfce4-screensaver and xfce4-power-manager, and each
        # has to be told separately.
        names = {service.name for service in caffeine.SESSION_INHIBITORS}
        assert names == {
            "org.freedesktop.ScreenSaver",
            "org.freedesktop.PowerManagement",
        }

    def test_each_one_names_the_path_and_interface_to_call(self, caffeine):
        for service in caffeine.SESSION_INHIBITORS:
            assert service.path.startswith("/")
            assert service.interface.startswith("org.freedesktop.")

    def test_it_needs_no_notification_library_of_its_own(self):
        # The expiry notice goes out over the session bus the applet is already
        # speaking, by calling org.freedesktop.Notifications directly. Importing
        # libnotify would add a gir dependency to portlin-desktop for one string.
        source = CAFFEINE.read_text()
        assert "org.freedesktop.Notifications" in source
        assert 'require_version("Notify"' not in source
        assert "import Notify" not in source


class TestThePanelIcons:
    """The two SVGs, taken as files something else has to rasterise.

    The applet falls back to a stock icon when a pixbuf will not load, which is
    right on a stick people remove packages from and wrong as the only thing
    between a malformed drawing and a shipped image: the panel still has an
    icon in it, so nothing anywhere says the cup never appeared. These are the
    assertions the fallback would otherwise swallow.
    """

    def test_both_states_point_at_an_icon_the_package_ships(self, caffeine):
        assert set(caffeine.ICONS) == {True, False}
        for path in caffeine.ICONS.values():
            assert path.lstrip("/") in package.CAFFEINE_ICONS

    def test_the_icons_are_xml_a_parser_will_take(self):
        # librsvg reads these with an XML parser: a file that fails here is a
        # file the panel quietly swaps for the fallback icon, in the one state
        # it fails in. Skipped rather than assumed where there is no parser to
        # ask, which is why the narrower check below does not lean on one.
        pytest.importorskip(
            "xml.parsers.expat",
            exc_type=ImportError,
            reason="no XML parser in this python to check with",
        )
        import xml.etree.ElementTree as ET

        for name in package.CAFFEINE_ICONS.values():
            ET.parse(RUNTIME / name)

    def test_no_comment_in_them_contains_a_double_hyphen(self):
        # XML forbids it inside a comment, and these icons carry long prose
        # ones, so it is an easy thing to write and an invisible thing to ship:
        # the drawing simply never loads and a stock icon stands in for it.
        for name in package.CAFFEINE_ICONS.values():
            for comment in re.findall(
                r"<!--(.*?)-->", (RUNTIME / name).read_text(), re.DOTALL
            ):
                assert "--" not in comment, f"{name}: {comment.strip()[:60]}"

    def test_the_off_cup_is_empty_and_the_on_cup_is_not(self):
        # The states are told apart by mass rather than shade, so the fill is
        # the difference: the root fill="none" and nothing else in the off cup.
        off = (RUNTIME / "caffeine-off.svg").read_text()
        on = (RUNTIME / "caffeine-on.svg").read_text()
        assert 'fill="none"' in off and off.count("fill=") == 1
        assert on.count("fill=") > 1


class TestTheFallbackSaysSomething:
    """What the panel does when a drawing will not load, and how loudly.

    The fallback is right and it is also the reason a broken icon can ship: a
    cup is always in the panel, so the substitution has to announce itself
    somewhere or nothing does. Once per path, because a repaint happens on
    every toggle and every panel size change.
    """

    @pytest.fixture
    def broken_pixbuf(self):
        pixbuf = sys.modules["gi.repository.GdkPixbuf"]
        pixbuf.Pixbuf.new_from_file_at_scale.side_effect = OSError("no such file")
        yield
        pixbuf.Pixbuf.new_from_file_at_scale.side_effect = None

    def test_it_names_the_file_and_what_stood_in_for_it(
        self, caffeine, broken_pixbuf, capsys
    ):
        caffeine.Caffeine(caffeine.Settings(), MagicMock())
        warning = capsys.readouterr().err
        assert caffeine.ICONS[False] in warning
        assert caffeine.FALLBACK_ICONS[False] in warning

    def test_it_says_it_once_however_often_the_panel_repaints(
        self, caffeine, broken_pixbuf, capsys
    ):
        applet = caffeine.Caffeine(caffeine.Settings(), MagicMock())
        applet._refresh()
        applet._on_size_changed(applet.icon, 24)
        assert len(_warnings(capsys.readouterr().err)) == 1

    def test_a_second_broken_drawing_is_still_worth_a_line(
        self, caffeine, broken_pixbuf, capsys
    ):
        # Per path rather than per process: the two states are two files, and
        # the state nobody happened to be in is the one that ships broken.
        applet = caffeine.Caffeine(caffeine.Settings(), MagicMock())
        applet.settings = caffeine.Settings(active=True)
        applet._refresh()
        assert len(_warnings(capsys.readouterr().err)) == 2


def _warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "could not draw" in line]


def _entry(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text())
    return parser["Desktop Entry"]


class TestTheDesktopEntries:
    def test_the_menu_entry_opens_the_program_the_package_ships(self):
        exec_line = _entry(MENU_ENTRY)["Exec"]
        assert exec_line == "portlin-caffeine"
        assert f"usr/bin/{exec_line}" in package.text_files("portlin-desktop")

    def test_the_menu_entry_draws_an_icon_the_package_ships(self):
        icon = _entry(MENU_ENTRY)["Icon"].lstrip("/")
        assert icon in package.binary_files("portlin-desktop")

    def test_the_autostart_entry_starts_the_same_program(self):
        # Two files that have to agree: an autostart entry pointing somewhere
        # the menu entry does not is a tray icon nobody can explain.
        assert _entry(AUTOSTART_ENTRY)["Exec"] == _entry(MENU_ENTRY)["Exec"]
        assert _entry(AUTOSTART_ENTRY)["Icon"] == _entry(MENU_ENTRY)["Icon"]

    def test_the_autostart_entry_is_an_application(self):
        assert _entry(AUTOSTART_ENTRY)["Type"] == "Application"

    def test_neither_entry_opens_a_terminal(self):
        for path in (MENU_ENTRY, AUTOSTART_ENTRY):
            assert _entry(path)["Terminal"] == "false"
