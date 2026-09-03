"""Checks on the Software window, and the menu entry that opens it.

Nothing here draws anything: the window needs GTK, an X display and an
installed stick. What can be checked is every decision the program makes
before it draws, and those are deliberately kept in functions with no GTK in
them. The most important is the one that turns a catalog entry into a
command line, because that is where a mistake would either ask for a
password that is not needed or run a vendor's installer as root.
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
SOFTWARE = RUNTIME / "portlin-software"
ENTRY = RUNTIME / "portlin-software.desktop"
POLICY = RUNTIME / "org.portlin.install.policy"


def _stub_gi() -> None:
    """A fake gi, so the window's module imports on a machine with no GTK."""
    if getattr(sys.modules.get("gi"), "_portlin_stub", False):
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
def software():
    _stub_gi()
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    loader = importlib.machinery.SourceFileLoader("portlin_software", str(SOFTWARE))
    spec = importlib.util.spec_from_file_location(loader.name, SOFTWARE, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog(software):
    return sys.modules["catalog"]


class TestTheProgramIsValidPython:
    def test_it_compiles(self):
        compile(SOFTWARE.read_text(), str(SOFTWARE), "exec")

    def test_it_has_a_python3_shebang(self):
        assert SOFTWARE.read_text().startswith("#!/usr/bin/env python3")

    def test_it_imports_without_error(self, software):
        assert software is not None

    def test_it_never_asks_to_be_root(self):
        # It opens from the menu in the user's session. Everything privileged
        # is portlin-install, reached through pkexec one command at a time.
        assert "geteuid" not in SOFTWARE.read_text()

    def test_it_runs_no_package_manager_of_its_own(self):
        # Two implementations of "install this" is one too many, and this is
        # the copy that would drift: portlin-install is the tested one.
        source = SOFTWARE.read_text()
        assert "apt-get" not in source
        assert "dpkg-deb" not in source


class TestHowItElevates:
    def test_a_system_entry_goes_through_pkexec(self, software, catalog):
        assert software.launch_argv(
            catalog.by_id("vlc"), "install", passwordless_sudo=False
        ) == ["pkexec", "/usr/bin/portlin-install", "install", "vlc"]

    def test_a_waived_sudo_password_is_not_asked_for_again(self, software, catalog):
        # First boot offers to skip the sudo password. Someone who took that
        # offer should not meet a polkit dialog here.
        assert software.launch_argv(
            catalog.by_id("vlc"), "install", passwordless_sudo=True
        ) == ["sudo", "-n", "/usr/bin/portlin-install", "install", "vlc"]

    def test_an_entry_that_installs_into_a_home_is_never_elevated(self, software, catalog):
        for waived in (True, False):
            argv = software.launch_argv(
                catalog.by_id("zed"), "install", passwordless_sudo=waived
            )
            assert argv == ["/usr/bin/portlin-install", "install", "zed"]

    def test_removal_goes_the_same_way_as_installation(self, software, catalog):
        assert software.launch_argv(
            catalog.by_id("vlc"), "remove", passwordless_sudo=False
        )[-2:] == ["remove", "vlc"]

    def test_upgrading_everything_needs_root(self, software):
        assert software.upgrade_argv(passwordless_sudo=False) == [
            "pkexec", "/usr/bin/portlin-install", "upgrade"
        ]

    def test_the_program_it_runs_is_the_one_the_polkit_action_grants(self, software):
        # pkexec checks the path against the action's annotation. If these two
        # drifted apart, every install would be refused with nothing on screen
        # to say why.
        granted = re.search(r'exec\.path">([^<]+)<', POLICY.read_text()).group(1)
        assert software.INSTALLER == granted

    def test_a_missing_sudo_is_not_a_waived_password(self, software):
        def raiser(*args, **kwargs):
            raise FileNotFoundError("sudo")

        assert software.has_passwordless_sudo(raiser) is False

    def test_sudo_is_asked_in_a_way_that_cannot_prompt(self, software):
        seen = {}

        def fake(argv, **kwargs):
            seen["argv"] = argv
            return types.SimpleNamespace(returncode=0)

        assert software.has_passwordless_sudo(fake) is True
        assert seen["argv"][:2] == ["sudo", "-n"]


class TestTheProtocolItReads:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("::step Installing VLC", ("step", "Installing VLC")),
            ("::progress 42", ("progress", "42")),
            ("::warn no account to add", ("warn", "no account to add")),
            ("::reboot", ("reboot", "")),
            ("::result ok vlc", ("result", "ok vlc")),
        ],
    )
    def test_it_reads_every_event(self, software, line, expected):
        assert software.parse_event(line) == expected

    def test_ordinary_tool_output_is_not_an_event(self, software):
        assert software.parse_event("Setting up vlc (3.0.23) ...") is None
        assert software.parse_event("") is None


