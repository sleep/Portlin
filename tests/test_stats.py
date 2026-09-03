"""The panel readout: what it says, what it refuses to say, and where it caches.

Every assertion here is about honesty rather than formatting. The readout is
read at a glance and believed, so the interesting failures are all the ones
where it prints a plausible number that is not true: a since-boot average shown
as the current load, an Intel clock speed shown as a utilisation percentage, a
resumed stick showing the previous laptop's hardware. None of those look wrong
on screen, which is why each has a test.

The rendering tests work on the markup string rather than a screenshot, so they
also stand in for the one failure that is invisible rather than merely wrong:
markup that does not parse makes the panel item disappear entirely, and only on
the machine whose hardware name contained the offending character.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
from pathlib import Path

import pytest

from conftest import load_tool

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"

ACCENT = "#FF3355"


@pytest.fixture(scope="module")
def stats():
    return load_tool("portlin-stats")


@pytest.fixture(scope="module")
def hostinfo():
    return load_tool("hostinfo.py")


def _fields(**overrides):
    base = {
        "cpu": "14%",
        "mem": "3.1G/15.5G",
        "gpu": "22%",
        "disk": "6.1G/31.0G",
        "ip": "192.168.1.42",
        "bat": "87%",
        "encrypted": False,
    }
    base.update(overrides)
    return base


# The tags the readout is allowed to emit, and the entities XML defines. Pango
# markup is XML, so an unescaped ampersand in a hardware name is not a cosmetic
# problem: the parse fails, genmon draws nothing, and the panel item vanishes on
# that machine alone.
SPAN = re.compile(r'<span foreground="#[0-9A-Fa-f]{6}">|</span>')
ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);")


def _assert_well_formed(markup: str) -> None:
    """Require the markup to be XML a parser would accept, without needing one.

    Checked by hand rather than through xml.etree because these tests have to
    run anywhere -- that is the whole point of portlin's unit suite, and expat
    is a C library that is not always there. The rule being asserted is small
    enough to state directly: the only angle brackets are portlin's own spans,
    those spans balance, and every remaining ampersand opens a real entity.

    A real Pango parse of the same string happens in scripts/test-stats.py,
    which runs on Linux with GTK present.
    """
    depth = 0
    for tag in SPAN.finditer(markup):
        depth += -1 if tag.group().startswith("</") else 1
        assert depth >= 0, f"closing span with nothing open: {markup}"
    assert depth == 0, f"unbalanced spans: {markup}"

    text = ENTITY.sub("", SPAN.sub("", markup))
    assert "<" not in text and ">" not in text, f"raw angle bracket: {markup}"
    assert "&" not in text, f"ampersand that opens no entity: {markup}"


class TestTheMarkupCheckerItself:
    """A hand-written checker that accepts everything makes the three tests
    below pass without asserting anything, so it is shown to reject first."""

    @pytest.mark.parametrize(
        "bad",
        [
            "Acme & Co",
            "a < b",
            "a > b",
            '<span foreground="#E8EDF3">unclosed',
            "</span>",
            "&nosuchentity;",
        ],
    )
    def test_it_rejects_what_pango_would_reject(self, bad):
        with pytest.raises(AssertionError):
            _assert_well_formed(bad)

    @pytest.mark.parametrize(
        "good",
        ["Acme &amp; Co", "&lt;Radeon&gt;", '<span foreground="#FF3355">luks</span>', ""],
    )
    def test_it_accepts_what_pango_would_accept(self, good):
        _assert_well_formed(good)


class TestTheReadoutIsValidMarkup:
    def test_an_ordinary_run_parses(self, stats):
        _assert_well_formed(stats.render_text(_fields()))

    def test_an_ampersand_in_hardware_does_not_blank_the_panel(self, stats, hostinfo):
        # "[AMD/ATI]" is ordinary lspci output and an ampersand in a DMI product
        # name is uncommon but real. Unescaped, both take the whole panel item
        # off screen with nothing to say why, on that machine only.
        host = hostinfo.Host(
            model="Acme & Co <Laptop>",
            cpu='Some "CPU" & friends',
            threads=8,
            memory_bytes=16 * 1024**3,
            firmware="UEFI, 64-bit",
        )
        gpu = hostinfo.Gpu("name", None, "", "card0 [AMD/ATI] <Radeon> & more")
        tooltip = stats.render_tooltip(host, gpu, {"source": "/dev/sda4"}, 0)
        _assert_well_formed(tooltip)
        assert "&amp;" in tooltip
        assert "&lt;Radeon&gt;" in tooltip

    def test_a_hostile_value_survives_the_panel_line_too(self, stats):
        _assert_well_formed(stats.render_text(_fields(ip="a&b<c>")))


class TestTheAccentMeansOneThing:
    """Crimson is the encrypted root, everywhere in portlin, and nothing else.

    The rule is only worth having if it is enforced, because the pressure to
    spend it is constant: a low battery and a pegged CPU both look like things
    that want a warning colour. A colour that means one thing stops meaning it
    the moment it is spent on a second.
    """

    def test_an_encrypted_root_is_the_one_thing_that_is_crimson(self, stats):
        assert ACCENT in stats.render_text(_fields(encrypted=True))

    def test_a_plain_root_uses_no_accent_at_all(self, stats):
        assert ACCENT not in stats.render_text(_fields(encrypted=False))

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cpu": "100%"},
            {"bat": "3%"},
            {"mem": "15.4G/15.5G"},
            {"disk": "6.1G/6.5G of 119.2G"},
            {"ip": "none"},
        ],
    )
    def test_nothing_else_ever_earns_it(self, stats, overrides):
        assert ACCENT not in stats.render_text(_fields(encrypted=False, **overrides))

    def test_the_word_luks_is_what_carries_it(self, stats):
        rendered = stats.render_text(_fields(encrypted=True))
        assert f'<span foreground="{ACCENT}">luks</span>' in rendered


class TestFieldsThatDisappear:
    def test_a_desktop_shows_no_battery_rather_than_a_placeholder(self, stats):
        # A machine with no battery is a desktop, and "bat --%" on a desktop is
        # a field that will never say anything.
        rendered = stats.render_text(_fields(bat=None))
        assert "bat" not in rendered

    def test_a_card_with_no_counter_shows_no_gpu_field(self, stats):
        # It has nothing that moves, and the panel line is for things that do.
        # The card is still named, in the tooltip.
        rendered = stats.render_text(_fields(gpu=None))
        assert "gpu" not in rendered

    def test_the_remaining_fields_keep_their_order(self, stats):
        rendered = stats.render_text(_fields(gpu=None, bat=None))
        assert rendered.index("cpu") < rendered.index("mem") < rendered.index("disk")
        assert rendered.index("disk") < rendered.index("ip")

    def test_an_unknown_cpu_says_so_rather_than_vanishing(self, stats):
        # Unlike gpu and bat, this field will have an answer in two seconds, so
        # removing it would make the line change width on every startup.
        assert "--%" in stats.render_text(_fields(cpu="--%"))


class TestTheGpuNeverFabricatesAPercentage:
    """The honesty rule, at the boundary where it is easiest to break.

    i915 exposes a clock speed and no busy-percent counter. Dividing the current
    clock by the maximum would produce a number between 0 and 100 that sits in
    the same column as AMD's and NVIDIA's real utilisation figures and means
    something entirely different. It is tempting exactly because it would look
    consistent.
    """

    def test_intel_renders_a_frequency(self, stats, hostinfo, tmp_path):
        card = tmp_path / "sys/class/drm/card0"
        (card / "device").mkdir(parents=True)
        (card / "device/boot_vga").write_text("1\n")
        (card / "gt_cur_freq_mhz").write_text("350\n")
        gpu = hostinfo.read_gpu(tmp_path)
        rendered = stats.render_text(_fields(gpu=gpu.label))
        assert "350MHz" in rendered
        assert "%" not in rendered.split("gpu")[1].split("disk")[0]

    def test_the_tooltip_says_in_words_what_the_number_is(self, stats, hostinfo):
        gpu = hostinfo.Gpu("frequency", 350, "350MHz", "i915 reports GPU frequency, not utilisation.")
        host = hostinfo.Host("", "", 0, 0, "BIOS")
        assert "not utilisation" in stats.render_tooltip(host, gpu, {}, 0)


class TestTheTravellingStick:
    """State cached on the stick describes the machine it was cached on.

    This is the one bug in the readout that exists purely because the product is
    portable. Suspend a stick in one laptop, resume it in another, and the
    filesystem comes across with the cached model, CPU and drive geometry
    intact. Every one of those is now wrong and none of them looks it.
    """

    def test_a_new_boot_discards_the_whole_cache(self, stats, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "boot_id": "yesterdays-laptop",
            "stat": [1000, 900],
            "host": {"model": "Some Other Machine"},
            "topology": {"drive_bytes": 123},
        }))
        assert stats.load_state(path, "todays-laptop") == {}

    def test_the_same_boot_keeps_it(self, stats, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"boot_id": "same", "stat": [1000, 900]}))
        assert stats.load_state(path, "same")["stat"] == [1000, 900]

    def test_a_truncated_file_is_not_fatal(self, stats, tmp_path):
        # A panel killed mid-write. Without this the CPU field would read "--%"
        # for the rest of the session with nothing to explain it.
        path = tmp_path / "state.json"
        path.write_text('{"boot_id": "same", "stat": [10')
        assert stats.load_state(path, "same") == {}

    def test_a_missing_file_is_the_ordinary_first_run(self, stats, tmp_path):
        assert stats.load_state(tmp_path / "absent.json", "same") == {}

    def test_a_json_document_that_is_not_an_object_is_refused(self, stats, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]")
        assert stats.load_state(path, "same") == {}


class TestTheStateFile:
    def test_it_is_written_private_to_the_user(self, stats, tmp_path):
        # The XDG_RUNTIME_DIR path is already 0700, but the fallback lives in a
        # shared temporary directory where the mode is the only protection.
        path = tmp_path / "state.json"
        stats.save_state(path, {"boot_id": "x"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_it_replaces_rather_than_truncating(self, stats, tmp_path):
        # os.replace is atomic within a filesystem, so a reader either sees the
        # old file whole or the new one whole, never a half-written one.
        path = tmp_path / "state.json"
        stats.save_state(path, {"boot_id": "first"})
        stats.save_state(path, {"boot_id": "second"})
        assert json.loads(path.read_text())["boot_id"] == "second"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".portlin-stats")]

    def test_an_unwritable_directory_does_not_take_the_panel_down(self, stats, tmp_path):
        # A readout that cannot cache is a readout with no CPU number, which is
        # a great deal better than a panel item that raises and disappears.
        stats.save_state(tmp_path / "nowhere" / "state.json", {"boot_id": "x"})

    def test_the_fallback_path_is_per_user(self, stats, monkeypatch):
        # A predictable shared name in /tmp is somewhere another account could
        # leave a file for this one to read.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert str(os.getuid()) in stats.state_path().name

    def test_the_runtime_directory_is_preferred(self, stats, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert stats.state_path().parent == tmp_path


class TestNvidiaIsAskedRarely:
    def test_a_recent_answer_is_reused(self, stats, monkeypatch):
        # Asking can wake a runtime-suspended discrete card. Doing that every
        # two seconds is a battery cost the stick would be blamed for.
        monkeypatch.setattr(
            stats.hostinfo, "read_nvidia_percent", lambda: pytest.fail("asked again")
        )
        state = {"nvidia": {"value": 31, "at": 1000.0}}
        assert stats.nvidia_reading(state, 1005.0) == 31

    def test_a_stale_answer_is_refreshed(self, stats, monkeypatch):
        monkeypatch.setattr(stats.hostinfo, "read_nvidia_percent", lambda: 42)
        state = {"nvidia": {"value": 31, "at": 1000.0}}
        assert stats.nvidia_reading(state, 1000.0 + stats.NVIDIA_INTERVAL_SECONDS) == 42

    def test_absence_is_cached_like_any_other_answer(self, stats, monkeypatch):
        # On a machine with no proprietary driver -- the ordinary case -- the
        # alternative is a which() lookup and a subprocess attempt every two
        # seconds for an answer that cannot change until someone installs one.
        calls = []
        monkeypatch.setattr(
            stats.hostinfo, "read_nvidia_percent", lambda: calls.append(1)
        )
        state = {}
        stats.nvidia_reading(state, 1000.0)
        assert stats.nvidia_reading(state, 1001.0) is None
        assert len(calls) == 1


class TestTheDiskFieldTellsAnExpansionStory:
    class _Usage:
        def __init__(self, used, total):
            self.used, self.total, self.free = used, total, total - used

    def test_an_expanded_stick_shows_one_capacity(self, stats, monkeypatch):
        # The filesystem fills its partition and the drive has no tail left, so
        # there is nothing to claim and nothing to say about it.
        monkeypatch.setattr(
            stats.shutil, "disk_usage", lambda _: TestTheDiskFieldTellsAnExpansionStory._Usage(6 * 1024**3, 31 * 1024**3)
        )
        field, unclaimed = stats.disk_field(
            {"partition_bytes": 31 * 1024**3, "tail_bytes": 0, "drive_bytes": 31 * 1024**3}
        )
        assert " of " not in field and unclaimed == 0

    def test_an_unexpanded_stick_names_the_drive_it_could_grow_into(
        self, stats, monkeypatch
    ):
        # The image ships at a fixed size, so a fresh stick on a large drive has
        # all its unclaimed space after the root partition, invisible from
        # inside it. Showing the second capacity is what makes that legible
        # without spending a colour on it.
        monkeypatch.setattr(
            stats.shutil, "disk_usage", lambda _: TestTheDiskFieldTellsAnExpansionStory._Usage(6 * 1024**3, 6 * 1024**3)
        )
        field, unclaimed = stats.disk_field(
            {
                "partition_bytes": 6 * 1024**3,
                "tail_bytes": 113 * 1024**3,
                "drive_bytes": 119 * 1024**3,
            }
        )
        assert " of " in field and unclaimed > 0

    def test_no_drive_size_means_no_of_clause(self, stats):
        # Unexpandedness is not knowable without the drive's size, so the claim
        # is dropped rather than guessed at.
        field, _ = stats.disk_field({"partition_bytes": 0, "tail_bytes": 0, "drive_bytes": 0})
        assert field is not None and " of " not in field


class TestTheClickTarget:
    def test_it_is_the_about_dialog_rather_than_a_privileged_command(self, stats):
        # portlin-expand needs root and prompts for the LUKS passphrase, so a
        # one-click path to it would mean a second polkit action. Privilege in
        # portlin goes through one command with a fixed set of verbs, and
        # widening that is a decision of its own, not a side effect of an applet.
        assert stats.CLICK_COMMAND == "portlin-about"
        assert "portlin-expand" not in stats.CLICK_COMMAND


class TestTheReadoutIsNotAGtkProgram:
    def test_it_imports_nothing_outside_the_standard_library(self):
        # It runs every two seconds for the life of the session. A preferences
        # dialog here would mean forking the GTK stack at that rate, which is
        # why this is the one member of portlin-desktop that must stay plain.
        source = (RUNTIME / "portlin-stats").read_text()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert "gi" not in imported
        # The two shipped modules it is allowed to reach for, both stdlib-only.
        assert imported - {"devices", "hostinfo"} <= set(sys_stdlib())


def sys_stdlib() -> set[str]:
    import sys

    return set(sys.stdlib_module_names)
