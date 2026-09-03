# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""The software catalog: everything the Software app can install.

Pure data, and pure helpers about that data. This module never runs a
command. portlin-install turns an entry into the commands that install it,
and portlin-software draws entries and hands their ids to portlin-install;
both read this one file from /usr/lib/portlin, so there is exactly one place
where a vendor's repository, key and package name are spelled.

Each entry carries a ``kind``, which is the whole of how it is installed:

apt          packages from the Debian archive, possibly from a component the
             stick was built without
apt-repo     a vendor apt repository: a signing key, a sources entry, then apt
deb-url      a .deb the vendor publishes at a fixed URL
github-deb   a .deb attached to the latest release of a GitHub repository
tarball-opt  a tarball unpacked under /opt, with a generated menu entry
user-script  the vendor's installer script, run as the user, into their home

The first five need root and go through pkexec. user-script must never run as
root, because it writes under a home directory.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path

KINDS = ("apt", "apt-repo", "deb-url", "github-deb", "tarball-opt", "user-script")
PRIVILEGED_KINDS = frozenset({"apt", "apt-repo", "deb-url", "github-deb", "tarball-opt"})
USER_KINDS = frozenset({"user-script"})

# Display order. Drivers last: it is the page that talks about this machine
# rather than about programs, and it is drawn differently.
CATEGORIES = (
    "Browsers",
    "Communication",
    "Media",
    "Office and graphics",
    "Security and privacy",
    "Networking",
    "Development",
    "AI tools",
    "Remote access",
    "System tools",
    "Drivers",
)

RESOLVERS = ("nvidia-detect",)

# Debian policy, section 5.6.1: lowercase letters, digits, plus, minus, dot;
# at least two characters, starting with a letter or digit.
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
ENTRY_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KEYRING_DIRS = ("/usr/share/keyrings/", "/etc/apt/keyrings/")
SOURCES_DIR = "/etc/apt/sources.list.d/"
DASHES = "–—"


@dataclass(frozen=True)
class Repo:
    """A vendor apt repository: where its key is, and what its sources entry says.

    Exactly one of ``sources_line`` and ``sources_url`` is set. The line is
    the one-line-style entry written verbatim; the URL is a sources file the
    vendor serves, fetched into place as is. ``{codename}`` anywhere in a URL
    is replaced with the stick's VERSION_CODENAME at install time, for vendors
    who publish one pocket per Debian release.
    """

    key_url: str
    keyring_path: str
    sources_path: str
    sources_line: str | None = None
    sources_url: str | None = None


@dataclass(frozen=True)
class Check:
    """How to tell whether an entry is installed.

    ``dpkg`` names packages, any one of which being installed counts.
    ``path`` names files, any one of which existing counts; a leading ``~/``
    is the invoking user's home, never root's.
    """

    kind: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    summary: str
    category: str
    kind: str
    check: Check
    homepage: str
    packages: tuple[str, ...] = ()
    repo: Repo | None = None
    url: str | None = None
    github_repo: str | None = None
    asset_pattern: str | None = None
    opt_dir: str | None = None
    launcher: str | None = None
    icon: str | None = None
    needs_components: tuple[str, ...] = ()
    resolver: str | None = None
    debconf: tuple[str, ...] = ()
    add_groups: tuple[str, ...] = ()
    post_install: tuple[tuple[str, ...], ...] = ()
    remove_paths: tuple[str, ...] = ()
    warning: str | None = None
    notes: str | None = None


def dpkg(*names: str) -> Check:
    return Check("dpkg", names)


def path(*paths: str) -> Check:
    return Check("path", paths)


DKMS_WARNING = (
    "The driver is built as a kernel module on this stick, is not signed, "
    "and will not load on a machine with Secure Boot turned on."
)

NVIDIA_WARNING = (
    "This installs NVIDIA's proprietary driver for the card in this machine "
    "and turns off the open nouveau driver. On machines with no NVIDIA card "
    "the open drivers keep working. On a machine with an NVIDIA card this "
    "driver does not support, the desktop may not start. If that happens, "
    "press Ctrl+Alt+F2, log in, and run: sudo portlin-install remove "
    "nvidia-driver. " + DKMS_WARNING
)

BROADCOM_WARNING = (
    "Only for Broadcom chips the built-in drivers do not handle. Built as a "
    "kernel module on this stick, unsigned, so it will not load with Secure "
    "Boot on."
)

