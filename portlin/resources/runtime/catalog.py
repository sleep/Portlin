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
github-zip-opt a ZIP attached to the latest GitHub release, unpacked under /opt
release-bin  one executable attached to the latest release on GitHub or
             Codeberg, installed into /usr/local/bin
tarball-opt  a tarball unpacked under /opt, with a generated menu entry
user-script  the vendor's installer script, run as the user, into their home

Every kind but user-script needs root and goes through pkexec. user-script
must never run as root, because it writes under a home directory. A
user-script entry can pass its script arguments and environment variables,
which is how an installer that would otherwise stop to ask a question is told
not to. Any entry can name other entries it ``requires``: the Software app
installs those first, and portlin-install refuses the entry until they are
there, since a vendor script run as the user cannot install what it finds
missing.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path

KINDS = (
    "apt", "apt-repo", "deb-url", "github-deb", "github-zip-opt", "release-bin", "tarball-opt",
    "user-script",
)
PRIVILEGED_KINDS = frozenset(
    {"apt", "apt-repo", "deb-url", "github-deb", "github-zip-opt", "release-bin", "tarball-opt"}
)
USER_KINDS = frozenset({"user-script"})

# Display order. Drivers last: it is the page that talks about this machine
# rather than about programs, and it is drawn differently.
CATEGORIES = (
    "Browsers",
    "Communication",
    "Media",
    "Office and graphics",
    "Security and privacy",
    "Security research",
    "Networking",
    "Development",
    "AI tools",
    "Remote access",
    "System tools",
    "Look and feel",
    "Drivers",
)

RESOLVERS = ("nvidia-detect",)

# Debian policy, section 5.6.1: lowercase letters, digits, plus, minus, dot;
# at least two characters, starting with a letter or digit.
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
ENTRY_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KEYRING_DIRS = ("/usr/share/keyrings/", "/etc/apt/keyrings/")
SOURCES_DIR = "/etc/apt/sources.list.d/"
# Where a release-bin entry's executable goes: on PATH for everyone, and
# ahead of /usr/bin, so it wins over any older Debian package of the same tool.
BIN_DIR = "/usr/local/bin/"
DASHES = "–—"
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


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
    # release-bin: the forge's "latest release" API endpoint, and where the
    # one asset asset_pattern picks out is installed. GitHub and Codeberg
    # (Forgejo) answer that endpoint in the same shape.
    release_api: str | None = None
    bin_path: str | None = None
    opt_dir: str | None = None
    launcher: str | None = None
    icon: str | None = None
    needs_components: tuple[str, ...] = ()
    resolver: str | None = None
    debconf: tuple[str, ...] = ()
    add_groups: tuple[str, ...] = ()
    post_install: tuple[tuple[str, ...], ...] = ()
    remove_paths: tuple[str, ...] = ()
    script_args: tuple[str, ...] = ()
    script_env: tuple[tuple[str, str], ...] = ()
    requires: tuple[str, ...] = ()
    warning: str | None = None
    notes: str | None = None


def dpkg(*names: str) -> Check:
    return Check("dpkg", names)


def path(*paths: str) -> Check:
    return Check("path", paths)


