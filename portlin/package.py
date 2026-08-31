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

TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt"]

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
    "xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml": "xfce4-desktop.xml",
    "gtk-3.0/settings.ini": "gtk-3.0-settings.ini",
    "gtk-4.0/settings.ini": "gtk-4.0-settings.ini",
    "xfce4/terminal/terminalrc": "terminalrc",
}

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
                ],
                recommends=["portlin-desktop"],
            ),
            "usr/lib/portlin/devices.py": (RESOURCES / "runtime" / "devices.py").read_text(),
        }
        for tool in TOOLS:
            files[f"usr/bin/{tool}"] = (RESOURCES / "runtime" / tool).read_text()
    elif package == "portlin-desktop":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop theme and wallpapers",
                depends=[],
            ),
        }
        for destination, source in THEME_FILES.items():
            files[destination] = (RESOURCES / "runtime" / "theme" / source).read_text()
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
        return {
            f"usr/share/backgrounds/portlin/portlin-{size}.png":
                RESOURCES / "wallpapers" / f"portlin-{size}.png"
            for size in WALLPAPER_SIZES
        }
    raise KeyError(package)


def executable_paths(package: str) -> set[str]:
    if package == "portlin-runtime":
        return {f"usr/bin/{tool}" for tool in TOOLS}
    return set()
