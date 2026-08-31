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
PACKAGES = ["portlin-archive-keyring", "portlin-runtime", "portlin-wallpapers"]

TOOLS = ["portlin-info", "portlin-expand", "portlin-encrypt"]

THEME_FILES = {
    "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml": "xsettings.xml",
    "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml": "xfwm4.xml",
    "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml": "xfce4-desktop.xml",
    "etc/xdg/gtk-3.0/settings.ini": "gtk-3.0-settings.ini",
    "etc/xdg/gtk-4.0/settings.ini": "gtk-4.0-settings.ini",
    "etc/xdg/xfce4/terminal/terminalrc": "terminalrc",
    "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf": "50-portlin.conf",
}

WALLPAPER_SIZES = [
    "1366x768",
    "1920x1080",
    "2560x1440",
    "3840x2160",
    "5120x2880",
    "7680x4320",
]


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


def text_files(package: str) -> dict[str, str]:
    """Text members of a package, keyed by path relative to the package root."""
    version = local_version()
    if package == "portlin-archive-keyring":
        return {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin archive signing key and apt source",
                depends=[],
            ),
            "etc/apt/sources.list.d/portlin.sources": render_sources_entry(),
        }
    if package == "portlin-runtime":
        files = {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop integration and tools",
                depends=[
                    "portlin-archive-keyring",
                    "python3",
                    "cloud-guest-utils",
                    "cryptsetup-bin",
                ],
                recommends=["portlin-wallpapers"],
            ),
            "usr/lib/portlin/devices.py": (RESOURCES / "runtime" / "devices.py").read_text(),
        }
        for tool in TOOLS:
            files[f"usr/bin/{tool}"] = (RESOURCES / "runtime" / tool).read_text()
        for destination, source in THEME_FILES.items():
            files[destination] = (RESOURCES / "runtime" / "theme" / source).read_text()
        return files
    if package == "portlin-wallpapers":
        return {
            "DEBIAN/control": render_control(
                name=package,
                version=version,
                description="Portlin desktop wallpapers",
                depends=[],
            ),
        }
    raise KeyError(package)


def binary_files(package: str) -> dict[str, Path]:
    """Binary members, keyed by path relative to the package root."""
    if package == "portlin-archive-keyring":
        # A dearmoured public key, so it is bytes rather than text.
        return {
            KEYRING_PATH.lstrip("/"):
                RESOURCES / "keyring" / "portlin-archive-keyring.gpg"
        }
    if package == "portlin-runtime":
        return {"usr/share/portlin/logo.svg": RESOURCES / "runtime" / "logo.svg"}
    if package == "portlin-wallpapers":
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
