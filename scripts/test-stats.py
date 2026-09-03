#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Run the shipped panel readout on real Linux and prove the panel could draw it.

Every unit test of this stops at the edge of the process. They assert what the
readout would print given text that looks like /proc, never that /proc is where
it looks, and never that Pango accepts the result. Both of those live past that
edge, and both fail in the same invisible way: genmon draws nothing, the panel
item is simply absent, and no log anywhere says why.

So this runs the real file, twice, against the real kernel, and hands the first
line to the real Pango. The unit suite cannot do the last part at all -- Pango
is a C library and the markup check there is hand-written precisely because
expat is not always present on a developer's machine.

Two runs rather than one, because the CPU field is the only thing here with
memory. A single run can only ever report "--%", so a script that ran once would
pass just as happily with the state file broken, never written, or written
somewhere nothing reads it back from.

Needs a Debian userland with GTK. Run under `make harness`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
STATS = RUNTIME / "portlin-stats"

TXT = re.compile(r"^<txt>(.*)</txt>$", re.S)
TOOL = re.compile(r"^<tool>(.*)</tool>$", re.S)
CLICK = re.compile(r"^<txtclick>(.*)</txtclick>$", re.S)

failures = 0


def check(condition: bool, message: str) -> None:
    global failures
    if condition:
        print(f"  ok    {message}")
    else:
        failures += 1
        print(f"  FAIL  {message}")


def run(runtime_dir: str) -> tuple[int, str]:
    """Run the shipped script the way genmon does: a fresh process, no arguments.

    PYTHONPATH stands in for the /usr/lib/portlin that only exists on an
    installed stick, which is the same substitution the unit tests make.
    """
    environment = dict(os.environ)
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    environment["PYTHONPATH"] = str(RUNTIME)
    result = subprocess.run(
        [sys.executable, str(STATS)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    if result.stderr.strip():
        print(f"  stderr: {result.stderr.strip()}")
    return result.returncode, result.stdout


def main() -> int:
    print("portlin-stats, against a real kernel and a real Pango")

    with tempfile.TemporaryDirectory() as runtime_dir:
        os.chmod(runtime_dir, 0o700)

        code, first = run(runtime_dir)
        check(code == 0, "the first run exits cleanly")

        lines = first.splitlines()
        check(len(lines) == 3, f"prints exactly the three genmon tags (got {len(lines)})")
        if len(lines) != 3:
            return 1

        text = TXT.match(lines[0])
        tooltip = TOOL.match(lines[1])
        click = CLICK.match(lines[2])
        check(text is not None, "the first line is a <txt> element")
        check(tooltip is not None, "the second line is a <tool> element")
        check(click is not None, "the third line is a <txtclick> element")
        if not (text and tooltip and click):
            return 1

        # The fields that cannot be unknown on a real Linux kernel. cpu is not
        # among them: the first run of a session has nothing to compare against.
        for field in ("cpu", "mem", "disk", "ip"):
            check(field in text.group(1), f"the readout carries a {field} field")
        check("--" not in text.group(1).split("mem")[1].split("disk")[0],
              "memory is a real number on a machine with /proc/meminfo")

        check_pango(text.group(1), "the panel line")
        check_pango(tooltip.group(1), "the tooltip")

        state = Path(runtime_dir) / "portlin-stats.json"
        check(state.exists(), "the state file lands in XDG_RUNTIME_DIR")
        if state.exists():
            check(state.stat().st_mode & 0o777 == 0o600, "the state file is private")

        # Busy the CPU so the second run has a delta worth reporting, and so a
        # readout that reported 0% for a working machine would be caught.
        deadline = 200_000_000
        while deadline:
            deadline -= 1

        code, second = run(runtime_dir)
        check(code == 0, "the second run exits cleanly")
        line = TXT.match(second.splitlines()[0])
        check(line is not None, "the second run prints a <txt> element")
        if line:
            cpu = line.group(1).split("cpu")[1].split("mem")[0]
            check("--%" not in cpu, f"the second run reports a real CPU figure ({cpu.strip()})")
            check_pango(line.group(1), "the second panel line")

    print("FAILED" if failures else "all checks passed")
    return 1 if failures else 0


def check_pango(markup: str, what: str) -> None:
    """Hand the markup to the library that will actually have to draw it.

    This is the assertion the unit suite structurally cannot make. An unescaped
    ampersand from a DMI product name or an lspci string parses fine as text and
    fails here, which on a real panel means the item silently disappears on that
    machine and no other.
    """
    try:
        import gi

        gi.require_version("Pango", "1.0")
        from gi.repository import Pango
    except (ImportError, ValueError) as error:
        check(False, f"Pango is available to parse {what} ({error})")
        return
    try:
        Pango.parse_markup(markup, -1, "\0")
        check(True, f"Pango accepts {what}")
    except Exception as error:  # GLib.GError, whose type needs the import above
        check(False, f"Pango rejects {what}: {error}")


if __name__ == "__main__":
    sys.exit(main())
