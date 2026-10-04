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
LAYOUT = RUNTIME / "portlin-about.menu"


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

    def test_it_names_the_commit_the_stick_was_written_from(self):
        assert "PORTLIN_COMMIT" in ABOUT.read_text()

    def test_it_names_its_window_icon_so_the_mark_reaches_the_window_list(self):
        # set_logo draws inside the dialog and nowhere else. What the window
        # manager, the task list and the alt-tab switcher show is a separate
        # icon, and a window that never names one gets a generic placeholder.
        # The name is the one the packages install into hicolor, so this and
        # the desktop entry cannot drift apart.
        source = ABOUT.read_text()
        assert "Gtk.Window.set_default_icon_name(ICON_NAME)" in source
        assert f'ICON_NAME = "{package.APP_ICON}"' in source
        # And that name has to be one the packages actually install, or the
        # window falls back to the placeholder this exists to replace.
        assert (
            f"usr/share/icons/hicolor/scalable/apps/{package.APP_ICON}.svg"
            in package.binary_files("portlin-desktop")
        )

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

    def test_it_sits_at_the_root_of_the_menu_rather_than_a_submenu(self):
        # The categories xfce4-about.desktop uses. Without X-Xfce-Toplevel the
        # entry falls into a submenu. This puts it at the same level as About
        # Xfce, not next to it -- see TestMenuLayout for that.
        categories = self._entry()["Categories"].strip(";").split(";")
        assert "X-XFCE" in categories
        assert "X-Xfce-Toplevel" in categories

    def test_its_exec_names_a_binary_the_packages_install(self):
        exec_name = self._entry()["Exec"].split()[0]
        shipped = package.text_files("portlin-desktop")
        assert f"usr/bin/{exec_name}" in shipped

    def test_its_icon_is_a_name_the_packages_install_into_hicolor(self):
        # A name rather than a path. The path this used to carry pointed into
        # portlin-runtime from an entry portlin-desktop ships, so the two
        # packages had to agree on a filename forever; a name is looked up in
        # the icon theme, and the file backing it can move between packages
        # without the entry knowing. It is also what the dialog hands
        # set_default_icon_name to get the mark into the window list.
        icon = self._entry()["Icon"]
        assert "/" not in icon
        assert (
            f"usr/share/icons/hicolor/scalable/apps/{icon}.svg"
            in package.binary_files("portlin-desktop")
        )


class TestMenuLayout:
    """The xfce-applications.menu override that positions the entry.

    Categories only say the entry belongs at the menu's root; where among the
    other root items it lands is a separate question, answered by a Layout,
    and About Xfce is placed there by Filename rather than by category. This
    file has to match that mechanism to land beside it.
    """

    def test_it_merges_into_the_stock_root_menu(self):
        # A <Menu> merges into an existing one of the same Name rather than
        # replacing the menu tree, and the stock xfce-applications.menu names
        # its root menu "Xfce".
        content = LAYOUT.read_text()
        assert "<Name>Xfce</Name>" in content

    def test_it_places_itself_immediately_above_about_xfce(self):
        content = LAYOUT.read_text()
        filenames = [
            line.strip().removeprefix("<Filename>").removesuffix("</Filename>")
            for line in content.splitlines()
            if "<Filename>" in line
        ]
        index = filenames.index("portlin-about.desktop")
        assert filenames[index + 1] == "xfce4-about.desktop"

    def test_it_keeps_every_root_item_the_stock_layout_places(self):
        # The last Layout wins outright, so anything the stock one names and
        # this one leaves out drops into the merged block in name order: Log
        # Out ended up between File Manager and Mail Reader that way.
        content = LAYOUT.read_text()
        for name in ("xfce4-run.desktop", "xfce4-terminal-emulator.desktop", "xfce4-file-manager.desktop",
                     "xfce4-mail-reader.desktop", "xfce4-web-browser.desktop", "xfce4-session-logout.desktop"):
            assert f"<Filename>{name}</Filename>" in content
        assert content.index("xfce4-about.desktop") < content.index("xfce4-session-logout.desktop")

    def test_it_puts_the_portlin_submenu_beside_settings(self):
        content = LAYOUT.read_text()
        assert "<Menuname>Portlin</Menuname>\n    <Menuname>Settings</Menuname>" in content

    def test_it_ships_under_the_merge_directory_xfce_reads(self):
        destination = package.MENU_LAYOUT_ENTRIES["portlin-about.menu"]
        assert destination == "etc/xdg/menus/applications-merged/portlin-about.menu"
        assert destination in package.text_files("portlin-desktop")

    def test_it_is_declared_a_conffile(self):
        # Under /etc, like the theme defaults and the autostart entry: an
        # admin who deletes or edits this to change the menu has as much
        # right to keep that edit as one who has tuned a theme.
        conffiles = package.text_files("portlin-desktop")["DEBIAN/conffiles"].splitlines()
        destination = package.MENU_LAYOUT_ENTRIES["portlin-about.menu"]
        assert f"/{destination}" in conffiles


TOOLS_MENU = RUNTIME / "portlin-tools.menu"
DIRECTORY = RUNTIME / "portlin.directory"
CATEGORISED = ["portlin-software.desktop", "portlin-drivers.desktop", "portlin-migration.desktop", "portlin-caffeine.desktop",
               "portlin-settings.desktop"]


class TestPortlinSubmenu:
    """The Portlin submenu, which gathers portlin's own tools in one place."""

    def _categories(self, name: str) -> list[str]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string((RUNTIME / name).read_text())
        return parser["Desktop Entry"]["Categories"].strip(";").split(";")

    def test_it_gathers_the_x_portlin_category(self):
        content = TOOLS_MENU.read_text()
        assert "<Name>Portlin</Name>" in content
        assert "<Include>\n      <Category>X-Portlin</Category>" in content

    def test_each_tool_is_in_the_category_and_keeps_a_standard_one(self):
        # The standard category is for any other desktop, which knows nothing
        # of X-Portlin and would otherwise file the tool under Other.
        for name in CATEGORISED:
            categories = self._categories(name)
            assert "X-Portlin" in categories, name
            assert any(not category.startswith("X-") for category in categories), name

    def test_the_stock_submenus_those_categories_reach_give_the_tools_up(self):
        # Without these each tool would appear twice: in Portlin, and wherever
        # its standard category files it.
        content = TOOLS_MENU.read_text()
        for submenu in ("Accessories", "Settings", "System"):
            block = content[content.index(f"<Name>{submenu}</Name>"):]
            block = block[:block.index("</Menu>")]
            assert "<Exclude>" in block and "<Category>X-Portlin</Category>" in block, submenu

    def test_the_submenu_has_a_name_and_icon(self):
        content = DIRECTORY.read_text()
        assert "Type=Directory" in content and "Name=Portlin" in content and "Icon=portlin" in content
        assert "<Directory>portlin.directory</Directory>" in TOOLS_MENU.read_text()

    def test_both_ship_where_xfce_looks(self):
        files = package.text_files("portlin-desktop")
        assert package.MENU_LAYOUT_ENTRIES["portlin-tools.menu"] == (
            "etc/xdg/menus/applications-merged/portlin-tools.menu")
        assert "etc/xdg/menus/applications-merged/portlin-tools.menu" in files
        assert "usr/share/desktop-directories/portlin.directory" in files
