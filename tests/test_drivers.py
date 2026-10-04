"""Checks on the Drivers window, and the menu entry that opens it.

As with Software and Migrate, nothing here draws anything: the window needs
GTK, an X display and an installed stick. What is checked is every decision
it makes before it draws, kept in functions with no GTK in them: the command
lines it runs, how it reads portlin-install's JSON and line protocol, and
what it says when something goes wrong. The JSON it reads is produced here
by the real portlin-install, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import argparse
import configparser
import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import load_tool
from portlin import package
from test_install_tool import AMD_LSPCI, LAPTOP_LSPCI, QEMU_LSPCI
from test_software import _stub_gi

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
WINDOW = RUNTIME / "portlin-drivers"
ENTRY = RUNTIME / "portlin-drivers.desktop"
POLICY = RUNTIME / "org.portlin.install.policy"
FIRSTBOOT = RUNTIME.parent / "firstboot" / "portlin-firstboot"


@pytest.fixture(scope="module")
def drivers():
    _stub_gi()
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    loader = importlib.machinery.SourceFileLoader("portlin_drivers", str(WINDOW))
    spec = importlib.util.spec_from_file_location(loader.name, WINDOW, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return load_tool("portlin-install")


@pytest.fixture
def ctx(tool):
    return tool.Context(
        root=False, codename="trixie", sources=(), components=("main",),
        dropin_components=(), user="somebody", home="/home/somebody",
        kernel="6.12.30-amd64", download_dir="/tmp/d", state_dir="/tmp/s",
    )


def real_scan(tool, ctx, tmp_path, capsys, lspci: str) -> str:
    """What `portlin-install scan --json` prints for this lspci output."""
    source = tmp_path / "lspci.txt"
    source.write_text(lspci)
    args = tool.build_parser().parse_args(["scan", "--json", "--from", str(source)])
    assert args.func(args, ctx) == tool.EXIT_OK
    return capsys.readouterr().out


def real_list(tool, ctx, capsys, monkeypatch, dpkg: set[str]) -> str:
    """What `portlin-install list --json --category Drivers` prints."""
    monkeypatch.setattr(tool, "dpkg_installed", lambda: dpkg)
    args = tool.build_parser().parse_args(["list", "--json", "--category", "Drivers"])
    assert args.func(args, ctx) == tool.EXIT_OK
    return capsys.readouterr().out


class TestTheProgramIsValidPython:
    def test_it_compiles(self):
        compile(WINDOW.read_text(), str(WINDOW), "exec")

    def test_it_has_a_python3_shebang(self):
        assert WINDOW.read_text().startswith("#!/usr/bin/env python3")

    def test_it_is_executable(self):
        assert WINDOW.stat().st_mode & 0o111

    def test_it_imports_without_error(self, drivers):
        assert drivers is not None

    def test_it_never_asks_to_be_root(self):
        assert "geteuid" not in WINDOW.read_text()

    def test_it_has_no_driver_logic_of_its_own(self):
        # portlin-install is the tested copy of "what fits this machine" and
        # "how to install it"; a second one here would be the one that drifts.
        source = WINDOW.read_text()
        for command in ("apt-get", "apt", "lspci", "nvidia-detect", "dpkg-query", "dpkg"):
            assert f'"{command}"' not in source
        assert "import catalog" not in source and "from catalog" not in source

    def test_it_uses_no_dash_characters_the_repo_forbids(self):
        source = WINDOW.read_text() + ENTRY.read_text()
        assert chr(0x2013) not in source and chr(0x2014) not in source


class TestCommandLines:
    def test_the_lookups_run_as_the_user(self, drivers):
        assert drivers.scan_argv() == ["/usr/bin/portlin-install", "scan", "--json"]
        assert drivers.list_argv() == [
            "/usr/bin/portlin-install", "list", "--json", "--category", "Drivers"
        ]

    def test_an_install_goes_through_pkexec(self, drivers):
        assert drivers.action_argv(
            "install", ["nvidia-driver"], privileged=True, passwordless_sudo=False
        ) == ["pkexec", "/usr/bin/portlin-install", "install", "nvidia-driver"]

    def test_several_installs_are_one_run(self, drivers):
        assert drivers.action_argv(
            "install", ["nvidia-driver", "intel-graphics"], privileged=True, passwordless_sudo=False
        )[-3:] == ["install", "nvidia-driver", "intel-graphics"]

    def test_a_waived_sudo_password_is_not_asked_for_again(self, drivers):
        assert drivers.action_argv(
            "remove", ["amd-graphics"], privileged=True, passwordless_sudo=True
        ) == ["sudo", "-n", "/usr/bin/portlin-install", "remove", "amd-graphics"]

    def test_an_unprivileged_entry_is_never_elevated(self, drivers):
        assert drivers.action_argv(
            "install", ["x"], privileged=False, passwordless_sudo=False
        ) == ["/usr/bin/portlin-install", "install", "x"]

    def test_removal_is_one_entry_at_a_time_as_the_tool_takes_it(self, drivers, tool):
        with pytest.raises(ValueError):
            drivers.action_argv("remove", ["a", "b"], privileged=True, passwordless_sudo=False)
        with pytest.raises(SystemExit):
            tool.build_parser().parse_args(["remove", "a", "b"])

    def test_every_verb_it_uses_is_one_the_tool_accepts(self, drivers, tool):
        parser = tool.build_parser()
        for argv in (
            drivers.scan_argv(),
            drivers.list_argv(),
            drivers.action_argv("install", ["intel-graphics"], privileged=False, passwordless_sudo=False),
            drivers.action_argv("remove", ["intel-graphics"], privileged=False, passwordless_sudo=False),
        ):
            assert isinstance(parser.parse_args(argv[1:]), argparse.Namespace)

    def test_the_category_it_asks_for_exists(self, drivers):
        catalog = load_tool("catalog.py")
        assert drivers.CATEGORY in catalog.CATEGORIES

    def test_the_program_it_runs_is_the_one_the_polkit_action_grants(self, drivers):
        granted = re.search(r'exec\.path">([^<]+)<', POLICY.read_text()).group(1)
        assert drivers.INSTALLER == granted

    def test_a_missing_installer_is_noticed(self, drivers, tmp_path):
        assert drivers.installer_present(str(tmp_path / "portlin-install")) is False
        script = tmp_path / "portlin-install"
        script.write_text("#!/bin/sh\n")
        assert drivers.installer_present(str(script)) is False
        script.chmod(0o755)
        assert drivers.installer_present(str(script)) is True

    def test_a_missing_sudo_is_not_a_waived_password(self, drivers):
        def raiser(*args, **kwargs):
            raise FileNotFoundError("sudo")

        assert drivers.has_passwordless_sudo(raiser) is False


class TestReadingTheRealTool:
    """Fed the JSON the real portlin-install prints, not a hand-written copy."""

    def test_a_hybrid_laptop_suggests_its_drivers_in_order(
        self, drivers, tool, ctx, tmp_path, capsys, monkeypatch
    ):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, LAPTOP_LSPCI))
        entries = drivers.parse_entries(real_list(tool, ctx, capsys, monkeypatch, set()))
        suggested, others = drivers.build_rows(scan, entries)
        assert [row.id for row in suggested] == ["nvidia-driver", "intel-graphics", "broadcom-wifi"]
        assert "printing" in [row.id for row in others]
        assert not {row.id for row in suggested} & {row.id for row in others}
        nvidia = suggested[0]
        assert nvidia.label == "NVIDIA driver"
        assert "GeForce MX150" in nvidia.reason
        assert nvidia.warning and nvidia.privileged and nvidia.installed is False

    def test_what_is_installed_comes_from_the_tools_list(
        self, drivers, tool, ctx, tmp_path, capsys, monkeypatch
    ):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, AMD_LSPCI))
        entries = drivers.parse_entries(
            real_list(tool, ctx, capsys, monkeypatch, {"mesa-vulkan-drivers", "cups"})
        )
        suggested, others = drivers.build_rows(scan, entries)
        assert [(row.id, row.installed) for row in suggested] == [("amd-graphics", True)]
        assert {row.id: row.installed for row in others}["printing"] is True

    def test_a_virtual_machine_has_nothing_to_suggest(
        self, drivers, tool, ctx, tmp_path, capsys
    ):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, QEMU_LSPCI))
        suggested, _ = drivers.build_rows(scan, [])
        assert suggested == []
        assert any("virtual machine" in note for note in scan["notes"])

    def test_an_unnamed_card_is_named_by_its_ids(self, drivers, tool, ctx, tmp_path, capsys):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, QEMU_LSPCI))
        assert drivers.hardware_lines(scan) == [("Graphics", "Unnamed device [1234:1111]")]

    def test_the_hardware_card_names_graphics_and_wifi(self, drivers, tool, ctx, tmp_path, capsys):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, LAPTOP_LSPCI))
        lines = drivers.hardware_lines(scan)
        assert [what for what, _ in lines] == ["Graphics", "Graphics", "Wi-Fi"]
        assert any("GeForce MX150" in name for _, name in lines)

    def test_a_thinkpad_is_named_first_and_offered_its_entry(
        self, drivers, tool, ctx, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(tool, "read_machine",
                            lambda *a: {"model": "LENOVO ThinkPad T14 Gen 3", "thinkpad": True})
        monkeypatch.setattr(tool.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, AMD_LSPCI, ""))
        args = tool.build_parser().parse_args(["scan", "--json"])
        assert args.func(args, ctx) == tool.EXIT_OK
        scan = drivers.parse_scan(capsys.readouterr().out)
        assert drivers.hardware_lines(scan)[0] == ("Model", "LENOVO ThinkPad T14 Gen 3")
        entries = drivers.parse_entries(real_list(tool, ctx, capsys, monkeypatch, set()))
        suggested, others = drivers.build_rows(scan, entries)
        thinkpad = next(row for row in suggested if row.id == "thinkpad")
        assert thinkpad.label == "ThinkPad power and battery care"
        assert "T14 Gen 3" in thinkpad.reason and thinkpad.notes

    def test_elsewhere_the_thinkpad_entry_is_still_available(
        self, drivers, tool, ctx, tmp_path, capsys, monkeypatch
    ):
        scan = drivers.parse_scan(real_scan(tool, ctx, tmp_path, capsys, AMD_LSPCI))
        entries = drivers.parse_entries(real_list(tool, ctx, capsys, monkeypatch, set()))
        _, others = drivers.build_rows(scan, entries)
        assert "thinkpad" in [row.id for row in others]

    def test_without_a_model_graphics_still_leads(self, drivers):
        assert drivers.hardware_lines({"machine": "", "gpus": []})[0] == ("Graphics", "not identified")
        lines = drivers.hardware_lines({"machine": "LENOVO ThinkPad X13", "gpus": []})
        assert lines == [("Model", "LENOVO ThinkPad X13"), ("Graphics", "not identified")]


class TestReadingBadOutput:
    def test_output_that_is_not_json_is_not_a_report(self, drivers):
        assert drivers.parse_scan("Traceback (most recent call last):") is None
        assert drivers.parse_entries("") is None
        assert drivers.parse_scan("[]") is None
        assert drivers.parse_entries("{}") is None

    def test_malformed_fields_cost_a_line_not_the_window(self, drivers):
        scan = drivers.parse_scan(json.dumps({
            "gpus": "nope", "suggestions": [{"entry": 3}, "x", {"entry": "intel-graphics"}],
            "notes": ["fine", 4], "machine": ["not", "a", "name"],
        }))
        assert scan == {"machine": "", "gpus": [], "wifi": [], "notes": ["fine"],
                        "suggestions": [{"entry": "intel-graphics"}]}

    def test_a_machine_with_no_graphics_found_says_so(self, drivers):
        assert drivers.hardware_lines({"gpus": [], "wifi": []}) == [("Graphics", "not identified")]

    def test_a_suggestion_the_list_does_not_have_is_dropped(self, drivers):
        scan = {"suggestions": [{"entry": "some-future-driver", "reason": "x"}]}
        assert drivers.build_rows(scan, []) == ([], [])

    def test_without_the_list_the_suggestions_still_show(self, drivers):
        # The installed state is unknown then, which is not the same as "no".
        scan = {"suggestions": [{"entry": "intel-graphics", "reason": "Intel graphics found"}]}
        suggested, others = drivers.build_rows(scan, None)
        assert [(row.id, row.installed) for row in suggested] == [("intel-graphics", None)]
        assert row_state_badge(drivers, suggested[0]) == "Suggested"
        assert others == []

    def test_a_suggestion_named_twice_is_one_row(self, drivers):
        scan = {"suggestions": [{"entry": "broadcom-wifi"}, {"entry": "broadcom-wifi"}]}
        suggested, _ = drivers.build_rows(scan, [{"id": "broadcom-wifi", "installed": False}])
        assert len(suggested) == 1


def row_state_badge(drivers, row) -> str:
    return drivers.row_state(row, busy=False, online="online")[0]


class TestWhatARowSays:
    def _row(self, drivers, **kw):
        return drivers.DriverRow(id="intel-graphics", label="Intel", help="", **kw)

    def test_installed_offers_removal(self, drivers):
        assert drivers.row_state(self._row(drivers, installed=True), busy=False, online="online") == (
            "Installed", "Remove", True, ""
        )

    def test_a_suggestion_offers_installation(self, drivers):
        row = self._row(drivers, suggested=True)
        assert drivers.row_state(row, busy=False, online="online") == ("Suggested", "Install", True, "")

    def test_offline_an_install_waits_and_says_why(self, drivers):
        badge, label, sensitive, hint = drivers.row_state(
            self._row(drivers), busy=False, online="offline")
        assert (label, sensitive) == ("Install", False)
        assert "network" in hint.lower()

    def test_offline_a_removal_still_works(self, drivers):
        assert drivers.row_state(self._row(drivers, installed=True), busy=False, online="offline")[2]

    def test_without_networkmanager_it_lets_the_install_try(self, drivers):
        assert drivers.row_state(self._row(drivers), busy=False, online="unknown")[2]

    def test_every_button_is_dead_while_a_job_runs(self, drivers):
        for installed in (True, False):
            assert drivers.row_state(
                self._row(drivers, installed=installed), busy=True, online="online")[2] is False

    def test_install_all_takes_only_what_is_missing(self, drivers):
        rows = [
            self._row(drivers, suggested=True),
            drivers.DriverRow(id="nvidia-driver", label="N", help="", suggested=True, installed=True),
            drivers.DriverRow(id="printing", label="P", help=""),
            drivers.DriverRow(id="amd-graphics", label="A", help="", suggested=True, installed=None),
        ]
        assert drivers.installable_suggestions(rows) == ["intel-graphics", "amd-graphics"]


class TestTheNetwork:
    def _nmcli(self, stdout="", code=0, exc=None):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            if exc:
                raise exc
            return types.SimpleNamespace(returncode=code, stdout=stdout)

        return run, seen

    def test_connected_is_online(self, drivers):
        run, seen = self._nmcli("connected\n")
        assert drivers.network_state(run) == "online"
        assert seen["argv"][0] == "nmcli"

    def test_it_cannot_hang_on_networkmanager(self, drivers):
        run, seen = self._nmcli("connected\n")
        drivers.network_state(run)
        assert seen["kwargs"]["timeout"] <= 5

    @pytest.mark.parametrize("state", ["disconnected", "connecting", "asleep",
                                       "connected (site only)", "connected (local only)"])
    def test_anything_short_of_connected_is_offline(self, drivers, state):
        assert drivers.network_state(self._nmcli(state)[0]) == "offline"

    @pytest.mark.parametrize("exc", [FileNotFoundError("nmcli"), subprocess.TimeoutExpired("nmcli", 3)])
    def test_no_networkmanager_is_unknown_not_offline(self, drivers, exc):
        assert drivers.network_state(self._nmcli(exc=exc)[0]) == "unknown"

    def test_networkmanager_not_running_is_unknown(self, drivers):
        assert drivers.network_state(self._nmcli("", code=8)[0]) == "unknown"

    def test_the_lookups_have_a_deadline(self, drivers):
        assert 0 < drivers.LOOKUP_TIMEOUT <= 120


class TestTheProtocolItReads:
    @pytest.mark.parametrize("line, expected", [
        ("::step Installing nvidia-detect", ("step", "Installing nvidia-detect")),
        ("::progress 42", ("progress", "42")),
        ("::reboot", ("reboot", "")),
        ("::result failed nvidia-driver apt-get exited with status 100",
         ("result", "failed nvidia-driver apt-get exited with status 100")),
    ])
    def test_it_reads_every_event(self, drivers, line, expected):
        assert drivers.parse_event(line) == expected

    def test_ordinary_output_is_not_an_event(self, drivers):
        assert drivers.parse_event("Setting up mesa-vulkan-drivers ...") is None

    def test_it_reads_the_lines_the_tool_actually_prints(self, drivers, tool, capsys):
        tool.emit("reboot")
        tool.emit("result", "failed", "nvidia-driver", "nvidia-detect found no driver for this card")
        tool.emit("result", "ok", "intel-graphics")
        outcome = drivers.Outcome(action="install", ids=["nvidia-driver", "intel-graphics"])
        for line in capsys.readouterr().out.splitlines():
            outcome.feed(*drivers.parse_event(line))
        assert outcome.reboot
        assert outcome.failed == {"nvidia-driver": "nvidia-detect found no driver for this card"}
        assert outcome.succeeded == ["intel-graphics"]


class TestWhatItSaysAtTheEnd:
    LABELS = {"nvidia-driver": "NVIDIA driver", "intel-graphics": "Intel video acceleration"}

    def _outcome(self, drivers, action="install", ids=("nvidia-driver",), events=(), output=()):
        outcome = drivers.Outcome(action=action, ids=list(ids), labels=self.LABELS)
        for kind, rest in events:
            outcome.feed(kind, rest)
        outcome.output.extend(output)
        return outcome

    def test_a_driver_that_built_a_module_asks_for_a_restart(self, drivers):
        outcome = self._outcome(drivers, events=[("reboot", ""), ("result", "ok nvidia-driver")])
        title, body, tone = outcome.summary(0, privileged=True)
        assert title == "Restart needed"
        assert "NVIDIA driver is installed" in body
        assert tone == "warn"

    def test_removing_one_says_why_a_restart_is_needed(self, drivers):
        outcome = self._outcome(drivers, action="remove",
                                events=[("reboot", ""), ("result", "ok nvidia-driver")])
        title, body, _ = outcome.summary(0, privileged=True)
        assert title == "Restart needed" and "removed" in body and "stays loaded" in body

    def test_an_ordinary_success_is_plain(self, drivers):
        outcome = self._outcome(drivers, ids=["intel-graphics"],
                                events=[("result", "ok intel-graphics")])
        assert outcome.summary(0, privileged=True) == (
            "Done", "Intel video acceleration: installed.", "good")

    def test_a_failure_names_the_driver_and_the_tools_reason(self, drivers):
        outcome = self._outcome(drivers, events=[
            ("result", "failed nvidia-driver nvidia-detect found no driver for this card")])
        title, body, tone = outcome.summary(1, privileged=True)
        assert title == "It did not finish" and tone == "warn"
        assert "NVIDIA driver: nvidia-detect found no driver for this card." in body

    def test_a_dropped_network_is_named_as_the_cause(self, drivers):
        outcome = self._outcome(
            drivers,
            events=[("result", "failed nvidia-driver apt-get exited with status 100")],
            output=["Err:1 http://deb.debian.org/debian trixie InRelease",
                    "  Temporary failure resolving 'deb.debian.org'"],
        )
        assert "could not reach the internet" in outcome.summary(1, privileged=True)[1]

    def test_a_cancelled_password_dialog_changed_nothing(self, drivers):
        title, body, tone = self._outcome(drivers).summary(126, privileged=True)
        assert title == "Nothing was changed" and "cancelled" in body and tone == "muted"

    def test_no_agent_says_what_is_missing(self, drivers):
        assert "authentication agent" in self._outcome(drivers).summary(127, privileged=True)[1]

    def test_a_partial_run_says_what_did_get_installed(self, drivers):
        outcome = self._outcome(drivers, ids=["intel-graphics", "nvidia-driver"], events=[
            ("result", "ok intel-graphics"),
            ("result", "failed nvidia-driver apt-get exited with status 100"),
        ])
        body = outcome.summary(1, privileged=True)[1]
        assert "Intel video acceleration: installed." in body
        assert "NVIDIA driver: apt-get exited with status 100." in body

    def test_an_exit_with_no_result_line_still_explains_itself(self, drivers):
        body = self._outcome(drivers).summary(3, privileged=True)[1]
        assert "privileges" in body

    def test_pkexec_codes_are_not_claimed_for_unelevated_jobs(self, drivers):
        assert "cancelled" not in drivers.explain_exit(126, privileged=False).lower()

    def test_the_tool_and_the_window_agree_on_the_privilege_status(self, drivers):
        installer = (RUNTIME / "portlin-install").read_text()
        assert f"EXIT_PRIVILEGE = {drivers.EXIT_PRIVILEGE}" in installer


class TestTheWordsMatchSetup:
    def test_labels_and_help_are_the_ones_first_boot_uses(self, drivers):
        # Someone who skipped a driver during setup should recognise it here.
        firstboot = FIRSTBOOT.read_text()
        for entry_id, label in drivers.DRIVER_LABELS.items():
            if entry_id == "printing":
                continue
            assert f'"{entry_id}": "{label}"' in firstboot
        for entry_id in ("intel-graphics", "amd-graphics", "broadcom-wifi"):
            assert f'"{entry_id}": "{drivers.DRIVER_HELP[entry_id]}"' in firstboot

    def test_every_driver_in_the_catalog_has_a_label_and_help(self, drivers):
        catalog = load_tool("catalog.py")
        for entry in catalog.by_category()["Drivers"]:
            assert entry.id in drivers.DRIVER_LABELS
            assert entry.id in drivers.DRIVER_HELP


class TestMenuEntry:
    def _entry(self) -> configparser.SectionProxy:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        return parser["Desktop Entry"]

    def test_it_is_a_valid_desktop_entry(self):
        entry = self._entry()
        assert entry["Type"] == "Application"
        assert entry["Name"] == "Drivers"
        assert entry["Icon"] == "portlin"

    def test_it_opens_the_window_rather_than_a_terminal(self):
        entry = self._entry()
        assert entry["Exec"] == "portlin-drivers"
        assert entry.get("Terminal", "false") == "false"

    def test_it_is_filed_with_portlins_tools_and_as_hardware_settings(self):
        assert self._entry()["Categories"] == "X-Portlin;System;HardwareSettings;"

    def test_its_exec_names_a_binary_the_packages_install(self):
        exec_name = self._entry()["Exec"].split()[0]
        assert f"usr/bin/{exec_name}" in package.text_files("portlin-desktop")
        assert f"usr/bin/{exec_name}" in package.executable_paths("portlin-desktop")

    def test_its_icon_is_installed_into_hicolor(self):
        icon = self._entry()["Icon"]
        assert f"usr/share/icons/hicolor/scalable/apps/{icon}.svg" in package.binary_files("portlin-desktop")

    def test_it_can_be_found_by_what_people_call_it(self):
        keywords = self._entry()["Keywords"].lower()
        for word in ("drivers", "nvidia", "graphics", "wifi"):
            assert word in keywords


class FakeJob:
    """Stands in for Job: records the argv, and lets a test play the output."""

    started: list = []

    def __init__(self, argv, *, on_event, on_output, on_done, timeout=None) -> None:
        self.argv, self.on_event, self.on_output, self.on_done = argv, on_event, on_output, on_done
        self.timeout = timeout
        self.timed_out = False
        FakeJob.started.append(self)

    def play(self, text: str, code: int = 0) -> None:
        for line in text.splitlines():
            event = sys.modules["portlin_drivers"].parse_event(line)
            if event:
                self.on_event(*event)
            else:
                self.on_output(line)
        self.on_done(code)


def _headless(drivers, monkeypatch, *, present=True, online="online") -> None:
    """Swap out everything the window would touch outside itself."""
    FakeJob.started = []
    monkeypatch.setattr(drivers, "Job", FakeJob)
    monkeypatch.setattr(drivers, "installer_present", lambda path=None: present)
    monkeypatch.setattr(drivers, "network_state", lambda run=None: online)
    # The stub window base is a plain class; give it the constructor and the
    # method surface (set_title, get_style_context, ...) a real one has.
    base = sys.modules["gi.repository.Gtk"].ApplicationWindow
    monkeypatch.setattr(base, "__init__", lambda self, **kwargs: None, raising=False)
    monkeypatch.setattr(base, "__getattr__", lambda self, name: MagicMock(name=name), raising=False)


def shown(label, text: str) -> bool:
    # Every stub Gtk.Label() is the same MagicMock, so "the last text set" is
    # whichever label was touched last; whether the text was ever set is the
    # question that can be answered.
    return any(call.args == (text,) for call in label.set_text.call_args_list)


class TestTheWindowEndToEnd:
    """The window's glue, run against the stubbed GTK.

    Nothing is drawn: every widget is a MagicMock. What this proves is that
    the window walks from scan to rows to an install and back without a
    NameError or a wrong attribute, and that it starts the commands it should
    in the order it should, which the pure functions alone cannot show.
    """

    @pytest.fixture
    def window(self, drivers, tool, ctx, tmp_path, capsys, monkeypatch):
        scan = real_scan(tool, ctx, tmp_path, capsys, LAPTOP_LSPCI)
        listing = real_list(tool, ctx, capsys, monkeypatch, {"cups"})
        _headless(drivers, monkeypatch)
        window = drivers.DriversWindow(sudo=False)
        lookups = {tuple(job.argv): job for job in FakeJob.started}
        lookups[tuple(drivers.scan_argv())].play(scan)
        lookups[tuple(drivers.list_argv())].play(listing)
        return window

    def test_it_looks_up_the_machine_and_the_list_as_the_user(self, window, drivers):
        argvs = [job.argv for job in FakeJob.started]
        assert argvs == [drivers.scan_argv(), drivers.list_argv()]
        assert all(job.timeout for job in FakeJob.started)

    def test_it_shows_the_suggestions_and_the_rest(self, window):
        assert [row.id for row in window.suggested] == ["nvidia-driver", "intel-graphics", "broadcom-wifi"]
        assert {row.id: row.installed for row in window.others}["printing"] is True
        assert set(window.row_widgets) >= {"nvidia-driver", "printing"}

    def test_an_install_runs_through_pkexec_and_reports_a_restart(self, window, drivers, monkeypatch):
        monkeypatch.setattr(window, "_confirm", lambda *args: True)
        window._on_button(window.suggested[0])
        job = FakeJob.started[-1]
        assert job.argv == ["pkexec", drivers.INSTALLER, "install", "nvidia-driver"]
        assert window.job is job
        job.play("::step Installing nvidia-detect\n::progress 50\nSetting up nvidia-driver\n"
                 "::reboot\n::result ok nvidia-driver\n")
        assert window.job is None
        assert shown(window.result_title, "Restart needed")
        assert any(call.args == (True,) for call in window.restart_button.set_visible.call_args_list)
        # Then it looks again, so the row reads Installed.
        assert [job.argv for job in FakeJob.started[-2:]] == [drivers.scan_argv(), drivers.list_argv()]

    def test_a_warning_that_is_declined_installs_nothing(self, window, monkeypatch):
        monkeypatch.setattr(window, "_confirm", lambda *args: False)
        before = len(FakeJob.started)
        window._on_button(window.suggested[0])
        assert len(FakeJob.started) == before

    def test_offline_an_install_does_not_start(self, window, drivers, monkeypatch):
        monkeypatch.setattr(drivers, "network_state", lambda run=None: "offline")
        before = len(FakeJob.started)
        window._on_button(window.suggested[1])
        assert len(FakeJob.started) == before
        assert window.online == "offline"

    def test_install_all_is_one_run_of_what_is_missing(self, window, drivers, monkeypatch):
        monkeypatch.setattr(window, "_confirm", lambda *args: True)
        window._install_all()
        assert FakeJob.started[-1].argv == [
            "pkexec", drivers.INSTALLER, "install", "nvidia-driver", "intel-graphics", "broadcom-wifi"
        ]

    def test_a_failed_removal_says_so(self, window, drivers, monkeypatch):
        monkeypatch.setattr(window, "_confirm", lambda *args: True)
        printing = next(row for row in window.others if row.id == "printing")
        window._on_button(printing)
        job = FakeJob.started[-1]
        assert job.argv == ["pkexec", drivers.INSTALLER, "remove", "printing"]
        job.play("::result failed printing apt-get exited with status 100\n", code=1)
        assert shown(window.result_title, "It did not finish")

    def test_a_scan_that_never_answers_still_leaves_a_usable_window(
        self, drivers, tool, ctx, capsys, monkeypatch
    ):
        listing = real_list(tool, ctx, capsys, monkeypatch, set())
        _headless(drivers, monkeypatch)
        window = drivers.DriversWindow(sudo=False)
        scan_job, list_job = FakeJob.started
        scan_job.timed_out = True
        scan_job.play("", code=-9)
        list_job.play(listing)
        assert window.suggested == []
        assert {row.id for row in window.others} >= {"nvidia-driver", "printing"}
        assert any("did not answer in time" in problem for problem in window.lookup_problems)

    def test_a_missing_installer_runs_nothing(self, drivers, monkeypatch):
        _headless(drivers, monkeypatch, present=False)
        window = drivers.DriversWindow(sudo=False)
        assert FakeJob.started == []
        assert window.suggested == [] and window.others == []
