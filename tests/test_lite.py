"""The lite session: labwc in place of Xfce, chosen at first boot.

Most of what can go wrong here is two files disagreeing about a name: the
session entry and the script it runs, the autostart and the config paths it
passes, the wizard and the session it tells LightDM to start. None of those
fail loudly on a stick. A wrong name is a login that returns straight to the
greeter, or a panel that never appears, so each pairing is asserted here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import load_tool
from portlin import package, packages
from test_firstboot import WIZARD, load_wizard, module_constant, summary_state

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
THEME = RUNTIME / "theme"
OVERLAY = f"/{package.XDG_OVERLAY}"


def desktop_files() -> dict[str, str]:
    return package.text_files("portlin-desktop")


def entry_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{key}=(.*)$", text)
    assert match, f"no {key}= line"
    return match.group(1)


def jsonc(text: str) -> dict:
    """waybar's config is JSON with // comments, which it strips the same way."""
    return json.loads(re.sub(r"(?m)^\s*//.*$", "", text))


class TestPackages:
    def test_the_lite_group_ships_by_default(self):
        # First boot has no network, so a session the wizard offers has to be
        # on the stick already.
        assert "lite" in packages.DEFAULT_GROUPS
        assert "labwc" in packages.resolve()

    def test_a_minimal_stick_carries_none_of_it(self):
        assert not set(packages.LITE) & set(packages.resolve(packages.MINIMAL_GROUPS))

    @pytest.mark.parametrize("script, providers", [
        ("theme/labwc-autostart", {
            "swaybg": "swaybg",
            "waybar": "waybar",
            "mako": "mako-notifier",
            "nm-applet": "network-manager-gnome",
            "polkit-mate-authentication-agent-1": "mate-polkit",
        }),
        ("portlin-lite-idle", {"swayidle": "swayidle", "swaylock": "swaylock"}),
        ("portlin-caffeine-lite", {"fuzzel": "fuzzel", "gdbus": "libglib2.0-bin",
                                   "systemd-inhibit": "systemd", "pkill": "procps"}),
    ])
    def test_every_program_the_session_starts_is_installed(self, script, providers):
        body = (RUNTIME / script).read_text()
        resolved = set(packages.resolve())
        for program, provider in providers.items():
            assert program in body, program
            if provider not in ("systemd", "procps"):  # in every Debian install
                assert provider in resolved, provider


class TestPackageLayout:
    def test_the_session_entry_runs_the_shipped_script(self):
        files = desktop_files()
        entry = files[package.LITE_SESSION_ENTRY]
        exec_path = entry_value(entry, "Exec")
        assert exec_path.lstrip("/") in files
        assert exec_path.lstrip("/") in package.executable_paths("portlin-desktop")

    def test_the_greeter_hides_the_session_without_labwc(self):
        # A stick built with --groups leaving out lite still ships the entry.
        entry = desktop_files()[package.LITE_SESSION_ENTRY]
        assert entry_value(entry, "TryExec") == "labwc"
        assert "labwc" in packages.LITE

    def test_the_session_name_is_the_one_the_wizard_sets(self):
        name = Path(package.LITE_SESSION_ENTRY).stem
        assert module_constant(WIZARD, "LITE_SESSION") == name

    def test_the_display_tool_ships_executable_where_autostart_runs_it(self):
        autostart = (THEME / "labwc-autostart").read_text()
        assert f"/{package.LITE_DISPLAY_TOOL}" in autostart
        assert package.LITE_DISPLAY_TOOL in package.executable_paths("portlin-desktop")

    def test_every_config_the_autostart_names_is_shipped(self):
        autostart = (THEME / "labwc-autostart").read_text()
        files = desktop_files()
        named = re.findall(r'"\$overlay/([^"]+)"', autostart)
        assert named, "the autostart names no config by path"
        for relative in named:
            assert f"{package.XDG_OVERLAY}/{relative}" in files, relative

    def test_labwc_finds_its_files_in_the_overlay(self):
        files = desktop_files()
        for name in ("rc.xml", "menu.xml", "autostart", "themerc-override"):
            assert f"{package.XDG_OVERLAY}/labwc/{name}" in files, name


