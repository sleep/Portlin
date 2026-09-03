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
    "kimi-code", "rustdesk", "anydesk",
    "vscode", "docker", "tailscale", "syncthing", "wireshark",
    "vlc", "libreoffice", "gimp", "obs-studio", "thunderbird", "keepassxc",
    "signal", "telegram", "discord",
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

    def test_tarball_entries_unpack_under_opt(self, catalog):
        palemoon = catalog.by_id("palemoon")
        assert _only(catalog, dataclasses.replace(palemoon, opt_dir="/usr/local/palemoon"))
        assert _only(catalog, dataclasses.replace(palemoon, launcher=None))

    def test_user_script_entries_are_checked_under_home_and_warn(self, catalog):
        zed = catalog.by_id("zed")
        assert _only(catalog, dataclasses.replace(zed, check=catalog.dpkg("zed")))
        assert _only(catalog, dataclasses.replace(zed, warning=None))

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
