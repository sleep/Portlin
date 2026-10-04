"""The first-boot wizard's screens, its navigation, and the settings it applies.

The wizard is loaded as a module and its real widgets are driven through a
stand-in for the curses window, fed a scripted list of keys. Nothing here
needs a terminal or root: every apply function is pointed at tmp_path or has
its commands recorded rather than run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from portlin import packages
from test_firstboot import UNIT, WIZARD, load_wizard

REPO = Path(__file__).resolve().parent.parent


class OutOfKeys(Exception):
    """The screen wanted another key after the script ran out."""


class FakeScreen:
    """Enough of a curses window for the widgets: a grid of cells and a key queue.

    A tuple in the queue is one key that arrives as several characters at
    once, the way an escape sequence does.
    """

    def __init__(self, keys, size=(25, 80)):
        self.keys, self.size = list(keys), size
        self.cells: dict[tuple[int, int], str] = {}
        self.frames: list[str] = []
        self.pending: list[str] = []
        self.waits = True

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        for offset, char in enumerate(text):
            self.cells[(y, x + offset)] = char

    def erase(self):
        self.cells = {}

    clear = erase

    def refresh(self):
        self.frames.append(self.text())

    def text(self) -> str:
        height, width = self.size
        return "\n".join(
            "".join(self.cells.get((y, x), " ") for x in range(width)).rstrip() for y in range(height)
        )

    def keypad(self, flag):
        pass

    def bkgd(self, *args):
        pass

    def move(self, y, x):
        pass

    def nodelay(self, flag):
        self.waits = not flag

    def get_wch(self):
        import curses

        if self.pending:
            return self.pending.pop(0)
        if not self.waits:
            raise curses.error("no input")
        if not self.keys:
            raise OutOfKeys()
        key = self.keys.pop(0)
        if isinstance(key, tuple):
            self.pending = list(key[1:])
            return key[0]
        return key


def content(frame: str, left: int = 27) -> str:
    """The right-hand side of a frame, as one line of words: wrapped text reads whole."""
    return " ".join(" ".join(line[left:] for line in frame.splitlines()).split())


def typed(text: str) -> list[str]:
    return list(text)


@pytest.fixture
def fb():
    module = load_wizard()
    module.ui.glyphs = module.UNICODE_GLYPHS
    return module


def screen(fb, keys, size=(25, 80)) -> FakeScreen:
    fb.ui.screen = FakeScreen(keys, size)
    return fb.ui.screen


ENTER, ESC = "\n", "\x1b"


def key(fb, name):
    return getattr(fb.curses, f"KEY_{name}")


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class TestSettingsScreen:
    def rows(self, fb):
        return [
            fb.Row("colour", "Colour", [("red", "red"), ("blue", "blue")], "red"),
            fb.Row("size", "Size", [(1, "small"), (2, "large")], 1),
            fb.Row("fixed", "Fixed", [(False, "needs network")], False, enabled=False),
            fb.Row("name", "Name", text=True, value="sam"),
        ]

    def test_enter_accepts_every_setting_as_shown(self, fb):
        screen(fb, [ENTER])
        assert fb.ui.settings("T", "", self.rows(fb)) == {"colour": "red", "size": 1, "fixed": False, "name": "sam"}

    def test_arrows_change_only_the_focused_row(self, fb):
        screen(fb, [key(fb, "DOWN"), key(fb, "RIGHT"), ENTER])
        assert fb.ui.settings("T", "", self.rows(fb))["size"] == 2

    def test_a_disabled_row_cannot_be_changed(self, fb):
        screen(fb, [key(fb, "DOWN"), key(fb, "DOWN"), key(fb, "RIGHT"), " ", ENTER])
        assert fb.ui.settings("T", "", self.rows(fb))["fixed"] is False

    def test_text_rows_take_typing(self, fb):
        keys = [key(fb, "UP"), key(fb, "BACKSPACE"), *typed("m-2"), ENTER]
        screen(fb, keys)
        assert fb.ui.settings("T", "", self.rows(fb))["name"] == "sam-2"

    def test_esc_goes_back(self, fb):
        screen(fb, [ESC])
        with pytest.raises(fb.Back):
            fb.ui.settings("T", "", self.rows(fb))

    def test_a_rejected_answer_stays_on_screen_and_says_why(self, fb):
        window = screen(fb, [ENTER, key(fb, "BACKSPACE"), ENTER])
        rows = [fb.Row("name", "Name", text=True, value="bad!")]
        values = fb.ui.settings("T", "", rows, validate=lambda v: ("name", "No punctuation.") if "!" in v["name"] else None)
        assert values == {"name": "bad"}
        assert any("No punctuation." in frame for frame in window.frames)

    def test_the_help_follows_the_other_rows(self, fb):
        # The sudo row's warning depends on the autologin row above it.
        window = screen(fb, [key(fb, "RIGHT"), key(fb, "DOWN"), key(fb, "RIGHT"), ENTER])
        rows = [
            fb.Row("autologin", "Log in automatically", [(False, "no"), (True, "yes")], False),
            fb.Row("sudo_password", "sudo password", [(True, "ask"), (False, "never")], True,
                   help=fb._sudo_help(encrypted=False)),
        ]
        fb.ui.settings("T", "", rows)
        assert "nothing else would stand in the way" in content(window.frames[-1])


class TestForm:
    def test_enter_walks_the_fields_then_submits(self, fb):
        screen(fb, [*typed("a"), ENTER, *typed("b"), ENTER])
        fields = [fb.Field("one", "One"), fb.Field("two", "Two")]
        assert fb.ui.form("T", "", fields) == {"one": "a", "two": "b"}

    def test_a_mismatched_password_pair_is_cleared_and_asked_again(self, fb):
        fb._user_exists = lambda name: False
        window = screen(fb, [
            *typed("Sam Rivera"), ENTER, ENTER,
            *typed("abcd"), ENTER, *typed("abce"), ENTER,
            *typed("abcd"), ENTER, *typed("abcd"), ENTER,
        ])
        state = fb.State()
        fb.step_account(state)
        assert (state.full_name, state.username, state.password) == ("Sam Rivera", "sam", "abcd")
        assert any("did not match" in frame for frame in window.frames)

    def test_a_name_useradd_would_refuse_is_caught_on_the_form(self, fb):
        fb._user_exists = lambda name: False
        window = screen(fb, [*typed("Smith: Jr"), ENTER, ENTER, *typed("abcd"), ENTER, *typed("abcd"), ENTER,
                             key(fb, "UP"), key(fb, "UP"), key(fb, "UP"), key(fb, "BACKSPACE"),
                             key(fb, "BACKSPACE"), key(fb, "BACKSPACE"), key(fb, "BACKSPACE"),
                             ENTER, ENTER, ENTER, ENTER])
        state = fb.State()
        fb.step_account(state)
        assert ":" not in state.full_name
        assert any("cannot contain ':'" in frame for frame in window.frames)

    def test_an_account_a_failed_run_created_can_be_taken_up_again(self, fb, tmp_path):
        fb.SENTINEL = tmp_path / "pending"
        fb.SENTINEL.write_text("pending\naccount=sam\n")
        fb._user_exists = lambda name: name in ("sam", "root")
        clear = [key(fb, "BACKSPACE")] * 4
        screen(fb, [ENTER, *clear, *typed("sam"), ENTER, *typed("abcd"), ENTER, *typed("abcd"), ENTER])
        state = fb.State()
        fb.step_account(state)
        assert state.username == "sam"
        calls = []
        fb.run = lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        fb.grp = type("G", (), {"getgrall": staticmethod(lambda: [])})
        fb.apply_account("sam", "Sam", "abcd")
        assert calls[0][0] == "usermod" and not any(call[0] == "useradd" for call in calls)

    def test_any_other_existing_name_is_still_refused(self, fb, tmp_path):
        fb.SENTINEL = tmp_path / "pending"
        fb.SENTINEL.write_text("pending\naccount=sam\n")
        fb._user_exists = lambda name: True
        window = screen(fb, [ENTER, *[key(fb, "BACKSPACE")] * 4, *typed("root"), ENTER, *typed("abcd"), ENTER,
                             *typed("abcd"), ENTER, *[key(fb, "BACKSPACE")] * 4, *typed("sam"), ENTER, ENTER, ENTER])
        state = fb.State()
        fb.step_account(state)
        assert state.username == "sam"
        assert any("already exists" in frame for frame in window.frames)

    def test_a_new_account_is_recorded_in_the_sentinel(self, fb, tmp_path):
        fb.SENTINEL = tmp_path / "pending"
        fb.SENTINEL.write_text("pending\n")
        fb._user_exists = lambda name: False
        fb.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        fb.grp = type("G", (), {"getgrall": staticmethod(lambda: [])})
        fb.apply_account("sam", "Sam", "abcd")
        assert fb._account_from_earlier_run() == "sam"

    def test_secrets_are_never_drawn(self, fb):
        window = screen(fb, [*typed("hunter22"), ENTER])
        fb.ui.form("T", "", [fb.Field("pw", "Password", secret=True)])
        assert not any("hunter22" in frame for frame in window.frames)

    def test_a_typed_username_is_not_overwritten_by_the_name(self, fb):
        fb._user_exists = lambda name: False
        screen(fb, [key(fb, "DOWN"), *typed("x"), key(fb, "UP"), *typed("Sam"), key(fb, "DOWN"),
                    key(fb, "DOWN"), *typed("abcd"), ENTER, *typed("abcd"), ENTER])
        state = fb.State()
        fb.step_account(state)
        assert state.username == "userx"


class TestMenu:
    ZONES = [("Europe/London", "London", "Europe"), ("America/New_York", "New York", "America"),
             ("Asia/Tokyo", "Tokyo", "Asia")]

    def test_enter_takes_the_default(self, fb):
        screen(fb, [ENTER])
        assert fb.ui.menu("T", "", self.ZONES, default="Asia/Tokyo") == "Asia/Tokyo"

    def test_typing_searches(self, fb):
        screen(fb, [*typed("new y"), ENTER])
        assert fb.ui.menu("T", "", self.ZONES, search=True) == "America/New_York"

    def test_esc_clears_the_search_before_going_back(self, fb):
        screen(fb, [*typed("zzz"), ESC, ENTER])
        assert fb.ui.menu("T", "", self.ZONES, search=True) == "Europe/London"
        screen(fb, [ESC])
        with pytest.raises(fb.Back):
            fb.ui.menu("T", "", self.ZONES, search=True)


class TestKeys:
    def test_postponing_needs_confirming(self, fb):
        # Enter on the question keeps going: the default is the safe answer.
        screen(fb, [key(fb, "F10"), ENTER, ENTER])
        assert fb.ui.confirm("T", "Go on?") is True

    def test_confirming_postpones(self, fb):
        screen(fb, [key(fb, "F10"), key(fb, "LEFT"), ENTER])
        with pytest.raises(fb.Cancelled):
            fb.ui.confirm("T", "Go on?")

    def test_ctrl_c_asks_the_same_question(self, fb):
        screen(fb, ["\x03", key(fb, "LEFT"), ENTER])
        with pytest.raises(fb.Cancelled):
            fb.ui.message("T", "Hello")

    def test_nothing_can_be_postponed_once_applying(self, fb):
        fb.ui.postpone_allowed = False
        screen(fb, [key(fb, "F10"), ENTER])
        fb.ui.message("T", "Hello")

    def test_an_unknown_escape_sequence_is_not_back(self, fb):
        # A key the terminfo entry does not describe arrives as Esc plus the
        # rest of its sequence. Read as Back, it threw people a step backwards.
        screen(fb, [("\x1b", "[", "4", "~"), ENTER])
        assert fb.ui.settings("T", "", [fb.Row("a", "A", [(1, "one")], 1)]) == {"a": 1}

    def test_a_lone_esc_is_back(self, fb):
        screen(fb, [ESC])
        with pytest.raises(fb.Back):
            fb.ui.dialog("T", "x", ["Ok"])


class TestLayout:
    def test_the_step_list_shows_where_setup_is(self, fb):
        window = screen(fb, [ENTER])
        fb.ui.sections = [("welcome", "Welcome"), ("keyboard", "Keyboard"), ("review", "Review")]
        fb.ui.visited, fb.ui.current = {"welcome"}, "keyboard"
        fb.ui.message("Keyboard layout", "Pick one.")
        text = window.frames[-1]
        assert "portlin" in text and "first-boot setup" in text
        assert "■ Welcome" in text and "  Keyboard" in text and "· Review" in text

    def test_a_narrow_console_gets_a_header_instead(self, fb):
        window = screen(fb, [ENTER], size=(24, 60))
        fb.ui.sections = [("welcome", "Welcome"), ("keyboard", "Keyboard")]
        fb.ui.current = "keyboard"
        fb.ui.message("Keyboard layout", "Pick one.")
        first = window.frames[-1].splitlines()[0]
        assert first.lstrip().startswith("portlin") and "Keyboard" in first and "2/2" in first

    def test_the_palette_is_the_brand(self, fb):
        # The Linux console's slots are repainted with these, so a drift from
        # the brand sheet shows on every stick's first screen.
        brand = (REPO / "docs" / "brand" / "README.md").read_text()
        for token in (fb.INK, fb.PANEL, fb.LINE, fb.MUTED, fb.PAPER, fb.ACCENT):
            assert f"#{token}" in brand
        assert fb.LINUX_PALETTE[0] == fb.INK and fb.LINUX_PALETTE[1] == fb.ACCENT

    def test_only_console_safe_glyphs(self, fb):
        # What the console font was seen to draw on a real boot. It has no
        # block elements: half blocks rendered as diamonds there.
        allowed = set(range(0x20, 0x100)) | {0x25A0, 0x2190, 0x2191, 0x2192, 0x2193, 0x2026}
        for glyph in fb.UNICODE_GLYPHS.values():
            assert all(ord(char) in allowed for char in glyph), glyph


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------


class TestNavigation:
    def run_wizard(self, fb, tmp_path, steps):
        fb.STEPS = steps
        fb.migration_candidates = lambda: []
        fb._luks_device = lambda: None
        fb._desktop_installed = lambda: False
        fb.apply_all = lambda state: None
        fb.SENTINEL = tmp_path / "pending"
        fb.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        fb.ui.screen = FakeScreen([ENTER])
        return fb.wizard()

    def test_esc_retraces_only_the_steps_that_were_shown(self, fb, tmp_path):
        visits = []
        backed = []

        def make(name, *, skip=False, back_once=False):
            def step(state):
                visits.append(name)
                if back_once and not backed:
                    backed.append(name)
                    raise fb.Back()
                return False if skip else True
            return (name, name, step)

        self.run_wizard(fb, tmp_path, [make("a"), make("b", skip=True), make("c", back_once=True)])
        assert visits == ["a", "b", "c", "a", "b", "c"]

    def test_editing_from_the_review_comes_straight_back(self, fb, tmp_path):
        visits = []
        answers = iter(["b", None])

        def make(name):
            def step(state):
                visits.append(name)
                return next(answers) if name == "review" else True
            return (name, name, step)

        self.run_wizard(fb, tmp_path, [make("a"), make("b"), make("c"), make("review")])
        assert visits == ["a", "b", "c", "review", "b", "review"]

    def test_esc_after_an_edit_from_the_review_still_steps_back_one_at_a_time(self, fb, tmp_path):
        visits = []
        script = {"review": iter(["b", "back", None]), "c": iter([True, "back", True])}

        def make(name):
            def step(state):
                visits.append(name)
                answer = next(script[name]) if name in script else True
                if answer == "back":
                    raise fb.Back()
                return answer
            return (name, name, step)

        self.run_wizard(fb, tmp_path, [make("a"), make("b"), make("c"), make("review")])
        # Edit b from the review and come back; then Esc twice walks c, b.
        assert visits == ["a", "b", "c", "review", "b", "review", "c", "b", "c", "review"]

    def test_the_sentinel_goes_only_after_everything_is_applied(self, fb, tmp_path):
        order = []
        sentinel = tmp_path / "pending"
        sentinel.write_text("pending\n")

        def apply(state):
            order.append(("applied", sentinel.exists()))

        steps = [("review", "Review", lambda state: None)]
        fb.STEPS = steps
        fb.migration_candidates = lambda: []
        fb._luks_device = lambda: None
        fb._desktop_installed = lambda: False
        fb.apply_all = apply
        fb.SENTINEL = sentinel
        fb.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        fb.ui.screen = FakeScreen([ENTER])
        fb.wizard()
        assert order == [("applied", True)] and not sentinel.exists()


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


class TestWifi:
    def test_a_keyfile_names_the_network_by_its_bytes(self, fb):
        text = fb.wifi_keyfile("u-1", "Café ;=[x]", "psk", "pa ss\\word")
        assert "ssid=" + "".join(f"{b};" for b in "Café ;=[x]".encode()) in text
        assert "key-mgmt=wpa-psk" in text
        assert "psk=pa ss\\\\word" in text
        assert "uuid=u-1" in text and "type=wifi" in text

    def test_wpa3_only_networks_use_sae(self, fb):
        assert "key-mgmt=sae" in fb.wifi_keyfile("u", "n", "sae", "secret")

    def test_open_and_hidden_networks(self, fb):
        text = fb.wifi_keyfile("u", "n", "open", "", hidden=True)
        assert "[wifi-security]" not in text and "hidden=true" in text

    def test_nmcli_terse_output_is_unescaped(self, fb):
        assert fb._terse(r"My\:Net:WPA2:70") == ["My:Net", "WPA2", "70"]
        assert fb._terse(r"back\\slash::5") == ["back\\slash", "", "5"]

    def test_the_scan_keeps_each_network_once_at_its_strongest(self, fb):
        listing = "\n".join([
            "Home:WPA2:40", "Home:WPA2:80", ":WPA2:90", "Cafe::30", "Uni:WPA2 802.1X:60",
            "New:WPA3:50", "Mixed:WPA2 WPA3:20",
        ])
        fb._nmcli = lambda args, timeout=30: subprocess.CompletedProcess(args, 0, listing, "")
        found = {n["ssid"]: n for n in fb.wifi_networks()}
        assert set(found) == {"Home", "Cafe", "Uni", "New", "Mixed"}
        assert found["Home"]["signal"] == 80
        assert [found[k]["security"] for k in ("Cafe", "Uni", "New", "Mixed")] == ["open", "enterprise", "sae", "psk"]
        assert list(found)[0] == "Home"

    def test_joining_keeps_the_password_out_of_argv_and_off_the_disk(self, fb, tmp_path):
        calls = []
        fb.NM_RUN_CONNECTIONS = tmp_path / "run"
        fb._nmcli = lambda args, timeout=30: calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        joined, reason = fb.wifi_connect("Home", "psk", "secretpass")
        assert joined and reason == ""
        assert not any("secretpass" in " ".join(call) for call in calls)
        path = Path(joined["path"])
        assert path.parent == tmp_path / "run"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert "psk=secretpass" in path.read_text()

    def test_a_failed_join_is_forgotten_and_explained(self, fb, tmp_path):
        fb.NM_RUN_CONNECTIONS = tmp_path / "run"

        def nmcli(args, timeout=30):
            failed = "up" in args
            return subprocess.CompletedProcess(args, 4 if failed else 0, "",
                                               "Error: Connection activation failed: Secrets were required." if failed else "")

        fb._nmcli = nmcli
        joined, reason = fb.wifi_connect("Home", "psk", "wrongpass")
        assert joined is None and reason.startswith("Connection activation failed")
        assert not list((tmp_path / "run").iterdir())

    def test_applying_moves_the_profile_to_etc_and_sets_the_address_rule(self, fb, tmp_path):
        fb.NM_CONNECTIONS, fb.NM_MAC_CONF = tmp_path / "etc", tmp_path / "conf.d" / "mac.conf"
        fb.NM_RUN_MAC_CONF = tmp_path / "run-mac.conf"
        fb.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
        source = tmp_path / "setup.nmconnection"
        source.write_text("psk=x\n")
        fb.apply_network({"ssid": "My Net!", "uuid": "abcdef123456", "path": str(source)}, "stable")
        kept = tmp_path / "etc" / "My_Net-abcdef12.nmconnection"
        assert kept.read_text() == "psk=x\n" and not source.exists()
        assert oct(kept.stat().st_mode & 0o777) == "0o600"
        assert "wifi.cloned-mac-address=stable" in fb.NM_MAC_CONF.read_text()
        fb.apply_network(None, "off")
        assert not fb.NM_MAC_CONF.exists()


# --------------------------------------------------------------------------
# Security and appearance
# --------------------------------------------------------------------------


class TestFoundMessage:
    THEME = REPO / "portlin" / "resources" / "grub" / "theme.txt"

    def setup(self, fb, tmp_path):
        fb.GRUB_THEME = tmp_path / "theme.txt"
        fb.GRUB_THEME.write_text(self.THEME.read_text())
        fb.FOUND_MESSAGE = tmp_path / "found-message"
        fb.FOUND_HOOK = tmp_path / "hooks" / "portlin-found-message"
        fb.FOUND_PREMOUNT = tmp_path / "init-premount" / "portlin-found-message"

    def test_it_is_cleaned_into_one_safe_line(self, fb):
        assert fb._clean_found_message(' If "found"\\ call\x07  me\n ') == "If 'found'/ call me"
        assert len(fb._clean_found_message("x" * 200)) == fb.FOUND_LIMIT

    def test_it_lands_on_the_boot_menu_and_in_the_initramfs(self, fb, tmp_path):
        self.setup(fb, tmp_path)
        fb.apply_found_message("If found, email sam@example.com")
        theme = fb.GRUB_THEME.read_text()
        assert 'text = "If found, email sam@example.com"' in theme
        assert theme.startswith(self.THEME.read_text().rstrip("\n"))
        assert fb.FOUND_MESSAGE.read_text() == "If found, email sam@example.com\n"
        assert os.access(fb.FOUND_HOOK, os.X_OK) and os.access(fb.FOUND_PREMOUNT, os.X_OK)

    def test_changing_or_clearing_it_leaves_no_trace(self, fb, tmp_path):
        self.setup(fb, tmp_path)
        fb.apply_found_message("first")
        fb.apply_found_message("second")
        theme = fb.GRUB_THEME.read_text()
        assert "first" not in theme and theme.count(fb.FOUND_BEGIN) == 1
        fb.apply_found_message("")
        assert fb.GRUB_THEME.read_text().strip() == self.THEME.read_text().strip()
        assert not fb.FOUND_MESSAGE.exists() and not fb.FOUND_HOOK.exists()

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.parametrize("name", ["FOUND_HOOK_TEXT", "FOUND_PREMOUNT_TEXT", "SCALE_SCRIPT_TEXT"])
    def test_the_scripts_it_writes_parse(self, fb, name, tmp_path):
        script = tmp_path / "script"
        script.write_text(getattr(fb, name))
        assert subprocess.run(["sh", "-n", str(script)]).returncode == 0

    def test_the_initramfs_scripts_answer_the_prereqs_probe(self, fb):
        for text in (fb.FOUND_HOOK_TEXT, fb.FOUND_PREMOUNT_TEXT):
            assert 'prereqs) prereqs; exit 0' in text


class TestScreenLock:
    def written(self, fb, tmp_path, minutes, suspend):
        fb.XFCONF_DEFAULTS = tmp_path
        fb.apply_screen_lock(minutes, suspend)
        return (tmp_path / "xfce4-screensaver.xml").read_text(), (tmp_path / "xfce4-power-manager.xml").read_text()

    def value(self, xml, name):
        return re.search(rf'name="{name}" type="\w+" value="([^"]*)"', xml).group(1)

    def test_idle_lock(self, fb, tmp_path):
        saver, power = self.written(fb, tmp_path, 15, True)
        assert 'name="delay" type="int" value="15"' in saver
        assert self.value(saver, "sleep-activation") == "true"
        assert self.value(power, "lock-screen-suspend-hibernate") == "true"

    def test_never_idle_but_still_on_suspend(self, fb, tmp_path):
        # The lock has to stay enabled for the suspend lock to have anything to do.
        saver, _ = self.written(fb, tmp_path, 0, True)
        lock = saver[saver.index('name="lock"'):]
        assert self.value(lock, "enabled") == "true"
        assert self.value(saver[saver.index('name="saver"'):], "enabled") == "false"

    def test_the_channels_written_are_not_ones_portlin_already_ships(self, fb):
        # xfconf takes a whole channel from the first directory that has it, so
        # a file here must never shadow one of the shipped channel files.
        from portlin import package

        shipped = {Path(destination).name for destination in package.XDG_DEFAULTS}
        assert not {"xfce4-screensaver.xml", "xfce4-power-manager.xml"} & shipped


class TestScale:
    def test_it_writes_the_choice_and_the_login_hook(self, fb, tmp_path):
        fb.SCALE_CONFIG = tmp_path / "display.conf"
        fb.SCALE_SCRIPT = tmp_path / "portlin-display-scale"
        fb.SCALE_AUTOSTART = tmp_path / "portlin-display-scale.desktop"
        fb.apply_scale("auto")
        assert fb.SCALE_CONFIG.read_text() == "scale=auto\n"
        assert os.access(fb.SCALE_SCRIPT, os.X_OK)
        assert f"Exec={'/usr/local/libexec/portlin-display-scale'}" in fb.SCALE_AUTOSTART.read_text()
        assert "/Gdk/WindowScalingFactor" in fb.SCALE_SCRIPT.read_text()

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
    @pytest.mark.parametrize("xrandr, mode, expected", [
        ("eDP-1 connected primary 2560x1600+0+0 (normal) 302mm x 189mm", "auto", "2"),
        ("HDMI-1 connected 2560x1440+0+0 (normal) 597mm x 336mm", "auto", "1"),
        ("eDP-1 connected 1920x1080+0+0 (normal) 344mm x 194mm", "auto", "1"),
        ("Virtual-1 connected 3840x2160+0+0 (normal) 0mm x 0mm", "auto", "1"),
        ("HDMI-2 disconnected (normal)\neDP-1 connected 3200x1800+0+0 (normal) 294mm x 165mm", "auto", "2"),
        ("eDP-1 connected 1920x1080+0+0 (normal) 344mm x 194mm", "2", "2"),
    ])
    def test_the_login_hook_picks_the_scale(self, fb, tmp_path, xrandr, mode, expected):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "xrandr").write_text(f"#!/bin/sh\ncat <<'EOF'\n{xrandr}\nEOF\n")
        (bin_dir / "xfconf-query").write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/set\n')
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        config = tmp_path / "display.conf"
        config.write_text(f"scale={mode}\n")
        script = tmp_path / "scale"
        script.write_text(fb.SCALE_SCRIPT_TEXT.replace("/etc/portlin/display.conf", str(config)))
        subprocess.run(["sh", str(script)], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=True)
        assert (tmp_path / "set").read_text().split()[-1] == expected


# --------------------------------------------------------------------------
# Hardware, services, storage
# --------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, check=True, stdin=None):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


class TestHardwareScreen:
    SCAN = {"gpus": [{"name": "NVIDIA GeForce MX150"}], "notes": [],
            "suggestions": [{"entry": "nvidia-driver", "reason": "found"}, {"entry": "../evil", "reason": "x"}]}

    def offered(self, fb, online):
        state = fb.State()
        state.online, state.scan, state.swap = online, self.SCAN, 50
        captured = {}

        def settings(title, text, rows, **kwargs):
            captured.update({row.key: row for row in rows})
            return {row.key: row.value for row in rows}

        fb.ui.settings = settings
        fb.step_hardware(state)
        return captured

    def test_drivers_need_a_network(self, fb):
        row = self.offered(fb, online=False)["driver:nvidia-driver"]
        assert not row.enabled and "Network step" in row.describe({})[0]
        assert self.offered(fb, online=True)["driver:nvidia-driver"].enabled

    def test_only_well_formed_entries_are_offered(self, fb):
        assert "driver:../evil" not in self.offered(fb, online=True)

    def test_swap_starts_at_what_the_image_ships(self, fb):
        from portlin import templates

        shipped = int(re.search(r"PERCENT=(\d+)", templates.render_zram_conf()).group(1))
        assert self.offered(fb, online=False)["swap"].value == shipped


class TestApplying:
    def test_swap_size_is_rewritten_in_place(self, fb, tmp_path):
        fb.ZRAM_CONFIG = tmp_path / "zramswap"
        fb.ZRAM_CONFIG.write_text("# Generated by portlin.\nALGO=zstd\nPERCENT=50\n")
        fb.run = record = Recorder()
        fb.apply_swap(25)
        assert fb.ZRAM_CONFIG.read_text() == "# Generated by portlin.\nALGO=zstd\nPERCENT=25\n"
        fb.apply_swap(0)
        assert ["systemctl", "disable", "--now", "zramswap.service"] in record.calls

    def test_ssh_gets_its_own_keys_before_it_starts(self, fb):
        fb.run = record = Recorder()
        fb.apply_ssh(True)
        assert record.calls.index(["ssh-keygen", "-A"]) < record.calls.index(
            ["systemctl", "enable", "--now", "ssh.service"])

    def test_the_firewall_closes_before_it_opens(self, fb):
        fb.run = record = Recorder()
        fb.apply_firewall(True, ssh=True)
        flat = [" ".join(call[1:]) for call in record.calls if call[0] == fb.UFW]
        assert flat.index("default deny incoming") < flat.index("--force enable")
        assert "limit 22/tcp" in flat
        record.calls.clear()
        fb.apply_firewall(True, ssh=False)
        assert not any("22/tcp" in " ".join(call) for call in record.calls)

    def test_the_clock_rule_never_rewrites_the_hardware_clock(self, fb, tmp_path):
        fb.run = record = Recorder()
        fb.apply_clock(True)
        assert record.calls == [["timedatectl", "set-local-rtc", "1", "--adjust-system-clock"]]

    def test_without_timedated_the_clock_rule_is_still_written(self, fb, tmp_path):
        fb.ADJTIME = tmp_path / "adjtime"
        fb.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "no bus")
        fb.apply_clock(True)
        assert fb.ADJTIME.read_text().splitlines()[2] == "LOCAL"

    def test_only_changed_wear_settings_are_sent(self, fb):
        fb.run = record = Recorder()
        fb.apply_wear({"journal": True, "ram_cache": True}, {"journal": False, "ram_cache": True})
        assert record.calls == [[fb.WEAR_TOOL, "set", "journal", "1"]]

    def test_an_optional_failure_is_reported_and_setup_goes_on(self, fb):
        state = summary_state(fb)
        stub_applies(fb)

        def broken(*args):
            raise RuntimeError("ufw exploded")

        fb.apply_firewall = broken
        fb.UFW = __file__
        fb.ui.screen = FakeScreen([])
        fb.apply_all(state)
        assert any("ufw exploded" in warning for warning in state.warnings)

    def test_an_essential_failure_stops_setup(self, fb):
        state = summary_state(fb)
        stub_applies(fb)

        def broken(*args):
            raise RuntimeError("useradd exploded")

        fb.apply_account = broken
        fb.ui.screen = FakeScreen([])
        with pytest.raises(RuntimeError, match="useradd"):
            fb.apply_all(state)


def summary_state(fb):
    from test_firstboot import summary_state as finished

    return finished(fb)


def stub_applies(fb):
    for name in ("apply_locale", "apply_timezone", "apply_clock", "apply_hostname", "apply_account",
                 "apply_sudo_password", "apply_autologin", "apply_keyring_autounlock", "apply_theme",
                 "apply_icon_theme", "apply_scale", "apply_screen_lock", "apply_network", "apply_swap",
                 "apply_ssh", "apply_firewall", "apply_wear", "apply_found_message",
                 "drop_passphrase_stash", "refresh_initramfs_for_keymap", "apply_expand"):
        setattr(fb, name, lambda *args, **kwargs: True)
    fb.finalise_encryption = lambda: False
    fb._zram_percent = lambda: 50


# --------------------------------------------------------------------------
# The image carries what setup offers
# --------------------------------------------------------------------------


class TestTheImageCarriesWhatSetupOffers:
    """First boot has no network, so everything offered has to be installed already."""

    def test_a_minimal_stick_has_the_ssh_server_and_firewall(self):
        resolved = packages.resolve(packages.MINIMAL_GROUPS)
        assert "openssh-server" in resolved and "ufw" in resolved

    def test_the_desktop_has_the_screen_locker_it_configures(self):
        assert "xfce4-screensaver" in packages.DESKTOP

    def test_the_ssh_server_is_off_until_asked_for(self):
        body = (REPO / "portlin" / "rootfs.py").read_text()
        assert re.search(r'for unit in \("ssh\.service", "ssh\.socket"\):\s*\n\s*chroot\.run\(\["systemctl", "disable", unit\]',
                         body)

    def test_setup_starts_after_networkmanager(self):
        assert "After=NetworkManager.service" in UNIT.read_text()


class TestConsoleFont:
    """The console font grows with the screen, so setup stays legible on 4K."""

    @pytest.fixture
    def fonts(self, tmp_path):
        directory = tmp_path / "consolefonts"
        directory.mkdir()
        for face in ("Terminus", "TerminusBold"):
            for size in ("18x10", "20x10", "22x11", "24x12", "28x14", "32x16"):
                (directory / f"Uni2-{face}{size}.psf.gz").touch()
        return directory

    def pick(self, fb, tmp_path, fonts, size):
        size_file = tmp_path / "virtual_size"
        size_file.write_text(f"{size}\n")
        font = fb.console_font(size_file, fonts)
        return font and font.name

    @pytest.mark.parametrize("size, font", [
        ("3840,2160", "Uni2-TerminusBold32x16.psf.gz"),
        ("3200,1800", "Uni2-TerminusBold24x12.psf.gz"),
        ("2560,1440", "Uni2-TerminusBold20x10.psf.gz"),
        ("1920,1200", None),
        ("1920,1080", None),
        ("1366,768", None),
    ])
    def test_it_scales_with_the_screen_height(self, fb, tmp_path, fonts, size, font):
        assert self.pick(fb, tmp_path, fonts, size) == font

    def test_it_falls_back_to_the_regular_face(self, fb, tmp_path, fonts):
        (fonts / "Uni2-TerminusBold32x16.psf.gz").unlink()
        assert self.pick(fb, tmp_path, fonts, "3840,2160") == "Uni2-Terminus32x16.psf.gz"

    def test_no_framebuffer_or_fonts_leaves_the_kernel_font(self, fb, tmp_path, fonts):
        assert fb.console_font(tmp_path / "missing", fonts) is None
        assert self.pick(fb, tmp_path, tmp_path / "nofonts", "3840,2160") is None
        assert self.pick(fb, tmp_path, fonts, "garbage") is None

    def test_the_font_is_set_before_curses_starts(self):
        source = WIZARD.read_text()
        body = source[source.index("def claim_console"):source.index("def log(")]
        assert 'attempt(["setfont", str(font)])' in body
        main_body = source[source.index("def main()"):]
        assert main_body.index("claim_console(True)") < main_body.index("ui.start()")

    def test_the_keyboard_step_leaves_the_font_alone(self):
        # A full setupcon reloads the 8x16 font from /etc/default/console-setup
        # and shrinks setup straight after the keyboard screen.
        source = WIZARD.read_text()
        body = source[source.index("def apply_keyboard"):source.index("def apply_locale")]
        assert 'run(["setupcon", "--keyboard-only", "--save"]' in body