class TestWhatItSaysWhenSomethingFails:
    def test_a_dismissed_password_dialog_says_so(self, software):
        assert "cancelled" in software.explain_exit(126, privileged=True).lower()

    def test_a_session_with_no_agent_says_what_is_missing(self, software):
        message = software.explain_exit(127, privileged=True)
        assert "authentication agent" in message.lower()

    def test_a_privilege_refusal_is_explained(self, software):
        assert "privileges" in software.explain_exit(3, privileged=False)

    def test_success_says_nothing(self, software):
        assert software.explain_exit(0, privileged=True) == ""

    def test_an_unknown_failure_points_at_the_log(self, software):
        assert "log" in software.explain_exit(1, privileged=True)

    def test_pkexec_codes_are_not_claimed_for_unelevated_jobs(self, software):
        # 126 from a vendor script is that script failing, not a dialog
        # nobody was shown.
        assert "cancelled" not in software.explain_exit(126, privileged=False).lower()


class TestWhatARowSays:
    def test_something_installed_offers_removal(self, software, catalog):
        assert software.row_state(catalog.by_id("vlc"), True, False) == (
            "Installed", "Remove", True
        )

    def test_something_absent_offers_installation(self, software, catalog):
        assert software.row_state(catalog.by_id("vlc"), False, False) == (
            "", "Install", True
        )

    def test_every_button_is_dead_while_a_job_runs(self, software, catalog):
        state, _, sensitive = software.row_state(catalog.by_id("vlc"), False, True)
        assert state == "Working..." and sensitive is False


class TestWhatTheListShows:
    def test_a_category_shows_only_itself(self, software, catalog):
        shown = software.visible_entries("", "Browsers")
        assert {entry.category for entry in shown} == {"Browsers"}

    def test_a_search_looks_past_the_open_category(self, software):
        found = {entry.id for entry in software.visible_entries("torrent", "Browsers")}
        assert found >= {"qbittorrent", "deluge"}

    def test_whitespace_is_not_a_search(self, software):
        assert software.visible_entries("   ", "Drivers") == software.visible_entries(
            "", "Drivers"
        )

    def test_no_category_shows_everything(self, software, catalog):
        assert len(software.visible_entries("", None)) == len(catalog.ENTRIES)


class TestTheDriversPage:
    SCAN = {
        "gpus": [{"slot": "01:00.0", "vendor": "nvidia",
                  "name": "NVIDIA Corporation GP108M [GeForce MX150]", "id": "10de:1d10"}],
        "wifi": [],
        "suggestions": [
            {"entry": "nvidia-driver", "reason": "GeForce MX150 found",
             "detail": "nvidia-detect recommends nvidia-driver"},
        ],
        "notes": ["Two GPUs: a hybrid laptop."],
    }

    def test_a_suggestion_becomes_a_row_with_its_reason(self, software):
        rows = software.suggestion_rows(self.SCAN)
        assert [entry.id for entry, _ in rows] == ["nvidia-driver"]
        assert "nvidia-detect recommends" in rows[0][1]

    def test_a_suggestion_this_catalog_does_not_know_is_skipped(self, software):
        scan = {"suggestions": [{"entry": "some-future-driver", "reason": "x"}]}
        assert software.suggestion_rows(scan) == []

    def test_it_describes_the_machine_in_words(self, software):
        described = software.describe_machine(self.SCAN)
        assert "GeForce MX150" in described
        assert "hybrid laptop" in described

    def test_a_machine_it_found_nothing_on_still_says_something(self, software):
        assert software.describe_machine({}).strip()


class TestMenuEntry:
    def _entry(self) -> configparser.SectionProxy:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        return parser["Desktop Entry"]

    def test_it_is_a_valid_desktop_entry(self):
        entry = self._entry()
        assert entry["Type"] == "Application"
        assert entry["Name"] == "Software"

    def test_it_opens_the_window_rather_than_a_terminal(self):
        entry = self._entry()
        assert entry["Exec"] == "portlin-software"
        assert entry.get("Terminal", "false") == "false"

    def test_its_exec_names_a_binary_the_packages_install(self):
        exec_name = self._entry()["Exec"].split()[0]
        assert f"usr/bin/{exec_name}" in package.text_files("portlin-desktop")

    def test_its_icon_is_a_name_the_packages_install_into_hicolor(self):
        icon = self._entry()["Icon"]
        assert "/" not in icon
        assert (
            f"usr/share/icons/hicolor/scalable/apps/{icon}.svg"
            in package.binary_files("portlin-desktop")
        )

    def test_it_is_filed_where_someone_looks_for_an_installer(self):
        categories = self._entry()["Categories"].strip(";").split(";")
        assert "System" in categories
        assert "PackageManager" in categories

    def test_it_can_be_found_by_what_people_call_it(self):
        keywords = self._entry()["Keywords"].lower()
        for word in ("software", "install", "drivers"):
            assert word in keywords