def github_releases(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def codeberg_releases(repo: str) -> str:
    return f"https://codeberg.org/api/v1/repos/{repo}/releases/latest"


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

# Said on every entry that reaches out to another machine, because the tool
# itself asks nothing before it does: the entry is where a person reads it.
AUTHORIZED_USE = "Only for machines and networks you own or have permission to test."
AUTHORIZED_DEVICE = "Only for devices you own or have permission to test."

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
    Entry(
        id="ffmpeg",
        name="FFmpeg",
        summary="Convert, cut and merge audio and video from the command line",
        category="Media",
        kind="apt",
        packages=("ffmpeg",),
        check=dpkg("ffmpeg"),
        homepage="https://ffmpeg.org/",
        notes="yt-dlp uses it to merge the video and audio it downloads.",
    ),
    # The two downloaders below break whenever a site changes, and the fix is
    # always a new release within days. Debian's packages freeze at a release
    # and stay there, so these take the project's own build instead, which
    # also knows how to update itself.
    Entry(
        id="yt-dlp",
        name="yt-dlp",
        summary="Download video and audio from supported websites",
        category="Media",
        kind="release-bin",
        # The nightly channel: a build on any day with changes, and the one
        # the project recommends for regular users. The stable channel is
        # released monthly and is the one Debian's package falls behind.
        release_api=github_releases("yt-dlp/yt-dlp-nightly-builds"),
        # The standalone x86_64 build, with its Python and every optional
        # dependency inside. Anchored at both ends: the same release also
        # ships yt-dlp_linux.zip and yt-dlp_linux_aarch64.
        asset_pattern=r"^yt-dlp_linux$",
        bin_path="/usr/local/bin/yt-dlp",
        requires=("ffmpeg",),
        check=path("/usr/local/bin/yt-dlp"),
        homepage="https://github.com/yt-dlp/yt-dlp",
        notes=(
            "The project's own nightly build rather than Debian's package, which stops "
            "working as sites change. FFmpeg is installed first if it is missing. To update, "
            "run: sudo yt-dlp -U. To switch to the monthly stable channel, run: sudo yt-dlp "
            "--update-to stable."
        ),
    ),
    Entry(
        id="gallery-dl",
        name="gallery-dl",
        summary="Download image galleries and media collections",
        category="Media",
        kind="release-bin",
        # gallery-dl's development and releases moved to Codeberg; the GitHub
        # mirror's releases carry only the source archives.
        release_api=codeberg_releases("mikf/gallery-dl"),
        # The release also carries gallery-dl.bin.sig, so the dot and the end
        # anchor both matter.
        asset_pattern=r"^gallery-dl\.bin$",
        bin_path="/usr/local/bin/gallery-dl",
        check=path("/usr/local/bin/gallery-dl"),
        homepage="https://gdl-org.github.io/",
        notes=(
            "The project's own release from Codeberg rather than Debian's package, which "
            "stops working as sites change. To update, run: sudo gallery-dl -U."
        ),
    ),
    Entry(
        id="handbrake",
        name="HandBrake",
        summary="Convert and compress video files",
        category="Media",
        kind="apt",
        packages=("handbrake",),
        check=dpkg("handbrake"),
        homepage="https://handbrake.fr/",
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
        id="kleopatra",
        name="Kleopatra",
        summary="Manage OpenPGP certificates and encrypt files",
        category="Security and privacy",
        kind="apt",
        packages=("kleopatra",),
        check=dpkg("kleopatra"),
        homepage="https://apps.kde.org/kleopatra/",
    ),
    Entry(
        id="veracrypt",
        name="VeraCrypt",
        summary="Create and open encrypted volumes",
        category="Security and privacy",
        kind="github-deb",
        github_repo="veracrypt/VeraCrypt",
        asset_pattern=r"^veracrypt-\d+\.\d+\.\d+-Debian-13-amd64\.deb$",
        check=dpkg("veracrypt"),
        homepage="https://www.veracrypt.fr/",
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
    # -- Security research -------------------------------------------------
    Entry(
        id="nmap",
        name="Nmap",
        summary="Scan a network for hosts, open ports and services",
        category="Security research",
        kind="apt",
        packages=("nmap", "zenmap"),
        check=dpkg("nmap"),
        homepage="https://nmap.org/",
        notes="Zenmap is the window; nmap is the command. " + AUTHORIZED_USE,
    ),
    Entry(
        id="metasploit",
        name="Metasploit Framework",
        summary="Exploitation framework, from Rapid7's apt repository",
        category="Security research",
        kind="apt-repo",
        packages=("metasploit-framework",),
        repo=Repo(
            key_url="https://apt.metasploit.com/metasploit-framework.gpg.key",
            keyring_path="/usr/share/keyrings/metasploit-framework.asc",
            sources_line=(
                "deb [arch=amd64 signed-by=/usr/share/keyrings/metasploit-framework.asc] "
                "https://apt.metasploit.com/ lucid main"
            ),
            sources_path="/etc/apt/sources.list.d/metasploit-framework.list",
        ),
        check=dpkg("metasploit-framework"),
        homepage="https://www.metasploit.com/",
        notes=(
            "One bundle carrying its own Ruby and every module, about 900 MB installed, so "
            "it fits only on a stick with room to spare. Start it with: msfconsole. "
            + AUTHORIZED_USE
        ),
    ),
    Entry(
        id="sqlmap",
        name="sqlmap",
        summary="Find and exploit SQL injection in a web application",
        category="Security research",
        kind="apt",
        packages=("sqlmap",),
        check=dpkg("sqlmap"),
        homepage="https://sqlmap.org/",
        notes=AUTHORIZED_USE,
    ),
    Entry(
        id="web-scanners",
        name="Web scanning tools",
        summary="Nikto, WhatWeb and wafw00f, with the dirb, gobuster and ffuf fuzzers",
        category="Security research",
        kind="apt",
        packages=("nikto", "whatweb", "wafw00f", "dirb", "gobuster", "ffuf", "cewl"),
        # Debian keeps Nikto in non-free, over the licence on its scan database.
        needs_components=("non-free",),
        check=dpkg("nikto", "gobuster", "ffuf"),
        homepage="https://www.debian.org/",
        notes=AUTHORIZED_USE,
    ),
    Entry(
        id="password-crackers",
        name="Password crackers",
        summary="John the Ripper, hashcat, Hydra, Medusa, Ncrack and crunch",
        category="Security research",
        kind="apt",
        packages=("john", "hashcat", "hashcat-data", "hydra", "medusa", "ncrack", "crunch"),
        check=dpkg("john", "hashcat", "hydra"),
        homepage="https://www.debian.org/",
        notes=(
            "hashcat uses the graphics card only where an OpenCL driver is installed, and "
            "falls back to the processor without one. " + AUTHORIZED_USE
        ),
    ),
    Entry(
        id="impacket",
        name="Impacket",
        summary="Python tools for SMB, Kerberos and the other Windows protocols",
        category="Security research",
        kind="apt",
        packages=("python3-impacket", "smbmap", "smbclient"),
        check=dpkg("python3-impacket"),
        homepage="https://github.com/fortra/impacket",
        notes=AUTHORIZED_USE,
    ),
    Entry(
        id="recon-tools",
        name="Network recon tools",
        summary="arp-scan, netdiscover and nbtscan, with dnsrecon, dnsenum, fierce and recon-ng",
        category="Security research",
        kind="apt",
        packages=(
            "arp-scan", "netdiscover", "nbtscan", "dnsrecon", "dnsenum", "fierce",
            "sublist3r", "recon-ng",
        ),
        check=dpkg("arp-scan", "dnsrecon", "recon-ng"),
        homepage="https://www.debian.org/",
        notes=AUTHORIZED_USE,
    ),
    Entry(
        id="packet-tools",
        name="Packet and socket tools",
        summary="tcpdump, tshark, ngrep, hping3, Scapy, netcat, socat and proxychains",
        category="Security research",
        kind="apt",
        packages=(
            "tcpdump", "tshark", "ngrep", "hping3", "python3-scapy", "netcat-openbsd",
            "socat", "proxychains4",
        ),
        # Not tshark: the Wireshark entry can bring that in on its own, and
        # this page would then say these tools are installed when they are not.
        check=dpkg("tcpdump", "ngrep"),
        homepage="https://www.debian.org/",
        notes="Wireshark's window is its own entry, under Networking.",
    ),
    Entry(
        id="mitm-tools",
        name="Man in the middle tools",
        summary="bettercap, Ettercap and dsniff, for traffic on a local network",
        category="Security research",
        kind="apt",
        packages=("bettercap", "ettercap-graphical", "dsniff"),
        check=dpkg("bettercap", "ettercap-graphical"),
        homepage="https://www.bettercap.org/",
        notes=AUTHORIZED_USE,
    ),
    Entry(
        id="wifi-tools",
        name="Wireless auditing tools",
        summary="aircrack-ng, Reaver, wifite, hcxtools and macchanger",
        category="Security research",
        kind="apt",
        packages=("aircrack-ng", "reaver", "wifite", "hcxtools", "macchanger"),
        check=dpkg("aircrack-ng"),
        homepage="https://www.aircrack-ng.org/",
        notes=(
            "Needs a wifi adapter whose driver does monitor mode and packet injection; many "
            "built-in cards do neither. " + AUTHORIZED_USE
        ),
    ),
    Entry(
        id="ghidra",
        name="Ghidra",
        summary="Software reverse engineering suite",
        category="Security research",
        kind="github-zip-opt",
        github_repo="NationalSecurityAgency/ghidra",
        asset_pattern=r"^ghidra_.*_PUBLIC_.*\.zip$",
        opt_dir="/opt/ghidra",
        launcher="ghidraRun",
        requires=("java",),
        check=path("/opt/ghidra/ghidraRun"),
        homepage="https://ghidra-sre.org/",
        notes="The release ZIP carries no Java, so Java is installed first if it is missing.",
    ),
    Entry(
        id="radare2",
        name="radare2",
        summary="Command line reverse engineering framework",
        category="Security research",
        kind="github-deb",
        github_repo="radareorg/radare2",
        asset_pattern=r"^radare2_\d+\.\d+\.\d+_amd64\.deb$",
        check=dpkg("radare2"),
        homepage="https://rada.re/",
        notes="Debian 13 carries no radare2 package, so this is the project's own .deb.",
    ),
    Entry(
        id="jd-gui",
        name="JD-GUI",
        summary="Browse and decompile Java class files",
        category="Security research",
        kind="github-deb",
        github_repo="java-decompiler/jd-gui",
        asset_pattern=r"^jd-gui-\d+\.\d+\.\d+\.deb$",
        check=dpkg("jd-gui"),
        homepage="https://java-decompiler.github.io/",
    ),
    Entry(
        id="apktool",
        name="Apktool",
        summary="Decode and rebuild Android APK files",
        category="Security research",
        kind="apt",
        packages=("apktool",),
        check=dpkg("apktool"),
        homepage="https://apktool.org/",
        notes="Decodes resources and smali; JD-GUI reads the Java side of the same app.",
    ),
    Entry(
        id="jadx",
        name="jadx",
        summary="Decompile an APK or a dex file back to readable Java",
        category="Security research",
        kind="github-zip-opt",
        github_repo="skylot/jadx",
        asset_pattern=r"^jadx-\d+\.\d+\.\d+\.zip$",
        opt_dir="/opt/jadx",
        launcher="bin/jadx-gui",
        requires=("java",),
        check=path("/opt/jadx/bin/jadx-gui"),
        homepage="https://github.com/skylot/jadx",
        notes=(
            "Java is installed first if it is missing. The window is jadx-gui; the command "
            "line is /opt/jadx/bin/jadx."
        ),
    ),
    Entry(
        id="apk-tools",
        name="APK tools",
        summary="androguard, enjarify and dexdump, with aapt, apksigner and zipalign",
        category="Security research",
        kind="apt",
        packages=("androguard", "enjarify", "dexdump", "aapt", "apksigner", "zipalign"),
        check=dpkg("androguard", "enjarify", "apksigner"),
        homepage="https://www.debian.org/",
        notes="Enough to take an APK apart, patch it, and sign and align it to install again.",
    ),
    Entry(
        id="android-tools",
        name="Android device tools",
        summary="adb and fastboot, with the udev rules that let them see a phone",
        category="Security research",
        kind="apt",
        packages=("adb", "fastboot", "android-sdk-platform-tools-common"),
        add_groups=("plugdev",),
        check=dpkg("adb", "fastboot"),
        homepage="https://developer.android.com/tools/adb",
        notes=(
            "Your account joins the plugdev group, so a plugged-in phone answers without root. "
            "Log out and in for that, turn on USB debugging on the phone, then: adb devices."
        ),
    ),
    Entry(
        id="android-image-tools",
        name="Android image tools",
        summary="Heimdall for flashing Samsung devices, abootimg and the sparse image tools",
        category="Security research",
        kind="apt",
        packages=("heimdall-flash", "abootimg", "android-sdk-libsparse-utils"),
        check=dpkg("heimdall-flash", "abootimg"),
        homepage="https://www.debian.org/",
        notes=(
            "simg2img turns a sparse Android image into one a loop mount can read; abootimg "
            "takes a boot image apart. " + AUTHORIZED_DEVICE
        ),
    ),
    Entry(
        id="ios-tools",
        name="iOS device tools",
        summary="libimobiledevice: read an iPhone or iPad, its logs, its backups and its apps",
        category="Security research",
        kind="apt",
        packages=(
            "libimobiledevice-utils", "ideviceinstaller", "libusbmuxd-tools", "usbmuxd",
            "libplist-utils", "ifuse",
        ),
        check=dpkg("libimobiledevice-utils", "ideviceinstaller"),
        homepage="https://libimobiledevice.org/",
        notes=(
            "Unlock the device and run idevicepair pair, and accept the prompt on it, before "
            "the rest see anything. ifuse mounts what the device will share, iproxy forwards "
            "a port over USB, and plistutil reads the binary plists that come back. "
            + AUTHORIZED_DEVICE
        ),
    ),
    Entry(
        id="ios-recovery",
        name="iOS recovery tools",
        summary="idevicerestore and irecovery, for a device in recovery or DFU mode",
        category="Security research",
        kind="apt",
        packages=("idevicerestore", "irecovery"),
        check=dpkg("idevicerestore", "irecovery"),
        homepage="https://libimobiledevice.org/",
        notes="These write firmware, and a restore wipes the device it is pointed at.",
    ),
    Entry(
        id="ipsw",
        name="ipsw",
        summary="Download Apple firmware and take it apart, dyld shared cache included",
        category="Security research",
        kind="github-deb",
        github_repo="blacktop/ipsw",
        asset_pattern=r"^ipsw_\d+\.\d+\.\d+_linux_x86_64\.deb$",
        check=dpkg("ipsw"),
        homepage="https://blacktop.github.io/ipsw/",
        notes="Debian has no package for it, so this is the project's own .deb.",
    ),
    Entry(
        id="debuggers",
        name="Debuggers and tracers",
        summary="gdb for any architecture, edb, ltrace and strace",
        category="Security research",
        kind="apt",
        packages=("gdb", "gdb-multiarch", "edb-debugger", "ltrace", "strace"),
        check=dpkg("gdb-multiarch", "edb-debugger"),
        homepage="https://www.debian.org/",
    ),
    Entry(
        id="binary-tools",
        name="Binary inspection tools",
        summary="binutils for any architecture, patchelf, checksec and YARA",
        category="Security research",
        kind="apt",
        packages=("binutils-multiarch", "patchelf", "checksec", "yara"),
        check=dpkg("binutils-multiarch", "yara"),
        homepage="https://www.debian.org/",
    ),
    Entry(
        id="pwntools",
        name="pwntools",
        summary="Exploit development toolkit, with the nasm assembler",
        category="Security research",
        kind="apt",
        packages=("python3-pwntools", "nasm"),
        check=dpkg("python3-pwntools"),
        homepage="https://docs.pwntools.com/",
    ),
    Entry(
        id="binwalk",
        name="binwalk",
        summary="Find and extract files embedded in a firmware image",
        category="Security research",
        kind="apt",
        packages=("binwalk",),
        check=dpkg("binwalk"),
        homepage="https://github.com/ReFirmLabs/binwalk",
    ),
    Entry(
        id="hex-editors",
        name="Hex editors",
        summary="Okteta and GHex for the desktop, hexedit for a terminal",
        category="Security research",
        kind="apt",
        packages=("okteta", "ghex", "hexedit"),
        check=dpkg("okteta", "ghex", "hexedit"),
        homepage="https://www.debian.org/",
    ),
    Entry(
        id="forensics-tools",
        name="Forensics and recovery tools",
        summary="The Sleuth Kit, Autopsy, TestDisk, foremost, ExifTool and steghide",
        category="Security research",
        kind="apt",
        packages=(
            "sleuthkit", "autopsy", "testdisk", "foremost", "libimage-exiftool-perl",
            "steghide",
        ),
        check=dpkg("sleuthkit", "testdisk"),
        homepage="https://www.sleuthkit.org/",
        notes="Read a disk image rather than the disk this system is running from.",
    ),
    Entry(
        id="audit-tools",
        name="Host audit tools",
        summary="Lynis, chkrootkit and rkhunter, which check the machine they run on",
        category="Security research",
        kind="apt",
        packages=("lynis", "chkrootkit", "rkhunter"),
        check=dpkg("lynis", "chkrootkit", "rkhunter"),
        homepage="https://cisofy.com/lynis/",
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
    Entry(
        id="java",
        name="Java",
        summary="Debian's default JDK, which Ghidra and jadx run on",
        category="Development",
        kind="apt",
        packages=("default-jdk",),
        check=dpkg("default-jdk"),
        homepage="https://openjdk.org/",
    ),
    Entry(
        id="php",
        name="PHP development tools",
        summary="PHP command line, common extensions and ImageMagick",
        category="Development",
        kind="apt",
        packages=("php-cli", "php-curl", "php-gd", "php-mbstring", "php-mysql", "php-xml", "php-zip", "imagemagick"),
        check=dpkg("php-cli"),
        homepage="https://www.php.net/",
        notes="Installs Debian's supported PHP version and the common web-development extensions.",
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
    # The agents below install themselves with the vendor's script, which
    # is how each vendor documents installing on Linux. Each is told not to
    # stop and ask anything: the script's output goes to the progress log,
    # where a question would wait for an answer nobody can type. Removal
    # takes the program away and leaves the agent's own settings, sign-in
    # and memory where they are, so installing it again picks them back up.
    Entry(
        id="hermes",
        name="Hermes Agent",
        summary="Nous Research's self-improving agent, installed under your home directory",
        category="AI tools",
        kind="user-script",
        url="https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh",
        # Without it the script runs `hermes setup` against /dev/tty when
        # it can open one, which it can when the app was started from a
        # terminal.
        script_args=("--non-interactive",),
        # It clones itself with git, which the image leaves out.
        requires=("build-tools",),
        check=path("~/.local/bin/hermes"),
        remove_paths=(
            "~/.hermes/hermes-agent",
            "~/.hermes/tools",
            "~/.hermes/installs",
            "~/.hermes/cache",
            "~/.local/bin/hermes",
            "~/.local/bin/hermes-acp",
            "~/.local/bin/hermes-agent",
        ),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://hermes-agent.nousresearch.com/",
        notes=(
            "Build tools, for git, is installed first if it is not already. A large download, "
            "about 3 GB once installed. Afterwards, run hermes setup "
            "in a terminal to pick a model provider. Removing it keeps your settings, "
            "skills and memories in ~/.hermes."
        ),
    ),
    Entry(
        id="openclaw",
        name="OpenClaw",
        summary="A personal AI assistant that works through your chat apps",
        category="AI tools",
        kind="user-script",
        # The installer for a home directory: its own Node.js under
        # ~/.openclaw, no sudo, and no onboarding questions. The site's main
        # install.sh instead reaches for sudo apt-get to install Node.js.
        url="https://openclaw.ai/install-cli.sh",
        # It wants git, and without it tries sudo apt-get, which cannot
        # prompt for a password here.
        requires=("build-tools",),
        check=path("~/.openclaw/bin/openclaw"),
        remove_paths=("~/.openclaw/bin", "~/.openclaw/tools"),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://openclaw.ai/",
        notes=(
            "Build tools, for git, is installed first if it is not already. To set it up, run "
            "~/.openclaw/bin/openclaw onboard in a terminal. Removing it "
            "keeps your settings in ~/.openclaw."
        ),
    ),
    Entry(
        id="codex",
        name="Codex CLI",
        summary="OpenAI's coding agent for the terminal, installed under your home directory",
        category="AI tools",
        kind="user-script",
        url="https://releases.openai.com/codex/install.sh",
        # Otherwise it offers to start Codex once installed.
        script_env=(("CODEX_NON_INTERACTIVE", "1"),),
        check=path("~/.local/bin/codex"),
        remove_paths=("~/.codex/packages", "~/.local/bin/codex"),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://developers.openai.com/codex/cli",
        notes=(
            "Run codex in a terminal to sign in. The installer adds ~/.local/bin to PATH in "
            "~/.bashrc; removing Codex leaves that line and your settings in ~/.codex."
        ),
    ),
    Entry(
        id="opencode",
        name="OpenCode",
        summary="An open-source coding agent for the terminal, for any model provider",
        category="AI tools",
        kind="user-script",
        url="https://opencode.ai/install",
        check=path("~/.opencode/bin/opencode"),
        remove_paths=("~/.opencode",),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://opencode.ai/",
        notes=(
            "The installer adds ~/.opencode/bin to PATH in ~/.bashrc. Removing OpenCode "
            "leaves that line behind; delete it by hand if you want it gone."
        ),
    ),
    Entry(
        id="goose",
        name="goose",
        summary="Block's open-source agent for the terminal, for any model provider",
        category="AI tools",
        kind="user-script",
        url="https://github.com/block/goose/releases/download/stable/download_cli.sh",
        # Otherwise the script runs `goose configure` and asks whether to
        # edit PATH, both against /dev/tty when it can open one.
        script_env=(("CONFIGURE", "false"),),
        check=path("~/.local/bin/goose"),
        remove_paths=("~/.local/bin/goose",),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://block.github.io/goose/",
        notes=(
            "Run goose configure in a terminal to pick a model provider. Removing it keeps "
            "your settings in ~/.config/goose."
        ),
    ),
    Entry(
        id="abtop",
        name="abtop",
        summary="A top for AI coding agents: sessions, tokens, context and rate limits, "
                "installed under your home directory",
        category="AI tools",
        kind="user-script",
        # The project's own installer, generated by cargo-dist: it unpacks
        # the release for this machine into ~/.cargo/bin.
        url="https://github.com/sleep/abtop/releases/latest/download/abtop-installer.sh",
        check=path("~/.cargo/bin/abtop"),
        remove_paths=("~/.cargo/bin/abtop", "~/.config/abtop/abtop-receipt.json"),
        warning=VENDOR_SCRIPT_WARNING,
        homepage="https://github.com/sleep/abtop",
        notes=(
            "Run abtop --setup once to give Claude Code a status line that records its rate "
            "limits; the HUD reads them from there and shows them beside each Claude Code "
            "session. The installer puts abtop in ~/.cargo/bin and adds that to PATH in "
            "~/.profile; removing abtop leaves that line."
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
    Entry(
        id="btop",
        name="btop",
        summary="Interactive CPU, memory, disk and process monitor",
        category="System tools",
        kind="apt",
        packages=("btop",),
        check=dpkg("btop"),
        homepage="https://github.com/aristocratos/btop",
    ),
    Entry(
        id="terminal-tools",
        name="Terminal monitoring tools",
        summary="nload, nmon and Byobu for network and system monitoring",
        category="System tools",
        kind="apt",
        packages=("nload", "nmon", "byobu"),
        check=dpkg("nload", "nmon", "byobu"),
        homepage="https://www.debian.org/",
    ),
    Entry(
        id="konsole",
        name="Konsole",
        summary="KDE's tabbed terminal emulator",
        category="System tools",
        kind="apt",
        packages=("konsole",),
        check=dpkg("konsole"),
        homepage="https://konsole.kde.org/",
    ),
    Entry(
        id="sqlitebrowser",
        name="DB Browser for SQLite",
        summary="Browse and edit SQLite databases",
        category="System tools",
        kind="apt",
        packages=("sqlitebrowser",),
        check=dpkg("sqlitebrowser"),
        homepage="https://sqlitebrowser.org/",
    ),
    Entry(
        id="wireguard-tools",
        name="WireGuard tools",
        summary="Create and manage WireGuard VPN connections",
        category="System tools",
        kind="apt",
        packages=("wireguard-tools",),
        check=dpkg("wireguard-tools"),
        homepage="https://www.wireguard.com/",
    ),
    Entry(
        id="virt-manager",
        name="Virtual Machine Manager",
        summary="Create and run virtual machines with libvirt and QEMU",
        category="System tools",
        kind="apt",
        packages=("virt-manager", "libvirt-daemon-system", "qemu-system-x86"),
        add_groups=("libvirt",),
        check=dpkg("virt-manager"),
        homepage="https://virt-manager.org/",
        notes="Your account is added to the libvirt group. Log out and in before using it.",
    ),
    Entry(
        id="virtualbox",
        name="VirtualBox",
        summary="Desktop virtual machines from Oracle's apt repository",
        category="System tools",
        kind="apt-repo",
        packages=("virtualbox-7.2",),
        repo=Repo(
            key_url="https://www.virtualbox.org/download/oracle_vbox_2016.asc",
            keyring_path="/usr/share/keyrings/oracle-virtualbox-2016.asc",
            sources_line=(
                "deb [arch=amd64 signed-by=/usr/share/keyrings/oracle-virtualbox-2016.asc] "
                "https://download.virtualbox.org/virtualbox/debian {codename} contrib"
            ),
            sources_path="/etc/apt/sources.list.d/oracle-virtualbox.list",
        ),
        add_groups=("vboxusers",),
        check=dpkg("virtualbox-7.2"),
        homepage="https://www.virtualbox.org/",
        notes="Your account is added to the vboxusers group. Log out and in before using it.",
    ),
    # -- Look and feel -----------------------------------------------------
    # Icon themes the first-boot wizard once shipped and the image no longer
    # carries. The wizard's picker cannot offer them - it runs with no
    # network, which is why the three defaults still live in the image - but
    # a networked stick can have them back in one step here.
    Entry(
        id="elementary-xfce-icons",
        name="elementary-xfce icon theme",
        summary="Soft and quiet, Xfce's own icon set",
        category="Look and feel",
        kind="apt",
        packages=("elementary-xfce-icon-theme",),
        check=dpkg("elementary-xfce-icon-theme"),
        homepage="https://docs.xfce.org/xfce/xfce4-settings/start",
        notes="After installing, pick it in Settings, or run: xfconf-query -c "
        "xsettings -p /Net/IconThemeName -s elementary-xfce",
    ),
    Entry(
        id="numix-circle-icons",
        name="Numix Circle icon themes",
        summary="Vivid circle icon themes, Numix and Numix-Circle",
        category="Look and feel",
        kind="apt",
        packages=("numix-icon-theme", "numix-icon-theme-circle"),
        check=dpkg("numix-icon-theme-circle"),
        homepage="https://numixproject.github.io/",
        notes="After installing, pick one in Settings, or run: xfconf-query -c "
        "xsettings -p /Net/IconThemeName -s Numix-Circle",
    ),
    # -- Drivers -----------------------------------------------------------
    Entry(
        id="nvidia-driver",
        name="NVIDIA proprietary driver",
        summary="NVIDIA's own driver, chosen for this card by nvidia-detect",
        category="Drivers",
        kind="apt",
        packages=("linux-headers-amd64", "firmware-nvidia-graphics"),
        resolver="nvidia-detect",
        needs_components=("non-free",),
        check=dpkg("nvidia-driver", "nvidia-tesla-535-driver"),
        warning=NVIDIA_WARNING,
        homepage="https://wiki.debian.org/NvidiaGraphicsDrivers",
        notes="The firmware the card needs is installed with the driver; the "
        "base image deliberately leaves it out.",
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
        id="thinkpad",
        name="ThinkPad power and battery care",
        summary="TLP, which tunes power use and charges a ThinkPad battery gently",
        category="Drivers",
        kind="apt",
        # thinkpad_acpi is in the kernel already, and TLP drives it: battery
        # charge thresholds, power tuning on battery, and tlp-rdw turning
        # radios off when the laptop is docked or on a wired network.
        packages=("tlp", "tlp-rdw"),
        check=dpkg("tlp"),
        homepage="https://linrunner.de/tlp/",
        notes="To keep the battery between 75 and 80 percent, run: sudo tlp "
        "setcharge 75 80 BAT0. The laptop itself holds that setting, so it "
        "stays after the stick is unplugged.",
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


def missing_requirements(entry: Entry, dpkg_installed: set[str], home: Path) -> list[Entry]:
    """The entries ``entry`` requires that are not installed yet."""
    return [
        required for required in map(by_id, entry.requires)
        if not installed(required, dpkg_installed, home)
    ]


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
    if entry.release_api:
        urls.append(entry.release_api)
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
    ids = {entry.id for entry in entries}

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
        elif entry.kind == "github-zip-opt":
            if not entry.github_repo or entry.github_repo.count("/") != 1:
                problem(entry, "github-zip-opt entries name owner/repo")
            if not entry.asset_pattern:
                problem(entry, "github-zip-opt entries carry an asset_pattern")
            else:
                try:
                    re.compile(entry.asset_pattern)
                except re.error as exc:
                    problem(entry, f"asset_pattern does not compile: {exc}")
            if not entry.opt_dir or not entry.opt_dir.startswith("/opt/"):
                problem(entry, "github-zip-opt entries unpack under /opt")
            if not entry.launcher:
                problem(entry, "github-zip-opt entries name their launcher")
        elif entry.kind == "release-bin":
            if not entry.release_api:
                problem(entry, "release-bin entries name the release API to ask")
            if not entry.asset_pattern:
                problem(entry, "release-bin entries carry an asset_pattern")
            else:
                try:
                    re.compile(entry.asset_pattern)
                except re.error as exc:
                    problem(entry, f"asset_pattern does not compile: {exc}")
            if not entry.bin_path or not entry.bin_path.startswith(BIN_DIR):
                problem(entry, f"release-bin entries install under {BIN_DIR}")
            elif entry.check.kind != "path" or entry.bin_path not in entry.check.values:
                problem(entry, "release-bin entries are checked by the executable they install")
            if entry.packages:
                problem(entry, "release-bin entries install one file, not packages")
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
        if (entry.script_args or entry.script_env) and entry.kind != "user-script":
            problem(entry, "only user-script entries pass their script arguments or environment")
        for name, _value in entry.script_env:
            if not ENV_NAME.match(name):
                problem(entry, f"not an environment variable name: {name!r}")
        for required in entry.requires:
            if required == entry.id or required not in ids:
                problem(entry, f"requires something not another catalog entry: {required!r}")

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
