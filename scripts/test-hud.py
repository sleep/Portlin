#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Run the shipped HUD against a real kernel, and in a real terminal.

The unit tests feed hud.py captured text and a /proc built under tmp_path.
What they cannot see is whether the readers look in the right place on a
real Linux: a parser correct about a string and a reader that opens the
wrong file pass the same suite and draw an empty card. So this runs
`portlin-hud --json` for real, as the screen would, and checks the answers
a kernel always has. Then it draws that snapshot, which is the one way to
find a card that raises on a shape of data the unit tests never drew, and
runs the screen itself in a pseudo-terminal until it is told to quit.

Needs a Debian userland. Run under `make harness`.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME = REPO / "portlin" / "resources" / "runtime"
HUD = RUNTIME / "portlin-hud"

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


def check_the_cards(snapshot: dict) -> None:
    """Draw the real snapshot at a narrow and a wide width, then an empty one."""
    sys.path.insert(0, str(RUNTIME))
    hud_tool = load(HUD, "portlin_hud")
    for width in (80, 160):
        lines = hud_tool.render(snapshot, width)
        check({hud_tool.line_width(line) for line in lines} == {width}, f"every line is {width} columns")
    text = hud_tool.render_text(snapshot, 120)
    for title in ("SYSTEM", "CPU", "MEMORY", "STORAGE", "NETWORK", "AI AGENTS"):
        check(title in text, f"the {title} card is drawn")
    (REPO / "out").mkdir(exist_ok=True)
    (REPO / "out" / "hud-harness.txt").write_text(text + "\n")
    print(f"  drawn: {REPO / 'out' / 'hud-harness.txt'}")
    try:
        hud_tool.render({"host": {}, "release": {}, "cpu": {}, "memory": None, "gpu": None,
                         "addresses": {}, "filesystems": [], "disks": [], "interfaces": [], "agents": {}}, 120)
        check(True, "an empty snapshot draws without raising")
    except Exception as error:
        check(False, f"an empty snapshot draws without raising: {type(error).__name__}: {error}")


def read_until(fd: int, wanted: bytes, deadline: float) -> bytes:
    """Read the pty until wanted appears; an empty wanted reads until the deadline."""
    seen = b""
    while (not wanted or wanted not in seen) and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
    return seen


def check_the_screen() -> None:
    """Start the real curses screen in a 120x40 terminal, wait for it, press q."""
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.environ.update({"TERM": "xterm-256color", "LANG": "C.UTF-8", "PYTHONPATH": str(RUNTIME)})
        os.execv(sys.executable, [sys.executable, str(HUD), "--no-public", "--interval", "1"])
    output = read_until(fd, b"AI AGENTS", time.monotonic() + 30)
    check(b"SYSTEM" in output and b"AI AGENTS" in output, "the screen draws its cards in a terminal")
    os.write(fd, b"q")
    status = None
    deadline = time.monotonic() + 10
    while status is None and time.monotonic() < deadline:
        # Keep reading, or a child writing its last frame blocks on a full pty.
        read_until(fd, b"", time.monotonic() + 0.2)
        waited, code = os.waitpid(pid, os.WNOHANG)
        if waited:
            status = os.waitstatus_to_exitcode(code)
    if status is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    check(status == 0, f"q quits it cleanly (exit {status})")
    os.close(fd)


def main() -> int:
    print("portlin-hud, against a real kernel and in a real terminal")
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        snapshot = run_json()
    if snapshot is None:
        return 1
    check_snapshot(snapshot)
    check_the_cards(snapshot)
    check_the_screen()
    print("FAILED" if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