VENDOR_SCRIPT_WARNING = (
    "Runs the vendor's installer script as you, under your home directory. "
    "Portlin downloads it first and runs the file it downloaded."
)

ENTRIES: tuple[Entry, ...] = (
    # -- Browsers ----------------------------------------------------------
    Entry(
        id="chromium",
        name="Chromium",
        summary="The open-source browser Chrome is built from",
        category="Browsers",
        kind="apt",
        packages=("chromium",),
        check=dpkg("chromium"),
        homepage="https://www.chromium.org/",
    ),
    Entry(
        id="chrome",
        name="Google Chrome",
        summary="Google's browser, from Google's own package",
        category="Browsers",
        kind="deb-url",
        url="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
        check=dpkg("google-chrome-stable"),
        homepage="https://www.google.com/chrome/",
        notes="The package adds Google's apt repository, so Chrome updates with the system.",
    ),
    Entry(
        id="brave",
        name="Brave",
        summary="A Chromium-based browser with a built-in ad blocker",
        category="Browsers",
        kind="apt-repo",
        packages=("brave-browser",),
        repo=Repo(
            key_url="https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg",
            keyring_path="/usr/share/keyrings/brave-browser-archive-keyring.gpg",
            sources_url="https://brave-browser-apt-release.s3.brave.com/brave-browser.sources",
            sources_path="/etc/apt/sources.list.d/brave-browser-release.sources",
        ),
        check=dpkg("brave-browser"),
        homepage="https://brave.com/",
    ),
    Entry(
        id="tor-browser",
        name="Tor Browser",
        summary="Browse through the Tor network, via Debian's launcher",
        category="Browsers",
        kind="apt",
        packages=("torbrowser-launcher",),
        needs_components=("contrib",),
        check=dpkg("torbrowser-launcher"),
        homepage="https://www.torproject.org/",
        notes="The launcher downloads and verifies Tor Browser itself the first time it runs.",
    ),
    Entry(
        id="palemoon",
        name="Pale Moon",
        summary="An independent browser descended from Firefox",
        category="Browsers",
        kind="tarball-opt",
        url="https://www.palemoon.org/download.php?mirror=us&bits=64&type=linuxgtk3",
        opt_dir="/opt/palemoon",
        launcher="palemoon",
        icon="browser/icons/mozicon128.png",
        check=path("/opt/palemoon/palemoon"),
        homepage="https://www.palemoon.org/",
        notes=(
            "Pale Moon publishes no Debian package, so it is unpacked under /opt and updates "
            "itself."
        ),
    ),
    # -- Communication -----------------------------------------------------
    Entry(
        id="signal",
        name="Signal",
        summary="Private messaging, from Signal's apt repository",
        category="Communication",
        kind="apt-repo",
        packages=("signal-desktop",),
        repo=Repo(
            key_url="https://updates.signal.org/desktop/apt/keys.asc",
            keyring_path="/usr/share/keyrings/signal-desktop-keyring.asc",
            sources_line=(
                "deb [arch=amd64 signed-by=/usr/share/keyrings/signal-desktop-keyring.asc] "
                "https://updates.signal.org/desktop/apt xenial main"
            ),
            sources_path="/etc/apt/sources.list.d/signal-xenial.list",
        ),
        check=dpkg("signal-desktop"),
        homepage="https://signal.org/",
    ),
    Entry(
        id="discord",
        name="Discord",
        summary="Voice and text chat, from Discord's own package",
        category="Communication",
        kind="deb-url",
        url="https://discord.com/api/download?platform=linux&format=deb",
        check=dpkg("discord"),
        homepage="https://discord.com/",
    ),
    Entry(
        id="telegram",
        name="Telegram",
        summary="Telegram Desktop, from Telegram's own build",
        category="Communication",
        kind="tarball-opt",
        url="https://telegram.org/dl/desktop/linux",
        opt_dir="/opt/telegram",
        launcher="Telegram",
        check=path("/opt/telegram/Telegram"),
        homepage="https://desktop.telegram.org/",
        notes=(
            "Debian ships no Telegram Desktop package, so this is Telegram's own build under "
            "/opt. It updates itself."
        ),
    ),
    Entry(
        id="thunderbird",
        name="Thunderbird",
        summary="Mail, calendar and contacts",
        category="Communication",
        kind="apt",
        packages=("thunderbird",),
        check=dpkg("thunderbird"),
        homepage="https://www.thunderbird.net/",
    ),
    # -- Media -------------------------------------------------------------
    Entry(
        id="vlc",
        name="VLC",
        summary="Plays almost any audio or video file",
        category="Media",
        kind="apt",
        packages=("vlc",),
        check=dpkg("vlc"),
        homepage="https://www.videolan.org/vlc/",
    ),
    Entry(
        id="obs-studio",
        name="OBS Studio",
        summary="Screen recording and live streaming",
        category="Media",
        kind="apt",
        packages=("obs-studio",),
        check=dpkg("obs-studio"),
        homepage="https://obsproject.com/",
    ),
    Entry(
        id="audacity",
        name="Audacity",
        summary="Audio recording and editing",
        category="Media",
        kind="apt",
        packages=("audacity",),
        check=dpkg("audacity"),
        homepage="https://www.audacityteam.org/",
    ),
    # -- Office and graphics -----------------------------------------------
    Entry(
        id="libreoffice",
        name="LibreOffice",
        summary="Documents, spreadsheets and presentations",
        category="Office and graphics",
        kind="apt",
        packages=("libreoffice", "libreoffice-gtk3"),
        check=dpkg("libreoffice"),
        homepage="https://www.libreoffice.org/",
    ),
    Entry(
        id="gimp",
        name="GIMP",
        summary="Image editing",
        category="Office and graphics",
        kind="apt",
        packages=("gimp",),
        check=dpkg("gimp"),
        homepage="https://www.gimp.org/",
    ),
    Entry(
        id="inkscape",
        name="Inkscape",
        summary="Vector drawing",
        category="Office and graphics",
        kind="apt",
        packages=("inkscape",),
        check=dpkg("inkscape"),
        homepage="https://inkscape.org/",
    ),
    # -- Security and privacy ----------------------------------------------
    Entry(
        id="keepassxc",
        name="KeePassXC",
        summary="A password manager that keeps its database on this stick",
        category="Security and privacy",
        kind="apt",
        packages=("keepassxc",),
        check=dpkg("keepassxc"),
        homepage="https://keepassxc.org/",
    ),
    Entry(
        id="mullvad",
        name="Mullvad VPN",
        summary="The Mullvad VPN client, from Mullvad's apt repository",
        category="Security and privacy",
        kind="apt-repo",
        packages=("mullvad-vpn",),
        repo=Repo(
            key_url="https://repository.mullvad.net/deb/mullvad-keyring.asc",
            keyring_path="/usr/share/keyrings/mullvad-keyring.asc",
            sources_line=(
                "deb [signed-by=/usr/share/keyrings/mullvad-keyring.asc arch=amd64] "
                "https://repository.mullvad.net/deb/stable stable main"
            ),
            sources_path="/etc/apt/sources.list.d/mullvad.list",
        ),
        check=dpkg("mullvad-vpn"),
        homepage="https://mullvad.net/",
    ),
    Entry(
        id="tailscale",
        name="Tailscale",
        summary="A private network between your own machines",
        category="Security and privacy",
        kind="apt-repo",
        packages=("tailscale",),
        repo=Repo(
            key_url="https://pkgs.tailscale.com/stable/debian/{codename}.noarmor.gpg",
            keyring_path="/usr/share/keyrings/tailscale-archive-keyring.gpg",
            sources_url="https://pkgs.tailscale.com/stable/debian/{codename}.tailscale-keyring.list",
            sources_path="/etc/apt/sources.list.d/tailscale.list",
        ),
        check=dpkg("tailscale"),
        homepage="https://tailscale.com/",
        notes="After installing, run: sudo tailscale up",
    ),
    # -- Networking --------------------------------------------------------
    Entry(
        id="qbittorrent",
        name="qBittorrent",
        summary="A BitTorrent client",
        category="Networking",
        kind="apt",
        packages=("qbittorrent",),
        check=dpkg("qbittorrent"),
        homepage="https://www.qbittorrent.org/",
    ),
    Entry(
        id="deluge",
        name="Deluge",
        summary="A lightweight BitTorrent client",
        category="Networking",
        kind="apt",
        packages=("deluge",),
        check=dpkg("deluge"),
        homepage="https://deluge-torrent.org/",
    ),
    Entry(
        id="syncthing",
        name="Syncthing",
        summary="Keeps folders in sync between your devices",
        category="Networking",
        kind="apt",
        packages=("syncthing",),
        check=dpkg("syncthing"),
        homepage="https://syncthing.net/",
        notes="Start it for your account with: systemctl --user enable --now syncthing",
    ),
    Entry(
        id="filezilla",
        name="FileZilla",
        summary="FTP and SFTP file transfer",
        category="Networking",
        kind="apt",
        packages=("filezilla",),
        check=dpkg("filezilla"),
        homepage="https://filezilla-project.org/",
    ),
    Entry(
        id="wireshark",
        name="Wireshark",
        summary="Network packet capture and analysis",
        category="Networking",
        kind="apt",
        packages=("wireshark",),
        debconf=("wireshark-common wireshark-common/install-setuid boolean true",),
        add_groups=("wireshark",),
        check=dpkg("wireshark"),
        homepage="https://www.wireshark.org/",
        notes=(
            "Your account is added to the wireshark group so capture works without root. Log "
            "out and in for that to take effect."
        ),
    ),
    # -- Development -------------------------------------------------------
    Entry(
        id="vscode",
        name="Visual Studio Code",
        summary="Microsoft's editor, from Microsoft's apt repository",
        category="Development",
        kind="apt-repo",
        packages=("code",),
        repo=Repo(
            key_url="https://packages.microsoft.com/keys/microsoft.asc",
            keyring_path="/usr/share/keyrings/microsoft.asc",
            sources_line=(
                "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.asc] "
                "https://packages.microsoft.com/repos/code stable main"
            ),
            sources_path="/etc/apt/sources.list.d/vscode.list",
        ),
        check=dpkg("code"),
        homepage="https://code.visualstudio.com/",
    ),
    Entry(
        id="zed",
        name="Zed",
        summary="A fast editor, installed under your home directory",
        category="Development",
        kind="user-script",
        url="https://zed.dev/install.sh",
        check=path("~/.local/bin/zed"),
        remove_paths=(
            "~/.local/zed.app",
            "~/.local/bin/zed",
            "~/.local/share/applications/zed.desktop",
        ),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://zed.dev/",
    ),
    Entry(
        id="cursor",
        name="Cursor",
        summary="An AI code editor built on VS Code",
        category="Development",
        kind="deb-url",
        url="https://api2.cursor.sh/updates/download/golden/linux-x64-deb/cursor/latest",
        check=dpkg("cursor"),
        homepage="https://cursor.com/",
        notes=(
            "The same download Cursor's own updater uses, so this is always the current "
            "release."
        ),
    ),
    Entry(
        id="docker",
        name="Docker",
        summary="Containers, from the Debian archive",
        category="Development",
        kind="apt",
        packages=("docker.io",),
        add_groups=("docker",),
        check=dpkg("docker.io"),
        homepage="https://www.docker.com/",
        notes=(
            "Your account is added to the docker group. Log out and in for that to take "
            "effect."
        ),
    ),
    Entry(
        id="build-tools",
        name="Build tools",
        summary="A C compiler, make, pkg-config and git",
        category="Development",
        kind="apt",
        packages=("build-essential", "pkg-config", "git"),
        check=dpkg("build-essential"),
        homepage="https://www.debian.org/",
    ),
    # -- AI tools ----------------------------------------------------------
    Entry(
        id="claude-desktop",
        name="Claude Desktop",
        summary="Anthropic's desktop app, from Anthropic's apt repository",
        category="AI tools",
        kind="apt-repo",
        packages=("claude-desktop",),
        repo=Repo(
            key_url="https://downloads.claude.ai/claude-desktop/key.asc",
            keyring_path="/usr/share/keyrings/claude-desktop-archive-keyring.asc",
            sources_line=(
                "deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] "
                "https://downloads.claude.ai/claude-desktop/apt/stable stable main"
            ),
            sources_path="/etc/apt/sources.list.d/claude-desktop.list",
        ),
        check=dpkg("claude-desktop"),
        homepage="https://claude.com/download",
    ),
    Entry(
        id="claude-code",
        name="Claude Code",
        summary="Anthropic's coding agent for the terminal",
        category="AI tools",
        kind="apt-repo",
        packages=("claude-code",),
        repo=Repo(
            key_url="https://downloads.claude.ai/keys/claude-code.asc",
            keyring_path="/etc/apt/keyrings/claude-code.asc",
            sources_line=(
                "deb [signed-by=/etc/apt/keyrings/claude-code.asc] "
                "https://downloads.claude.ai/claude-code/apt/stable stable main"
            ),
            sources_path="/etc/apt/sources.list.d/claude-code.list",
        ),
        check=dpkg("claude-code"),
        homepage="https://code.claude.com/docs/en/setup",
        notes=(
            "The repository signing key has fingerprint "
            "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE. Run claude in a terminal to sign in."
        ),
    ),
    Entry(
        id="kimi-code",
        name="Kimi Code",
        summary=(
            "Moonshot's coding agent for the terminal, installed under your home directory"
        ),
        category="AI tools",
        kind="user-script",
        url="https://code.kimi.com/kimi-code/install.sh",
        check=path("~/.kimi-code/bin/kimi"),
        remove_paths=("~/.kimi-code",),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://www.kimi.com/code/",
        notes=(
            "The installer adds ~/.kimi-code/bin to PATH in ~/.profile. Removing Kimi Code "
            "leaves that line behind; delete it by hand if you want it gone."
        ),
    ),
    # -- Remote access -----------------------------------------------------
    Entry(
        id="rustdesk",
        name="RustDesk",
        summary="Open-source remote desktop, from its GitHub release",
        category="Remote access",
        kind="github-deb",
        github_repo="rustdesk/rustdesk",
        asset_pattern=r"^rustdesk-\d+\.\d+\.\d+-x86_64\.deb$",
        check=dpkg("rustdesk"),
        homepage="https://rustdesk.com/",
    ),
    Entry(
        id="anydesk",
        name="AnyDesk",
        summary="Remote desktop, from AnyDesk's apt repository",
        category="Remote access",
        kind="apt-repo",
        packages=("anydesk",),
        repo=Repo(
            key_url="https://keys.anydesk.com/repos/DEB-GPG-KEY",
            keyring_path="/etc/apt/keyrings/keys.anydesk.com.asc",
            sources_line=(
                "deb [signed-by=/etc/apt/keyrings/keys.anydesk.com.asc] "
                "https://deb.anydesk.com all main"
            ),
            sources_path="/etc/apt/sources.list.d/anydesk-stable.list",
        ),
        check=dpkg("anydesk"),
        homepage="https://anydesk.com/",
    ),
    Entry(
        id="remmina",
        name="Remmina",
        summary="A client for RDP and VNC desktops",
        category="Remote access",
        kind="apt",
        packages=("remmina", "remmina-plugin-rdp", "remmina-plugin-vnc"),
        check=dpkg("remmina"),
        homepage="https://remmina.org/",
    ),
    # -- System tools ------------------------------------------------------
    Entry(
        id="flatpak",
        name="Flatpak and Flathub",
        summary="Flatpak, with the Flathub app store configured",
        category="System tools",
        kind="apt",
        packages=("flatpak",),
        post_install=(
            (
                "flatpak", "remote-add", "--if-not-exists", "flathub",
                "https://dl.flathub.org/repo/flathub.flatpakrepo",
            ),
        ),
        check=dpkg("flatpak"),
        homepage="https://flatpak.org/",
        notes="Afterwards, flatpak install flathub <app> works from a terminal.",
    ),
    Entry(
        id="gparted",
        name="GParted",
        summary="Partition editor",
        category="System tools",
        kind="apt",
        packages=("gparted",),
        check=dpkg("gparted"),
        homepage="https://gparted.org/",
    ),
    Entry(
        id="tmux",
        name="tmux",
        summary="A terminal multiplexer",
        category="System tools",
        kind="apt",
        packages=("tmux",),
        check=dpkg("tmux"),
        homepage="https://github.com/tmux/tmux",
    ),
    # -- Drivers -----------------------------------------------------------
    Entry(
        id="nvidia-driver",
        name="NVIDIA proprietary driver",
        summary="NVIDIA's own driver, chosen for this card by nvidia-detect",
        category="Drivers",
        kind="apt",
        packages=("linux-headers-amd64",),
        resolver="nvidia-detect",
        needs_components=("non-free",),
        check=dpkg("nvidia-driver", "nvidia-tesla-535-driver"),
        warning=NVIDIA_WARNING,
        homepage="https://wiki.debian.org/NvidiaGraphicsDrivers",
    ),
    Entry(
        id="intel-graphics",
        name="Intel graphics acceleration",
        summary="Video decoding and Vulkan for Intel GPUs",
        category="Drivers",
        kind="apt",
        packages=(
            "intel-media-va-driver",
            "i965-va-driver",
            "mesa-vulkan-drivers",
            "vulkan-tools",
            "vainfo",
        ),
        check=dpkg("intel-media-va-driver"),
        homepage="https://wiki.debian.org/HardwareVideoAcceleration",
    ),
    Entry(
        id="amd-graphics",
        name="AMD graphics acceleration",
        summary="Video decoding, Vulkan and firmware for AMD GPUs",
        category="Drivers",
        kind="apt",
        packages=(
            "mesa-va-drivers",
            "mesa-vulkan-drivers",
            "vulkan-tools",
            "vainfo",
            "firmware-amd-graphics",
        ),
        check=dpkg("mesa-vulkan-drivers"),
        homepage="https://wiki.debian.org/AtiHowTo",
    ),
    Entry(
        id="broadcom-wifi",
        name="Broadcom STA wifi driver",
        summary="Broadcom's driver for the wifi chips the open ones do not cover",
        category="Drivers",
        kind="apt",
        packages=("broadcom-sta-dkms", "linux-headers-amd64"),
        needs_components=("contrib",),
        check=dpkg("broadcom-sta-dkms"),
        warning=BROADCOM_WARNING,
        homepage="https://wiki.debian.org/wl",
    ),
    Entry(
        id="printing",
        name="Printing and scanning",
        summary="CUPS, printer drivers and a scanner app",
        category="Drivers",
        kind="apt",
        packages=(
            "cups",
            "system-config-printer",
            "printer-driver-all",
            "simple-scan",
            "sane-airscan",
        ),
        check=dpkg("cups"),
        homepage="https://wiki.debian.org/SystemPrinting",
    ),
)