class TestSessionScript:
    SCRIPT = RUNTIME / "portlin-lite-session"

    @pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
    @pytest.mark.parametrize("path", [RUNTIME / "portlin-lite-session", THEME / "labwc-autostart"])
    def test_it_parses(self, path):
        subprocess.run(["sh", "-n", str(path)], check=True)

    def test_it_sources_the_hooks_xsession_would_have(self):
        # A Wayland session skips /etc/X11/Xsession.d, and without the first
        # hook labwc never finds portlin's rc.xml.
        script = self.SCRIPT.read_text()
        assert f"/{package.XSESSION_SNIPPET}" in script
        assert f"/{package.CACHE_SESSION_HOOK}" in script

    def test_it_takes_the_keyboard_layout_from_the_system(self):
        # The wizard sets the layout in /etc/default/keyboard, which X reads
        # by itself and a Wayland compositor does not.
        script = self.SCRIPT.read_text()
        assert "/etc/default/keyboard" in script
        assert "XKB_DEFAULT_LAYOUT" in script

    def test_it_ends_by_starting_labwc(self):
        assert self.SCRIPT.read_text().rstrip().splitlines()[-1] == "exec labwc"


class TestPanel:
    def test_the_readout_runs_in_waybar_mode_and_opens_about(self, stats):
        config = jsonc((THEME / "waybar-config.jsonc").read_text())
        readout = config["custom/stats"]
        assert readout["exec"] == "portlin-stats --waybar"
        assert readout["return-type"] == "json"
        assert readout["on-click"] == stats.CLICK_COMMAND

    def test_every_module_placed_is_configured(self):
        config = jsonc((THEME / "waybar-config.jsonc").read_text())
        for module in config["modules-left"] + config["modules-right"]:
            if module.split("#")[0] in ("tray", "clock", "wlr/taskbar", "custom/stats",
                                        "custom/apps", "image"):
                assert module in config, module

    def test_the_launcher_hides_entries_that_only_work_under_xfce(self):
        # fuzzel ignores NotShowIn unless told otherwise, and Caffeine relies
        # on it to stay out of a session where it cannot work.
        assert "filter-desktop=yes" in (THEME / "fuzzel.ini").read_text().splitlines()
        assert "NotShowIn=labwc;" in (RUNTIME / "portlin-caffeine.desktop").read_text()

    def test_the_mark_is_the_one_portlin_installs(self):
        config = jsonc((THEME / "waybar-config.jsonc").read_text())
        assert config["image#mark"]["path"] == f"/{package.HICOLOR_APP_ICON}"

    @pytest.mark.parametrize("name", ["waybar-style.css", "labwc-themerc-override",
                                      "fuzzel.ini", "mako-config"])
    def test_no_crimson(self, name):
        # Crimson means an encrypted root and nothing else, anywhere.
        assert "ff3355" not in (THEME / name).read_text().lower()


@pytest.fixture(scope="module")
def stats():
    return load_tool("portlin-stats")


