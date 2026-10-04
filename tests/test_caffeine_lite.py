"""Caffeine in the lite session: the logind lock, swayidle, and the cup in waybar.

The holder's wait loop uses sigtimedwait, which only Linux has, so it is
exercised in a real labwc session rather than here. What these cover is every
decision it makes once woken: what a click or a menu pick does to the lock and
to the screen lock, what the panel is told, and that the two applets keep
agreeing about the settings file they share.
"""

from __future__ import annotations

import json
import re
import signal
from pathlib import Path

import pytest

from conftest import load_tool
from portlin import package

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
THEME = RUNTIME / "theme"


@pytest.fixture
def lite(monkeypatch, tmp_path):
    module = load_tool("portlin-caffeine-lite")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return module


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False

    def kill(self):
        self.running = False

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def holder(lite, monkeypatch, tmp_path):
    started = []

    def popen(argv, **kwargs):
        process = FakeProcess(argv, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(lite.subprocess, "Popen", popen)
    monkeypatch.setattr(lite.subprocess, "run", lambda *a, **k: None)
    made = lite.Holder(lite.Settings(), tmp_path)
    made.started = started
    return made


def running(holder, name):
    return [p for p in holder.started if p.running and name in " ".join(p.argv)]


class TestTheTwoRefusals:
    def test_turning_it_on_takes_the_lock_and_stops_the_screen_lock(self, holder, lite):
        holder.start_idle()
        holder.set_active(True)
        assert running(holder, "systemd-inhibit")
        assert not running(holder, lite.IDLE_TOOL)

    def test_turning_it_off_gives_both_back(self, holder, lite):
        holder.set_active(True)
        holder.set_active(False)
        assert not running(holder, "systemd-inhibit")
        assert running(holder, lite.IDLE_TOOL)

    def test_the_lock_is_the_same_one_the_xfce_applet_takes(self, holder, lite):
        holder.set_active(True)
        lock = running(holder, "systemd-inhibit")[0]
        assert "--what=idle:sleep:handle-lid-switch" in lock.argv
        assert "--mode=block" in lock.argv

    def test_the_lock_dies_with_the_holder_and_unblocks_its_signals(self, holder, lite):
        # The holder blocks SIGTERM to wait on it, and a mask survives exec:
        # without the reset, the lock could not be released by terminating it.
        holder.set_active(True)
        assert running(holder, "systemd-inhibit")[0].kwargs["preexec_fn"] is lite._lock_child_setup

    def test_the_screen_lock_outlives_the_holder(self, holder, lite):
        # A crashed holder must leave the session locking, not unlocked forever.
        holder.start_idle()
        idle = running(holder, lite.IDLE_TOOL)[0]
        assert idle.kwargs["start_new_session"] is True
        assert idle.kwargs["preexec_fn"] is lite._child_setup

    def test_it_never_runs_two_screen_locks(self, holder, lite):
        holder.start_idle()
        holder.start_idle()
        assert len(running(holder, lite.IDLE_TOOL)) == 1


class TestClicksAndRequests:
    def test_a_click_uses_the_preferred_duration(self, holder, lite):
        holder.settings = lite.Settings(default_duration=1800)
        holder.toggle()
        assert holder.settings.active
        assert 1790 < holder.settings.deadline - lite.time.time() <= 1800

    def test_a_second_click_turns_it_off(self, holder):
        holder.toggle()
        holder.toggle()
        assert not holder.settings.active

    def test_a_timed_session_ends_by_itself(self, holder, lite, monkeypatch):
        said = []
        monkeypatch.setattr(lite, "notify", lambda *a: said.append(a))
        holder.set_active(True, 60)
        holder.expire()
        assert not holder.settings.active and said

    def test_the_wait_never_outlasts_the_deadline(self, holder, lite):
        holder.set_active(True, 30)
        assert holder.timeout(lite.time.time()) <= 30
        holder.set_active(True, None)
        assert holder.timeout(lite.time.time()) == lite.TICK

    def test_a_preference_changes_nothing_else(self, holder, lite):
        holder.set_active(True)
        holder.handle({"notify_on_expiry": False})
        assert holder.settings.active and not holder.settings.notify_on_expiry

    def test_preferences_land_in_the_file_both_applets_read(self, holder, lite):
        holder.handle({"restore_at_login": False, "default_duration": 3600})
        saved = lite.load_settings()
        assert saved.restore_at_login is False and saved.default_duration == 3600

    def test_a_request_is_read_once(self, lite, tmp_path):
        (tmp_path / "request.json").write_text(json.dumps({"active": False}))
        assert lite.take_request(tmp_path) == {"active": False}
        assert lite.take_request(tmp_path) is None

    def test_a_garbled_request_is_ignored(self, lite, tmp_path):
        (tmp_path / "request.json").write_text("[1, 2")
        assert lite.take_request(tmp_path) is None


class TestTheMenu:
    def test_off_it_offers_every_span_and_no_turn_off(self, lite):
        labels = [label for label, _ in lite.menu_entries(lite.Settings())]
        assert "Turn off" not in labels
        assert "Activate for 15 minutes" in labels
        assert "Activate until turned off" in labels

    def test_on_it_offers_turn_off_first(self, lite):
        entries = lite.menu_entries(lite.Settings(active=True))
        assert entries[0] == ("Turn off", {"active": False})

    def test_each_toggle_offers_the_opposite_of_now(self, lite):
        entries = dict(lite.menu_entries(lite.Settings(restore_at_login=True, notify_on_expiry=False)))
        assert entries["Restore at login: on"] == {"restore_at_login": False}
        assert entries["Tell me when time runs out: off"] == {"notify_on_expiry": True}

    def test_it_names_what_a_click_does(self, lite):
        labels = [label for label, _ in lite.menu_entries(lite.Settings(default_duration=3600))]
        assert "Clicking the cup: 1 hour" in labels

    def test_every_span_can_be_made_the_click_default(self, lite):
        offered = [request["default_duration"] for _, request in lite.duration_entries()]
        assert offered == [seconds for seconds, _ in lite.DURATIONS]


class TestThePanel:
    def test_the_icon_follows_the_holder_not_the_saved_restore_state(self, lite, tmp_path, capsys):
        # load_settings answers what a new session starts as; the panel has
        # to show what this one is doing.
        (tmp_path / "state.json").write_text(json.dumps({"active": True, "deadline": None}))
        lite.command_icon(tmp_path)
        path, tooltip = capsys.readouterr().out.splitlines()
        assert path == lite.ICONS[True]
        assert tooltip == "Caffeine is active"

    def test_with_no_holder_it_draws_off(self, lite, tmp_path, capsys):
        lite.command_icon(tmp_path)
        assert capsys.readouterr().out.splitlines()[0] == lite.ICONS[False]

    def test_waybar_runs_its_commands_and_listens_on_its_signal(self, lite):
        config = json.loads(re.sub(r"(?m)^\s*//.*$", "", (THEME / "waybar-config.jsonc").read_text()))
        module = config["image#caffeine"]
        assert "image#caffeine" in config["modules-right"]
        assert module["signal"] == lite.WAYBAR_SIGNAL
        for key, command in (("exec", "icon"), ("on-click", "toggle"), ("on-click-right", "menu")):
            assert module[key] == f"portlin-caffeine-lite {command}"
            assert command in lite.COMMANDS

    def test_the_session_starts_the_holder(self):
        assert "portlin-caffeine-lite run" in (THEME / "labwc-autostart").read_text()

    def test_the_holder_waits_on_exactly_what_the_commands_send(self, lite):
        assert lite.TOGGLE in lite.WAITED and lite.REQUEST in lite.WAITED
        assert signal.SIGTERM in lite.WAITED


class TestPackaging:
    def test_it_ships_executable_with_its_screen_lock(self):
        executables = package.executable_paths("portlin-desktop")
        assert "usr/bin/portlin-caffeine-lite" in executables
        assert package.LITE_IDLE_TOOL in executables

    def test_the_shared_module_ships_where_both_applets_look(self):
        files = package.text_files("portlin-desktop")
        assert "usr/lib/portlin/caffeine.py" in files
        for applet in ("portlin-caffeine", "portlin-caffeine-lite"):
            assert 'sys.path.insert(0, "/usr/lib/portlin")' in (RUNTIME / applet).read_text()

    def test_it_paths_the_screen_lock_it_ships(self, lite):
        assert lite.IDLE_TOOL == f"/{package.LITE_IDLE_TOOL}"

    def test_it_imports_no_gtk(self):
        # It runs for the whole session; the lite session exists to save memory.
        source = (RUNTIME / "portlin-caffeine-lite").read_text() + (RUNTIME / "caffeine.py").read_text()
        assert "import gi" not in source
