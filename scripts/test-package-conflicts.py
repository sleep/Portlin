#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Prove portlin's packages install where something else already owns /etc/xdg.

dpkg lets exactly one installed package own a path, and declaring that path a
conffile buys no exemption. The consequence is not hypothetical: on a rootfs
with a desktop, xfce4-settings owns
/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml, so a portlin-desktop
that claimed the same path was refused at unpack, which aborts the entire apt
transaction installing all three packages and leaves the stick unwritten.
--force-confnew does not help, because it governs conffile prompts rather than
ownership.

Portlin's answer is to ship its defaults under a directory it owns and put that
directory on XDG_CONFIG_DIRS. The one path it does claim from another package,
the backdrop xfdesktop falls back to, it takes by diversion instead, and this
harness is the only place where those maintainer scripts ever run. It holds
both answers in place. It builds
a stand-in package claiming the canonical /etc/xdg location of every default
portlin-desktop ships, installs it first, and then requires portlin's own
install to succeed anyway.

The claims are derived from package.XDG_DEFAULTS rather than from a list of
Debian package names, so this keeps its meaning if Xfce reorganises which of
its packages owns what, and starts failing the moment any portlin default moves
back out of the overlay. No unit test can see any of it: the rule being tested
is enforced by dpkg against a real installed package.

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

from portlin import package as pkg  # noqa: E402  (needs REPO on the path first)

PROBE = "portlin-conflict-probe"


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    return subprocess.run(argv, capture_output=True, text=True, env=env, **kwargs)


def build_portlin_packages(output: Path) -> list[Path]:
    result = run(
        [sys.executable, "-m", "portlin", "package", "--output", str(output)],
        cwd=REPO,
    )
    if result.returncode != 0:
        sys.exit(f"building portlin's packages failed:\n{result.stderr}")
    return sorted(output.glob("*.deb"))


def build_probe(root: Path) -> Path:
    """Build a package owning every path portlin has to work around.

    Two different dpkg rules, one stand-in: the canonical /etc/xdg location of
    every default, which portlin must not claim, and the backdrop portlin does
    claim by diverting it away from xfdesktop4-data.
    """
    debian = root / "DEBIAN"
    debian.mkdir(parents=True)
    (debian / "control").write_text(
        "\n".join(
            [
                f"Package: {PROBE}",
                "Version: 1",
                "Section: utils",
                "Priority: optional",
                "Architecture: all",
                "Maintainer: The portlin authors <portlin@localhost>",
                "Description: Stand-in for the Xfce packages that own /etc/xdg",
                " Built by portlin's test harness. Claims the canonical /etc/xdg",
                " locations so dpkg has a real second owner to refuse.",
                "",
            ]
        )
    )
    for relative in pkg.XDG_DEFAULTS:
        claimed = root / "etc/xdg" / relative
        claimed.parent.mkdir(parents=True, exist_ok=True)
        claimed.write_text("# claimed by the conflict probe\n")

    # Stands in for xfdesktop4-data. A diversion only does anything when some
    # other package genuinely owns the path, so without this the maintainer
    # scripts would run against nothing and prove nothing.
    backdrop = root / pkg.DEFAULT_BACKDROP
    backdrop.parent.mkdir(parents=True, exist_ok=True)
    backdrop.write_text("<svg><!-- claimed by the conflict probe --></svg>\n")

    deb = root.parent / f"{PROBE}.deb"
    result = run(["dpkg-deb", "--build", str(root), str(deb)])
    if result.returncode != 0:
        sys.exit(f"building the probe failed:\n{result.stderr}")
    return deb


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("needs root; run it under make harness")

    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / "packages"
        built.mkdir()
        debs = build_portlin_packages(built)
        if len(debs) != 3:
            sys.exit(f"expected three packages, built {len(debs)}")

        probe = build_probe(Path(tmp) / PROBE)
        result = run(["dpkg", "-i", str(probe)])
        if result.returncode != 0:
            sys.exit(f"installing the probe failed:\n{result.stderr}")

        # A probe that owns nothing would let this harness pass while proving
        # nothing at all, which is the one failure mode it cannot report on
        # its own. Confirm dpkg really did record it as the owner.
        contested = f"/etc/xdg/{next(iter(pkg.XDG_DEFAULTS))}"
        owner = run(["dpkg", "-S", contested])
        if PROBE not in owner.stdout:
            sys.exit(f"the probe does not own {contested}; this harness proves nothing")
        print(f"ok: {PROBE} owns {contested} and {len(pkg.XDG_DEFAULTS) - 1} more")

        result = run(["apt-get", "install", "-y", *[str(d) for d in debs]])
        if result.returncode != 0:
            sys.exit(
                "installing portlin's packages failed while another package "
                f"owned the canonical /etc/xdg paths:\n{result.stdout}\n{result.stderr}"
            )
        print("ok: all three packages installed alongside the contested paths")

        # dpkg reports the conflict at unpack and apt still exits non-zero, so
        # the check above is the real gate. This one names the specific message
        # so a future regression reads as itself rather than as a bare failure.
        output = result.stdout + result.stderr
        if "trying to overwrite" in output:
            sys.exit(f"dpkg refused a path portlin claimed:\n{output}")

        # Nothing else in the tree runs portlin-desktop's maintainer scripts.
        # A unit test can read them; only dpkg can show that the triple they
        # name -- package, divert-to path, original path -- is the one dpkg
        # recorded. A mismatched pair is silent, because dpkg-divert finds
        # nothing to undo and exits 0, so the removal below is the assertion
        # that actually matters.
        served = Path("/") / pkg.DEFAULT_BACKDROP
        if not served.read_bytes().startswith(b"\x89PNG"):
            sys.exit(f"{served} is not portlin's render; the diversion did not take")
        if not Path(f"{served}.distrib").exists():
            sys.exit(f"{served}.distrib is missing; the probe's file was not displaced")
        print(f"ok: portlin's render serves {served}, the probe's copy moved aside")

        result = run(["dpkg", "-r", "portlin-desktop"])
        if result.returncode != 0:
            sys.exit(f"removing portlin-desktop failed:\n{result.stderr}")
        if "conflict probe" not in served.read_text(errors="replace"):
            sys.exit(
                "removing portlin-desktop did not hand the backdrop back to its "
                "owner; the preinst and the postrm name different diversions"
            )
        print("ok: removing portlin-desktop restored the probe's backdrop")

        run(["dpkg", "-r", PROBE])

    print("\npackage conflict harness passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
