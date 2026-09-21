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
            "pkexec", window.TOOL, "inventory", "/dev/sdb4", "--json",
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

    def test_the_plan_file_is_written_0600_from_the_start(self, window, tmp_path):
        path = tmp_path / "plan.json"
        plan = {"source": "/dev/sdb4", "kind": "stick"}
        window.write_plan_file(path, plan)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == plan


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
        window.MigrationWindow._on_event(fake, "result", "failed the passphrase did not open the drive")
        assert fake.last_failure == "the passphrase did not open the drive"

    def test_the_failure_reason_is_shown_over_the_generic_exit_message(self, window):
        fake = window.MigrationWindow.__new__(window.MigrationWindow)
        fake.last_failure = "the passphrase did not open the drive"
        fake.candidates = None
        fake.sources = type("Sources", (), {"get_children": lambda self: []})()
        seen = {}
        fake.sources_note = type("Note", (), {"set_text": lambda self, text: seen.setdefault("text", text)})()
        window.MigrationWindow._on_candidates(fake, 1)
        assert seen["text"] == "the passphrase did not open the drive"


class TestMenuEntry:
    def test_it_parses_and_opens_the_window_under_system(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        entry = parser["Desktop Entry"]
        assert entry["Exec"] == "portlin-migration"
        assert entry["Icon"] == "portlin"
        assert "System" in entry["Categories"]
        assert entry["Terminal"] == "false"
