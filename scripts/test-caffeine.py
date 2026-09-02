#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Run the caffeine applet against a real X server and prove it moves the screen settings.

Every unit test of this applet stops at the edge of the process: they assert
what it would ask for, never that asking works. The applet is a GTK program
whose whole job is a side effect on other software, so the interesting
failures all live past that edge -- a StatusIcon that will not construct, an
xset invocation the server rejects, a restore that puts back different numbers
from the ones that were read.

This drives the shipped file under Xvfb. It sets known blanking settings,
turns caffeine on, and requires the server to report them off; then turns it
back off and requires the exact numbers it started with, not the X defaults.

Deliberately run with no session bus and no logind: that is the degraded case,
and it is the one where a bug is invisible. Every optional layer fails here,
so what is left is the xset layer alone -- which is precisely the backstop that
has to work when the other two are missing.

It also rasterises both panel icons through the library that draws them. The
applet answers a pixbuf it cannot load by putting a stock icon in the panel
instead, which is right on a machine somebody has taken librsvg off and is
also why a malformed drawing ships unnoticed: there is always a cup-shaped
hole, and something is always in it. Only a real rasteriser can say the icon
that appears is the icon in the repository.

What it cannot reach is DPMS. Xvfb does not implement the extension, with or
without +extension DPMS, so a server here has screen-blanking settings and no
display-power ones. The blanking half is exercised end to end; the DPMS half
is asserted only if some future server here does offer it, and the run says
which of the two it covered rather than passing quietly on half the job.

Needs a Debian userland with GTK. Run under `make harness`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME = REPO / "portlin" / "resources" / "runtime"
APPLET = RUNTIME / "portlin-caffeine"
DISPLAY = ":99"

# What the applet must find, and must put back. Not the X server's defaults:
# the whole point of reading before writing is that these are somebody's
# settings rather than the ones the server was compiled with.
BASELINE_SCREENSAVER = ("600", "600")
BASELINE_DPMS = ("900", "1200", "1800")


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str]) -> str:
    result = run(argv)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout


def load_applet():
    loader = importlib.machinery.SourceFileLoader("portlin_caffeine", str(APPLET))
    spec = importlib.util.spec_from_file_location(loader.name, APPLET, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


def check_icons(caffeine) -> None:
    """Load every icon the applet names, at the size the panel asks for."""
    from gi.repository import GdkPixbuf

    for active, installed in caffeine.ICONS.items():
        path = RUNTIME / Path(installed).name
        if not path.exists():
            raise SystemExit(f"the applet draws {active} with {installed}, not shipped")
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 22, 22, True)
        except Exception as error:
            raise SystemExit(f"{path.name} will not rasterise: {error}")
        # A pixbuf of nothing loads without complaint. The drawing has to have
        # put something opaque in it, or an empty file would pass this.
        if not pixbuf.get_has_alpha() or pixbuf.get_width() != 22:
            raise SystemExit(f"{path.name} rasterised to {pixbuf.get_width()} px")
        if not any(pixbuf.get_pixels()[3::4]):
            raise SystemExit(f"{path.name} rasterised to a blank square")
    print("ok: both panel icons rasterised at panel size")


def start_xvfb() -> subprocess.Popen:
    # +extension DPMS: Xvfb does not load it by default, and without it the
    # half of this harness that matters most -- the display power timers --
    # would quietly assert nothing at all.
    server = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1024x768x24", "+extension", "DPMS"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(50):
        if run(["xset", "q"]).returncode == 0:
            return server
        time.sleep(0.2)
    raise SystemExit("Xvfb never came up")


def screen_state(caffeine) -> dict:
    return caffeine.parse_xset(must(["xset", "q"]))


def main() -> int:
    if not APPLET.exists():
        raise SystemExit(f"no applet at {APPLET}")

    server = start_xvfb()
    try:
        caffeine = load_applet()
        check_icons(caffeine)
        home = Path("/tmp/caffeine-harness-home")
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")

        must(["xset", "s", *BASELINE_SCREENSAVER])
        # Tolerated rather than required: xset reports a server with no DPMS
        # extension on stderr and still exits 0, and Xvfb is such a server.
        run(["xset", "dpms", *BASELINE_DPMS])
        run(["xset", "+dpms"])

        has_dpms = "dpms" in screen_state(caffeine)
        print(
            "ok: baseline blanking settings are in place"
            + ("" if has_dpms else " (this server has no DPMS extension)")
        )

        class FakeApplication:
            """Stands in for the Gtk.Application, which only quit() is used on."""

            def quit(self) -> None:
                pass

        applet = caffeine.Caffeine(caffeine.Settings(), FakeApplication())
        print("ok: the applet constructed against a real X server")

        applet.set_active(True)
        state = screen_state(caffeine)
        # Only the timeout. `xset s off` disarms the blank without touching
        # the cycle, so the cycle still reads back as the baseline -- which is
        # also why restoring both of them has to put the timeout back.
        if state.get("screensaver", (None,))[0] != 0:
            raise SystemExit(f"screen blanking still armed: {state}")
        if has_dpms and state.get("dpms_enabled") is not False:
            raise SystemExit(f"DPMS still enabled after turning caffeine on: {state}")
        print("ok: turning caffeine on disarmed screen blanking")

        # The logind lock and both session inhibitors are unavailable here.
        # Surviving that is the assertion: the applet has to keep working on
        # the one layer it has left rather than falling over.
        if not applet.settings.active:
            raise SystemExit("the applet did not stay active without logind")
        print("ok: it stayed active with no logind and no session bus to ask")

        stored = caffeine.parse_settings(caffeine.settings_path().read_text())
        if not stored.active:
            raise SystemExit("the active state was not written to disk")
        print("ok: the state reached the settings file")

        applet.set_active(False)
        state = screen_state(caffeine)
        expected_saver = tuple(int(value) for value in BASELINE_SCREENSAVER)
        expected_dpms = tuple(int(value) for value in BASELINE_DPMS)
        if state.get("screensaver") != expected_saver:
            raise SystemExit(
                f"screen blanking came back as {state.get('screensaver')}, "
                f"not the {expected_saver} it was asked to restore"
            )
        if has_dpms:
            if state.get("dpms") != expected_dpms:
                raise SystemExit(
                    f"DPMS timeouts came back as {state.get('dpms')}, "
                    f"not {expected_dpms}"
                )
            if state.get("dpms_enabled") is not True:
                raise SystemExit("DPMS was left disabled after turning caffeine off")
        print("ok: turning it off restored the exact settings it found")

        # Off, then on again, on a machine whose settings the applet has now
        # written once. The second read has to see the restored numbers rather
        # than the zeros it left behind, or the first restore was the only one
        # that would ever work.
        applet.set_active(True)
        applet.set_active(False)
        if screen_state(caffeine).get("screensaver") != expected_saver:
            raise SystemExit("a second cycle did not restore the same settings")
        print("ok: a second on and off cycle restored them again")

        covered = "screen blanking and DPMS" if has_dpms else "screen blanking"
        print(f"\ncaffeine harness passed ({covered})")
        return 0
    finally:
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
