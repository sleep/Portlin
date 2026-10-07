#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Run the shipped HUD against a real kernel, and build its window under Xvfb.

The unit tests feed hud.py captured text and a /proc built under tmp_path.
What they cannot see is whether the readers look in the right place on a
real Linux: a parser correct about a string and a reader that opens the
wrong file pass the same suite and draw an empty card. So this runs
`portlin-hud --json` for real, as the window would, and checks the answers
a kernel always has. Then it builds the real window against a real X server
and feeds it a snapshot, which is the one way to find a card that raises on
a shape of data the unit tests never drew.

Needs a Debian userland with GTK and Xvfb. Run under `make harness`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME = REPO / "portlin" / "resources" / "runtime"
HUD = RUNTIME / "portlin-hud"
DISPLAY = ":98"

failures = 0


def check(condition: bool, message: str) -> None:
    global failures
    if condition:
        print(f"  ok    {message}")
    else:
        failures += 1
        print(f"  FAIL  {message}")


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_json() -> dict | None:
    environment = {**os.environ, "PYTHONPATH": str(RUNTIME)}
    result = subprocess.run(
        [sys.executable, str(HUD), "--json", "--no-public"],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    if result.stderr.strip():
        print(f"  stderr: {result.stderr.strip()[:2000]}")
    check(result.returncode == 0, "portlin-hud --json exits cleanly")
    try:
        return json.loads(result.stdout)
    except ValueError:
        check(False, "the output is JSON")
        print(f"  output was:\n{result.stdout[:2000]}")
        return None


def check_snapshot(snapshot: dict) -> None:
    host = snapshot.get("host") or {}
    check(bool(host.get("kernel")), f"the kernel is named ({host.get('kernel')})")
    check(host.get("uptime_seconds", 0) > 0, "uptime comes from /proc/uptime")
    check(host.get("load") is not None, "the load average is read")
    cpu = snapshot.get("cpu") or {}
    check(cpu.get("percent") is not None, f"the second sample has a CPU figure ({cpu.get('percent')})")
    check(len(cpu.get("cores") or []) >= 1 and cpu["cores"][0] is not None,
          f"every core has a figure ({len(cpu.get('cores') or [])} cores)")
    memory = snapshot.get("memory") or {}
    check(bool(memory.get("total")), "memory has a total")
    check(memory.get("used") is not None and memory["used"] <= memory.get("total", 0),
          "used memory is no more than the total")
    parts = sum(memory.get(key) or 0 for key in ("anon", "page_cache", "buffers", "shmem", "reclaimable", "free"))
    check(0 < parts <= memory.get("total", 0) * 1.05, "the breakdown adds up to no more than the total")
    filesystems = snapshot.get("filesystems") or []
    root_has_device = any(
        fields[1] == "/" and fields[0].startswith("/dev/")
        for fields in (line.split() for line in Path("/proc/mounts").read_text().splitlines())
        if len(fields) > 1
    )
    check(any(fs["mountpoint"] == "/" for fs in filesystems) or not root_has_device,
          "the root filesystem is listed when /proc/mounts names a device for it")
    for fs in filesystems:
        check(fs["used_bytes"] <= fs["total_bytes"], f"{fs['mountpoint']} is not over full")
    interfaces = snapshot.get("interfaces") or []
    check(all(interface["name"] != "lo" for interface in interfaces), "loopback is left out")
    check(all(interface["state"] == "up" or interface["kind"] in ("ethernet", "wifi", "mobile")
              for interface in interfaces),
          "the kernel's dead tunnel devices are left out")
    for interface in interfaces:
        check(interface["rx_rate"] is not None, f"{interface['name']} has a rate on the second sample")
    addresses = snapshot.get("addresses") or {}
    check(addresses.get("public_state") == "off", "--no-public never asks for the public address")
    agents = snapshot.get("agents") or {}
    check(isinstance(agents.get("processes"), list), "the agents card has a process list")
    check("installed" in agents, "the agents card says what is installed")
    disks = snapshot.get("disks") or []
    for disk in disks:
        check(not disk["name"].startswith(("loop", "ram", "dm-")), f"{disk['name']} is a whole disk")


def start_xvfb() -> subprocess.Popen:
    server = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1280x800x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(50):
        if subprocess.run(["xset", "q"], capture_output=True).returncode == 0:
            return server
        time.sleep(0.2)
    raise SystemExit("Xvfb never came up")


def check_the_window(snapshot: dict) -> None:
    """Build the real window, hand it the real snapshot, then an empty one.

    The collector is replaced with one that answers the saved snapshot, so
    the window draws what --json just read without sampling again. A timer
    then inspects the cards, applies a snapshot with nothing in it, which is
    what every card has to survive on a machine missing that source, and
    closes the window, which ends the application.
    """
    server = start_xvfb()
    try:
        sys.path.insert(0, str(RUNTIME))
        hud_tool = load(HUD, "portlin_hud")
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk

        hud_tool.Collector.snapshot = lambda self: snapshot  # type: ignore[method-assign]
        seen: dict = {}
        (REPO / "out").mkdir(exist_ok=True)

        deadline = time.monotonic() + 30

        def inspect() -> bool:
            windows = [w for w in Gtk.Window.list_toplevels()
                       if w.get_visible() and isinstance(w, Gtk.ApplicationWindow)]
            seen["windows"] = len(windows)
            if not windows:
                return time.monotonic() < deadline
            window = windows[0]
            # The first snapshot arrives from the collector's thread; on a
            # cold container the toolkit's own start-up can take longer than
            # that, so wait for it rather than for a number of seconds.
            if not window._cards["cpu"].headline.get_text() and time.monotonic() < deadline:
                return True
            seen["cards"] = len(window.grid.get_children())
            seen["columns"] = window.columns
            seen["cpu"] = window._cards["cpu"].headline.get_text()
            seen["memory"] = window._cards["memory"].headline.get_text()
            seen["agents"] = window._cards["agents"].headline.get_text()
            shot = subprocess.run(["import", "-window", "root", str(REPO / "out" / "hud-harness.png")],
                                  capture_output=True)
            seen["screenshot"] = shot.returncode == 0
            try:
                window._apply({"host": {}, "release": {}, "cpu": {}, "memory": None, "gpu": None,
                               "addresses": {}, "filesystems": [], "disks": [], "interfaces": [],
                               "agents": {}})
                seen["empty_ok"] = True
            except Exception as error:
                seen["empty_error"] = f"{type(error).__name__}: {error}"
            window.destroy()
            return False

        GLib.timeout_add(500, inspect)
        hud_tool.run_window(interval=60, fullscreen=False, lookup_public=False)

        check(seen.get("windows") == 1, "the HUD window opened")
        check(seen.get("cards") == 9, f"it drew every card ({seen.get('cards')})")
        check(seen.get("columns") == 3, f"a 1280 px screen gets three columns ({seen.get('columns')})")
        check(seen.get("cpu", "--%") != "--%", f"the CPU headline is a number ({seen.get('cpu')})")
        check("/" in seen.get("memory", ""), f"the memory headline is used over total ({seen.get('memory')})")
        check(bool(seen.get("agents")), f"the agents headline says something ({seen.get('agents')})")
        check(seen.get("empty_ok", False), f"an empty snapshot draws without raising {seen.get('empty_error', '')}")
        if seen.get("screenshot"):
            print(f"  screenshot: {REPO / 'out' / 'hud-harness.png'}")
    finally:
        server.terminate()


def main() -> int:
    print("portlin-hud, against a real kernel and a real X server")
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        snapshot = run_json()
    if snapshot is None:
        return 1
    check_snapshot(snapshot)
    check_the_window(snapshot)
    print("FAILED" if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
