"""What portlin-install would run, asserted as exact commands.

The tool is a planner and an executor: every install is a list of steps built
by pure functions from a catalog entry and a description of the system, and
only then run. That split is what makes this file possible on a machine with
no apt, no dpkg and no root. What is checked here is the part that has to be
right before anything is run at all: the order of the steps, the exact argv
of each one, and the two refusals that keep root out of a home directory and
a home directory's tools out of root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import load_tool

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
TOOL = RUNTIME / "portlin-install"


@pytest.fixture(scope="module")
def tool():
    return load_tool("portlin-install")


@pytest.fixture(scope="module")
def catalog():
    return load_tool("catalog.py")


@pytest.fixture
def ctx(tool):
    """A stick as portlin writes one: contrib and non-free-firmware, no non-free."""
    return tool.Context(
        root=True,
        codename="trixie",
        sources=(
            ("http://deb.debian.org/debian", "trixie", ("main", "contrib", "non-free-firmware")),
            ("http://deb.debian.org/debian", "trixie-updates", ("main", "contrib", "non-free-firmware")),
            ("http://security.debian.org/debian-security", "trixie-security",
             ("main", "contrib", "non-free-firmware")),
        ),
        components=("main", "contrib", "non-free-firmware"),
        dropin_components=(),
        user="somebody",
        home="/home/somebody",
        kernel="6.12.30-amd64",
        download_dir="/var/cache/portlin/downloads",
        state_dir="/var/lib/portlin/software",
    )


def argvs(steps) -> list[tuple[str, ...]]:
    return [step.argv for step in steps if step.argv]


def flat(steps) -> str:
    """Every step rendered as one string, for order assertions."""
    parts = []
    for step in steps:
        if step.argv:
            parts.append("run " + " ".join(step.argv))
        if step.write:
            parts.append("write " + step.write[0])
        if step.mkdir:
            parts.append("mkdir " + step.mkdir)
        for path in step.remove:
            parts.append("remove " + path)
        if step.check_file:
            parts.append("check " + step.check_file)
        if step.warn:
            parts.append("warn " + step.warn)
    return "\n".join(parts)


class TestCommandBuilders:
    def test_apt_waits_for_the_lock_and_never_prompts(self, tool):
        argv = tool.apt_argv("install", "tmux")
        assert argv[0] == "apt-get" and "-y" in argv
        assert "DPkg::Lock::Timeout=300" in argv
        assert "Dpkg::Options::=--force-confold" in argv
        assert "Dpkg::Options::=--force-confdef" in argv
        assert dict(tool.APT_ENV)["DEBIAN_FRONTEND"] == "noninteractive"

    def test_curl_follows_redirects_fails_on_errors_and_retries(self, tool):
        argv = tool.curl_argv("https://example.test/x.deb", "/tmp/x.deb")
        assert argv[:2] == ("curl", "-fsSL")
        assert "--retry" in argv
        assert argv[-2:] == ("/tmp/x.deb", "https://example.test/x.deb")

    def test_headers_package_names_the_running_kernel(self, tool):
        assert tool.headers_package("6.12.30-amd64") == "linux-headers-6.12.30-amd64"


class TestInvokingUser:
    def _pw(self, name, home):
        class Record:
            pw_name = name
            pw_dir = home
        return lambda uid: Record()

    def test_pkexec_wins_over_sudo(self, tool):
        found = tool.invoking_user(
            {"PKEXEC_UID": "1000", "SUDO_UID": "1001"}, self._pw("a", "/home/a")
        )
        assert found == ("a", "/home/a")

    def test_sudo_when_there_is_no_pkexec(self, tool):
        assert tool.invoking_user({"SUDO_UID": "1000"}, self._pw("b", "/home/b"))[0] == "b"

    def test_nobody_when_root_ran_it_directly(self, tool):
        assert tool.invoking_user({}, self._pw("x", "/home/x")) is None

    def test_an_unresolvable_uid_is_nobody(self, tool):
        def raiser(uid):
            raise KeyError(uid)

        assert tool.invoking_user({"SUDO_UID": "4242"}, raiser) is None


class TestComponents:
    def test_reads_the_sources_list_portlin_writes(self, tool):
        from portlin import templates

        text = templates.render_sources_list(
            suite="trixie",
            mirror="http://deb.debian.org/debian",
            security_mirror="http://security.debian.org/debian-security",
            components="main contrib non-free-firmware",
        )
        parsed = tool.parse_sources_list(text)
        assert ("http://deb.debian.org/debian", "trixie",
                ("main", "contrib", "non-free-firmware")) in parsed
        assert any(suite == "trixie-security" for _, suite, _ in parsed)

    def test_ignores_comments_and_reads_past_options(self, tool):
        text = (
            "# a comment\n"
            "deb [signed-by=/x.asc arch=amd64] https://vendor.test/deb stable main\n"
            "deb-src http://deb.debian.org/debian trixie main\n"
        )
        assert tool.parse_sources_list(text) == [
            ("https://vendor.test/deb", "stable", ("main",))
        ]

    def test_nothing_missing_when_everything_is_enabled(self, tool):
        assert tool.missing_components(("contrib",), ("main", "contrib")) == ()

    def test_the_drop_in_names_one_stanza_per_mirror(self, tool, ctx):
        rendered = tool.render_component_sources(ctx.sources, ("non-free",))
        assert rendered == (
            "# Written by portlin-install. Adds archive components the stick was built\n"
            "# without. Delete this file to take them away again.\n"
            "Types: deb\n"
            "URIs: http://deb.debian.org/debian\n"
            "Suites: trixie trixie-updates\n"
            "Components: non-free\n"
            "\n"
            "Types: deb\n"
            "URIs: http://security.debian.org/debian-security\n"
            "Suites: trixie-security\n"
            "Components: non-free\n"
        )

    def test_parse_deb822_components_reads_the_drop_in_back(self, tool, ctx):
        rendered = tool.render_component_sources(ctx.sources, ("non-free", "contrib"))
        assert tool.parse_deb822_components(rendered) == ("non-free", "contrib")

    def test_an_entry_from_an_enabled_component_needs_no_drop_in(self, tool, catalog, ctx):
        assert tool.plan_components(catalog.by_id("tor-browser"), ctx) == []

    def test_a_missing_component_is_written_then_apt_is_refreshed(self, tool, catalog, ctx):
        steps = tool.plan_components(catalog.by_id("nvidia-driver"), ctx)
        assert steps[0].write[0] == "/etc/apt/sources.list.d/portlin-components.sources"
        assert "non-free" in steps[0].write[1]
        assert steps[1].argv == tool.apt_argv("update")

    def test_a_second_component_joins_the_first_rather_than_replacing_it(self, tool, catalog, ctx):
        import dataclasses

        # A stick built without contrib, whose drop-in already added non-free.
        already = dataclasses.replace(
            ctx,
            dropin_components=("non-free",),
            components=("main", "non-free-firmware", "non-free"),
        )
        steps = tool.plan_components(catalog.by_id("broadcom-wifi"), already)
        assert "Components: non-free contrib" in steps[0].write[1]


class TestAptEntries:
    def test_installs_exactly_its_packages_with_no_refresh(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("vlc"), ctx)
        assert argvs(steps) == [tool.apt_argv("install", "vlc")]

    def test_several_packages_go_in_one_transaction(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("remmina"), ctx)
        assert argvs(steps) == [
            tool.apt_argv("install", "remmina", "remmina-plugin-rdp", "remmina-plugin-vnc")
        ]

    def test_wireshark_answers_its_question_before_installing(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("wireshark"), ctx)
        rendered = flat(steps)
        assert rendered.index("debconf-set-selections") < rendered.index("install wireshark")
        preseeded = [step.write[1] for step in steps if step.write][0]
        assert "install-setuid boolean true" in preseeded
        assert "usermod -aG wireshark somebody" in rendered

    def test_docker_adds_the_invoking_user_to_the_group(self, tool, catalog, ctx):
        assert ("usermod", "-aG", "docker", "somebody") in argvs(
            tool.plan_install(catalog.by_id("docker"), ctx)
        )

    def test_with_no_invoking_user_the_group_is_only_explained(self, tool, catalog, ctx):
        import dataclasses

        steps = tool.plan_install(catalog.by_id("docker"), dataclasses.replace(ctx, user=None))
        assert not any(step.argv and step.argv[0] == "usermod" for step in steps)
        assert any(step.warn and "docker group" in step.warn for step in steps)

    def test_flatpak_adds_flathub_after_installing(self, tool, catalog, ctx):
        rendered = flat(tool.plan_install(catalog.by_id("flatpak"), ctx))
        assert rendered.index("install flatpak") < rendered.index("flatpak remote-add")

    def test_a_dkms_entry_asks_for_the_running_kernel_headers(self, tool, catalog, ctx):
        argv = argvs(tool.plan_install(catalog.by_id("broadcom-wifi"), ctx))[-1]
        assert "broadcom-sta-dkms" in argv
        assert "linux-headers-amd64" in argv
        assert "linux-headers-6.12.30-amd64" in argv

    def test_missing_headers_for_the_running_kernel_only_warn(self, tool, catalog, ctx):
        import dataclasses

        stale = dataclasses.replace(ctx, kernel_headers_available=False)
        steps = tool.plan_install(catalog.by_id("broadcom-wifi"), stale)
        assert any(step.warn and "Reboot" in step.warn for step in steps)
        assert "linux-headers-6.12.30-amd64" not in " ".join(argvs(steps)[-1])

    def test_a_dkms_entry_asks_for_a_reboot(self, tool, catalog, ctx):
        assert tool.needs_reboot(tool.plan_install(catalog.by_id("broadcom-wifi"), ctx))
        assert not tool.needs_reboot(tool.plan_install(catalog.by_id("vlc"), ctx))


class TestNvidia:
    def test_the_detector_is_installed_and_asked_first(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("nvidia-driver"), ctx)
        rendered = flat(steps)
        assert rendered.index("portlin-components.sources") < rendered.index("nvidia-detect")
        assert steps[-1].argv == ("nvidia-detect",)
        assert steps[-1].capture

    def test_the_driver_it_names_is_installed_with_the_headers(self, tool, catalog, ctx):
        steps = tool.plan_install(
            catalog.by_id("nvidia-driver"), ctx, resolved_packages=("nvidia-driver",)
        )
        argv = argvs(steps)[-1]
        assert "nvidia-driver" in argv
        assert "linux-headers-amd64" in argv
        assert "linux-headers-6.12.30-amd64" in argv

    def test_it_reads_the_metapackage_out_of_the_detector_output(self, tool):
        text = (
            "Detected NVIDIA GPUs:\n"
            "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA104 [10de:2484]\n"
            "Your card is supported by the default drivers.\n"
            "It is recommended to install the\n"
            "    nvidia-driver\n"
            "package.\n"
        )
        assert tool.parse_nvidia_detect(text) == "nvidia-driver"

    def test_it_reads_a_legacy_metapackage_too(self, tool):
        text = "It is recommended to install the\n    nvidia-tesla-535-driver\npackage.\n"
        assert tool.parse_nvidia_detect(text) == "nvidia-tesla-535-driver"

    def test_an_unsupported_card_names_nothing(self, tool):
        assert tool.parse_nvidia_detect("Your card is not supported by any driver.") is None


class TestVendorRepositories:
    def test_the_key_lands_before_the_sources_before_the_refresh(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        rendered = flat(tool.plan_install(entry, ctx))
        assert (
            rendered.index(entry.repo.key_url)
            < rendered.index("write " + entry.repo.sources_path)
            < rendered.index("apt-get -y -q")
        )

    def test_a_key_that_did_not_download_stops_the_plan(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        steps = tool.plan_install(entry, ctx)
        assert any(step.check_file == entry.repo.keyring_path for step in steps)

    def test_the_sources_line_is_signed_by_the_key_just_fetched(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        written = [s.write[1] for s in tool.plan_install(entry, ctx) if s.write][0]
        assert f"signed-by={entry.repo.keyring_path}" in written

    def test_a_vendor_served_sources_file_is_fetched_into_place(self, tool, catalog, ctx):
        entry = catalog.by_id("brave")
        steps = tool.plan_install(entry, ctx)
        assert tool.curl_argv(entry.repo.sources_url, entry.repo.sources_path) in argvs(steps)

    def test_the_codename_is_substituted_into_vendor_urls(self, tool, catalog, ctx):
        rendered = flat(tool.plan_install(catalog.by_id("tailscale"), ctx))
        assert "{codename}" not in rendered
        assert "debian/trixie.noarmor.gpg" in rendered
        assert "debian/trixie.tailscale-keyring.list" in rendered


class TestDownloadedPackages:
    def test_a_deb_is_installed_through_apt_so_its_dependencies_resolve(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("chrome"), ctx)
        deb = "/var/cache/portlin/downloads/chrome.deb"
        assert argvs(steps) == [
            tool.curl_argv(catalog.by_id("chrome").url, deb),
            tool.apt_argv("install", deb),
        ]
        assert not any(step.argv and step.argv[0] == "dpkg" for step in steps)

    def test_the_download_is_checked_and_then_cleaned_up(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("chrome"), ctx)
        deb = "/var/cache/portlin/downloads/chrome.deb"
        assert any(step.check_file == deb for step in steps)
        assert any(deb in step.remove for step in steps)

    def test_the_release_asset_is_the_one_for_this_architecture(self, tool):
        payload = {
            "assets": [
                {"name": "rustdesk-1.4.9-aarch64.deb", "browser_download_url": "https://x/arm.deb"},
                {"name": "rustdesk-1.4.9-x86_64.deb", "browser_download_url": "https://x/amd.deb"},
                {"name": "rustdesk-1.4.9-x86_64.rpm", "browser_download_url": "https://x/amd.rpm"},
            ]
        }
        pattern = r"^rustdesk-\d+\.\d+\.\d+-x86_64\.deb$"
        assert tool.pick_release_asset(payload, pattern) == "https://x/amd.deb"

    def test_a_release_with_no_matching_asset_is_an_error(self, tool):
        with pytest.raises(ValueError):
            tool.pick_release_asset({"assets": []}, r"^rustdesk.*\.deb$")


class TestTarballs:
    def test_the_archive_is_listed_before_it_is_unpacked(self, tool, catalog, ctx):
        rendered = flat(tool.plan_install(catalog.by_id("palemoon"), ctx))
        assert rendered.index("tar -tJf") < rendered.index("tar -xJf")

    def test_it_strips_the_top_directory_into_opt(self, tool, catalog, ctx):
        entry = catalog.by_id("palemoon")
        unpack = [a for a in argvs(tool.plan_install(entry, ctx)) if a[:2] == ("tar", "-xJf")][0]
        assert unpack[-3:] == ("-C", entry.opt_dir, "--strip-components=1")

    def test_a_menu_entry_is_written_pointing_into_opt(self, tool, catalog, ctx):
        entry = catalog.by_id("palemoon")
        written = {s.write[0]: s.write[1] for s in tool.plan_install(entry, ctx) if s.write}
        rendered = written["/usr/share/applications/portlin-palemoon.desktop"]
        assert f"Exec={entry.opt_dir}/{entry.launcher} %u" in rendered
        assert "Terminal=false" in rendered
        assert "Categories=Network;WebBrowser;" in rendered

    def test_the_menu_entry_parses_as_a_desktop_file(self, tool, catalog):
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(tool.render_desktop_entry(catalog.by_id("palemoon")))
        assert parser["Desktop Entry"]["Type"] == "Application"


class TestVendorScripts:
    def test_the_script_is_downloaded_to_a_file_and_never_piped(self, tool, catalog, ctx):
        import dataclasses

        entry = catalog.by_id("zed")
        user_ctx = dataclasses.replace(ctx, root=False, download_dir="/home/somebody/.cache/portlin")
        steps = tool.plan_install(entry, user_ctx)
        script = "/home/somebody/.cache/portlin/zed-install.sh"
        assert argvs(steps) == [tool.curl_argv(entry.url, script), ("bash", script)]
        for step in steps:
            assert not step.argv or "|" not in " ".join(step.argv)

    def test_a_truncated_download_is_caught_before_it_runs(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("zed"), ctx)
        rendered = flat(steps)
        assert rendered.index("check ") < rendered.index("run bash")

    def test_the_script_is_deleted_afterwards(self, tool, catalog, ctx):
        steps = tool.plan_install(catalog.by_id("zed"), ctx)
        assert any(step.remove for step in steps)


class TestRemoval:
    def test_it_purges_what_the_record_says_was_installed(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        record = {"packages": ["mullvad-vpn"], "paths": [entry.repo.sources_path]}
        steps = tool.plan_remove(entry, ctx, record)
        assert tool.apt_argv("purge", "mullvad-vpn") in argvs(steps)
        assert any(entry.repo.sources_path in step.remove for step in steps)

    def test_with_no_record_it_falls_back_to_the_catalog(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        steps = tool.plan_remove(entry, ctx, None)
        assert tool.apt_argv("purge", "mullvad-vpn") in argvs(steps)
        assert any(entry.repo.keyring_path in step.remove for step in steps)

    def test_a_downloaded_deb_is_purged_by_the_name_its_check_uses(self, tool, catalog, ctx):
        steps = tool.plan_remove(catalog.by_id("chrome"), ctx, None)
        assert tool.apt_argv("purge", "google-chrome-stable") in argvs(steps)

    def test_a_tarball_takes_its_directory_and_menu_entry_with_it(self, tool, catalog, ctx):
        removed = flat(tool.plan_remove(catalog.by_id("palemoon"), ctx, None))
        assert "remove /opt/palemoon" in removed
        assert "remove /usr/share/applications/portlin-palemoon.desktop" in removed

    def test_a_user_script_removes_the_paths_it_named_under_home(self, tool, catalog, ctx):
        removed = flat(tool.plan_remove(catalog.by_id("zed"), ctx, None))
        assert "remove /home/somebody/.local/bin/zed" in removed
        assert "~" not in removed

    def test_a_user_script_with_nowhere_named_only_explains(self, tool, catalog, ctx):
        import dataclasses

        entry = dataclasses.replace(catalog.by_id("zed"), remove_paths=())
        steps = tool.plan_remove(entry, ctx, None)
        assert all(not step.remove for step in steps)
        assert any(step.warn for step in steps)


class TestUpgrade:
    def test_it_refreshes_then_upgrades_everything(self, tool, ctx):
        assert argvs(tool.plan_upgrade(ctx))[:2] == [
            tool.apt_argv("update"),
            tool.apt_argv("full-upgrade"),
        ]


class TestWhatAnInstallRecords:
    def test_it_records_the_packages_and_the_repository_files(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        record = tool.record_for(entry, ctx, tool.plan_install(entry, ctx))
        assert record["packages"] == ["mullvad-vpn"]
        assert entry.repo.keyring_path in record["paths"]

    def test_it_records_the_driver_nvidia_detect_chose(self, tool, catalog, ctx):
        entry = catalog.by_id("nvidia-driver")
        record = tool.record_for(entry, ctx, [], resolved_packages=("nvidia-driver",))
        assert "nvidia-driver" in record["packages"]
        assert "linux-headers-6.12.30-amd64" in record["packages"]

    def test_it_is_json(self, tool, catalog, ctx):
        entry = catalog.by_id("zed")
        json.dumps(tool.record_for(entry, ctx, []))


class TestPrivilege:
    def test_a_privileged_entry_refuses_without_root(self, tool, catalog, ctx):
        import dataclasses

        assert tool.check_privilege(
            catalog.by_id("vlc"), dataclasses.replace(ctx, root=False)
        ) == tool.EXIT_PRIVILEGE

    def test_a_user_entry_refuses_as_root(self, tool, catalog, ctx):
        assert tool.check_privilege(catalog.by_id("zed"), ctx) == tool.EXIT_PRIVILEGE

    def test_each_is_allowed_on_its_own_side(self, tool, catalog, ctx):
        import dataclasses

        assert tool.check_privilege(catalog.by_id("vlc"), ctx) == tool.EXIT_OK
        assert tool.check_privilege(
            catalog.by_id("zed"), dataclasses.replace(ctx, root=False)
        ) == tool.EXIT_OK

    def test_nothing_is_spawned_when_privilege_is_wrong(self, tool, catalog, ctx, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("a command was run despite the privilege check")

        monkeypatch.setattr(tool.subprocess, "Popen", forbidden)
        args = tool.build_parser().parse_args(["install", "zed"])
        assert args.func(args, ctx) == tool.EXIT_PRIVILEGE


class TestRunningAPlan:
    def test_a_dry_run_prints_every_command_and_runs_none(
        self, tool, catalog, ctx, monkeypatch, capsys
    ):
        def forbidden(*args, **kwargs):
            raise AssertionError("a dry run spawned a command")

        monkeypatch.setattr(tool.subprocess, "Popen", forbidden)
        result = tool.run_plan(tool.plan_install(catalog.by_id("mullvad"), ctx), dry_run=True)
        printed = capsys.readouterr().out
        assert result.ok
        assert "would run: curl" in printed
        assert "would write /etc/apt/sources.list.d/mullvad.list" in printed

    def test_a_dry_run_writes_nothing(self, tool, catalog, ctx, tmp_path):
        import dataclasses

        entry = catalog.by_id("mullvad")
        sandbox = dataclasses.replace(ctx, download_dir=str(tmp_path / "downloads"))
        tool.run_plan(tool.plan_install(entry, sandbox), dry_run=True)
        assert not (tmp_path / "downloads").exists()

    def test_every_event_line_is_spelled_by_one_function(self):
        # A protocol whose lines are written out by hand in a dozen places is
        # one the app stops understanding after a typo nobody notices.
        source = TOOL.read_text()
        for line in source.splitlines():
            if '"::' in line or "'::" in line:
                assert "def format_event" in line or "return " in line, line

    def test_progress_comes_from_apts_own_status_stream(self, tool, capsys):
        tool._status_line("pmstatus:tmux:42.7:Unpacking tmux", None)
        assert capsys.readouterr().out.strip() == "::progress 42"

    def test_a_status_line_it_cannot_read_is_ignored(self, tool, capsys):
        tool._status_line("pmstatus:tmux:unknown:x", None)
        tool._status_line("something else entirely", None)
        assert capsys.readouterr().out == ""

    def test_a_failing_step_stops_the_plan(self, tool, monkeypatch, capsys):
        steps = [
            tool.Step("first", argv=("true",)),
            tool.Step("second", argv=("false",)),
            tool.Step("third", argv=("echo", "never")),
        ]
        result = tool.run_plan(steps)
        assert not result.ok
        assert "exited with status" in result.failure
        assert "never" not in capsys.readouterr().out

    def test_it_captures_output_a_planner_needs_back(self, tool):
        steps = [tool.Step("ask", argv=("echo", "It is recommended to install the"),
                           capture=True)]
        assert "recommended" in tool.run_plan(steps).captured


# Real `lspci -nn` output, trimmed to the lines a scan looks at. A hybrid
# laptop: Intel display, NVIDIA on the side, and a Broadcom wifi card the
# in-tree drivers are known to struggle with.
LAPTOP_LSPCI = """\
00:00.0 Host bridge [0600]: Intel Corporation Xeon E3-1200 v6/7th Gen Core Processor Host Bridge/DRAM Registers [8086:5904] (rev 02)
00:02.0 VGA compatible controller [0300]: Intel Corporation HD Graphics 620 [8086:5916] (rev 02)
00:1f.3 Audio device [0403]: Intel Corporation Sunrise Point-LP HD Audio [8086:9d71] (rev 21)
01:00.0 3D controller [0302]: NVIDIA Corporation GP108M [GeForce MX150] [10de:1d10] (rev a1)
02:00.0 Network controller [0280]: Broadcom Inc. and subsidiaries BCM4322 802.11a/b/g/n Wireless LAN Controller [14e4:432b] (rev 01)
03:00.0 Ethernet controller [0200]: Realtek Semiconductor Co., Ltd. RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller [10ec:8168] (rev 15)
"""

# A QEMU guest, which is what the harness and every developer sees.
QEMU_LSPCI = """\
00:00.0 Host bridge [0600]: Intel Corporation 440FX - 82441FX PMC [Natoma] [8086:1237] (rev 02)
00:02.0 VGA compatible controller [0300]: Device [1234:1111] (rev 02)
00:03.0 Ethernet controller [0200]: Red Hat, Inc. Virtio network device [1af4:1000]
"""

# A desktop with an AMD card and an Intel wifi card, so the AMD branch and
# the "a Broadcom card that does not need wl" case are both covered.
AMD_LSPCI = """\
00:02.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Navi 23 [Radeon RX 6600] [1002:73ff] (rev c7)
02:00.0 Network controller [0280]: Broadcom Inc. and subsidiaries BCM4360 802.11ac Wireless Network Adapter [14e4:43ba] (rev 03)
"""


class TestReadingTheHardware:
    def test_it_reads_slot_class_vendor_and_device(self, tool):
        devices = {d.slot: d for d in tool.parse_lspci(LAPTOP_LSPCI)}
        gpu = devices["01:00.0"]
        assert (gpu.class_code, gpu.vendor, gpu.device) == ("0302", "10de", "1d10")
        assert "GeForce MX150" in gpu.name

    def test_it_ignores_anything_without_ids(self, tool):
        assert tool.parse_lspci("not an lspci line at all\n\n") == []

    def test_it_reads_every_line_of_a_real_report(self, tool):
        assert len(tool.parse_lspci(LAPTOP_LSPCI)) == 6


class TestDriverSuggestions:
    def _for(self, tool, text, detect=None):
        report = tool.recommend(tool.parse_lspci(text), nvidia_detect_output=detect)
        return report, [s["entry"] for s in report["suggestions"]]

    def test_an_nvidia_card_suggests_the_proprietary_driver(self, tool):
        report, suggested = self._for(tool, LAPTOP_LSPCI)
        assert "nvidia-driver" in suggested
        assert "GeForce MX150" in report["suggestions"][0]["reason"]

    def test_it_repeats_the_driver_nvidia_detect_named(self, tool):
        detect = "It is recommended to install the\n    nvidia-driver\npackage.\n"
        report, _ = self._for(tool, LAPTOP_LSPCI, detect)
        assert report["suggestions"][0]["detail"] == "nvidia-detect recommends nvidia-driver"

    def test_without_the_detector_it_says_so_rather_than_guessing(self, tool):
        report, _ = self._for(tool, LAPTOP_LSPCI)
        assert "Install to find" in report["suggestions"][0]["detail"]

    def test_intel_and_nvidia_together_are_named_as_a_hybrid_laptop(self, tool):
        report, suggested = self._for(tool, LAPTOP_LSPCI)
        assert suggested[:2] == ["nvidia-driver", "intel-graphics"]
        assert any("hybrid laptop" in note for note in report["notes"])

    def test_an_amd_card_suggests_the_amd_entry(self, tool):
        _, suggested = self._for(tool, AMD_LSPCI)
        assert "amd-graphics" in suggested

    def test_a_broadcom_chip_the_open_drivers_miss_suggests_wl(self, tool):
        _, suggested = self._for(tool, LAPTOP_LSPCI)
        assert "broadcom-wifi" in suggested

    def test_a_broadcom_chip_outside_the_list_suggests_nothing(self, tool):
        _, suggested = self._for(tool, AMD_LSPCI)
        assert "broadcom-wifi" not in suggested

    def test_a_virtual_display_needs_no_driver(self, tool):
        report, suggested = self._for(tool, QEMU_LSPCI)
        assert suggested == []
        assert any("virtual machine" in note for note in report["notes"])

    def test_a_machine_lspci_says_nothing_about_is_reported_not_guessed(self, tool):
        report, suggested = self._for(tool, "")
        assert suggested == []
        assert report["gpus"] == [] and report["wifi"] == []
        assert any("No display controller" in note for note in report["notes"])

    def test_every_suggestion_names_a_real_catalog_entry(self, tool, catalog):
        for text in (LAPTOP_LSPCI, AMD_LSPCI, QEMU_LSPCI):
            report = tool.recommend(tool.parse_lspci(text))
            for suggestion in report["suggestions"]:
                assert catalog.by_id(suggestion["entry"])

    def test_the_report_is_json_with_the_four_keys(self, tool):
        report = tool.recommend(tool.parse_lspci(LAPTOP_LSPCI))
        assert set(report) == {"gpus", "wifi", "suggestions", "notes"}
        json.dumps(report)


class TestTheScanCommand:
    def test_it_reads_a_captured_report_without_running_lspci(
        self, tool, ctx, tmp_path, monkeypatch, capsys
    ):
        def forbidden(*args, **kwargs):
            raise AssertionError("scan --from ran a command")

        monkeypatch.setattr(tool.subprocess, "run", forbidden)
        captured = tmp_path / "lspci.txt"
        captured.write_text(LAPTOP_LSPCI)
        args = tool.build_parser().parse_args(
            ["scan", "--json", "--from", str(captured)]
        )
        assert args.func(args, ctx) == tool.EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert [s["entry"] for s in report["suggestions"]][0] == "nvidia-driver"

    def test_a_machine_with_no_lspci_still_answers(self, tool, ctx, monkeypatch, capsys):
        def missing(*args, **kwargs):
            raise FileNotFoundError("lspci")

        monkeypatch.setattr(tool.subprocess, "run", missing)
        args = tool.build_parser().parse_args(["scan", "--json"])
        assert args.func(args, ctx) == tool.EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert report["suggestions"] == []
        assert any("Could not run lspci" in note for note in report["notes"])


class TestRemovingWithoutARecord:
    """The state record can be missing: an entry installed by hand, a record
    deleted, a stick whose /var was cleared. Removal still has to take away
    what is actually there, and this is the path the NVIDIA entry's own
    recovery instructions go down, from a text console, on a machine whose
    desktop will not start."""

    def test_the_fallback_purges_the_driver_a_resolver_chose(self, tool, catalog, ctx):
        entry = catalog.by_id("nvidia-driver")
        record = tool.fallback_record(entry, {"nvidia-driver", "linux-headers-amd64"})
        assert set(record["packages"]) == {"nvidia-driver", "linux-headers-amd64"}
        assert tool.apt_argv("purge", *record["packages"]) in argvs(
            tool.plan_remove(entry, ctx, record)
        )

    def test_it_never_names_a_driver_that_was_not_chosen(self, tool, catalog, ctx):
        # nvidia-detect picks one of several metapackages. Asking apt to purge
        # the one it did not pick fails the whole removal, because apt has
        # never heard of it unless non-free is still enabled.
        entry = catalog.by_id("nvidia-driver")
        record = tool.fallback_record(entry, {"nvidia-driver"})
        assert "nvidia-tesla-535-driver" not in record["packages"]

    def test_nothing_installed_means_no_record_to_act_on(self, tool, catalog, ctx):
        assert tool.fallback_record(catalog.by_id("vlc"), set()) is None

    def test_it_still_takes_the_repository_files_away(self, tool, catalog, ctx):
        entry = catalog.by_id("mullvad")
        record = tool.fallback_record(entry, {"mullvad-vpn"})
        assert entry.repo.keyring_path in record["paths"]

    def test_the_catalog_fallback_covers_both_name_sets(self, tool, catalog):
        # packages and the check can name different things, and neither alone
        # is the whole install.
        names = tool.installed_packages(catalog.by_id("nvidia-driver"), None)
        assert "linux-headers-amd64" in names
        assert "nvidia-driver" in names


class TestWhatShowPrints:
    def test_a_long_warning_is_wrapped_for_a_terminal(self, tool, catalog, ctx, capsys):
        args = tool.build_parser().parse_args(["show", "nvidia-driver"])
        args.func(args, ctx)
        printed = capsys.readouterr().out
        assert all(len(line) <= 80 for line in printed.splitlines()[1:]), printed
        assert "Ctrl+Alt+F2" in printed

    def test_it_says_the_driver_is_chosen_at_install_time(self, tool, ctx, capsys):
        # The catalog lists only the headers for this entry, so printing the
        # package list alone would say the driver is a package it is not.
        args = tool.build_parser().parse_args(["show", "nvidia-driver"])
        args.func(args, ctx)
        assert "chosen by nvidia-detect" in capsys.readouterr().out

    def test_an_entry_that_needs_no_root_says_so(self, tool, ctx, capsys):
        args = tool.build_parser().parse_args(["show", "zed"])
        args.func(args, ctx)
        assert "no root needed" in capsys.readouterr().out

    def test_show_json_is_machine_readable(self, tool, ctx, capsys):
        args = tool.build_parser().parse_args(["show", "vlc", "--json"])
        assert args.func(args, ctx) == tool.EXIT_OK
        state = json.loads(capsys.readouterr().out)
        assert state["id"] == "vlc" and state["privileged"] is True


class TestUpgradeAsksBeforeRemovingAnything:
    """A full upgrade may remove packages to resolve a transition, and this
    one runs from a button rather than from a command somebody typed."""

    def test_the_simulation_changes_nothing(self, tool, ctx):
        argvs_run = argvs(tool.plan_upgrade_simulation(ctx))
        assert tool.apt_argv("--simulate", "full-upgrade") in argvs_run
        assert tool.apt_argv("full-upgrade") not in argvs_run

    def test_it_reads_removals_out_of_apts_own_simulation(self, tool):
        text = (
            "Inst libfoo1 [1.0] (1.1 Debian:13/stable [amd64])\n"
            "Remv oldpackage [2.3-1]\n"
            "Remv another [1.0-2]\n"
            "Conf libfoo1 (1.1 Debian:13/stable [amd64])\n"
        )
        assert tool.parse_removals(text) == ["oldpackage", "another"]

    def test_an_upgrade_that_removes_nothing_reads_as_nothing(self, tool):
        assert tool.parse_removals("Inst libfoo1 [1.0]\nConf libfoo1\n") == []

    def test_it_refuses_with_its_own_status_and_names_them(
        self, tool, ctx, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            tool, "run_plan",
            lambda steps, **kwargs: tool.PlanResult(True, captured="Remv oldpackage [1]"),
        )
        args = tool.build_parser().parse_args(["upgrade"])
        assert args.func(args, ctx) == tool.EXIT_NEEDS_REMOVALS
        printed = capsys.readouterr().out
        assert "::warn would remove oldpackage" in printed
        assert "--allow-removals" in printed

    def test_the_flag_goes_straight_through_without_simulating(
        self, tool, ctx, monkeypatch
    ):
        seen = []

        def record(steps, **kwargs):
            seen.append(argvs(steps))
            return tool.PlanResult(True)

        monkeypatch.setattr(tool, "run_plan", record)
        args = tool.build_parser().parse_args(["upgrade", "--allow-removals"])
        assert args.func(args, ctx) == tool.EXIT_OK
        assert seen == [argvs(tool.plan_upgrade(ctx))]

    def test_an_upgrade_with_nothing_to_remove_just_runs(self, tool, ctx, monkeypatch):
        plans = []

        def record(steps, **kwargs):
            plans.append(argvs(steps))
            return tool.PlanResult(True, captured="Inst libfoo1 [1.0]")

        monkeypatch.setattr(tool, "run_plan", record)
        args = tool.build_parser().parse_args(["upgrade"])
        assert args.func(args, ctx) == tool.EXIT_OK
        assert plans[-1] == argvs(tool.plan_upgrade(ctx))