class TestStatsWaybarMode:
    def test_it_prints_one_json_object_with_the_same_markup(self, stats, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(stats, "state_path", lambda: tmp_path / "state.json")
        monkeypatch.setattr(stats, "read_topology", lambda: {"encrypted": False})
        monkeypatch.setattr(stats, "disk_field", lambda topology: ("6.1G/31.0G", 0))
        monkeypatch.setattr(stats.hostinfo, "read_gpu", lambda **_: None)
        monkeypatch.setattr(stats.hostinfo, "read_battery", lambda: None)
        monkeypatch.setattr(stats.hostinfo, "local_address", lambda: "192.168.1.42")
        assert stats.main(["--waybar"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert set(payload) == {"text", "tooltip"}
        assert "192.168.1.42" in payload["text"]

        assert stats.main([]) == 0
        genmon = capsys.readouterr().out
        assert genmon.startswith("<txt>") and "<txtclick>" in genmon


@pytest.fixture(scope="module")
def display():
    return load_tool("portlin-lite-display")


def output(name="eDP-1", width=1920, mm=344, enabled=True):
    return {
        "name": name,
        "enabled": enabled,
        "physical_size": {"width": mm, "height": mm * 9 // 16},
        "modes": [{"width": 1280, "height": 720, "current": False},
                  {"width": width, "height": width * 9 // 16, "current": True}],
    }


class TestDisplayScale:
    def test_a_fixed_choice_applies_to_every_screen(self, display):
        outputs = [output(), output("HDMI-A-1", 3840, 600)]
        assert display.scales(outputs, "2") == {"eDP-1": 2, "HDMI-A-1": 2}

    def test_automatic_doubles_only_dense_wide_screens(self, display):
        laptop_4k = output(width=3840, mm=344)  # about 280 ppi
        monitor_4k = output("DP-1", 3840, 1210)  # 55 inch, about 80 ppi
        ordinary = output("HDMI-A-1", 1920, 530)
        assert display.scales([laptop_4k, monitor_4k, ordinary], "auto") == {
            "eDP-1": 2, "DP-1": 1, "HDMI-A-1": 1,
        }

    def test_a_screen_with_no_size_stays_at_one(self, display):
        # Projectors and virtual machines report 0 mm.
        assert display.scales([output(width=3840, mm=0)], "auto") == {"eDP-1": 1}

    def test_it_matches_the_xfce_rule(self, display):
        # The same 2560 px and 180 ppi thresholds as the X script the wizard writes.
        script = module_constant(WIZARD, "SCALE_SCRIPT_TEXT")
        assert '"$1" -ge 2560' in script and '"$ppi" -ge 180' in script
        assert display.automatic(2559, 100) == 1
        assert display.automatic(2560, 362) == 1  # 179.6 ppi
        assert display.automatic(2560, 360) == 2

    def test_disabled_screens_are_left_alone(self, display):
        assert display.scales([output(enabled=False)], "2") == {}

    def test_it_reads_the_file_the_wizard_writes(self, display, tmp_path):
        assert f'SCALE_CONFIG = Path("{display.CONFIG}")' in WIZARD.read_text()
        config = tmp_path / "display.conf"
        config.write_text("scale=2\n")
        assert display.configured(config) == "2"
        assert display.configured(tmp_path / "missing") == "auto"


class TestWizard:
    def test_it_offers_the_choice_only_while_both_are_installed(self):
        for choose_desktop in (True, False):
            fb = load_wizard()
            seen = {}

            def settings(title, text, rows, **_):
                seen["keys"] = [row.key for row in rows]
                return {row.key: row.value for row in rows}

            fb.ui.settings = settings
            state = summary_state(fb, choose_desktop=choose_desktop)
            fb.step_appearance(state)
            assert ("desktop" in seen["keys"]) is choose_desktop

    def test_choosing_lite_sets_both_greeter_and_autologin_sessions(self, tmp_path):
        fb = load_wizard()
        fb.SESSION_CONFIG = tmp_path / "20-portlin-session.conf"
        fb.apply_desktop("lite")
        text = fb.SESSION_CONFIG.read_text()
        assert "[Seat:*]" in text
        assert f"user-session={fb.LITE_SESSION}" in text
        assert f"autologin-session={fb.LITE_SESSION}" in text

    def test_choosing_xfce_puts_lightdm_back_to_its_default(self, tmp_path):
        fb = load_wizard()
        fb.SESSION_CONFIG = tmp_path / "20-portlin-session.conf"
        fb.apply_desktop("lite")
        fb.apply_desktop("xfce")
        assert not fb.SESSION_CONFIG.exists()

    def test_the_summary_names_the_desktop(self):
        fb = load_wizard()
        state = summary_state(fb, choose_desktop=True, desktop="lite")
        assert "Lite (labwc)" in dict((k, v) for k, _, v in fb.summary_groups(state))["appearance"]

    def test_the_summary_says_nothing_of_it_without_a_choice(self):
        fb = load_wizard()
        state = summary_state(fb, choose_desktop=False)
        appearance = dict((k, v) for k, _, v in fb.summary_groups(state))["appearance"]
        assert "Xfce" not in appearance

    def test_it_is_applied(self):
        source = WIZARD.read_text()
        applying = source[source.index("def apply_all("):source.index("def wizard(")]
        assert "apply_desktop(state.desktop)" in applying

    def test_the_choice_needs_both_desktops_still_installed(self):
        # A setup re-run after a removal must not offer the one that is gone.
        source = WIZARD.read_text()
        body = source[source.index("def wizard("):source.index("def main(")]
        assert "_lite_installed()" in body and "_xfce_installed()" in body

    def test_the_xfce_session_entry_is_the_one_xfce_ships(self):
        # Debian's xfce4-session installs this path; it is how the wizard
        # tells that Xfce is still here.
        assert module_constant_path("XFCE_SESSION_ENTRY") == "/usr/share/xsessions/xfce.desktop"


def module_constant_path(name: str) -> str:
    match = re.search(rf'^{name} = Path\("([^"]+)"\)', WIZARD.read_text(), re.M)
    assert match, name
    return match.group(1)


GSETTINGS = {"libglib2.0-bin", "dconf-gsettings-backend", "gsettings-desktop-schemas"}

# What the lite session runs that comes from the Xfce side of the package
# list, and what the login screen needs whichever desktop follows it.
SHARED = {
    "thunar", "thunar-archive-plugin", "xfce4-terminal", "mousepad", "ristretto",
    "mate-polkit", "network-manager-gnome", "pavucontrol", "lightdm",
    "lightdm-gtk-greeter", "xserver-xorg", "xinit", "x11-xserver-utils", "xfconf",
}


class TestRemovingTheOtherDesktop:
    @pytest.fixture
    def lists(self):
        return module_constant(WIZARD, "DESKTOP_PACKAGES")

    def test_the_lite_list_is_the_lite_group_less_what_others_need(self, lists):
        assert set(lists["lite"]) == set(packages.LITE) - GSETTINGS

    def test_removing_xfce_spares_what_the_lite_session_runs(self, lists):
        assert not set(lists["xfce"]) & (SHARED | set(packages.LITE))

    def test_removing_lite_spares_what_xfce_runs(self, lists):
        assert not set(lists["lite"]) & (SHARED | set(packages.DESKTOP))

    def test_neither_removal_takes_portlin_desktop_with_it(self, lists):
        # A Depends on anything purged here would purge portlin's own package:
        # the wallpaper, the panel readout and every tool in the menu.
        control = package.text_files("portlin-desktop")["DEBIAN/control"]
        depends = {d.strip() for d in control.split("Depends: ")[1].splitlines()[0].split(",")}
        assert not depends & (set(lists["xfce"]) | set(lists["lite"]))

    def test_it_purges_only_what_is_installed_then_autoremoves(self):
        fb = load_wizard()
        calls = []

        class Done:
            def __init__(self, stdout=""):
                self.stdout, self.returncode = stdout, 0

        def run(argv, check=True, stdin=None):
            calls.append(argv)
            if argv[0] == "dpkg-query":
                return Done("labwc ii \nwaybar ii \nfuzzel un \n")
            return Done()

        fb.run = run
        fb.log = lambda *a, **k: None
        fb.remove_desktop("lite")
        apt = [argv[argv.index("apt-get"):] for argv in calls if "apt-get" in argv]
        assert apt == [["apt-get", "-y", "purge", "labwc", "waybar"],
                       ["apt-get", "-y", "--purge", "autoremove"]]
        assert all("DEBIAN_FRONTEND=noninteractive" in argv for argv in calls if "apt-get" in argv)

    def test_the_removal_is_optional_and_after_the_session_is_set(self):
        source = WIZARD.read_text()
        applying = source[source.index("def apply_all("):source.index("def wizard(")]
        assert applying.index("apply_desktop(") < applying.index("remove_desktop(")
        line = next(l for l in applying.splitlines() if "remove_desktop(" in l)
        assert line.rstrip().endswith("True)")
