"""Checks on the About dialog and the menu entry that opens it.

Nothing here can run the dialog: it needs GTK, an X display and an installed
stick's /etc/portlin-release. What can be verified is that it parses, that it
asks the system rather than carrying its own copy of the answers, and that the
desktop entry points at things the packages actually ship -- a menu item whose
Exec or Icon names a path nobody installed fails silently, as a line in the
menu that does nothing.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from portlin import package

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
ABOUT = RUNTIME / "portlin-about"
ENTRY = RUNTIME / "portlin-about.desktop"


class TestAboutTool:
    def test_it_parses(self):
        compile(ABOUT.read_text(), str(ABOUT), "exec")

    def test_it_has_a_python3_shebang(self):
        assert ABOUT.read_text().startswith("#!/usr/bin/env python3")

    def test_it_reports_the_version_the_stick_records(self):
        # Not __version__ baked in at build time: portlin-runtime can be
        # upgraded from the archive after the stick was written, and a dialog
        # naming the version that wrote it would be wrong from then on.
        source = ABOUT.read_text()
        assert "/etc/portlin-release" in source
        assert "PORTLIN_VERSION" in source

    def test_it_asks_portlin_info_for_the_details_rather_than_recomputing_them(self):
        # Two implementations of "how big is this drive" is one too many, and
        # the dialog is the copy nobody would notice going stale.
        source = ABOUT.read_text()
        assert "portlin-info" in source
        assert "lsblk" not in source
        assert "statvfs" not in source and "disk_usage" not in source

    def test_it_needs_no_root(self):
        # It opens from a menu, in the user's session. Anything demanding root
        # here would put a password prompt in front of an About box.
        assert "geteuid" not in ABOUT.read_text()


class TestMenuEntry:
    def _entry(self) -> configparser.SectionProxy:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(ENTRY.read_text())
        return parser["Desktop Entry"]

    def test_it_is_a_valid_desktop_entry(self):
        entry = self._entry()
        assert entry["Type"] == "Application"
        assert entry["Name"] == "About Portlin"

    def test_it_runs_the_about_tool_not_a_terminal(self):
        entry = self._entry()
        assert entry["Exec"] == "portlin-about"
        assert entry.get("Terminal", "false") == "false"

    def test_it_sits_beside_about_xfce_at_the_top_of_the_menu(self):
        # The categories xfce4-about.desktop uses. Without X-Xfce-Toplevel the
        # entry falls into a submenu, away from the About it belongs next to.
        categories = self._entry()["Categories"].strip(";").split(";")
        assert "X-XFCE" in categories
        assert "X-Xfce-Toplevel" in categories

    def test_its_exec_names_a_binary_the_packages_install(self):
        exec_name = self._entry()["Exec"].split()[0]
        shipped = package.text_files("portlin-desktop")
        assert f"usr/bin/{exec_name}" in shipped

    def test_its_icon_names_a_file_the_packages_install(self):
        icon = self._entry()["Icon"]
        assert icon.startswith("/")
        assert icon.lstrip("/") in package.binary_files("portlin-runtime")
