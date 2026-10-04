"""Checks on the Migrate window, and the menu entry that opens it.

As with the Software window, nothing here draws anything. What is checked is
every decision the program makes before it draws: the command lines it runs
through pkexec, how an inventory becomes rows of tick boxes, and that the
plan it writes carries exactly the keys portlin-migrate apply reads.
"""

from __future__ import annotations

import configparser
import importlib.machinery
import importlib.util
import json
import re
import stat
import sys
from pathlib import Path

import pytest

from conftest import load_tool
from test_software import _stub_gi

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
WINDOW = RUNTIME / "portlin-migration"
ENTRY = RUNTIME / "portlin-migration.desktop"
POLICY = RUNTIME / "org.portlin.migrate.policy"


@pytest.fixture(scope="module")
def window():
    _stub_gi()
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    loader = importlib.machinery.SourceFileLoader("portlin_migration", str(WINDOW))
    spec = importlib.util.spec_from_file_location(loader.name, WINDOW, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


class TestCommandLines:
    def test_everything_goes_through_the_one_tool_as_root(self, window):
        assert window.candidates_argv(passwordless_sudo=False) == ["pkexec", window.TOOL, "candidates", "--json"]
        assert window.inventory_argv("/dev/sdb4", passwordless_sudo=False) == [
            "pkexec", window.TOOL, "inventory", "/dev/sdb4", "--json", "--progress",
        ]
        assert window.apply_argv("/home/x/.cache/portlin/migrate-plan.json", passwordless_sudo=True) == [
            "sudo", "-n", window.TOOL, "apply", "--plan", "/home/x/.cache/portlin/migrate-plan.json",
        ]
        assert window.export_argv("/media/x/D", passwordless_sudo=False) == ["pkexec", window.TOOL, "export", "/media/x/D"]

    def test_the_polkit_action_grants_exactly_that_tool(self, window):
        granted = re.search(r'exec\.path">([^<]+)<', POLICY.read_text()).group(1)
        assert granted == window.TOOL

    def test_exit_codes_are_explained_including_no_space(self, window):
        assert window.explain_exit(0) == ""
        assert "cancelled" in window.explain_exit(126).lower()
        assert "space" in window.explain_exit(6).lower()


class TestRows:
    def test_an_inventory_becomes_headed_groups_of_tickable_rows(self, window, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        inventory = migrate.build_inventory(tmp_path)
        groups = window.tree_rows(inventory)
        headings = [heading for _, heading, _ in groups]
        assert headings[:2] == ["Account", "Machine identity"]
        assert "Network" not in headings
        files = next(rows for category, _, rows in groups if category == "home.files")
        assert ("home.files.Documents", "Documents", "1 KB", True) in files
        settings = next(rows for category, _, rows in groups if category == "home.settings")
        assert ("home.settings..cache", ".cache", "300 B", False) in settings

    def test_a_note_is_part_of_the_label(self, window, migrate):
        inventory = migrate.Inventory("0.1.2", "office", "home/x", (
            migrate.Item("software.nvidia", "software", "NVIDIA driver", note="describes the old machine", default=False),
        ))
        rows = window.tree_rows(inventory)[0][2]
        assert rows[0][1] == "NVIDIA driver (describes the old machine)"
        assert rows[0][2] == ""


class TestPlan:
    def test_the_plan_carries_what_apply_reads_and_nothing_secret(self, window):
        candidate = {"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk Ultra", "size": "57.3G",
                     "encrypted": True, "version": ""}
        inventory_data = {"version": "0.1.2", "hostname": "office", "home": "home/olduser",
                          "items": [], "accounts": [], "identity": {}}
        plan = window.plan_document(candidate, ["home.files.Documents"], inventory_data)
        assert set(plan) >= {"source", "kind", "encrypted", "ids", "firstboot", "label", "inventory"}
        assert plan["source"] == "/dev/sdb4" and plan["kind"] == "stick" and plan["encrypted"] is True
        assert plan["firstboot"] is False
        assert plan["label"] == "SanDisk Ultra 57.3G   portlin, encrypted"
        assert "passphrase" not in json.dumps(plan)

    def test_the_plan_names_the_drive_by_uuid_too_for_resuming(self, window):
        candidate = {"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk", "size": "57.3G",
                     "encrypted": False, "version": "", "uuid": "5d1e7c2a-93b4-4f0e-8a61-0c7d2b9e4f13"}
        plan = window.plan_document(candidate, ["home.files.Documents"], {})
        assert plan["uuid"] == "5d1e7c2a-93b4-4f0e-8a61-0c7d2b9e4f13"
        # A backup chosen from the file dialog has no uuid, and needs none.
        archive = {"kind": "archive", "path": "/media/x/a.portlin-backup.tar.zst", "model": "x",
                   "size": "", "encrypted": False, "version": ""}
        assert window.plan_document(archive, [], {})["uuid"] == ""

    def test_an_unfinished_plan_is_found_and_junk_is_not(self, window, tmp_path):
        path = tmp_path / "plan.json"
        assert window.read_plan_file(path) is None
        path.write_text("{not json")
        assert window.read_plan_file(path) is None
        path.write_text(json.dumps({"source": "/dev/sdb4", "ids": ["home.files.Documents"]}))
        assert window.read_plan_file(path)["source"] == "/dev/sdb4"

    def test_the_plan_file_is_written_0600_from_the_start(self, window, tmp_path):
        path = tmp_path / "plan.json"
        plan = {"source": "/dev/sdb4", "kind": "stick"}
        window.write_plan_file(path, plan)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == plan


class TestLive:
    """--live: the window as the whole of an upgrade from first-boot setup."""

    PLAN = {
        "ids": ["account.ann", "home.files.Documents"],
        "inventory": {"accounts": [
            {"name": "bob", "gecos": "Bob", "sudo_nopasswd": False, "autologin": False},
            {"name": "ann", "gecos": "Ann Example,,,", "sudo_nopasswd": True, "autologin": True},
        ]},
    }

    def test_setup_is_told_the_account_that_came_over_and_its_answers(self, window):
        assert window.live_result(self.PLAN) == {
            "account": "ann", "full_name": "Ann Example", "sudo_nopasswd": True, "autologin": True,
        }

    def test_no_account_ticked_tells_setup_nothing(self, window):
        assert window.live_result({**self.PLAN, "ids": ["home.files.Documents"]}) is None
        assert window.has_account(["account.ann"]) and not window.has_account(["home.files.Documents"])

    def test_the_inventory_leaves_software_for_later_during_setup(self, window):
        assert window.inventory_argv("/dev/sdb4", passwordless_sudo=False, firstboot=True)[-1] == "--firstboot"
        assert "--firstboot" not in window.inventory_argv("/dev/sdb4", passwordless_sudo=False)

    def test_as_root_the_tool_is_run_directly(self, window, monkeypatch):
        monkeypatch.setattr(window.os, "geteuid", lambda: 0)
        assert window.apply_argv("/root/plan.json", passwordless_sudo=False) == [
            window.TOOL, "apply", "--plan", "/root/plan.json",
        ]

    def test_a_pause_is_explained(self, window, migrate):
        assert "unplug" in window.explain_exit(migrate.EXIT_PAUSED)


class TestPauseFeedback:
    def test_the_paused_result_text_is_kept_for_the_page(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.last_failure = ""
        fake.result_text = ""
        fake._append = lambda line: None
        fake._keep_live = lambda: None
        window.MigrationWindow._on_event(fake, "result", "paused Paused. Safe to unplug.")
        assert fake.result_text == "Paused. Safe to unplug."
        assert fake.last_failure == ""


class TestBusyGuard:
    """A job already running must block a second one, the way Software's
    buttons refuse a click while its own job is in flight."""

    def test_a_refresh_is_refused_while_a_job_runs(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.job = object()
        fake._run = lambda *a, **k: pytest.fail("a second job must not start")
        window.MigrationWindow._refresh(fake)

    def test_starting_apply_is_refused_while_a_job_runs(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.job = object()
        fake._chosen_ids = lambda: pytest.fail("the guard must return before this is read")
        window.MigrationWindow._start_apply(fake)

    def test_opening_a_selection_is_refused_while_a_job_runs(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.job = object()
        fake.sources = None  # touched only if the guard fails to return first
        window.MigrationWindow._open_selected(fake)

    def test_choosing_an_archive_is_refused_while_a_job_runs(self, window, monkeypatch):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.job = object()
        monkeypatch.setattr(
            window.Gtk, "FileChooserDialog",
            lambda *a, **k: pytest.fail("the guard must return before a dialog is built"),
        )
        window.MigrationWindow._choose_archive(fake)

    def test_exporting_is_refused_while_a_job_runs(self, window, monkeypatch):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.job = object()
        monkeypatch.setattr(
            window.Gtk, "FileChooserDialog",
            lambda *a, **k: pytest.fail("the guard must return before a dialog is built"),
        )
        window.MigrationWindow._export(fake)


class TestFailureFeedback:
    """The reason a candidates or inventory run failed is what portlin-migrate
    put in its ::result failed line, not a generic exit-code message."""

    def test_a_failed_result_is_captured_as_the_last_failure(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.last_failure = ""
        fake._append = lambda line: None
        fake._keep_live = lambda: None
        window.MigrationWindow._on_event(fake, "result", "failed the passphrase did not open the drive")
        assert fake.last_failure == "the passphrase did not open the drive"

    def test_the_failure_reason_is_shown_over_the_generic_exit_message(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.last_failure = "the passphrase did not open the drive"
        fake.candidates = None
        fake.sources = type("Sources", (), {"get_children": lambda self: []})()
        seen = {}
        fake.sources_note = type("Note", (), {"set_text": lambda self, text: seen.setdefault("text", text)})()
        fake._set_source_state = lambda name: seen.setdefault("state", name)
        window.MigrationWindow._on_candidates(fake, 1)
        assert seen["text"] == "the passphrase did not open the drive"
        assert seen["state"] == "empty"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


OUTLINE = json.dumps([
    {"text": "Creating the account ann", "bytes": 0},
    {"text": "Copying Documents", "bytes": 3000},
    {"text": "Copying Pictures", "bytes": 1000},
    {"text": "Installing vlc", "bytes": 0},
])


class TestRunTracker:
    """The copy page's numbers, from the protocol alone."""

    def _tracker(self, window):
        clock = Clock()
        tracker = window.RunTracker(clock=clock)
        tracker.on_stages(OUTLINE)
        return tracker, clock

    def test_every_phase_is_listed_before_the_first_starts(self, window):
        tracker, _ = self._tracker(window)
        assert [stage.text for stage in tracker.stages][1:3] == ["Copying Documents", "Copying Pictures"]
        assert all(stage.state == "pending" for stage in tracker.stages)
        assert tracker.total == 4000
        assert tracker.position() == (1, 4)

    def test_steps_move_through_the_phases_and_time_each(self, window):
        tracker, clock = self._tracker(window)
        tracker.on_step("Creating the account ann")
        clock.now += 2
        tracker.on_step("Copying Documents")
        first, second = tracker.stages[:2]
        assert first.state == "done" and first.finished - first.started == 2
        assert second.state == "active"
        assert tracker.position() == (2, 4)

    def test_a_repeated_step_is_the_same_phase(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_step("Copying Documents")
        tracker.on_step("Copying Documents")
        assert tracker.active == 1 and tracker.stages[1].state == "active"
        # The account phase was skipped over: it is done, not left waiting.
        assert tracker.stages[0].state == "done"

    def test_a_childs_own_step_becomes_detail(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_step("Installing vlc")
        tracker.on_step("Downloading vlc")
        assert tracker.stages[3].state == "active"
        assert tracker.stages[3].detail == "Downloading vlc"
        assert len(tracker.stages) == 4

    def test_without_an_outline_each_step_becomes_a_phase(self, window):
        tracker = window.RunTracker(clock=Clock())
        tracker.on_step("Copying A")
        tracker.on_step("Copying B")
        assert [stage.state for stage in tracker.stages] == ["done", "active"]

    def test_bytes_give_a_fraction_a_speed_and_time_left(self, window):
        tracker, clock = self._tracker(window)
        tracker.on_step("Copying Documents")
        tracker.on_bytes("0 4000")
        clock.now += 1
        tracker.on_bytes("100 4000")
        assert tracker.rate == pytest.approx(100)
        assert tracker.remaining() is None  # too early to promise anything
        clock.now += 4
        tracker.on_bytes("500 4000")
        assert tracker.fraction() == pytest.approx(500 / 4000)
        assert tracker.remaining() == pytest.approx(3500 / tracker.rate)
        assert tracker.stage_fraction(1) == pytest.approx(500 / 3000)

    def test_samples_closer_than_half_a_second_do_not_move_the_speed(self, window):
        tracker, clock = self._tracker(window)
        tracker.on_bytes("0 4000")
        clock.now += 0.1
        tracker.on_bytes("2000 4000")
        assert tracker.rate is None and tracker.copied == 2000

    def test_the_strip_is_divided_where_each_copy_ends(self, window):
        tracker, _ = self._tracker(window)
        assert tracker.boundaries() == [0.75]

    def test_files_and_warnings_are_counted(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_files("12408")
        tracker.on_warn("ignoring the source's software 'x'")
        assert tracker.files == 12408 and len(tracker.warnings) == 1

    def test_finishing_well_fills_everything(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_step("Copying Pictures")
        tracker.finish(True)
        assert tracker.fraction() == 1.0
        assert all(stage.state == "done" for stage in tracker.stages)
        assert tracker.remaining() is None

    def test_pausing_marks_where_it_paused_and_is_neither_done_nor_failed(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_step("Copying Documents")
        tracker.finish(False, "Paused. The old drive is unmounted.", paused=True)
        assert tracker.outcome == "paused"
        assert [stage.state for stage in tracker.stages] == ["done", "paused", "pending", "pending"]
        assert tracker.remaining() is None

    def test_failing_marks_where_it_stopped_and_leaves_the_rest(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_step("Copying Documents")
        tracker.finish(False, "rsync exited with status 11")
        assert [stage.state for stage in tracker.stages] == ["done", "failed", "pending", "pending"]
        assert tracker.message == "rsync exited with status 11"

    def test_garbage_is_ignored(self, window):
        tracker, _ = self._tracker(window)
        tracker.on_stages("not json")
        tracker.on_bytes("lots")
        tracker.on_progress("most")
        tracker.on_files("many")
        assert len(tracker.stages) == 4 and tracker.copied == 0


class TestWording:
    def test_times_and_speeds(self, window):
        assert window.format_clock(7) == "0:07"
        assert window.format_clock(252) == "4:12"
        assert window.format_clock(3729) == "1:02:09"
        assert window.format_remaining(30) == "under a minute"
        assert window.format_remaining(61) == "about 2 min"
        assert window.format_remaining(3600 * 2 + 60 * 5) == "about 2 h 5 min"
        assert window.format_span(0.42) == "0.4 s"
        assert window.format_span(41) == "41 s"
        assert window.format_rate(84_200_000) == "84.2 MB/s"

    def test_a_card_splits_what_describe_says_on_one_line(self, window):
        stick = {"kind": "stick", "path": "/dev/sdb4", "model": "SanDisk Ultra", "size": "57.3G",
                 "encrypted": True, "version": "0.1.2"}
        assert window.card_text(stick) == ("SanDisk Ultra 57.3G", "portlin 0.1.2 · /dev/sdb4")
        archive = {"kind": "archive", "path": "/media/x/D/office.portlin-backup.tar.zst", "model": "D"}
        assert window.card_text(archive) == ("office.portlin-backup.tar.zst", "backup on D")

    def test_the_space_line_says_whether_it_fits(self, window):
        assert window.space_summary(1_000_000_000, 5_000_000_000) == ("4 GB free here afterwards", True)
        text, fits = window.space_summary(6_000_000_000, 5_000_000_000)
        assert not fits and text == "needs 1 GB more than is free here"


class TestMenuEntry:
    def test_it_parses_and_opens_the_window_under_system(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        entry = parser["Desktop Entry"]
        assert entry["Exec"] == "portlin-migration"
        assert entry["Icon"] == "portlin"
        assert "System" in entry["Categories"]
        assert entry["Terminal"] == "false"


class TestLiveSession:
    """The X client first-boot setup runs for an upgrade."""

    SCRIPT = RUNTIME / "portlin-migrate-live"

    def test_setup_starts_the_script_the_desktop_package_installs(self):
        from portlin import package
        from test_firstboot import load_wizard

        assert load_wizard().LIVE_SESSION == f"/{package.MIGRATE_LIVE_TOOL}"
        assert package.MIGRATE_LIVE_TOOL in package.executable_paths("portlin-desktop")
        assert package.MIGRATE_LIVE_TOOL in package.text_files("portlin-desktop")

    def test_it_is_valid_shell_and_opens_the_window_in_live_mode(self):
        import subprocess

        assert subprocess.run(["sh", "-n", str(self.SCRIPT)]).returncode == 0
        text = self.SCRIPT.read_text()
        assert f"{window_path()} --live" in text

    @pytest.mark.parametrize("xrandr, expected", [
        ("eDP-1 connected primary 3840x2160+0+0 (normal) 344mm x 194mm", "2"),
        ("HDMI-1 connected 2560x1440+0+0 (normal) 597mm x 336mm", ""),
        ("eDP-1 connected 1920x1080+0+0 (normal) 344mm x 194mm", ""),
        ("Virtual-1 connected 3840x2160+0+0 (normal) 0mm x 0mm", ""),
    ])
    def test_it_scales_the_window_for_a_dense_screen(self, tmp_path, xrandr, expected):
        import os
        import subprocess

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "xrandr").write_text(f"#!/bin/sh\ncat <<'EOF'\n{xrandr}\nEOF\n")
        (bin_dir / "portlin-migration").write_text(f'#!/bin/sh\nprintf %s "$GDK_SCALE" > {tmp_path}/scale\n')
        # No session bus, keyboard or root window to set in a test.
        (bin_dir / "dbus-run-session").write_text('#!/bin/sh\nshift\nexec "$@"\n')
        for name in ("setxkbmap", "xsetroot"):
            (bin_dir / name).write_text("#!/bin/sh\n")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        script = tmp_path / "live"
        script.write_text(self.SCRIPT.read_text().replace(window_path(), str(bin_dir / "portlin-migration")))
        env = {k: v for k, v in os.environ.items() if k != "GDK_SCALE"}
        subprocess.run(["sh", str(script)], env={**env, "PATH": f"{bin_dir}:{env['PATH']}"}, check=True)
        assert (tmp_path / "scale").read_text() == expected

    def test_setup_and_the_window_agree_on_where_the_result_goes(self, window):
        from test_firstboot import load_wizard

        assert load_wizard().LIVE_RESULT == window.LIVE_RESULT


def window_path() -> str:
    return "/usr/bin/portlin-migration"