def by_id(entry_id: str) -> Entry:
    for entry in ENTRIES:
        if entry.id == entry_id:
            return entry
    raise KeyError(entry_id)


def by_category(entries: tuple[Entry, ...] = ENTRIES) -> dict[str, list[Entry]]:
    """Entries grouped by category, keys in CATEGORIES order, empty ones kept.

    Empty categories stay so the sidebar is the same shape whatever is
    filtered; a category that vanishes and comes back moves every other one.
    """
    grouped: dict[str, list[Entry]] = {category: [] for category in CATEGORIES}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(entry)
    return grouped


def search(query: str, entries: tuple[Entry, ...] = ENTRIES) -> list[Entry]:
    """Entries whose name, summary, id or package names mention every word."""
    words = query.lower().split()
    if not words:
        return list(entries)
    found = []
    for entry in entries:
        haystack = " ".join(
            [entry.id, entry.name, entry.summary, *entry.packages]
        ).lower()
        if all(word in haystack for word in words):
            found.append(entry)
    return found


def is_privileged(entry: Entry) -> bool:
    return entry.kind in PRIVILEGED_KINDS


def parse_dpkg_status(text: str) -> set[str]:
    """Package names that are fully installed.

    From ``dpkg-query -W -f='${Package} ${db:Status-Status}\\n'``. Only
    ``installed`` counts: a package in ``config-files`` has been removed and
    left its conffiles, and offering to remove it again is a button that does
    nothing.
    """
    installed = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "installed":
            installed.add(parts[0])
    return installed


