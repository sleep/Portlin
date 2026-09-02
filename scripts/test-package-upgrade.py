#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Install the runtime packages, then upgrade them, and prove conffiles behave.

Two assertions matter, and both halves of the contract have to be tested
together or neither means anything:

- An unmodified conffile must be replaced silently. dpkg must not stop to ask
  about it either. That prompt would surface in the middle of an unrelated
  apt full-upgrade, which is where a user is least equipped to answer it.
- A conffile the user has edited by hand must survive an upgrade untouched.
  Without this, portlin's own packages could not honestly ship anything under
  /etc: every local edit would be destroyed by the first update.

No unit test can see either of these because both only exist once dpkg is
really running against a real, installed conffile.

Needs root and a Debian userland. Run under `make harness`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from portlin import package as pkg  # noqa: E402

# Two different theme conffiles, one per scenario, so that touching the
# installed copy of one for the "locally modified" case can never be mistaken
# for interference with the other's "left alone" case.
# Both live under the overlay directory portlin-desktop owns, because dpkg
# lets only one installed package own a path and the canonical /etc/xdg
# locations belong to Xfce's own packages.
OVERLAY = "etc/xdg/xdg-portlin"
UNMODIFIED = f"{OVERLAY}/xfce4/terminal/terminalrc"
LOCALLY_MODIFIED = f"{OVERLAY}/gtk-3.0/settings.ini"


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


def clear_previous_installs() -> None:
    """Remove any portlin packages this container already carries.

    make harness runs several of these in one container, and the one before
    this installs portlin's packages at the working tree's own version. The
    first install below is deliberately an older v1, and apt refuses to
    downgrade -- so without this the versions here would have to be bumped in
    step with every release, and forgetting turns the whole gate red for a
    reason that has nothing to do with what any of these harnesses test.

    Purged rather than removed: dpkg -r leaves conffiles on disk, and this
    harness exists to assert what happens to conffiles across an upgrade.

    Derived from pkg.PACKAGES so a fourth package cannot be left behind.
    """
    run(["dpkg", "--purge", "--force-depends", *pkg.PACKAGES])


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

        clear_previous_installs()
        build(first, "0.1.0~test")
        debs = sorted(first.glob("*.deb"))
        if len(debs) != 3:
            sys.exit(f"expected three packages, built {len(debs)}")

        result = install(debs)
        if result.returncode != 0:
            sys.exit(f"first install failed:\n{result.stderr}")

        unmodified = Path("/") / UNMODIFIED
        locally_modified = Path("/") / LOCALLY_MODIFIED
        if not unmodified.exists():
            sys.exit(f"{UNMODIFIED} was not installed")
        if not locally_modified.exists():
            sys.exit(f"{LOCALLY_MODIFIED} was not installed")
        print(f"ok: first install placed {UNMODIFIED} and {LOCALLY_MODIFIED}")

        # Simulate a user hand-editing one installed conffile before the next
        # version ships. This is the exact scenario a conffiles declaration
        # exists to protect: without it, dpkg has no way to know this file was
        # ever touched, and the next install overwrites it like any other.
        edited_content = locally_modified.read_text() + "\n# edited locally by the user\n"
        locally_modified.write_text(edited_content)

        # Change the shipped content of both files so the second build
        # genuinely differs from the first in each. An upgrade whose files are
        # byte-identical would never need to make a conffile decision at all,
        # and would prove nothing about either scenario.
        unmodified_source = REPO / "portlin/resources/runtime/theme/terminalrc"
        modified_source = REPO / "portlin/resources/runtime/theme/gtk-3.0-settings.ini"
        original_unmodified_source = unmodified_source.read_text()
        original_modified_source = modified_source.read_text()
        unmodified_source.write_text(original_unmodified_source + "\n# upgrade probe\n")
        modified_source.write_text(original_modified_source + "\n# upgrade probe\n")
        try:
            build(second, "0.1.1~test")
        finally:
            unmodified_source.write_text(original_unmodified_source)
            modified_source.write_text(original_modified_source)

        result = install(sorted(second.glob("*.deb")))
        if result.returncode != 0:
            sys.exit(f"upgrade failed:\n{result.stderr}")

        # dpkg prints an informational "Configuration file ... ==> Keeping old
        # config file as default." banner even when --force-confdef resolves
        # a modified conffile without asking anyone anything, so matching on
        # "Configuration file" would flag exactly the outcome this harness
        # wants. The one line unique to dpkg genuinely stopping to wait for an
        # answer is the question itself.
        output = result.stdout + result.stderr
        for phrase in ("What would you like to do about it", "(Y/I/N/O/D/Z)"):
            if phrase in output:
                sys.exit(f"the upgrade stopped to ask about a conffile:\n{output}")
        print("ok: the upgrade did not stop to ask about either conffile")

        if "# upgrade probe" not in unmodified.read_text():
            sys.exit(f"{UNMODIFIED} was not updated by the upgrade")
        print(f"ok: the unmodified conffile ({UNMODIFIED}) was replaced")

        if locally_modified.read_text() != edited_content:
            sys.exit(
                f"{LOCALLY_MODIFIED} was overwritten; a conffile the user has "
                "edited must survive an upgrade untouched"
            )
        print(f"ok: the locally modified conffile ({LOCALLY_MODIFIED}) was preserved")

    print("\npackage upgrade harness passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
