"""Checks on the software catalog the Software app and portlin-install share.

The catalog is data, edited by hand, and the failures that matter are the
quiet ones: a vendor URL over plain http, a sources line signed by a keyring
the entry never fetches, a kind whose installer will reach for a field the
entry does not carry. Every rule in validate() is tripped here on purpose
once, so a rule that stops firing is noticed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from conftest import load_tool

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"

# Every entry the design asked for, by id. A literal list rather than
# len(ENTRIES), so a deleted entry is named by the failure.
REQUESTED = [
    "mullvad", "qbittorrent", "deluge", "tor-browser", "chrome", "chromium",
    "brave", "palemoon", "zed", "cursor", "claude-desktop", "claude-code",
    "kimi-code", "hermes", "openclaw", "codex", "opencode", "goose", "rustdesk", "anydesk",
    "vscode", "docker", "tailscale", "syncthing", "wireshark",
    "vlc", "libreoffice", "gimp", "obs-studio", "thunderbird", "keepassxc",
    "signal", "telegram", "discord",
    "ffmpeg", "yt-dlp", "gallery-dl", "handbrake", "kleopatra", "veracrypt", "php", "jd-gui", "ghidra",
    "nmap", "metasploit", "sqlmap", "web-scanners", "password-crackers", "impacket",
    "recon-tools", "packet-tools", "mitm-tools", "wifi-tools", "radare2", "apktool",
    "debuggers", "binary-tools", "pwntools", "binwalk", "hex-editors", "forensics-tools",
    "audit-tools", "jadx", "apk-tools", "android-tools", "android-image-tools", "ios-tools",
    "ios-recovery", "ipsw", "java",
    "btop", "terminal-tools", "konsole", "sqlitebrowser", "wireguard-tools", "virt-manager", "virtualbox",
    "elementary-xfce-icons", "numix-circle-icons",
    "nvidia-driver", "intel-graphics", "amd-graphics", "broadcom-wifi", "printing",
]


@pytest.fixture(scope="module")
def catalog():
    return load_tool("catalog.py")


@pytest.fixture
def good(catalog):
    """A clean apt-repo entry to break in one place at a time."""
    return catalog.by_id("mullvad")


def _only(catalog, entry) -> list[str]:
    return catalog.validate((entry,))


class TestTheShippedCatalog:
    def test_it_compiles(self):
        source = (RUNTIME / "catalog.py").read_text()
        compile(source, str(RUNTIME / "catalog.py"), "exec")

    def test_it_imports_nothing_from_portlin(self):
        # It ships to /usr/lib/portlin on a stick that has no portlin package.
        source = (RUNTIME / "catalog.py").read_text()
        assert "from portlin" not in source and "import portlin" not in source

    def test_validate_is_clean(self, catalog):
        assert catalog.validate() == []

    def test_every_requested_entry_is_present(self, catalog):
        ids = {entry.id for entry in catalog.ENTRIES}
        missing = [wanted for wanted in REQUESTED if wanted not in ids]
        assert missing == []

    def test_every_kind_is_either_privileged_or_run_as_the_user(self, catalog):
        # The two sets partition the kinds, so there is no kind whose
        # privilege the installer has no answer for.
        assert catalog.PRIVILEGED_KINDS & catalog.USER_KINDS == set()
        assert catalog.PRIVILEGED_KINDS | catalog.USER_KINDS == set(catalog.KINDS)

    def test_every_entry_serialises_to_json(self, catalog):
        for entry in catalog.ENTRIES:
            json.dumps(catalog.to_dict(entry))

    def test_dkms_entries_warn_about_secure_boot(self, catalog):
        for entry in catalog.ENTRIES:
            if any(name.endswith("-dkms") for name in entry.packages) or entry.resolver:
                assert entry.warning and "Secure Boot" in entry.warning, entry.id


class TestWhatMovedFromTheImage:
    """Entries the default image deliberately leaves out.

    Each one is a package portlin.packages stopped installing to save space;
    the catalog is where a user gets it back. The tests here face the other
    way from tests/test_packages.py: that file proves the image skips them,
    these prove the app offers them, and that the driver entry carries what
    the purge removes.
    """

    def test_the_dropped_icon_themes_are_installable(self, catalog):
        from portlin import packages

        resolved = set(packages.resolve())
        for entry in catalog.ENTRIES:
            if entry.id in ("elementary-xfce-icons", "numix-circle-icons"):
                assert entry.packages, entry.id
                assert not any(name in resolved for name in entry.packages), (
                    f"{entry.id} installs something the image already has"
                )

    def test_the_nvidia_driver_brings_its_own_firmware(self, catalog):
        from portlin import packages

        # install.py purges firmware-nvidia-graphics at write time, so the
        # opt-in driver has to reinstall it: an NVIDIA machine whose driver
        # lands without firmware depends on the free firmware covering the
        # card, which is exactly what the proprietary driver exists for.
        assert "firmware-nvidia-graphics" in packages.NEVER_INSTALL
        entry = catalog.by_id("nvidia-driver")
        assert "firmware-nvidia-graphics" in entry.packages


class TestTheSecurityResearchPage:
    """The page of tools that act on a machine other than this one.

    Neither rule here is something validate() can know: whether an entry
    that reaches across the network says so where a person reads it, and
    whether the reversing tools sit on one page rather than half of them
    being left behind under Development.
    """

    OUTWARD = (
        "nmap", "metasploit", "sqlmap", "web-scanners", "password-crackers",
        "impacket", "recon-tools", "mitm-tools", "wifi-tools",
    )

    def test_entries_that_reach_other_machines_say_so(self, catalog):
        for entry_id in self.OUTWARD:
            notes = catalog.by_id(entry_id).notes
            assert notes and catalog.AUTHORIZED_USE in notes, entry_id

    def test_the_entries_that_reach_a_phone_say_so_too(self, catalog):
        for entry_id in ("android-image-tools", "ios-tools"):
            notes = catalog.by_id(entry_id).notes
            assert notes and catalog.AUTHORIZED_DEVICE in notes, entry_id

    def test_the_reversing_tools_share_one_page(self, catalog):
        for entry_id in ("ghidra", "jd-gui", "radare2", "apktool", "jadx", "debuggers",
                         "binary-tools"):
            assert catalog.by_id(entry_id).category == "Security research", entry_id

    def test_the_java_tools_bring_java(self, catalog):
        # Ghidra and jadx are unpacked archives, not packages, so nothing
        # pulls a JDK in behind them: without this they install and then
        # refuse to start.
        for entry_id in ("ghidra", "jadx"):
            assert catalog.by_id(entry_id).requires == ("java",), entry_id
        assert "default-jdk" in catalog.by_id("java").packages

    def test_the_phone_entries_cover_both_platforms(self, catalog):
        android = ("android-tools", "android-image-tools", "apk-tools", "jadx", "apktool")
        ios = ("ios-tools", "ios-recovery", "ipsw")
        for entry_id in android + ios:
            assert catalog.by_id(entry_id).category == "Security research", entry_id
        # adb only reaches a phone through the udev rules, and those only
        # help an account in plugdev.
        assert catalog.by_id("android-tools").add_groups == ("plugdev",)

    def test_a_search_finds_the_page_by_the_words_people_use(self, catalog):
        for query, wanted in (
            ("reverse", "ghidra"),
            ("packet", "packet-tools"),
            ("wifi", "wifi-tools"),
            ("password", "password-crackers"),
        ):
            assert wanted in {entry.id for entry in catalog.search(query)}, query


class TestTheMediaDownloaders:
    """yt-dlp and gallery-dl come from their own releases, not Debian's.

    Both break whenever a site changes and are fixed in a release within
    days; Debian's packages freeze at a release. The rules here are the ones
    validate() cannot know: that the entries left apt, that the pattern each
    carries picks the one Linux executable out of a real release's asset
    list, and that yt-dlp still gets the ffmpeg it merges streams with.
    """

    # The asset names a release of each really carries, from the forge.
    YT_DLP_ASSETS = (
        "SHA2-256SUMS", "SHA2-256SUMS.sig", "yt-dlp", "yt-dlp.exe", "yt-dlp.tar.gz",
        "yt-dlp_linux", "yt-dlp_linux.zip", "yt-dlp_linux_aarch64", "yt-dlp_linux_aarch64.zip",
        "yt-dlp_linux_armv7l.zip", "yt-dlp_macos", "yt-dlp_musllinux", "yt-dlp_win.zip",
        "_update_spec",
    )
    GALLERY_DL_ASSETS = (
        "gallery_dl-1.32.15-py3-none-any.whl", "gallery_dl-1.32.15.tar.gz", "gallery-dl_x86.exe",
        "gallery-dl.bin", "gallery-dl.bin.sig", "gallery-dl.exe", "gallery-dl.exe.sig",
        "SHA256SUMS", "SHA256SUMS.sig",
    )

    def test_neither_comes_from_debian_any_more(self, catalog):
        for entry_id in ("yt-dlp", "gallery-dl"):
            entry = catalog.by_id(entry_id)
            assert entry.kind == "release-bin", entry_id
            assert entry.packages == (), entry_id

    def test_each_pattern_picks_exactly_the_linux_executable(self, catalog):
        import re

        for entry_id, assets, wanted in (
            ("yt-dlp", self.YT_DLP_ASSETS, "yt-dlp_linux"),
            ("gallery-dl", self.GALLERY_DL_ASSETS, "gallery-dl.bin"),
        ):
            pattern = re.compile(catalog.by_id(entry_id).asset_pattern)
            assert [name for name in assets if pattern.search(name)] == [wanted], entry_id

    def test_yt_dlp_asks_the_project_not_debian(self, catalog):
        # The nightly channel is the one the project recommends for people
        # who use it rather than develop it.
        assert catalog.by_id("yt-dlp").release_api == catalog.github_releases(
            "yt-dlp/yt-dlp-nightly-builds"
        )

    def test_gallery_dl_asks_codeberg_where_its_releases_live(self, catalog):
        # The GitHub mirror's releases carry only the source archives.
        assert catalog.by_id("gallery-dl").release_api == catalog.codeberg_releases(
            "mikf/gallery-dl"
        )

    def test_yt_dlp_still_brings_ffmpeg(self, catalog):
        # The apt entry used to install ffmpeg alongside; a release binary
        # cannot, so it is a requirement the Software app installs first.
        assert catalog.by_id("yt-dlp").requires == ("ffmpeg",)
        ffmpeg = catalog.by_id("ffmpeg")
        assert ffmpeg.kind == "apt" and ffmpeg.packages == ("ffmpeg",)

    def test_both_land_ahead_of_any_debian_package_on_path(self, catalog):
        for entry_id in ("yt-dlp", "gallery-dl"):
            entry = catalog.by_id(entry_id)
            assert entry.bin_path.startswith("/usr/local/bin/"), entry_id
            assert entry.check == catalog.path(entry.bin_path), entry_id

    def test_both_say_how_to_update(self, catalog):
        # Nothing on the stick upgrades them: apt does not know they exist.
        # The binary itself does, and the entry is where a person reads that.
        for entry_id in ("yt-dlp", "gallery-dl"):
            notes = catalog.by_id(entry_id).notes
            assert notes and f"sudo {entry_id} -U" in notes, entry_id


class TestValidationRules:
    def test_ids_are_lowercase_hyphenated(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, id="Mullvad_VPN"))

    def test_duplicate_ids_are_reported(self, catalog, good):
        assert any("duplicate" in p for p in catalog.validate((good, good)))

    def test_unknown_category(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, category="Games"))

    def test_unknown_kind(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, kind="snap"))

    def test_http_urls_are_refused(self, catalog, good):
        repo = dataclasses.replace(good.repo, key_url="http://repository.mullvad.net/k.asc")
        assert any("https" in p for p in _only(catalog, dataclasses.replace(good, repo=repo)))

    def test_http_inside_a_sources_line_is_refused(self, catalog, good):
        line = good.repo.sources_line.replace("https://", "http://")
        repo = dataclasses.replace(good.repo, sources_line=line)
        assert _only(catalog, dataclasses.replace(good, repo=repo))

    def test_bad_package_names(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, packages=("Mullvad VPN",)))

    def test_dashes_are_refused_anywhere(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, summary="VPN – private"))

    def test_apt_entries_need_packages(self, catalog):
        entry = dataclasses.replace(catalog.by_id("vlc"), packages=())
        assert _only(catalog, entry)

    def test_apt_entries_with_a_resolver_may_leave_the_driver_unnamed(self, catalog):
        # nvidia-detect picks the metapackage at install time.
        assert _only(catalog, catalog.by_id("nvidia-driver")) == []

    def test_a_repo_has_exactly_one_sources_form(self, catalog, good):
        both = dataclasses.replace(good.repo, sources_url="https://x/y.sources")
        neither = dataclasses.replace(good.repo, sources_line=None)
        assert _only(catalog, dataclasses.replace(good, repo=both))
        assert _only(catalog, dataclasses.replace(good, repo=neither))

    def test_keyrings_live_where_apt_looks(self, catalog, good):
        repo = dataclasses.replace(good.repo, keyring_path="/root/mullvad.asc")
        assert _only(catalog, dataclasses.replace(good, repo=repo))

    def test_sources_live_in_sources_list_d(self, catalog, good):
        repo = dataclasses.replace(good.repo, sources_path="/etc/apt/mullvad.list")
        assert _only(catalog, dataclasses.replace(good, repo=repo))

    def test_a_sources_line_is_signed_by_the_keyring_it_fetches(self, catalog, good):
        # A line signed by some other path is a repository apt will refuse
        # after the key has been fetched to the wrong place.
        line = good.repo.sources_line.replace("mullvad-keyring.asc", "other.asc", 1)
        repo = dataclasses.replace(good.repo, sources_line=line)
        assert _only(catalog, dataclasses.replace(good, repo=repo))

    def test_deb_url_entries_install_the_file_not_packages(self, catalog):
        chrome = catalog.by_id("chrome")
        assert _only(catalog, dataclasses.replace(chrome, packages=("google-chrome-stable",)))
        assert _only(catalog, dataclasses.replace(chrome, url=None))

    def test_github_deb_entries_need_a_repo_and_a_pattern(self, catalog):
        rustdesk = catalog.by_id("rustdesk")
        assert _only(catalog, dataclasses.replace(rustdesk, github_repo="rustdesk"))
        assert _only(catalog, dataclasses.replace(rustdesk, asset_pattern="("))

    def test_release_bin_entries_name_a_release_a_pattern_and_a_binary(self, catalog):
        gallery = catalog.by_id("gallery-dl")
        assert _only(catalog, gallery) == []
        assert _only(catalog, dataclasses.replace(gallery, release_api=None))
        assert _only(catalog, dataclasses.replace(gallery, asset_pattern="("))
        assert _only(catalog, dataclasses.replace(gallery, bin_path="/usr/bin/gallery-dl"))
        assert _only(catalog, dataclasses.replace(gallery, packages=("gallery-dl",)))

    def test_release_bin_entries_are_checked_by_the_binary_they_install(self, catalog):
        # A check on some other path is an entry that says "not installed"
        # forever, or "installed" after a removal.
        ytdlp = catalog.by_id("yt-dlp")
        assert _only(catalog, dataclasses.replace(ytdlp, check=catalog.path("/usr/local/bin/x")))
        assert _only(catalog, dataclasses.replace(ytdlp, check=catalog.dpkg("yt-dlp")))

    def test_a_release_api_over_plain_http_is_refused(self, catalog):
        ytdlp = catalog.by_id("yt-dlp")
        http = ytdlp.release_api.replace("https://", "http://")
        assert any("https" in p for p in _only(catalog, dataclasses.replace(ytdlp, release_api=http)))

    def test_tarball_entries_unpack_under_opt(self, catalog):
        palemoon = catalog.by_id("palemoon")
        assert _only(catalog, dataclasses.replace(palemoon, opt_dir="/usr/local/palemoon"))
        assert _only(catalog, dataclasses.replace(palemoon, launcher=None))

    def test_abtop_comes_from_its_fork_and_says_how_the_hud_uses_it(self, catalog):
        entry = catalog.by_id("abtop")
        assert entry.kind == "user-script" and entry.category == "AI tools"
        assert entry.url.startswith("https://github.com/sleep/abtop/releases/latest/download/")
        assert entry.check == catalog.path("~/.cargo/bin/abtop")
        assert "abtop --setup" in entry.notes

    def test_user_script_entries_are_checked_under_home_and_warn(self, catalog):
        zed = catalog.by_id("zed")
        assert _only(catalog, dataclasses.replace(zed, check=catalog.dpkg("zed")))
        assert _only(catalog, dataclasses.replace(zed, warning=None))

    def test_only_user_scripts_pass_their_script_arguments_or_environment(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, script_args=("--yes",)))
        assert _only(catalog, dataclasses.replace(good, script_env=(("CONFIGURE", "false"),)))

    def test_an_entry_requires_only_other_entries_in_the_catalog(self, catalog):
        hermes = catalog.by_id("hermes")
        build_tools = catalog.by_id("build-tools")
        assert catalog.validate((hermes, build_tools)) == []
        assert catalog.validate((hermes,))
        assert _only(catalog, dataclasses.replace(build_tools, requires=("build-tools",)))

    def test_missing_requirements_are_the_ones_not_installed(self, catalog, tmp_path):
        hermes = catalog.by_id("hermes")
        assert [e.id for e in catalog.missing_requirements(hermes, set(), tmp_path)] == ["build-tools"]
        assert catalog.missing_requirements(hermes, {"build-essential"}, tmp_path) == []

    def test_script_environment_names_are_variable_names(self, catalog):
        zed = catalog.by_id("zed")
        assert _only(catalog, dataclasses.replace(zed, script_env=(("not a name", "1"),)))
        assert _only(catalog, dataclasses.replace(zed, script_env=(("CONFIGURE", "false"),))) == []

    def test_every_entry_has_a_check(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, check=catalog.Check("dpkg", ())))
        assert _only(catalog, dataclasses.replace(good, check=catalog.Check("magic", ("x",))))

    def test_path_checks_are_absolute_or_under_home(self, catalog):
        zed = catalog.by_id("zed")
        assert _only(catalog, dataclasses.replace(zed, check=catalog.path("bin/zed")))

    def test_unknown_resolver(self, catalog, good):
        assert _only(catalog, dataclasses.replace(good, resolver="ubuntu-drivers"))

    def test_only_apt_kinds_need_components(self, catalog):
        chrome = catalog.by_id("chrome")
        assert _only(catalog, dataclasses.replace(chrome, needs_components=("non-free",)))


class TestLookups:
    def test_by_id_raises_on_unknown(self, catalog):
        with pytest.raises(KeyError):
            catalog.by_id("emacs")

    def test_search_matches_name_summary_id_and_package(self, catalog):
        assert {e.id for e in catalog.search("torrent")} >= {"qbittorrent", "deluge"}
        assert [e.id for e in catalog.search("docker.io")] == ["docker"]
        assert catalog.search("nothing-matches-this") == []

    def test_search_is_case_insensitive_and_needs_every_word(self, catalog):
        assert [e.id for e in catalog.search("Claude desktop")] == ["claude-desktop"]

    def test_empty_search_returns_everything(self, catalog):
        assert catalog.search("  ") == list(catalog.ENTRIES)

    def test_by_category_follows_display_order_and_keeps_empty_ones(self, catalog):
        grouped = catalog.by_category()
        assert list(grouped) == list(catalog.CATEGORIES)
        assert grouped["Drivers"][0].id == "nvidia-driver"
        assert list(catalog.by_category(())) == list(catalog.CATEGORIES)

    def test_is_privileged(self, catalog):
        assert catalog.is_privileged(catalog.by_id("vlc"))
        assert catalog.is_privileged(catalog.by_id("cursor"))
        assert not catalog.is_privileged(catalog.by_id("zed"))


class TestInstalledState:
    def test_parse_dpkg_status_keeps_only_installed(self, catalog):
        text = "tmux installed\nvlc config-files\nfoo half-installed\nbad line here\n"
        assert catalog.parse_dpkg_status(text) == {"tmux"}

    def test_dpkg_check_is_any_of(self, catalog):
        nvidia = catalog.by_id("nvidia-driver")
        assert catalog.installed(nvidia, {"nvidia-tesla-535-driver"}, Path("/home/x"))
        assert not catalog.installed(nvidia, {"nouveau"}, Path("/home/x"))

    def test_path_check_expands_home(self, catalog, tmp_path):
        zed = catalog.by_id("zed")
        assert not catalog.installed(zed, set(), tmp_path)
        (tmp_path / ".local" / "bin").mkdir(parents=True)
        (tmp_path / ".local" / "bin" / "zed").touch()
        assert catalog.installed(zed, set(), tmp_path)

    def test_path_check_with_an_absolute_path_ignores_home(self, catalog, tmp_path):
        palemoon = catalog.by_id("palemoon")
        assert catalog.expand_home("/opt/palemoon/palemoon", tmp_path) == Path("/opt/palemoon/palemoon")
        assert not catalog.installed(palemoon, set(), tmp_path)
