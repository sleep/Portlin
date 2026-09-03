"""Descriptions of the Debian packages portlin installs onto a stick.

Portlin's contribution to a stick is split in two. Anything whose failure means
a drive that will not boot or will not unlock is written directly by ``write``
and is frozen for the life of that drive. Everything else lives in these
packages, so a signed archive can move it forward later.

Kept pure, as a mapping from destination path to content, so the whole package
layout is assertable by unit tests on a machine with no dpkg and no root. The
code that turns these into .deb files lives in install.py.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

RESOURCES = Path(__file__).parent / "resources"

# A tilde sorts below the empty string in Debian version comparison, so a
# package built here from a working tree is always superseded by the signed
# build of the same version from the archive. An unsigned local build can
# therefore never present itself as a release.
LOCAL_SUFFIX = "~local"

ARCHIVE_URI = "https://sleep.github.io/Portlin/apt"
ARCHIVE_SUITE = "portlin"
KEYRING_PATH = "/usr/share/keyrings/portlin-archive-keyring.gpg"

# Build order: the keyring first, because the others are installed alongside it
# in one apt transaction that has to resolve.
PACKAGES = ["portlin-archive-keyring", "portlin-runtime", "portlin-desktop"]

TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt", "portlin-install"]

# Python modules the tools import from /usr/lib/portlin rather than carrying
# a copy of. catalog.py is here rather than inside portlin-install because
# portlin-software reads it too, and a copy in each would be the copy that
# drifts.
SHARED_MODULES = ["devices.py", "catalog.py", "hostinfo.py"]

# The polkit action the Software app elevates through. It ships in
# portlin-runtime, beside the program its exec.path annotation names, so the
# two cannot end up in different packages naming different paths.
POLKIT_ACTIONS = {
    "org.portlin.install.policy": "usr/share/polkit-1/actions/org.portlin.install.policy",
}

# The graphical half, kept out of TOOLS on purpose: portlin-runtime is what
# a --minimal, headless stick installs, and putting a GTK program there would
# drag X libraries onto a system that has no X.
DESKTOP_TOOLS = [
    "portlin-about",
    "portlin-caffeine",
    "portlin-software",
    # The only one of these that is not a GTK program. It is here rather than
    # in TOOLS because it is useless without a panel to print into, and a
    # --minimal stick has none.
    "portlin-stats",
]

# The panel id genmon is given, which is also the id in the filename genmon
# looks its configuration up under. One constant, because a layout naming one
# id and a file named for another is a panel item that runs nothing.
STATS_PLUGIN_ID = 5
MENU_ENTRIES = {
    "portlin-about.desktop": "usr/share/applications/portlin-about.desktop",
    "portlin-caffeine.desktop": "usr/share/applications/portlin-caffeine.desktop",
    "portlin-software.desktop": "usr/share/applications/portlin-software.desktop",
}

# X-Xfce-Toplevel (see portlin-about.desktop) only keeps About Portlin out of
# a category submenu; it says nothing about where among the other root-level
# entries it lands. About Xfce sits at the very bottom of the stock menu not
# because of a category but because xfce-applications.menu names it by
# Filename in its own Layout, and the only way to sit next to a name placed
# that way is the same treatment. This ships under
# /etc/xdg/menus/xfce-applications-merged, the directory <DefaultMergeDirs/>
# reads for a menu file named xfce-applications.menu, and is a conffile like
# the rest of what portlin puts under /etc: see THEME_FILES.
MENU_LAYOUT_ENTRIES = {
    "portlin-about.menu": "etc/xdg/menus/xfce-applications-merged/portlin-about.menu",
}

# Entries that start a program at login rather than from the menu. Kept apart
# from MENU_ENTRIES because the destination is under /etc, which makes it a
# conffile: the file is how someone turns the applet off for good, by
# unticking it in Session and Startup or editing it, and an ordinary package
# file would be put back, enabled, by the next upgrade.
AUTOSTART_ENTRIES = {
    "portlin-caffeine-autostart.desktop": "etc/xdg/autostart/portlin-caffeine.desktop",
}

# The two states of the caffeine applet's panel icon. Both ship, because the
# icon is the only thing that says whether the machine is being kept awake.
CAFFEINE_ICONS = {
    f"usr/share/portlin/caffeine-{state}.svg": f"caffeine-{state}.svg"
    for state in ("on", "off")
}

# The icon theme portlin ships. It carried exactly one icon until the panel
# layout landed: the applications menu took its button icon from a name
# compiled into the plugin, and answering to that name from a theme portlin
# owns was the only way to reach it. whiskermenu takes its icon from an
# ordinary panel property instead, so nothing asks for that name any more and
# the theme is now an index with no icons under it, inheriting the stock set
# and changing nothing. Retiring it is its own change: IconThemeName is spelled
# in four shipped files and folding that into a panel rework makes one commit
# nobody can bisect.
ICON_THEME = "Portlin"
ICON_THEME_DIR = f"usr/share/icons/{ICON_THEME}"

# portlin's own icon, by the name desktop entries and the panel both ask for.
# In hicolor because that is the icon spec's terminal fallback: every theme
# ends there, so an entry saying Icon=portlin resolves under whichever icon
# theme is in force, including one the user picks later in Settings.
#
# This one name is why portlin no longer ships an icon theme of its own. The
# applications menu took its button icon from a name compiled into the plugin,
# and answering to that name from a one-icon theme was the only way to reach
# it; whiskermenu takes its icon from an ordinary panel property, which this
# name is now written into.
APP_ICON = "portlin"
HICOLOR_APP_ICON = f"usr/share/icons/hicolor/scalable/apps/{APP_ICON}.svg"

# The mark, by destination. A copy rather than a symlink into portlin-runtime's
# logo.svg for the same reason the default backdrop is a copy: 614 bytes,
# against a dangling link the day anything moves.
MARK_ICONS = {
    HICOLOR_APP_ICON: "logo.svg",
}

# The icon theme's index. Not in THEME_FILES because that dict is what lands
# under /etc, which is what the conffiles member is derived from; this one
# lives under /usr/share and is an ordinary package file.
ICON_THEME_FILES = {
    f"{ICON_THEME_DIR}/index.theme": "icon-theme-index.theme",
}

# dpkg lets exactly one installed package own a path, so portlin's system
# defaults cannot live at the canonical /etc/xdg locations: xfce4-settings
# already ships xsettings.xml there, and unpacking over it aborts the whole
# apt transaction. Everything under /etc/xdg is looked up through
# XDG_CONFIG_DIRS, so the defaults go in a directory only portlin ships and
# the session snippet below puts that directory on the search path.
XDG_OVERLAY = "etc/xdg/xdg-portlin"

# Sourced before the session starts, and the only thing that makes XDG_OVERLAY
# more than a directory of unread files.
XSESSION_SNIPPET = "etc/X11/Xsession.d/40portlin-desktop_xdg-config-dirs"

# Keyed by path relative to a config root rather than by full destination, so
# these can only ever be written inside XDG_OVERLAY.
XDG_DEFAULTS = {
    "xfce4/xfconf/xfce-perchannel-xml/xsettings.xml": "xsettings.xml",
    "xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml": "xfwm4.xml",
    "xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml": "xfce4-panel.xml",
    "gtk-3.0/settings.ini": "gtk-3.0-settings.ini",
    "gtk-4.0/settings.ini": "gtk-4.0-settings.ini",
    "xfce4/terminal/terminalrc": "terminalrc",
    f"xfce4/panel/genmon-{STATS_PLUGIN_ID}.rc": "genmon-stats.rc",
}

# Portlin ships the panel layout in full rather than layering properties onto
# Debian's. It has to: plugin-ids is a single array property and xfconf has no
# append, so adding the readout means restating the list, and restating the
# list is owning the layout.
#
# Owning it removes more than it costs. The version label used to sit at the
# numeric path of whichever plugin Debian made plugin-1, so it depended on
# Debian never renumbering; now portlin assigns the ids and the dependency is
# gone rather than merely checked.
PANEL_DEFAULTS_FILE = f"{XDG_OVERLAY}/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"

# Substituted into PANEL_DEFAULTS_FILE after it is read, so the button label
# can never drift from the version everything else on the stick reports.
PANEL_VERSION_PLACEHOLDER = "@PORTLIN_VERSION@"

# Every file portlin-desktop ships under /etc, by destination. The greeter
# configuration stays outside the overlay because it is not an XDG path:
# lightdm reads its own directory, and the greeter runs before any session has
# set XDG_CONFIG_DIRS.
THEME_FILES = {
    **{f"{XDG_OVERLAY}/{relative}": source for relative, source in XDG_DEFAULTS.items()},
    "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf": "50-portlin.conf",
    XSESSION_SNIPPET: "xdg-config-dirs.sh",
}

KEYRING_FILE = RESOURCES / "keyring" / "portlin-archive-keyring.gpg"

# xfdesktop has no system-wide default-wallpaper setting to write. Its backdrop
# is an xfconf property keyed by the monitor's name -- monitorHDMI-1,
# monitoreDP-1, monitorVirtual-1 -- and that name comes from the hardware the
# stick is eventually plugged into, so no default shipped at write time can
# spell it. What xfdesktop draws when no such property exists is a single path
# compiled into the binary, which makes taking that path over the only way to
# give a fresh account a wallpaper. Asserted against the installed binary by
# verify-image.sh, because a Debian that rebuilds xfdesktop with a different
# default would otherwise take the wallpaper away silently.
DEFAULT_BACKDROP = "usr/share/backgrounds/xfce/xfce-x.svg"

# Where the displaced file goes. dpkg needs somewhere to put the file it is
# moving aside, and .distrib is the suffix dpkg-divert's own documentation
# uses for taking over a distribution's copy of a path.
DIVERTED_BACKDROP = f"/{DEFAULT_BACKDROP}.distrib"

# The render that becomes that default. xfdesktop scales whatever it finds
# there to each monitor, so one size has to stand in for all of them until
# someone picks another in Settings, and 1920x1080 is the one the largest
# number of panels display without resampling at all.
DEFAULT_BACKDROP_SIZE = "1920x1080"

WALLPAPER_SIZES = [
    "1365x768",
    "1920x1080",
    "2560x1440",
    "3840x2160",
    "5120x2880",
    "7680x4320",
]


def _keyring_is_real() -> bool:
    """Whether a real signing key has been committed at KEYRING_FILE.

    The repository carries a zero-byte placeholder there until the archive's
    signing key exists, which is a separate, later decision about custody of a
    long-lived secret. Keying this off the file itself, rather than a flag
    someone has to remember to flip, means the keyring package and its apt
    source start shipping for real the moment a key lands, with no other code
    change, and can never ship a source pointed at an archive that does not
    exist yet or a key with nothing in it.
    """
    return KEYRING_FILE.exists() and KEYRING_FILE.stat().st_size > 0


def local_version() -> str:
    return f"{__version__}{LOCAL_SUFFIX}"


def render_control(
    *,
    name: str,
    version: str,
    description: str,
    depends: list[str],
    recommends: list[str] | None = None,
) -> str:
    lines = [
        f"Package: {name}",
        f"Version: {version}",
        "Section: utils",
        "Priority: optional",
        "Architecture: all",
        "Maintainer: The portlin authors <portlin@localhost>",
    ]
    if depends:
        lines.append(f"Depends: {', '.join(depends)}")
    if recommends:
        lines.append(f"Recommends: {', '.join(recommends)}")
    lines += [
        f"Description: {description}",
        " Installed by portlin onto the stick it writes. Portlin's own files",
        " are split so that the ones which cannot break a boot are carried by",
        " packages and can be updated from the portlin archive.",
        "",
    ]
    return "\n".join(lines)


def render_sources_entry() -> str:
    """Render the deb822 apt source for the portlin archive.

    Architectures is pinned rather than left to apt. Every portlin package is
    Architecture: all, so the archive publishes only binary-all; without the
    pin, apt on amd64 requests binary-amd64/Packages and reports a fetch
    failure on every apt update.
    """
    return "\n".join(
        [
            "Types: deb",
            f"URIs: {ARCHIVE_URI}",
            f"Suites: {ARCHIVE_SUITE}",
            "Components: main",
            "Architectures: all",
            f"Signed-By: {KEYRING_PATH}",
            "",
        ]
    )


# The dpkg actions each half of the diversion has to cover. A failed upgrade
# rolls back to the old portlin-desktop, which is still installed and still
# serving the file, so abort-upgrade belongs with the additions: dropping the
# diversion there would hand the path back to xfdesktop4-data underneath a
# package that is still shipping its own copy of it.
DIVERSION_ACTIONS = {
    "add": ["install", "upgrade", "abort-upgrade"],
    "remove": ["remove", "abort-install", "disappear"],
}


def render_diversion_script(action: str) -> str:
    """Render the maintainer script that adds or removes the backdrop diversion.

    Both halves are rendered by one function, from one path constant, because
    dpkg matches a diversion on the whole triple of owning package, divert-to
    path and original path. Written out twice by hand, a later edit to one
    could leave the two naming different paths, and the mismatch is silent:
    dpkg-divert finds nothing to remove, reports success, and xfdesktop's own
    file stays displaced for the life of the machine.
    """
    return "\n".join(
        [
            "#!/bin/sh",
            "# Generated by portlin. Part of portlin-desktop.",
            "#",
            "# The path below is the backdrop xfdesktop draws when no xfconf property",
            "# matches the monitor, and it belongs to xfdesktop4-data. A diversion is",
            "# how one package may serve a file another package owns: dpkg moves the",
            "# original aside and keeps it there, including when xfdesktop4-data is",
            "# upgraded, which is exactly what overwriting the file would not survive.",
            "set -e",
            "",
            'case "$1" in',
            f"    {'|'.join(DIVERSION_ACTIONS[action])})",
            f"        dpkg-divert --package portlin-desktop --{action} --rename \\",
            f"            --divert {DIVERTED_BACKDROP} \\",
            f"            /{DEFAULT_BACKDROP}",
            "        ;;",
            "esac",
            "",
        ]
    )


def text_files(package: str, *, version: str | None = None) -> dict[str, str]:
    """Text members of a package, keyed by path relative to the package root.

    ``version`` stamps the ``DEBIAN/control`` file, so a CI release build can
    render the same tree as ``write`` does but with a real version instead of
    the ``~local`` default. It is threaded straight into ``render_control``
    rather than substituted into rendered text afterward, so the only file
    that can ever carry a version string is the one that is supposed to.
    """
    version = version or local_version()
    if package == "portlin-archive-keyring":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin archive signing key and apt source",
                depends=[],
            ),
        }
        if _keyring_is_real():
            files["etc/apt/sources.list.d/portlin.sources"] = render_sources_entry()
    elif package == "portlin-runtime":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin tools and the shared device module",
                depends=[
                    "portlin-archive-keyring",
                    "python3",
                    "cloud-guest-utils",
                    "cryptsetup-bin",
                    # portlin-install's own two: every download it makes is a
                    # curl, and the driver scan reads lspci. Neither shows in
                    # the file list, and without either the tool starts, lists
                    # the catalog, and then fails at the first useful thing.
                    "curl",
                    "pciutils",
                ],
                recommends=["portlin-desktop"],
            ),
        }
        for module in SHARED_MODULES:
            files[f"usr/lib/portlin/{module}"] = (
                RESOURCES / "runtime" / module
            ).read_text()
        for tool in TOOLS:
            files[f"usr/bin/{tool}"] = (RESOURCES / "runtime" / tool).read_text()
        for source, destination in POLKIT_ACTIONS.items():
            files[destination] = (RESOURCES / "runtime" / source).read_text()
    elif package == "portlin-desktop":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop theme, wallpapers and About dialog",
                # None of this is derivable from the file list: the About dialog
                # is GTK, it draws the logo portlin-runtime installs, it shells
                # out to portlin-info, and rasterising that SVG logo needs the
                # gdk-pixbuf loader librsvg2-common carries.
                # The caffeine applet adds the last two: it reads and restores
                # the X blanking settings with xset, and takes its logind lock
                # by running systemd-inhibit. Neither shows in the file list,
                # and without either the applet still starts, still looks
                # right, and still lets the machine go to sleep.
                depends=[
                    "portlin-runtime",
                    "python3-gi",
                    "gir1.2-gtk-3.0",
                    "librsvg2-common",
                    "x11-xserver-utils",
                    "systemd",
                    # The icon theme portlin's own one-icon theme inherits.
                    # Without it, naming Portlin in xsettings does not fall
                    # back to the stock icons -- it leaves the desktop with
                    # the menu button and nothing else.
                    "adwaita-icon-theme",
                    # The two panel plugins the shipped layout names. Not
                    # derivable from the file list: they are named inside a
                    # data file this package ships, which is the same reason
                    # the icon theme above is here.
                    "xfce4-genmon-plugin",
                    "xfce4-whiskermenu-plugin",
                    # What the Software app becomes root through. It runs
                    # portlin-install and nothing else, so without pkexec the
                    # window opens, lists everything, and installs none of it.
                    "pkexec",
                ],
                # The agent that draws pkexec's password dialog. A Recommends
                # rather than a Depends because any polkit agent will do, and
                # someone running a different one should not have to remove
                # portlin-desktop to do it. Without one, pkexec exits 127 and
                # the app says that an agent is what is missing.
                recommends=["mate-polkit"],
            ),
        }
        for destination, source in {**THEME_FILES, **ICON_THEME_FILES}.items():
            files[destination] = (RESOURCES / "runtime" / "theme" / source).read_text()
        files[PANEL_DEFAULTS_FILE] = files[PANEL_DEFAULTS_FILE].replace(
            PANEL_VERSION_PLACEHOLDER, __version__
        )
        for tool in DESKTOP_TOOLS:
            files[f"usr/bin/{tool}"] = (RESOURCES / "runtime" / tool).read_text()
        for source, destination in {
            **MENU_ENTRIES,
            **AUTOSTART_ENTRIES,
            **MENU_LAYOUT_ENTRIES,
        }.items():
            files[destination] = (RESOURCES / "runtime" / source).read_text()
        for action, script in (("add", "preinst"), ("remove", "postrm")):
            files[f"DEBIAN/{script}"] = render_diversion_script(action)
    else:
        raise KeyError(package)

    # dpkg only preserves a locally-edited file across an upgrade, or stays
    # quiet about an untouched one, for paths it has been told are conffiles.
    # Without this member every /etc file here is an ordinary package file,
    # silently overwritten on every upgrade regardless of local edits. Derived
    # from the files this package actually ships -- text and binary alike --
    # rather than a maintained list, so it cannot drift when a file is added
    # or moved.
    conffiles = sorted(
        path for path in (*files, *binary_files(package)) if path.startswith("etc/")
    )
    if conffiles:
        files["DEBIAN/conffiles"] = "".join(f"/{path}\n" for path in conffiles)
    return files


def binary_files(package: str) -> dict[str, Path]:
    """Binary members, keyed by path relative to the package root."""
    if package == "portlin-archive-keyring":
        # A dearmoured public key, so it is bytes rather than text. Ships only
        # once KEYRING_FILE holds a real key; see _keyring_is_real.
        if not _keyring_is_real():
            return {}
        return {KEYRING_PATH.lstrip("/"): KEYRING_FILE}
    if package == "portlin-runtime":
        return {"usr/share/portlin/logo.svg": RESOURCES / "runtime" / "logo.svg"}
    if package == "portlin-desktop":
        renders = {
            f"usr/share/backgrounds/portlin/portlin-{size}.png":
                RESOURCES / "wallpapers" / f"portlin-{size}.png"
            for size in WALLPAPER_SIZES
        }
        # A second copy of one render rather than a symlink into the directory
        # above. It costs 636 KB, and it buys a default backdrop that cannot
        # be turned into a dangling link by anything that reorganises the
        # renders, which is the one file on the stick whose absence leaves a
        # fresh account with no wallpaper at all.
        icons = {
            destination: RESOURCES / "runtime" / source
            for destination, source in {**CAFFEINE_ICONS, **MARK_ICONS}.items()
        }
        return renders | icons | {
            DEFAULT_BACKDROP:
                RESOURCES / "wallpapers" / f"portlin-{DEFAULT_BACKDROP_SIZE}.png"
        }
    raise KeyError(package)


def executable_paths(package: str) -> set[str]:
    if package == "portlin-runtime":
        return {f"usr/bin/{tool}" for tool in TOOLS}
    if package == "portlin-desktop":
        return {"DEBIAN/preinst", "DEBIAN/postrm"} | {
            f"usr/bin/{tool}" for tool in DESKTOP_TOOLS
        }
    return set()