def expand_home(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def installed(entry: Entry, dpkg_installed: set[str], home: Path) -> bool:
    if entry.check.kind == "dpkg":
        return any(name in dpkg_installed for name in entry.check.values)
    return any(expand_home(value, home).exists() for value in entry.check.values)


def to_dict(entry: Entry) -> dict:
    return dataclasses.asdict(entry)


def _urls(entry: Entry) -> list[str]:
    urls = [entry.homepage]
    if entry.url:
        urls.append(entry.url)
    if entry.repo:
        urls.append(entry.repo.key_url)
        if entry.repo.sources_url:
            urls.append(entry.repo.sources_url)
        if entry.repo.sources_line:
            urls += [
                token for token in entry.repo.sources_line.split()
                if "://" in token
            ]
    for argv in entry.post_install:
        urls += [token for token in argv if "://" in token]
    return urls


def _strings(entry: Entry) -> list[str]:
    """Every string in the entry, for checks that apply to all of them."""
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(to_dict(entry))
    return found


def validate(entries: tuple[Entry, ...] = ENTRIES) -> list[str]:
    """Every problem with the catalog, as text. Empty when it is clean.

    All of them at once rather than the first, because a catalog is edited
    by hand and the person editing it wants the whole list. Returned rather
    than raised so the unit tests can name the exact rule each bad entry
    trips, and so the tools can refuse to start with the reason on screen.
    """
    problems: list[str] = []
    seen: set[str] = set()

    def problem(entry: Entry, text: str) -> None:
        problems.append(f"{entry.id}: {text}")

    for entry in entries:
        if not ENTRY_ID.match(entry.id):
            problem(entry, "id must be lowercase words joined by single hyphens")
        if entry.id in seen:
            problem(entry, "duplicate id")
        seen.add(entry.id)
        if entry.category not in CATEGORIES:
            problem(entry, f"unknown category {entry.category!r}")
        if entry.kind not in KINDS:
            problem(entry, f"unknown kind {entry.kind!r}")
        if not entry.name or not entry.summary:
            problem(entry, "needs a name and a summary")

        for url in _urls(entry):
            if not url.startswith("https://"):
                problem(entry, f"URL is not https: {url}")
        for name in entry.packages:
            if not PACKAGE_NAME.match(name):
                problem(entry, f"not a Debian package name: {name!r}")
        for text in _strings(entry):
            if any(dash in text for dash in DASHES):
                problem(entry, f"contains a dash character: {text!r}")

        # Per kind: the fields the installer will reach for.
        if entry.kind == "apt":
            if not entry.packages and entry.resolver is None:
                problem(entry, "apt entries name their packages")
        elif entry.kind == "apt-repo":
            if not entry.packages:
                problem(entry, "apt-repo entries name their packages")
            if entry.repo is None:
                problem(entry, "apt-repo entries carry a repo")
            else:
                repo = entry.repo
                if bool(repo.sources_line) == bool(repo.sources_url):
                    problem(entry, "a repo has exactly one of sources_line and sources_url")
                if not repo.keyring_path.startswith(KEYRING_DIRS):
                    problem(entry, f"keyring outside {KEYRING_DIRS}: {repo.keyring_path}")
                if not repo.sources_path.startswith(SOURCES_DIR):
                    problem(entry, f"sources outside {SOURCES_DIR}: {repo.sources_path}")
                if repo.sources_line and f"signed-by={repo.keyring_path}" not in repo.sources_line:
                    problem(entry, "sources_line must be signed-by the keyring it fetches")
        elif entry.kind == "deb-url":
            if not entry.url:
                problem(entry, "deb-url entries carry a url")
            if entry.packages:
                problem(entry, "deb-url entries install the file, not named packages")
        elif entry.kind == "github-deb":
            if not entry.github_repo or entry.github_repo.count("/") != 1:
                problem(entry, "github-deb entries name owner/repo")
            if not entry.asset_pattern:
                problem(entry, "github-deb entries carry an asset_pattern")
            else:
                try:
                    re.compile(entry.asset_pattern)
                except re.error as exc:
                    problem(entry, f"asset_pattern does not compile: {exc}")
        elif entry.kind == "tarball-opt":
            if not entry.url:
                problem(entry, "tarball-opt entries carry a url")
            if not entry.opt_dir or not entry.opt_dir.startswith("/opt/"):
                problem(entry, "tarball-opt entries unpack under /opt")
            if not entry.launcher:
                problem(entry, "tarball-opt entries name their launcher")
        elif entry.kind == "user-script":
            if not entry.url:
                problem(entry, "user-script entries carry a url")
            if entry.check.kind != "path" or not all(
                value.startswith("~") for value in entry.check.values
            ):
                problem(entry, "user-script entries are checked by a path under ~")
            if entry.warning is None:
                problem(entry, "user-script entries warn that they run a vendor script")

        if entry.check.kind not in ("dpkg", "path") or not entry.check.values:
            problem(entry, "every entry has a check")
        elif entry.check.kind == "dpkg":
            for name in entry.check.values:
                if not PACKAGE_NAME.match(name):
                    problem(entry, f"check names a bad package: {name!r}")
        else:
            for value in entry.check.values:
                if not (value.startswith("/") or value.startswith("~")):
                    problem(entry, f"check path is not absolute: {value!r}")

        if entry.resolver is not None and entry.resolver not in RESOLVERS:
            problem(entry, f"unknown resolver {entry.resolver!r}")
        if entry.needs_components and entry.kind not in ("apt", "apt-repo"):
            problem(entry, "only apt kinds can need components")

    return problems
