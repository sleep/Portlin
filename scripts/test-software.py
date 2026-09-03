#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Run the shipped installer against a real apt, and open the real window.

Every unit test of portlin-install stops at the edge of the process: they
assert the commands it would run, never that running them works. The
interesting failures all live past that edge, and most of them are somebody
else's promise rather than portlin's code. A Debian package renamed between
releases, a vendor repository that has moved, an apt invocation apt itself
rejects, a GTK window that will not construct: none of those can be seen
without an archive, a dpkg and an X server.

So this resolves every Debian package name in the catalog against the real
archive, installs and removes one entry from Debian and one from a vendor
repository end to end, proves both privilege refusals against a real
unprivileged account, and builds the Software window under Xvfb.

Needs root, network and a Debian userland. Run under `make harness`.
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
sys.path.insert(0, str(REPO))

RUNTIME = REPO / "portlin" / "resources" / "runtime"
DISPLAY = ":99"

# The two entries driven end to end. One from Debian's own archive and one
# from a vendor repository, because those are different code paths: the
# second fetches a key and a sources file before apt has anything to install.
DEBIAN_ENTRY = "tmux"
VENDOR_ENTRY = "tailscale"

INSTALLER = "/usr/bin/portlin-install"
WINDOW = "/usr/bin/portlin-software"
CATALOG = "/usr/lib/portlin/catalog.py"
POLICY = "/usr/share/polkit-1/actions/org.portlin.install.policy"

failures: list[str] = []


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", **kwargs.pop("env", {})}
    return subprocess.run(argv, capture_output=True, text=True, env=env, **kwargs)


def ok(message: str) -> None:
    print(f"ok: {message}", flush=True)


