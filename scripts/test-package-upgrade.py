#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Install the runtime packages, then upgrade them, and prove it stays quiet.

The assertion that matters is the second install: dpkg must not stop to ask
about a conffile. That prompt would surface in the middle of an unrelated
apt full-upgrade, which is where a user is least equipped to answer it, and no
unit test can see it because it only exists once dpkg is really running.

Needs root and a Debian userland. Run under `make harness`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THEME = "etc/xdg/xfce4/terminal/terminalrc"


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def build(output: Path, version: str) -> None:
    result = run(
        [sys.executable, "-m", "portlin", "package",
         "--output", str(output), "--version", version],
        cwd=REPO,
    )
    if result.returncode != 0:
        sys.exit(f"building {version} failed:\n{result.stderr}")


def install(debs: list[Path]) -> subprocess.CompletedProcess:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    return subprocess.run(
        ["apt-get", "install", "-y",
         "-o", "Dpkg::Options::=--force-confdef",
         *[str(d) for d in debs]],
        capture_output=True, text=True, env=env,
    )


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("needs root; run it under make harness")

    with tempfile.TemporaryDirectory() as tmp:
        first, second = Path(tmp) / "v1", Path(tmp) / "v2"
        first.mkdir()
        second.mkdir()

        build(first, "0.1.0~test")
        debs = sorted(first.glob("*.deb"))
        if len(debs) != 3:
            sys.exit(f"expected three packages, built {len(debs)}")

        result = install(debs)
        if result.returncode != 0:
            sys.exit(f"first install failed:\n{result.stderr}")

        installed = Path("/") / THEME
        if not installed.exists():
            sys.exit(f"{THEME} was not installed")
        print(f"ok: first install placed {THEME}")

        # Change the shipped content so the second build genuinely differs.
        # An upgrade whose files are byte-identical would never prompt, and
        # would prove nothing.
        source = REPO / "portlin/resources/runtime/theme/terminalrc"
        original = source.read_text()
        source.write_text(original + "\n# upgrade probe\n")
        try:
            build(second, "0.1.1~test")
        finally:
            source.write_text(original)

        result = install(sorted(second.glob("*.deb")))
        if result.returncode != 0:
            sys.exit(f"upgrade failed:\n{result.stderr}")

        output = result.stdout + result.stderr
        for phrase in ("Configuration file", "conffile", "What would you like"):
            if phrase in output:
                sys.exit(f"the upgrade prompted about a conffile:\n{output}")
        print("ok: the upgrade did not prompt")

        if "# upgrade probe" not in installed.read_text():
            sys.exit(f"{THEME} was not updated by the upgrade")
        print("ok: the upgrade replaced the unmodified conffile")

    print("\npackage upgrade harness passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
