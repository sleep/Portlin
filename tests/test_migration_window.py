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


class TestMenuEntry:
    def test_it_parses_and_opens_the_window_under_system(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        entry = parser["Desktop Entry"]
        assert entry["Exec"] == "portlin-migration"
        assert entry["Icon"] == "portlin"
        assert "System" in entry["Categories"]
        assert entry["Terminal"] == "false"