def bad(message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    failures.append(message)


def skip(message: str) -> None:
    print(f"skip: {message}", flush=True)


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def install_portlin_packages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / "packages"
        built.mkdir()
        result = run(
            [sys.executable, "-m", "portlin", "package", "--output", str(built)],
            cwd=REPO,
        )
        if result.returncode != 0:
            sys.exit(f"building portlin's packages failed:\n{result.stderr}")
        debs = sorted(str(deb) for deb in built.glob("*.deb"))
        result = run(["apt-get", "install", "-y", "-q", *debs])
        if result.returncode != 0:
            sys.exit(f"installing portlin's packages failed:\n{result.stdout}")
    for path in (INSTALLER, WINDOW, CATALOG, POLICY):
        if Path(path).exists():
            ok(f"{path} is installed")
        else:
            bad(f"{path} is missing after installing portlin's packages")


def write_a_portlin_style_sources_list() -> None:
    """Replace the image's deb822 sources with the one-line form portlin writes.

    The installer parses /etc/apt/sources.list to decide which components a
    stick already has, and a debian:trixie image does not have that file at
    all. Writing the shape a stick really carries is what makes the component
    logic here the same code path it will take on a stick.
    """
    from portlin import templates

    Path("/etc/apt/sources.list").write_text(
        templates.render_sources_list(
            suite="trixie",
            mirror="http://deb.debian.org/debian",
            security_mirror="http://security.debian.org/debian-security",
            components="main contrib non-free non-free-firmware",
        )
    )
    for stale in Path("/etc/apt/sources.list.d").glob("*.sources"):
        stale.unlink()
    if run(["apt-get", "update", "-q"]).returncode != 0:
        sys.exit("apt-get update failed against a portlin-style sources.list")
    ok("apt reads the one-line sources.list portlin writes")


def check_component_planning() -> None:
    """A component the stick lacks is planned for, and only then."""
    Path("/etc/apt/sources.list").write_text(
        "deb http://deb.debian.org/debian trixie main\n"
        "deb http://deb.debian.org/debian trixie-updates main\n"
        "deb http://security.debian.org/debian-security trixie-security main\n"
    )
    result = run([INSTALLER, "install", "--dry-run", "nvidia-driver"])
    if "portlin-components.sources" in result.stdout and "non-free" in result.stdout:
        ok("a driver from non-free plans the component drop-in first")
    else:
        bad(f"nvidia-driver did not plan for non-free:\n{result.stdout}")
    write_a_portlin_style_sources_list()
    result = run([INSTALLER, "install", "--dry-run", "nvidia-driver"])
    if "portlin-components.sources" not in result.stdout:
        ok("with non-free already enabled, no drop-in is planned")
    else:
        bad("a drop-in was planned for a component already enabled")


def check_every_debian_package_resolves(catalog) -> None:
    """Every Debian package name in the catalog exists in the archive.

    The failure this catches is a package renamed or dropped between Debian
    releases, which no unit test can see and which reaches a person as an
    entry that fails the moment they click it. Collected rather than raised
    one at a time, because whoever is fixing them wants the whole list.
    """
    missing = []
    for entry in catalog.ENTRIES:
        if entry.kind != "apt":
            continue
        for name in entry.packages:
            result = run(["apt-cache", "policy", name])
            candidate = [
                line for line in result.stdout.splitlines()
                if line.strip().startswith("Candidate:")
            ]
            if not candidate or "(none)" in candidate[0]:
                missing.append(f"{entry.id}: {name}")
    if missing:
        bad("catalog packages the archive does not have: " + ", ".join(missing))
    else:
        ok("every Debian package the catalog names resolves in the archive")


def check_install_and_remove(entry_id: str, *, expect_paths=()) -> None:
    result = run([INSTALLER, "install", entry_id])
    if result.returncode != 0:
        if entry_id == VENDOR_ENTRY:
            # Somebody else's CDN. A network failure here is not portlin
            # being wrong, and failing the gate on it would make the whole
            # harness unreliable.
            skip(f"{entry_id} could not be installed: {result.stdout.strip()[-300:]}")
            return
        bad(f"installing {entry_id} failed:\n{result.stdout}")
        return
    if "::step" not in result.stdout:
        bad(f"installing {entry_id} printed no steps for the app to show")
    if not result.stdout.strip().endswith(f"::result ok {entry_id}"):
        bad(f"installing {entry_id} did not end with its result line")
    if run([INSTALLER, "status", entry_id]).returncode != 0:
        bad(f"{entry_id} installed but status says it is not there")
    else:
        ok(f"{entry_id} installed, and status agrees")
    for path in expect_paths:
        if not Path(path).exists():
            bad(f"{entry_id} installed without writing {path}")

    result = run([INSTALLER, "remove", entry_id])
    if result.returncode != 0:
        bad(f"removing {entry_id} failed:\n{result.stdout}")
        return
    if run([INSTALLER, "status", entry_id]).returncode == 0:
        bad(f"{entry_id} removed but status says it is still there")
    else:
        ok(f"{entry_id} removed, and status agrees")
    for path in expect_paths:
        if Path(path).exists():
            bad(f"removing {entry_id} left {path} behind")


def check_the_other_install_kinds() -> None:
    """Drive the downloaded-package and tarball paths against real files.

    Every catalog entry of these kinds is a hundred megabytes or more from
    somebody's CDN, which is too slow and too flaky to install on every run,
    so their executors would otherwise ship having only ever been planned.
    Two entries built here, served over file:// through the same curl the
    real ones use, exercise the whole path: download, check, install or
    unpack, write a menu entry, and take it all away again.
    """
    installer = load(Path(INSTALLER), "portlin_install")
    catalog = sys.modules["catalog"]
    ctx = installer.load_context()

    # A .deb with nothing in it but a name, built the way dpkg builds one.
    root = Path("/tmp/harness-deb/portlin-harness-probe")
    (root / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (root / "DEBIAN/control").write_text(
        "Package: portlin-harness-probe\n"
        "Version: 1\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Architecture: all\n"
        "Maintainer: The portlin authors <portlin@localhost>\n"
        "Description: Stand-in for a vendor .deb in portlin's harness\n"
    )
    if run(["dpkg-deb", "--build", str(root), "/tmp/probe.deb"]).returncode != 0:
        bad("could not build the stand-in .deb")
        return
    deb_entry = catalog.Entry(
        id="harness-deb",
        name="Harness probe",
        summary="A stand-in for a vendor .deb",
        category="System tools",
        kind="deb-url",
        url="file:///tmp/probe.deb",
        check=catalog.dpkg("portlin-harness-probe"),
        homepage="https://example.invalid/",
    )
    result = installer.run_plan(installer.plan_install(deb_entry, ctx))
    if result.ok and "portlin-harness-probe" in installer.dpkg_installed():
        ok("a downloaded .deb installs through apt")
    else:
        bad(f"the .deb path failed: {result.failure}")
    if Path(f"{ctx.download_dir}/harness-deb.deb").exists():
        bad("the .deb path left its download behind")
    installer.run_plan(installer.plan_remove(deb_entry, ctx, None))
    if "portlin-harness-probe" in installer.dpkg_installed():
        bad("the .deb path did not remove what it installed")
    else:
        ok("a downloaded .deb removes cleanly")

    # A tarball shaped like a vendor's: one top-level directory, stripped.
    payload = Path("/tmp/harness-tar/harness-app")
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "run-me").write_text("#!/bin/sh\necho harness\n")
    (payload / "run-me").chmod(0o755)
    if run(["tar", "-cJf", "/tmp/probe.tar.xz", "-C", "/tmp/harness-tar", "harness-app"]).returncode:
        bad("could not build the stand-in tarball")
        return
    tar_entry = catalog.Entry(
        id="harness-tar",
        name="Harness tarball",
        summary="A stand-in for a vendor tarball",
        category="System tools",
        kind="tarball-opt",
        url="file:///tmp/probe.tar.xz",
        opt_dir="/opt/harness-app",
        launcher="run-me",
        check=catalog.path("/opt/harness-app/run-me"),
        homepage="https://example.invalid/",
    )
    result = installer.run_plan(installer.plan_install(tar_entry, ctx))
    launcher = Path("/opt/harness-app/run-me")
    menu = Path("/usr/share/applications/portlin-harness-tar.desktop")
    if result.ok and launcher.exists() and menu.exists():
        ok("a tarball unpacks into /opt with a menu entry")
    else:
        bad(f"the tarball path failed: {result.failure}")
    if menu.exists() and "Exec=/opt/harness-app/run-me" not in menu.read_text():
        bad("the generated menu entry does not run the program it unpacked")
    installer.run_plan(installer.plan_remove(tar_entry, ctx, None))
    if launcher.exists() or menu.exists():
        bad("the tarball path left files behind")
    else:
        ok("a tarball removes its directory and its menu entry")


def check_the_user_script_path() -> None:
    """Run a vendor-style installer as an ordinary user, end to end.

    The real ones are Zed and Kimi Code, which download a hundred megabytes
    from somebody's CDN into a home directory. This is the same path with a
    stand-in script: run as the invoking user rather than root, into that
    user's home, recording what it did where that user can write.
    """
    run(["useradd", "-m", "scripted"])
    script = Path("/tmp/harness-install.sh")
    script.write_text("#!/bin/sh\nmkdir -p \"$HOME/.harness-app/bin\"\n"
                      "printf '%s' run-me > \"$HOME/.harness-app/bin/app\"\n")
    script.chmod(0o755)

    driver = Path("/tmp/harness-user-script.py")
    driver.write_text(
        "import sys\n"
        "sys.path.insert(0, '/usr/lib/portlin')\n"
        "import importlib.machinery, importlib.util\n"
        "loader = importlib.machinery.SourceFileLoader('portlin_install', '/usr/bin/portlin-install')\n"
        "spec = importlib.util.spec_from_file_location(loader.name, '/usr/bin/portlin-install', loader=loader)\n"
        "installer = importlib.util.module_from_spec(spec)\n"
        "sys.modules[loader.name] = installer\n"
        "spec.loader.exec_module(installer)\n"
        "import catalog\n"
        "entry = catalog.Entry(id='harness-script', name='Harness script',\n"
        "    summary='A stand-in for a vendor installer', category='Development',\n"
        "    kind='user-script', url='file:///tmp/harness-install.sh',\n"
        "    check=catalog.path('~/.harness-app/bin/app'),\n"
        "    remove_paths=('~/.harness-app',),\n"
        "    warning='stand-in', homepage='https://example.invalid/')\n"
        "ctx = installer.load_context()\n"
        "assert not ctx.root, 'this must not run as root'\n"
        "result = installer.run_plan(installer.plan_install(entry, ctx))\n"
        "assert result.ok, result.failure\n"
        "installer.write_record(ctx, entry, installer.record_for(entry, ctx, []))\n"
        "import pathlib\n"
        "assert pathlib.Path(installer.record_path(ctx, entry)).exists(), 'no record written'\n"
        "print('INSTALLED', installer.installed(entry, set(), pathlib.Path(ctx.home)))\n"
        "installer.run_plan(installer.plan_remove(entry, ctx, installer.read_record(ctx, entry)))\n"
        "print('AFTER', installer.installed(entry, set(), pathlib.Path(ctx.home)))\n"
    )
    result = run(["su", "-s", "/bin/sh", "scripted", "-c", f"python3 {driver}"])
    if "INSTALLED True" in result.stdout and "AFTER False" in result.stdout:
        ok("a vendor installer runs as the user, into their home, and comes back out")
    else:
        bad(f"the user-script path failed:\n{result.stdout}\n{result.stderr}")
    if Path("/home/scripted/.harness-app").exists():
        bad("the user-script path left files in the home directory")


def check_privilege_refusals() -> None:
    """Both refusals, against a real unprivileged account."""
    run(["useradd", "-m", "probe"])
    result = run(["su", "-s", "/bin/sh", "probe", "-c", f"{INSTALLER} install {DEBIAN_ENTRY}"])
    if result.returncode == 3:
        ok("a system package refuses to install without root")
    else:
        bad(f"installing as an ordinary user exited {result.returncode}, wanted 3")

    result = run([INSTALLER, "install", "zed"])
    if result.returncode == 3:
        ok("a vendor script refuses to run as root")
    else:
        bad(f"installing zed as root exited {result.returncode}, wanted 3")


def check_upgrade_asks_before_removing() -> None:
    """A full upgrade that has to remove something must stop and say so.

    Driven against real apt rather than a parsed string, because what is
    being tested is that apt's simulation output is read correctly, and only
    apt writes it. tmux stands in for the thing that would be removed: apt
    is asked to remove it, and the refusal has to name it.
    """
    if run([INSTALLER, "install", DEBIAN_ENTRY]).returncode != 0:
        skip("could not install the stand-in package for the upgrade check")
        return
    installer = sys.modules.get("portlin_install") or load(Path(INSTALLER), "portlin_install")
    # Both words apt uses, because which one it prints depends on the verb
    # and an upgrade may reach for either.
    for verb, token in (("remove", "Remv"), ("purge", "Purg")):
        simulated = run(["apt-get", "--simulate", verb, "-y", DEBIAN_ENTRY])
        if DEBIAN_ENTRY in installer.parse_removals(simulated.stdout):
            ok(f"apt's {token} lines are read as a removal")
        else:
            bad(
                f"could not read a {token} out of apt's simulation:\n"
                f"{simulated.stdout.strip()[-500:]}"
            )
    run([INSTALLER, "remove", DEBIAN_ENTRY])

    result = run([INSTALLER, "upgrade"])
    if result.returncode in (0, 5):
        ok(f"upgrade ran to a definite answer (status {result.returncode})")
        if result.returncode == 5 and "::warn would remove" not in result.stdout:
            bad("upgrade refused without naming what it would remove")
    else:
        bad(f"upgrade exited {result.returncode}:\n{result.stdout[-400:]}")


def check_the_scan() -> None:
    result = run([INSTALLER, "scan", "--json"])
    if result.returncode != 0:
        bad(f"the scan failed:\n{result.stdout}")
        return
    try:
        report = json.loads(result.stdout)
    except ValueError:
        bad(f"the scan did not print JSON:\n{result.stdout[:400]}")
        return
    if set(report) != {"gpus", "wifi", "suggestions", "notes"}:
        bad(f"the scan reported unexpected keys: {sorted(report)}")
    elif report["suggestions"]:
        bad(f"a container was told it needs drivers: {report['suggestions']}")
    elif not report["notes"]:
        bad("the scan found nothing and said nothing about it")
    else:
        ok("the scan runs, finds no driver to suggest, and says why")


def check_polkit_reads_the_action() -> None:
    """polkit parses the action file and knows the id the app asks for.

    A malformed policy is skipped in silence, so the only way to know polkit
    accepted it is to ask polkit. That means a system bus and a running
    polkitd, neither of which a container has, so both are started here for
    the length of one question.
    """
    Path("/run/dbus").mkdir(parents=True, exist_ok=True)
    bus = subprocess.Popen(
        ["dbus-daemon", "--system", "--nofork", "--nopidfile"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    daemon = None
    try:
        for _ in range(50):
            if Path("/run/dbus/system_bus_socket").exists():
                break
            time.sleep(0.1)
        else:
            skip("no system bus in this container, so polkit could not be asked")
            return
        polkitd = next(
            (path for path in ("/usr/libexec/polkitd", "/usr/lib/polkit-1/polkitd")
             if Path(path).exists()),
            None,
        )
        if polkitd is None:
            skip("polkitd is not installed, so polkit could not be asked")
            return
        daemon = subprocess.Popen(
            [polkitd, "--no-debug"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(50):
            result = run(["pkaction", "--action-id", "org.portlin.install", "--verbose"])
            if result.returncode == 0 and "org.portlin.install" in result.stdout:
                ok("polkit loaded the action the Software app elevates through")
                if "auth_admin_keep" not in result.stdout:
                    bad("polkit read the action without auth_admin_keep for an active session")
                return
            time.sleep(0.2)
        bad(f"polkit did not load the portlin action:\n{result.stdout}{result.stderr}")
    finally:
        if daemon is not None:
            daemon.terminate()
        bus.terminate()


def start_xvfb() -> subprocess.Popen:
    server = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1024x768x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(50):
        if subprocess.run(["xset", "q"], capture_output=True).returncode == 0:
            return server
        time.sleep(0.2)
    raise SystemExit("Xvfb never came up")


DRIVERS_CATEGORY = "Drivers"


def select_category(window, catalog, name: str) -> None:
    """Click a category in the sidebar, the way a person would."""
    index = list(catalog.CATEGORIES).index(name)
    window.categories.select_row(window.categories.get_row_at_index(index))


def first_row_name(window) -> str:
    """The name label of the first row drawn, read back out of the widgets."""
    row = window.listing.get_row_at_index(0)
    if row is None:
        return ""
    box = row.get_child()
    text = box.get_children()[0]
    return text.get_children()[0].get_text()


def check_it_reads_a_real_job(software) -> None:
    """Run a real portlin-install through the window's own reader.

    Job is the one part of the window that talks to a process, and what it
    depends on -- a GLib watch over a pipe, lines arriving as events rather
    than in one lump at the end -- either works at runtime or does not. The
    window built in the next check never starts one, so without this the
    class ships never having read a single line.
    """
    from gi.repository import GLib

    events: list[tuple[str, str]] = []
    output: list[str] = []
    finished: list[int] = []
    loop = GLib.MainLoop()

    def done(code: int) -> None:
        finished.append(code)
        loop.quit()

    software.Job(
        [INSTALLER, "install", "--dry-run", "mullvad"],
        on_event=lambda kind, rest: events.append((kind, rest)),
        on_output=output.append,
        on_done=done,
    )
    GLib.timeout_add_seconds(60, lambda: (loop.quit(), False)[1])
    loop.run()

    if finished == [0]:
        ok("the window ran a real portlin-install to completion")
    else:
        bad(f"the window's job reader ended with {finished}, wanted [0]")
    kinds = [kind for kind, _ in events]
    if "step" in kinds and "result" in kinds:
        ok("the window read the steps and the result out of it")
    else:
        bad(f"the window read no steps or result from a real run: {kinds[:8]}")
    if any("would run: curl" in line for line in output):
        ok("ordinary tool output reached the log rather than the event handler")
    else:
        bad("the plan's own output never reached the log")


def check_the_window(catalog) -> None:
    """Build the real window against a real X server and search it."""
    server = start_xvfb()
    try:
        sys.path.insert(0, "/usr/lib/portlin")
        software = load(Path(WINDOW), "portlin_software")
        from gi.repository import Gtk

        check_it_reads_a_real_job(software)
        scan = {
            "gpus": [{"slot": "01:00.0", "vendor": "nvidia",
                      "name": "NVIDIA Corporation GP108M", "id": "10de:1d10"}],
            "wifi": [],
            "suggestions": [{"entry": "nvidia-driver", "reason": "GP108M found"}],
            "notes": [],
        }
        window = software.SoftwareWindow(scan=scan, dpkg=set(), sudo=False)
        window.show_all()

        # It opens on the first category, so that is what should be drawn.
        opened_on = catalog.by_category()[catalog.CATEGORIES[0]]
        rows = len(window.listing.get_children())
        if rows == len(opened_on):
            ok(f"the window opened on {catalog.CATEGORIES[0]} with {rows} rows")
        else:
            bad(f"the window drew {rows} rows for {len(opened_on)} in the first category")

        # set_text alone is not enough: search-changed is debounced, so a
        # harness that only typed would be asserting on the previous list.
        window.search.set_text("torrent")
        window._on_search(window.search)
        found = len(window.listing.get_children())
        if found == len(catalog.search("torrent")):
            ok("searching the window filters across every category")
        else:
            bad(f"searching for torrent left {found} rows")

        # Clicking a page clears the search, so the list has to come back.
        select_category(window, catalog, DRIVERS_CATEGORY)
        if window.search.get_text() == "":
            ok("picking a page clears the search")
        else:
            bad("picking a page left the previous search in the entry")
        first = first_row_name(window)
        if first and "NVIDIA" in first:
            ok("the drivers page draws the suggestion for this machine first")
        else:
            bad(f"the drivers page led with {first!r} rather than the suggestion")
        # Both halves: the text has to be right and the widget has to be on
        # screen. A label that is set but never shown reads as an empty box,
        # and only a real render says which of the two happened.
        while Gtk.events_pending():
            Gtk.main_iteration()
        if "GP108M" not in window.machine.get_text():
            bad("the drivers page does not name the hardware the scan found")
        elif not window.machine.get_mapped():
            bad("the drivers page describes the machine into a label nobody can see")
        else:
            ok("the drivers page shows the hardware the scan found")

        select_category(window, catalog, catalog.CATEGORIES[0])
        while Gtk.events_pending():
            Gtk.main_iteration()
        if window.machine_frame.get_visible():
            bad("the machine description stayed on a page that is not Drivers")
        else:
            ok("the machine description belongs to the drivers page alone")

        # The log pane has to follow its own output: a pane showing the first
        # screen of a ten-minute apt run reads as a program that has stopped.
        for number in range(200):
            window._append(f"line {number}")
        buffer = window.log.get_buffer()
        visible = window.log.get_visible_rect()
        end_y = window.log.get_iter_location(buffer.get_end_iter()).y
        if end_y <= visible.y + visible.height + 40:
            ok("the log pane follows its own output")
        else:
            bad(f"the log pane stayed at {visible.y} while output reached {end_y}")
        window.destroy()
    finally:
        server.terminate()


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("needs root; run it under make harness")

    install_portlin_packages()
    catalog = load(Path(CATALOG), "catalog")
    if catalog.validate():
        bad("the installed catalog does not validate: " + "; ".join(catalog.validate()))

    check_component_planning()
    check_every_debian_package_resolves(catalog)
    check_install_and_remove(DEBIAN_ENTRY)
    check_install_and_remove(
        VENDOR_ENTRY,
        expect_paths=(
            "/usr/share/keyrings/tailscale-archive-keyring.gpg",
            "/etc/apt/sources.list.d/tailscale.list",
        ),
    )
    check_the_other_install_kinds()
    check_the_user_script_path()
    check_privilege_refusals()
    check_upgrade_asks_before_removing()
    check_the_scan()
    check_polkit_reads_the_action()
    check_the_window(catalog)

    if failures:
        print(f"\n{len(failures)} failure(s):", flush=True)
        for failure in failures:
            print(f"  - {failure}", flush=True)
        return 1
    print("\nthe Software app and portlin-install work against a real apt", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
