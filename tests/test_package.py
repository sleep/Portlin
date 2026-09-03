"""The package descriptions are pure data, so they are asserted directly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portlin import __version__, package, packages


def test_local_builds_are_suffixed_so_they_sort_below_a_release():
    # A tilde sorts below the empty string in Debian version comparison, so
    # 0.1.0~local is older than 0.1.0 and the published build always wins.
    # Not asserted by comparing the strings here: Python orders them the other
    # way round, because the tilde rule is dpkg's, not Python's.
    assert package.local_version() == f"{__version__}~local"
    assert package.local_version().startswith(__version__)


def test_control_names_the_package_and_its_version():
    control = package.render_control(
        name="portlin-runtime",
        version="0.1.0~local",
        description="Portlin desktop integration and tools",
        depends=["python3"],
    )
    assert "Package: portlin-runtime" in control
    assert "Version: 0.1.0~local" in control
    assert "Architecture: all" in control
    assert "Depends: python3" in control
    assert control.endswith("\n")


def test_control_omits_recommends_when_there_are_none():
    control = package.render_control(
        name="portlin-archive-keyring",
        version="0.1.0~local",
        description="Portlin archive signing key",
        depends=[],
    )
    assert "Recommends:" not in control
    assert "Depends:" not in control


def test_runtime_recommends_rather_than_depends_on_desktop():
    # A --minimal stick has no desktop, so the theme and 14 MB of wallpaper
    # must not be a hard dependency of the tools.
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    assert "Recommends: portlin-desktop" in control
    assert "portlin-desktop" not in control.split("Depends:")[1].split("\n")[0]


def test_sources_entry_pins_the_architecture():
    # Without this apt requests binary-amd64/Packages, which this archive will
    # never publish, and reports a fetch failure on every apt update.
    entry = package.render_sources_entry()
    assert "Architectures: all" in entry
    assert "Signed-By: /usr/share/keyrings/portlin-archive-keyring.gpg" in entry


def test_keyring_package_ships_no_key_or_source_while_the_placeholder_is_empty():
    # The repository ships a zero-byte placeholder until a real signing key is
    # committed. Shipping the apt source without it would point every stick at
    # an archive that either does not exist yet or cannot be verified, failing
    # apt update on every run, forever.
    assert package.KEYRING_FILE.stat().st_size == 0
    assert "etc/apt/sources.list.d/portlin.sources" not in package.text_files(
        "portlin-archive-keyring"
    )
    assert package.binary_files("portlin-archive-keyring") == {}


def test_keyring_package_ships_the_key_and_source_once_a_real_key_lands(
    tmp_path, monkeypatch
):
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    files = package.text_files("portlin-archive-keyring")
    assert "etc/apt/sources.list.d/portlin.sources" in files
    assert package.binary_files("portlin-archive-keyring") == {
        package.KEYRING_PATH.lstrip("/"): real_key
    }


def test_runtime_ships_every_tool_as_an_executable():
    executables = package.executable_paths("portlin-runtime")
    assert executables == {
        "usr/bin/portlin-info",
        "usr/bin/portlin-expand",
        "usr/bin/portlin-encrypt",
        "usr/bin/portlin-install",
    }


@pytest.mark.parametrize("name", package.PACKAGES)
def test_every_package_has_a_control_file(name):
    assert "DEBIAN/control" in package.text_files(name)


@pytest.mark.parametrize("name", package.PACKAGES)
def test_text_files_defaults_to_the_local_version(name):
    control = package.text_files(name)["DEBIAN/control"]
    assert f"Version: {package.local_version()}" in control


@pytest.mark.parametrize("name", package.PACKAGES)
def test_text_files_stamps_the_requested_version_onto_control(name):
    # A CI release build stamps a real version onto content this module still
    # renders with the ~local suffix by default, so the version has to be
    # threaded into render_control rather than patched into rendered text.
    control = package.text_files(name, version="2.5.1")["DEBIAN/control"]
    assert "Version: 2.5.1" in control
    assert package.local_version() not in control


@pytest.mark.parametrize("name", package.PACKAGES)
def test_requested_version_touches_only_the_control_file(name):
    # Nothing else in a package's tree names a version, so a version string
    # must never show up anywhere but DEBIAN/control.
    files = package.text_files(name, version="2.5.1")
    for relative, content in files.items():
        if relative != "DEBIAN/control":
            assert "2.5.1" not in content, relative


def test_runtime_ships_the_shared_device_lookup_module():
    # Two of the three tools need to find the disk or partition backing the
    # root filesystem, and this is the one place that logic is written down.
    files = package.text_files("portlin-runtime")
    assert "usr/lib/portlin/devices.py" in files
    assert "usr/lib/portlin/devices.py" not in package.executable_paths("portlin-runtime")

    source = files["usr/lib/portlin/devices.py"]
    assert "def sysfs_node" in source
    assert "def backing_partition" in source
    # Regression guard: sysfs_node must locate the device by its major:minor
    # number, never by assuming udev created a /sys/class/block/<name> symlink,
    # because without udev cryptsetup makes a real device node instead and that
    # lookup finds nothing for an encrypted root.
    #
    # Scoped to that one function rather than the whole file. sysfs_sectors also
    # reads /sys/class/block, and correctly so: it is handed a kernel name that
    # already exists there and only reads geometry from it, which is a different
    # question from finding a device node in the first place.
    node_body = source[source.index("def sysfs_node") : source.index("def backing_partition")]
    assert "st_rdev" in node_body
    assert "/sys/class/block" not in node_body


def test_runtime_ships_the_catalog_beside_the_device_module():
    # Both shared modules land in the same directory the tools put on
    # sys.path, and neither is executable: they are imported, not run.
    files = package.text_files("portlin-runtime")
    executables = package.executable_paths("portlin-runtime")
    for module in package.SHARED_MODULES:
        assert f"usr/lib/portlin/{module}" in files
        assert f"usr/lib/portlin/{module}" not in executables


def test_runtime_ships_the_polkit_action_beside_the_program_it_names():
    # The action grants the right to run one path as root. If the file said
    # one path and the package installed the program at another, pkexec would
    # refuse every request with nothing on screen to say why.
    files = package.text_files("portlin-runtime")
    destination = package.POLKIT_ACTIONS["org.portlin.install.policy"]
    assert destination in files
    named = re.search(r"exec\.path\">([^<]+)<", files[destination]).group(1)
    assert named.lstrip("/") in files
    assert named.lstrip("/") in package.executable_paths("portlin-runtime")


def test_runtime_depends_on_what_the_installer_shells_out_to():
    # Neither is visible in the file list: portlin-install downloads with
    # curl and reads the hardware with lspci, and without them it starts,
    # lists the catalog, and fails at the first thing anyone wanted.
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    depends = [d.strip() for d in control.split("Depends: ")[1].splitlines()[0].split(",")]
    assert "curl" in depends
    assert "pciutils" in depends


def test_info_tool_is_shipped_and_executable():
    files = package.text_files("portlin-runtime")
    assert files["usr/bin/portlin-info"].startswith("#!/usr/bin/env python3")
    assert "usr/bin/portlin-info" in package.executable_paths("portlin-runtime")


def test_expand_tool_resizes_in_the_only_order_that_works():
    # Each layer can only grow into space the layer beneath it has claimed, so
    # the order is not a preference. Asserted on the source because the real
    # behaviour is covered by the harness against a live device.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-expand"]
    assert source.index("growpart") < source.index("cryptsetup")
    assert source.index("cryptsetup") < source.index("resize2fs")


def test_encrypt_tool_refuses_without_the_frozen_finaliser():
    # The frozen tier of a stick written before the finaliser existed cannot be
    # brought forward, so the tool must detect its absence rather than arm an
    # encryption nothing on that stick can complete.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "/usr/local/sbin/portlin-finalise-encryption" in source


def test_encrypt_tool_does_not_encrypt_anything_itself():
    # All the dangerous work stays in the frozen, harness-tested initramfs
    # script. This tool only sets the flag that wakes it up.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "reencrypt" not in source
    assert "luksFormat" not in source
    assert "portlin.encrypt=ask" in source


def test_pkname_lookups_exclude_holder_devices():
    # Without -d (no-deps), lsblk lists the whole subtree rooted at the queried
    # device -- including an open LUKS mapping sitting on top of the very
    # partition being asked about, which is the ordinary case for a live,
    # mounted, encrypted stick. That returns two rows glued into one string
    # instead of one, and the harness caught this reaching a real growpart
    # call with an empty partition number.
    files = package.text_files("portlin-runtime")
    for tool in ("usr/bin/portlin-info", "usr/bin/portlin-expand"):
        source = files[tool]
        assert '"lsblk", "-no", "PKNAME"' not in source
        assert '"lsblk", "-dno", "PKNAME"' in source


def test_tools_use_the_shared_device_module_not_a_hand_copy():
    # A hand-written /sys/class/block lookup has already reached a USB stick
    # once in this project's history: it resolves the mapper alias for an
    # encrypted root, which sysfs never created because cryptsetup made a real
    # device node there instead of a udev symlink. Every tool must import the
    # shared lookup in usr/lib/portlin/devices.py rather than repeat that bug,
    # or duplicate the command-running and root-finding helpers that live
    # there alongside it.
    files = package.text_files("portlin-runtime")
    for tool in ("usr/bin/portlin-info", "usr/bin/portlin-expand", "usr/bin/portlin-encrypt"):
        source = files[tool]
        assert "/sys/class/block" not in source
        assert "/usr/lib/portlin" in source
        assert "from devices import" in source


def test_desktop_ships_every_theme_file():
    files = package.text_files("portlin-desktop")
    for destination in package.THEME_FILES:
        assert destination in files, destination
    assert packages.DEFAULT_THEME in files[
        f"{package.XDG_OVERLAY}/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
    ]


def test_desktop_ships_its_xdg_defaults_only_under_its_own_overlay():
    # dpkg refuses to let two installed packages own the same path, and a
    # conffiles declaration buys no exemption: xfce4-settings already ships
    # etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml, so shipping it
    # there aborts the entire apt transaction that installs portlin. Anything
    # under /etc/xdg is found through XDG_CONFIG_DIRS, so portlin's defaults
    # go in a directory only portlin ships and the session is told to search
    # it. Asserted over every path rather than that one file, because a future
    # Debian can start shipping any of the others.
    files = package.text_files("portlin-desktop")
    outside = sorted(
        path for path in files
        if path.startswith("etc/xdg/") and not path.startswith(f"{package.XDG_OVERLAY}/")
    )
    # What may sit outside the overlay is a drop-in named after a portlin
    # program, in a directory dpkg lets every desktop package drop into --
    # /etc/xdg/autostart is shared by xfce4-notifyd, blueman and the rest, and
    # xfce-applications-merged is the same idea for menu.spec's
    # <DefaultMergeDirs/>. A file nobody else can be named collides with
    # nobody. A default that another package also ships is the thing that
    # cannot go there, and this fails the moment one is added under any name.
    SHARED_DROPIN_DIRS = {"autostart", "xfce-applications-merged"}
    assert all(Path(path).name.startswith("portlin-") for path in outside), outside
    assert not {Path(path).name for path in outside} & set(package.XDG_DEFAULTS.values())
    assert not {
        path for path in outside if Path(path).parent.name not in SHARED_DROPIN_DIRS
    }


def test_desktop_puts_its_overlay_on_the_session_config_search_path():
    # The overlay is inert unless something adds it to XDG_CONFIG_DIRS, and
    # exporting it into the session shell alone is not enough: xfconfd is a
    # D-Bus activated user service, so it is started from the activation
    # environment rather than inheriting the session's. Debian's own
    # 55xfce4-session does exactly this dance for XDG_DATA_DIRS.
    snippet = package.text_files("portlin-desktop")[package.XSESSION_SNIPPET]
    assert f"/{package.XDG_OVERLAY}" in snippet
    assert "XDG_CONFIG_DIRS" in snippet
    assert "dbus-update-activation-environment" in snippet


def test_desktop_ships_the_portlin_icon_theme():
    files = package.text_files("portlin-desktop")
    assert f"{package.ICON_THEME_DIR}/index.theme" in files


def test_the_icon_theme_inherits_the_stock_set_rather_than_replacing_it():
    # It provides one icon. Everything else -- every application, every mime
    # type, every stock arrow -- has to come from the theme it inherits, or
    # naming this one in xsettings strips the icons off the whole desktop.
    index = package.text_files("portlin-desktop")[
        f"{package.ICON_THEME_DIR}/index.theme"
    ]
    inherits = [line for line in index.splitlines() if line.startswith("Inherits=")]
    assert inherits, index
    assert "Adwaita" in inherits[0]
    # Last, and always present: hicolor is where portlin's own icon lands, and
    # the spec makes it the fallback every theme ends at.
    assert inherits[0].strip().endswith("hicolor")


def test_the_icon_theme_carries_the_applications_menu_button():
    # xfce4-panel does not set button-icon in Debian's panel layout, so the
    # plugin falls back to the icon name compiled into it. Answering to that
    # name is the whole mechanism by which the mark reaches the menu button.
    destinations = package.binary_files("portlin-desktop")
    icon = f"{package.ICON_THEME_DIR}/scalable/apps/{package.MENU_BUTTON_ICON}.svg"
    assert icon in destinations
    assert destinations[icon].name == "logo.svg"


def test_the_mark_is_installed_under_its_own_name_in_hicolor():
    # Icon=portlin in a desktop entry resolves here. hicolor rather than the
    # portlin theme because this one has to be found whichever icon theme is
    # active: every theme falls back to hicolor, none falls back to Portlin.
    destinations = package.binary_files("portlin-desktop")
    assert package.HICOLOR_APP_ICON in destinations
    assert destinations[package.HICOLOR_APP_ICON].name == "logo.svg"


def test_the_about_entry_asks_for_the_icon_by_name():
    # A named icon is what puts the mark in the window list, the alt-tab
    # switcher and the appfinder; an absolute path is honoured by the menu and
    # ignored by most of the rest.
    entry = package.text_files("portlin-desktop")[
        package.MENU_ENTRIES["portlin-about.desktop"]
    ]
    assert f"Icon={package.APP_ICON}\n" in entry


def test_every_surface_that_names_a_theme_asks_for_the_portlin_icons():
    # The four files that spell a theme name each need the icon theme too, and
    # the greeter is one of them: it runs before any session exists, so it
    # reads its own directory rather than the XDG overlay.
    files = package.text_files("portlin-desktop")
    overlay = f"{package.XDG_OVERLAY}"
    assert f'"IconThemeName" type="string" value="{package.ICON_THEME}"' in files[
        f"{overlay}/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"
    ]
    for relative in ("gtk-3.0/settings.ini", "gtk-4.0/settings.ini"):
        assert f"gtk-icon-theme-name={package.ICON_THEME}" in files[
            f"{overlay}/{relative}"
        ], relative
    assert f"icon-theme-name={package.ICON_THEME}" in files[
        "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf"
    ]


def test_the_greeter_shows_the_portlin_wallpaper():
    # The greeter runs before any session exists, so nothing it displays can
    # come through the XDG overlay or xfconf: it reads its own drop-in and a
    # literal path. That path has to be one this same package ships, or the
    # login screen quietly falls back to lightdm's own grey while every other
    # assertion in this file still passes. Derived from the file rather than
    # spelled twice, so pointing it at a render that was never shipped fails
    # here instead of on a stranger's laptop.
    conf = package.text_files("portlin-desktop")[
        "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf"
    ]
    background = next(
        line.removeprefix("background=") for line in conf.splitlines()
        if line.startswith("background=")
    )
    assert background.lstrip("/") in package.binary_files("portlin-desktop")
    # The same render xfdesktop falls back to. One size has to stand in for
    # every panel, and the greeter has no more idea than xfdesktop does what
    # it will be plugged into.
    assert package.DEFAULT_BACKDROP_SIZE in background


def test_theme_files_are_not_executable():
    assert not (package.executable_paths("portlin-desktop")
                & set(package.THEME_FILES))


def test_runtime_carries_no_desktop_configuration():
    # A --minimal stick installs portlin-runtime alone. Theme configuration
    # for lightdm or xfconf in that package would misrepresent software the
    # stick does not have, so it belongs only in portlin-desktop.
    files = package.text_files("portlin-runtime")
    assert not any(path.startswith("etc/xdg/") for path in files)
    assert not any(path.startswith("etc/lightdm/") for path in files)


def test_every_resource_directory_is_declared_as_package_data():
    # A resources directory missing from package-data is invisible here and
    # fatal once installed: the files are simply absent from the wheel, so the
    # working tree passes every test in this file and the first pip-installed
    # copy to read one dies at exactly the wrong moment -- part way through
    # writing somebody's drive. Derived from the tree rather than listed, so a
    # new resources directory fails this until pyproject.toml learns about it.
    import tomllib

    root = Path(__file__).resolve().parent.parent
    declared = set(
        tomllib.loads((root / "pyproject.toml").read_text())
        ["tool"]["setuptools"]["package-data"]["portlin"]
    )
    resources = root / "portlin" / "resources"
    needed = {
        f"{directory.relative_to(root / 'portlin').as_posix()}/*"
        for directory in resources.rglob("*")
        if directory.is_dir()
        and directory.name != "__pycache__"
        and any(child.is_file() for child in directory.iterdir())
    }
    assert needed <= declared, sorted(needed - declared)


def test_every_declared_binary_member_exists():
    for name in package.PACKAGES:
        for destination, source in package.binary_files(name).items():
            assert source.exists(), f"{source} is missing for {destination}"


def test_wallpapers_carry_every_declared_size():
    # Every size, plus the one extra copy that sits where xfdesktop looks when
    # nothing has configured a backdrop.
    destinations = package.binary_files("portlin-desktop")
    renders = [d for d in destinations if d.startswith("usr/share/backgrounds/portlin/")]
    assert len(renders) == len(package.WALLPAPER_SIZES)
    assert set(destinations) - set(renders) == {
        package.DEFAULT_BACKDROP,
        *package.CAFFEINE_ICONS,
        *package.MARK_ICONS,
    }


def test_desktop_declares_every_etc_path_it_ships_as_a_conffile():
    # Without this member dpkg treats these /etc files as ordinary package
    # files and overwrites a locally-edited one silently on every upgrade. The
    # session snippet is counted here too: an admin who has tuned the search
    # path has as much right to keep that edit as one who has tuned a theme,
    # one who has stopped the caffeine applet starting at login, or one who
    # has edited the menu layout override back out.
    files = package.text_files("portlin-desktop")
    conffiles = files["DEBIAN/conffiles"].splitlines()
    expected = {
        f"/{destination}"
        for destination in (
            *package.THEME_FILES,
            *package.AUTOSTART_ENTRIES.values(),
            *package.MENU_LAYOUT_ENTRIES.values(),
        )
    }
    assert set(conffiles) == expected
    assert len(conffiles) == len(expected)
    assert conffiles == sorted(conffiles)


def test_keyring_declares_its_sources_entry_as_a_conffile(tmp_path, monkeypatch):
    # A user who points portlin.sources at a mirror must not lose that edit
    # to the next apt update of the keyring package. Only assertable once a
    # real key ships the sources entry at all.
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    files = package.text_files("portlin-archive-keyring")
    assert files["DEBIAN/conffiles"] == "/etc/apt/sources.list.d/portlin.sources\n"


def test_runtime_ships_no_etc_files_and_declares_no_conffiles():
    # portlin-runtime carries no /etc files (test_runtime_carries_no_desktop_
    # configuration), so a DEBIAN/conffiles member here would either be empty
    # or, worse, list paths the package does not actually ship.
    files = package.text_files("portlin-runtime")
    assert "DEBIAN/conffiles" not in files


def test_conffiles_also_cover_binary_members_under_etc(monkeypatch):
    # A binary member under /etc must be declared just like a text one, or
    # dpkg treats it as an ordinary file and silently overwrites a local edit
    # on every upgrade. portlin-runtime ships no /etc files of its own, which
    # makes it a clean package to prove this on: without this, the fake
    # binary member below would produce no DEBIAN/conffiles member at all.
    monkeypatch.setattr(
        package,
        "binary_files",
        lambda name: {"etc/portlin/example.bin": Path("/dev/null")},
    )
    files = package.text_files("portlin-runtime")
    assert files["DEBIAN/conffiles"] == "/etc/portlin/example.bin\n"


def test_keyring_package_carries_the_key_at_the_path_the_source_names(
    tmp_path, monkeypatch
):
    # The Signed-By path in the apt source and the path the package installs
    # the key to are the same string in two files, and apt fails silently on
    # every update if they drift apart.
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    destinations = package.binary_files("portlin-archive-keyring")
    assert package.KEYRING_PATH.lstrip("/") in destinations
    assert package.KEYRING_PATH in package.render_sources_entry()


def test_desktop_takes_over_the_backdrop_xfdesktop_falls_back_to():
    # xfdesktop has no system-wide "default wallpaper" setting. Its backdrop is
    # an xfconf property keyed by the monitor's name -- monitorHDMI-1,
    # monitoreDP-1, monitorVirtual-1 -- which cannot be known when a stick is
    # written, so no shipped default can name the property. What it draws when
    # no such property exists is one path compiled into the binary, so that
    # path is the only place a default wallpaper can actually be put.
    destinations = package.binary_files("portlin-desktop")
    assert package.DEFAULT_BACKDROP in destinations
    assert destinations[package.DEFAULT_BACKDROP].exists()


def test_desktop_diverts_the_default_backdrop_symmetrically():
    # dpkg matches a diversion by the whole triple of owning package, divert-to
    # path and original path. Added under one triple and removed under another,
    # it is never removed at all, and xfdesktop's own file stays displaced for
    # the life of the machine.
    files = package.text_files("portlin-desktop")
    preinst, postrm = files["DEBIAN/preinst"], files["DEBIAN/postrm"]
    for script in (preinst, postrm):
        assert f"/{package.DEFAULT_BACKDROP}" in script
        assert package.DIVERTED_BACKDROP in script
        assert "--package portlin-desktop" in script
    assert "--add" in preinst and "--remove" not in preinst
    assert "--remove" in postrm and "--add" not in postrm


def test_maintainer_scripts_are_executable():
    # dpkg-deb --build refuses a maintainer script that is not executable, so
    # this fails the build rather than the stick.
    executable = package.executable_paths("portlin-desktop")
    assert {"DEBIAN/preinst", "DEBIAN/postrm"} <= executable


def test_no_backdrop_is_configured_by_monitor_number():
    # xfdesktop stopped reading /backdrop/screen0/monitor0/... in 4.11, and its
    # one migration path ignores any property whose name contains /workspace.
    # A default written that way installs cleanly, verifies cleanly and is
    # never read, which is how it went unnoticed the first time.
    for name in package.PACKAGES:
        for destination, content in package.text_files(name).items():
            assert "monitor0" not in content, destination


def test_desktop_ships_the_about_dialog_and_its_menu_entry():
    files = package.text_files("portlin-desktop")
    assert files["usr/bin/portlin-about"].startswith("#!/usr/bin/env python3")
    assert "usr/share/applications/portlin-about.desktop" in files
    assert "usr/bin/portlin-about" in package.executable_paths("portlin-desktop")
    assert "etc/xdg/menus/xfce-applications-merged/portlin-about.menu" in files


def test_desktop_depends_on_what_the_about_dialog_needs():
    # The dialog is GTK, reads the logo portlin-runtime installs, and shells out
    # to portlin-info. dpkg has no way to know any of that from the file list.
    control = package.text_files("portlin-desktop")["DEBIAN/control"]
    depends = next(
        line.split(":", 1)[1] for line in control.splitlines() if line.startswith("Depends:")
    )
    for dependency in ("portlin-runtime", "python3-gi", "gir1.2-gtk-3.0"):
        assert dependency in depends


def test_desktop_ships_the_software_app_and_its_menu_entry():
    files = package.text_files("portlin-desktop")
    assert files["usr/bin/portlin-software"].startswith("#!/usr/bin/env python3")
    assert "usr/bin/portlin-software" in package.executable_paths("portlin-desktop")
    assert "usr/share/applications/portlin-software.desktop" in files


def test_desktop_depends_on_what_the_software_app_elevates_through():
    # pkexec is a separate package from polkitd in trixie, and it is the
    # whole of how the app acts as root. Without it the window opens, lists
    # everything, and installs none of it.
    control = package.text_files("portlin-desktop")["DEBIAN/control"]
    depends = [d.strip() for d in control.split("Depends: ")[1].splitlines()[0].split(",")]
    assert "pkexec" in depends


def test_desktop_recommends_an_authentication_agent():
    # Recommends rather than Depends: any polkit agent draws the prompt, and
    # someone running a different one should not have to remove this package.
    control = package.text_files("portlin-desktop")["DEBIAN/control"]
    assert "mate-polkit" in control.split("Recommends: ")[1].splitlines()[0]


def test_a_minimal_stick_gets_the_installer_without_the_window():
    # portlin-runtime goes onto a stick with no X at all, so the GTK half
    # has to stay in portlin-desktop.
    runtime = package.text_files("portlin-runtime")
    assert "usr/bin/portlin-install" in runtime
    assert "usr/bin/portlin-software" not in runtime


def test_desktop_ships_the_caffeine_applet_and_both_its_entries():
    files = package.text_files("portlin-desktop")
    assert files["usr/bin/portlin-caffeine"].startswith("#!/usr/bin/env python3")
    assert "usr/share/applications/portlin-caffeine.desktop" in files
    assert "etc/xdg/autostart/portlin-caffeine.desktop" in files
    assert "usr/bin/portlin-caffeine" in package.executable_paths("portlin-desktop")


def test_desktop_ships_both_states_of_the_caffeine_icon():
    # Two files, because the panel icon is the only thing that says whether
    # the machine is being kept awake. One of them missing is a switch with no
    # indicator on it.
    icons = package.binary_files("portlin-desktop")
    assert "usr/share/portlin/caffeine-on.svg" in icons
    assert "usr/share/portlin/caffeine-off.svg" in icons


def test_the_autostart_entry_is_a_conffile():
    # It is how someone stops the applet starting at login: untick it in
    # Session and Startup, or edit the file. Without this member dpkg treats
    # /etc/xdg/autostart/portlin-caffeine.desktop as an ordinary package file
    # and puts it back, enabled, on the next upgrade.
    conffiles = package.text_files("portlin-desktop")["DEBIAN/conffiles"]
    assert "/etc/xdg/autostart/portlin-caffeine.desktop" in conffiles.splitlines()


def test_desktop_depends_on_what_the_caffeine_applet_needs():
    # xset comes from x11-xserver-utils and systemd-inhibit from systemd.
    # Neither is derivable from the file list, and each is a layer of the
    # inhibition: without them the applet starts, looks right, and lets the
    # machine sleep.
    control = package.text_files("portlin-desktop")["DEBIAN/control"]
    depends = next(
        line.split(":", 1)[1] for line in control.splitlines() if line.startswith("Depends:")
    )
    for dependency in ("x11-xserver-utils", "systemd"):
        assert dependency in depends


def test_desktop_depends_on_the_icon_theme_its_own_theme_inherits():
    # The Portlin icon theme carries one icon and inherits the rest. Naming it
    # in xsettings makes it the theme for the entire desktop, so if the theme
    # it inherits from is not installed the result is not "the stock icons" --
    # it is one menu button and blank space where every other icon was. Not
    # derivable from the file list: the dependency is a line inside a data
    # file this package ships.
    control = package.text_files("portlin-desktop")["DEBIAN/control"]
    depends = next(
        line.split(":", 1)[1] for line in control.splitlines() if line.startswith("Depends:")
    )
    index = package.text_files("portlin-desktop")[
        f"{package.ICON_THEME_DIR}/index.theme"
    ]
    inherits = next(
        line.removeprefix("Inherits=") for line in index.splitlines()
        if line.startswith("Inherits=")
    )
    # hicolor is the spec's terminal fallback and comes from hicolor-icon-theme,
    # which every GTK stack already pulls; the themes named ahead of it are the
    # ones that have to be asked for.
    for theme in [name for name in inherits.split(",") if name != "hicolor"]:
        assert f"{theme.lower()}-icon-theme" in depends, theme


def test_a_minimal_stick_never_pulls_gtk_for_the_about_dialog():
    # portlin-runtime is what a --minimal, headless stick installs. Putting the
    # dialog there would drag GTK and its dependencies onto a system with no X.
    assert "usr/bin/portlin-about" not in package.text_files("portlin-runtime")
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    assert "python3-gi" not in control


# The dependency named on the left is not listed in packages.py, but is pulled
# in by the one on the right, which is. Kept explicit rather than resolved by
# asking apt, because these tests run on machines with no Debian archive.
GUARANTEED_TRANSITIVELY = {
    "cryptsetup-bin": "cryptsetup",
    # systemd-timesyncd is versioned-depends on systemd, so a stick that
    # keeps its clock also has systemd-inhibit.
    "systemd": "systemd-timesyncd",
}


def _declared_depends(name: str) -> list[str]:
    control = package.text_files(name)["DEBIAN/control"]
    line = next(
        (line for line in control.splitlines() if line.startswith("Depends:")), ""
    )
    return [dep.strip() for dep in line.partition(":")[2].split(",") if dep.strip()]


@pytest.mark.parametrize("name", package.PACKAGES)
def test_every_dependency_is_already_in_the_rootfs(name):
    """write installs these .debs into a chroot with no network.

    install.py opens that chroot with network=False, on purpose: a write is
    meant to work from a cached rootfs on a machine that is offline. So apt has
    no archive to fetch from and nothing to fall back on, and a Depends naming
    anything the rootfs does not already carry cannot be satisfied. It does not
    fail quietly either -- one unsatisfiable dependency aborts the single
    transaction that installs all three packages, so the stick is written with
    none of portlin's own files on it.

    The rootfs is whatever `build` installed, which is packages.resolve(). That
    is the only thing a Depends here may name, besides portlin's own packages.
    """
    available = set(packages.resolve()) | set(package.PACKAGES)
    available |= set(GUARANTEED_TRANSITIVELY)
    missing = [dep for dep in _declared_depends(name) if dep not in available]
    assert not missing, (
        f"{name} depends on {missing}, which `build` never installs into the "
        f"rootfs -- the offline apt transaction in write cannot satisfy it"
    )


def test_packages_installed_on_a_minimal_stick_need_no_desktop():
    """portlin-runtime goes onto a --minimal stick, which has no desktop group.

    Its dependencies therefore have to be satisfiable from the minimal groups
    alone. portlin-desktop is exempt: write leaves it out entirely when the
    rootfs has no desktop.
    """
    minimal = set(packages.resolve(packages.MINIMAL_GROUPS)) | set(package.PACKAGES)
    minimal |= set(GUARANTEED_TRANSITIVELY)
    for name in ("portlin-archive-keyring", "portlin-runtime"):
        missing = [dep for dep in _declared_depends(name) if dep not in minimal]
        assert not missing, f"{name} depends on {missing}, absent from a --minimal stick"
