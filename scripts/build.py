#!/usr/bin/env python3
#
# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Build a portlin image, with a display that says how far along it is.

A build is twenty minutes on a fast native host and well over an hour emulated,
and portlin's Runner captures command output rather than streaming it, so
without this the whole thing is silent. Silence is indistinguishable from a
hang, which is how a working build gets killed at minute fifty.

This drives the real pipeline - build_rootfs, then write_stick, then
verify-image.sh - by importing portlin rather than parsing its logs, and attaches
a progress hook to the Runner. Every percentage shown comes from the tools
themselves: apt's status stream, debootstrap's package lines, tar's checkpoints.

On anything that is not x86_64 Linux with root, it re-runs itself inside a
privileged linux/amd64 container and shows the same display from in there.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Absolute, and derived from this file rather than the working directory. A
# build that only works when launched from the right folder is a build that
# fails in a container for reasons that look nothing like the cause.
sys.path.insert(0, str(REPO))

from portlin import __version__, progress  # noqa: E402
from portlin.config import BuildConfig, WriteConfig  # noqa: E402
from portlin.errors import PortlinError  # noqa: E402
from portlin.install import write_stick  # noqa: E402
from portlin.rootfs import build_rootfs  # noqa: E402
from portlin.runner import Runner  # noqa: E402

CONTAINER_IMAGE = "debian:trixie"

# The one package the container needs before this script can run at all, which
# is why it is installed by the shell line that launches us rather than by the
# build: there is no display yet to draw it in.
CONTAINER_BOOTSTRAP = "python3"

# Everything else is installed by the build itself, as its first stage, so apt
# reports into the log pane under the bars instead of scrolling past above the
# banner and pushing the display off the top of the screen.
CONTAINER_TOOLS = [
    "debootstrap", "gdisk", "parted", "dosfstools", "e2fsprogs",
    "cryptsetup-bin", "util-linux", "zstd", "tar", "mount", "ca-certificates",
]

# apt-get, told to describe itself. -q drops the terminal rendering that means
# nothing on a pipe, and Status-Fd replaces it with the machine-readable stream
# the progress parser reads. The chroot's installs are invoked the same way.
APT = ["apt-get", "-y", "-q", "-o", "APT::Status-Fd=1", "-o", "Acquire::Retries=3"]

# Beside the output rather than in a home directory: the container's home is
# thrown away with the container, so a cache kept there would never survive to
# improve the estimate it exists for.
TIMINGS_FILE = ".portlin-timings.json"


# --------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------

class Theme:
    """Colours, or nothing at all.

    Honours NO_COLOR and a non-terminal stdout. A build log full of escape
    sequences is worse than a plain one, and this output gets redirected to
    files often enough to matter.
    """

    INK = (124, 139, 158)      # muted, for structure
    PAPER = (232, 237, 243)    # text
    ACCENT = (255, 51, 85)     # crimson, the root partition's colour
    DIM = (44, 53, 66)         # empty bar cells

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, rgb: tuple[int, int, int]) -> str:
        if not self.enabled:
            return text
        r, g, b = rgb
        return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


# Widths in true rank order, the same shape as the logo mark: three system
# partitions and root, which is the long one and the only one in crimson.
LOGO_BARS = [4, 10, 15, 24]
LOGO_WIDTH = 26


def render_logo(theme: Theme, unicode_ok: bool) -> list[str]:
    fill, tl, tr, bl, br, h, v = (
        ("█", "┌", "┐", "└", "┘", "─", "│") if unicode_ok
        else ("#", "+", "+", "+", "+", "-", "|")
    )
    lines = [theme(tl + h * LOGO_WIDTH + tr, Theme.INK)]
    for index, width in enumerate(LOGO_BARS):
        colour = Theme.ACCENT if index == len(LOGO_BARS) - 1 else Theme.INK
        bar = theme(fill * width, colour)
        padding = " " * (LOGO_WIDTH - width - 2)
        lines.append(theme(v, Theme.INK) + " " + bar + padding + " " + theme(v, Theme.INK))
    lines.append(theme(bl + h * LOGO_WIDTH + br, Theme.INK))
    return lines


class Display:
    """Draws the build. Knows nothing about what a build is."""

    LOG_LINES = 6
    BAR_WIDTH = 26
    MIN_REDRAW_INTERVAL = 0.1

    def __init__(self, timeline: progress.Timeline, header: list[str], stream=None):
        self.timeline = timeline
        self.header = header
        # Resolved now rather than defaulted in the signature: a default
        # argument binds sys.stdout at import time, so anything that replaces
        # the stream afterwards is written straight past.
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.theme = Theme(self.interactive and "NO_COLOR" not in os.environ)
        self.unicode_ok = "UTF-8" in (os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")).upper()
        self.log: list[str] = []
        self.started = time.monotonic()
        self._painted = 0
        self._last_paint = 0.0
        self._last_stage: str | None = None

    def add_log(self, line: str, echo: bool = False) -> None:
        self.log.append(line)
        del self.log[:-self.LOG_LINES]
        # The verify stage's output is the verdict on the whole build, so it is
        # the one thing worth printing even when there is no terminal to draw
        # in. Everything else would just be apt's chatter in a log file.
        if echo and not self.interactive:
            print(line, file=self.stream, flush=True)

    def refresh(self, force: bool = False) -> None:
        if not self.interactive:
            self._report_plainly()
            return
        now = time.monotonic()
        if not force and now - self._last_paint < self.MIN_REDRAW_INTERVAL:
            return
        self._last_paint = now
        self._paint()

    def _report_plainly(self) -> None:
        """One line per stage change, for logs and CI.

        Deliberately not a periodic percentage: a redirected build log with a
        progress line every second is unreadable, and the thing a log needs to
        answer is which stage was running when it stopped.
        """
        if self.timeline.current != self._last_stage:
            self._last_stage = self.timeline.current
            if self.timeline.current:
                elapsed = progress.format_duration(time.monotonic() - self.started)
                print(f"[{elapsed}] {self.timeline.current}", file=self.stream, flush=True)

    def _paint(self) -> None:
        # Re-read every frame: a resize mid-build would otherwise wrap lines,
        # and a wrapped line breaks the cursor arithmetic for every frame after
        # it, turning the display into a scrolling smear.
        width = max(40, shutil.get_terminal_size((80, 24)).columns)
        text_width = width - 6

        lines = list(self.header)
        lines.append("")
        for stage in self.timeline.stages:
            lines.append(self._stage_row(stage))
        lines.append("")
        lines.append(self._total_row())
        lines.append("")
        lines.append("  " + self.theme(self._truncate(self.timeline.detail, text_width), Theme.PAPER))
        rule = ("─" if self.unicode_ok else "-") * min(70, text_width)
        lines.append("  " + self.theme(rule, Theme.DIM))
        for entry in self.log:
            lines.append("  " + self.theme(self._truncate(entry, text_width), Theme.DIM))

        out = []
        if self._painted:
            out.append(f"\x1b[{self._painted}A")
        for line in lines:
            out.append("\x1b[2K" + line + "\n")
        self.stream.write("".join(out))
        self.stream.flush()
        self._painted = len(lines)

    def _stage_row(self, stage: progress.Stage) -> str:
        running = stage.key == self.timeline.current
        done = self.timeline.is_done(stage.key)

        if done:
            fraction, estimated = 1.0, False
        elif running:
            fraction, estimated = self.timeline.displayed_fraction()
        else:
            fraction, estimated = None, False

        bar = progress.render_bar(fraction, self.BAR_WIDTH)
        bar = self.theme(bar, Theme.PAPER if (running or done) else Theme.DIM)

        if fraction is None:
            percent = "    -"
        else:
            percent = f"{'~' if estimated else ' '}{fraction * 100:3.0f}%"

        if done:
            timing = progress.format_duration(self.timeline.durations().get(stage.key, 0))
            trailer = f"{timing:>8}"
        elif running:
            elapsed = self.timeline.elapsed()
            remaining = progress.estimate_remaining(fraction, elapsed)
            trailer = f"{progress.format_duration(elapsed):>8}"
            if remaining is not None:
                trailer += f"   ETA {progress.format_duration(remaining)}"
        else:
            trailer = ""

        label = self.theme(f"{stage.label:<14}", Theme.PAPER if running else Theme.INK)
        return f"  {label} {bar} {percent}  {trailer}"

    def _total_row(self) -> str:
        fraction = self.timeline.overall()
        elapsed = time.monotonic() - self.started
        remaining = progress.estimate_remaining(fraction, elapsed)
        # Padded before colouring. Formatting an already-coloured string counts
        # the escape codes toward the width, which silently shifts the column.
        label = self.theme(f"{'total':<14}", Theme.PAPER)
        bar = self.theme(progress.render_bar(fraction, self.BAR_WIDTH), Theme.PAPER)
        row = f"  {label} {bar} {fraction * 100:4.0f}%"
        row += f"  {progress.format_duration(elapsed):>8}"
        if remaining is not None:
            # Always a tilde: the total is weighted from previous runs, never
            # measured, however precise the stage it is currently inside.
            row += f"   ETA ~{progress.format_duration(remaining)}"
        return row

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        text = text.strip()
        return text if len(text) <= width else text[: width - 1] + "…"

    def finish(self, message: str, ok: bool = True) -> None:
        if self.interactive:
            self.refresh(force=True)
        elif not ok:
            # Without a terminal the log pane was never drawn, so a failure
            # would otherwise end with a verdict and no evidence behind it.
            for line in self.log:
                print("  " + line, file=self.stream, flush=True)
        colour = Theme.PAPER if ok else Theme.ACCENT
        print("\n" + self.theme("  " + message, colour) + "\n", file=self.stream, flush=True)


# --------------------------------------------------------------------------
# the hook
# --------------------------------------------------------------------------

class BuildWatcher:
    """Turns the Runner's output stream into what the display shows.

    One instance watches every command in the build, which is why it identifies
    the stage from the command line: "Unpacking ..." is printed by both
    debootstrap and apt, and means something different in each.
    """

    def __init__(self, display: Display) -> None:
        self.display = display
        self.timeline = display.timeline
        self.retrieved = 0
        self.installed = 0
        self.bytes_seen = 0
        # Set while a stage runs commands that the argv rules would misread.
        self.pinned: str | None = None

    def __call__(self, argv: list[str], stream: str, line: str) -> None:
        # The pin exists for the container's own tool install: those are
        # apt-get calls like the chroot's, and stage_for would call them
        # "packages", lighting up a bar for work that has not started.
        stage = self.pinned or progress.stage_for(argv)
        if stage is not None and stage != self.timeline.current:
            self.timeline.start(stage)
            self.retrieved = 0
            self.installed = 0
            self.bytes_seen = 0

        current = self.timeline.current
        if current in ("packages", "tools"):
            self._apt(argv, line)
        elif current == "debootstrap":
            self._debootstrap(line)
        elif current in ("tarball", "unpack"):
            self._tar(line)
        else:
            self.display.add_log(line, echo=current == "verify")

        self.display.refresh()

    def _apt(self, argv: list[str], line: str) -> None:
        status = progress.parse_apt_status(line)
        if status is None:
            self.display.add_log(line)
            return
        # Only the install run drives the bar. apt update and dist-upgrade run
        # first and each reports its own 0-100%, so accepting all three would
        # send the bar backwards twice before the real work started.
        if "install" in argv:
            self.timeline.update(fraction=status.fraction, detail=status.detail)
        else:
            self.timeline.update(detail=status.detail)

    def _debootstrap(self, line: str) -> None:
        step = progress.parse_debootstrap(line)
        if step is None:
            self.display.add_log(line)
            return
        verb, subject = step
        if verb in ("Retrieving", "Validating"):
            # No denominator exists yet: debootstrap does not say how many
            # packages the base system has until it has fetched them all.
            self.retrieved += 1 if verb == "Retrieving" else 0
            self.timeline.update(detail=f"{verb} {subject} ({self.retrieved})")
            return
        self.installed += 1
        total = max(self.retrieved, 1) * 3  # extract, unpack, configure
        self.timeline.update(
            fraction=min(1.0, self.installed / total),
            detail=f"{verb} {subject}",
        )

    def _tar(self, line: str) -> None:
        records = progress.parse_tar_checkpoint(line)
        if records is None:
            self.display.add_log(line)
            return
        # No total to divide by: the compressed size is not known until it is
        # written. The bar falls back to elapsed against last run's duration,
        # and this line shows the real work done meanwhile.
        self.bytes_seen = progress.checkpoint_bytes(records)
        self.timeline.update(detail=f"{self.bytes_seen / 1e9:.2f} GB")


# --------------------------------------------------------------------------
# host detection and the container
# --------------------------------------------------------------------------

def can_build_here() -> bool:
    return (
        sys.platform.startswith("linux")
        and platform.machine() in ("x86_64", "amd64")
        and os.geteuid() == 0
    )


def describe_host() -> str:
    return f"{sys.platform}/{platform.machine()}"


def run_in_container(args: argparse.Namespace) -> int:
    """Re-run this script inside a privileged linux/amd64 container."""
    if shutil.which("docker") is None:
        print(
            "This host cannot build directly (needs x86_64 Linux and root), and\n"
            "docker was not found to fall back on.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Silenced deliberately. This is the only part of a build that cannot be
    # drawn, because the thing that draws it is what is being installed, and
    # dpkg's "Setting up ..." lines would otherwise fill the screen before the
    # display appears. stderr is left alone, so a real apt failure still says so.
    inner = "; ".join(
        [
            "set -e",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update -qq >/dev/null",
            f"apt-get install -y -qq --no-install-recommends {CONTAINER_BOOTSTRAP} >/dev/null",
            " ".join(
                ["python3", "-u", "/src/scripts/build.py", "--in-container"]
                + _forwarded_flags(args)
            ),
        ]
    )

    command = [
        "docker", "run", "--rm", "--name", "portlin-build",
        "--privileged", "--platform", "linux/amd64",
        "-v", f"{REPO}:/src",
        "-v", f"{out_dir}:/out",
        "-e", "LANG=C.UTF-8",
        "-w", "/src",
    ]
    if sys.stdin.isatty() and sys.stdout.isatty():
        command.append("-it")
    command += [CONTAINER_IMAGE, "bash", "-c", inner]

    # flush: stdout is block-buffered when redirected to a log, and a line
    # that only appears when the build ends is no use to whoever is waiting.
    print(
        f"building in a {CONTAINER_IMAGE} linux/amd64 container "
        f"({describe_host()} cannot build directly)\n"
        f"fetching the image and installing {CONTAINER_BOOTSTRAP}, "
        "then the build display takes over",
        flush=True,
    )
    return subprocess.run(command).returncode


def _forwarded_flags(args: argparse.Namespace) -> list[str]:
    """The arguments that still mean something on the other side.

    Paths deliberately are not forwarded: inside the container the output
    directory is always /out, whatever it is called out here.
    """
    flags = ["--out-dir", "/out", "--name", args.name, "--suite", args.suite]
    flags += ["--image-size", args.image_size]
    if args.minimal:
        flags.append("--minimal")
    if args.keep:
        flags.append("--keep")
    return flags


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------

def _stages(in_container: bool) -> list[progress.Stage]:
    """The stages this run will actually have.

    A container installs its own build tools, which is a minute natively and
    many minutes emulated; a native root build already has them. The weights are
    renormalised so that the total bar still ends at exactly full either way.
    """
    stages = list(progress.DEFAULT_STAGES)
    if not in_container:
        return stages
    stages.insert(0, progress.Stage("tools", "tools", 0.05))
    total = sum(stage.weight for stage in stages)
    return [progress.Stage(s.key, s.label, s.weight / total) for s in stages]


def _install_build_tools(runner: Runner, watcher: BuildWatcher) -> None:
    """Install the container's build tools, drawn like every other stage.

    Run through the Runner rather than by the shell that launched us purely so
    that it is visible: the same hook, the same bar, the same log pane. The
    stage is pinned because these commands are apt-get, which the argv rules
    read as the packages stage.
    """
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    watcher.pinned = "tools"
    try:
        watcher.timeline.start("tools")
        runner.run(APT + ["update"], env=env)
        runner.run(APT + ["install", "--no-install-recommends", *CONTAINER_TOOLS], env=env)
    finally:
        watcher.pinned = None


def build(args: argparse.Namespace) -> int:
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball = out_dir / f"{args.name}.tar.zst"
    image = out_dir / f"{args.name}.img"
    timings_path = out_dir / TIMINGS_FILE

    timings = progress.load_timings(timings_path)
    timeline = progress.Timeline(_stages(args.in_container), timings=timings)

    theme = Theme(sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    unicode_ok = "UTF-8" in (os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")).upper()
    header = _header(theme, unicode_ok, args, image, timings)

    display = Display(timeline, header)
    watcher = BuildWatcher(display)
    runner = Runner(on_output=watcher)

    started = time.monotonic()
    try:
        # Painted before anything runs. The first command's first line of output
        # can be a minute away, and a blank screen for a minute is the exact
        # thing this display exists to prevent.
        display.refresh(force=True)
        if args.in_container:
            _install_build_tools(runner, watcher)

        build_cfg = BuildConfig(
            output=tarball,
            suite=args.suite,
            groups=["boot", "system", "storage", "network"] if args.minimal else None,
            keep_work_dir=args.keep,
        )
        build_rootfs(build_cfg, runner)

        write_cfg = WriteConfig(
            target=str(image),
            rootfs=tarball,
            image_size=_parse_size(args.image_size),
            assume_yes=True,
        )
        write_stick(write_cfg, runner)

        timeline.start("verify")
        verified = runner.run(
            ["bash", str(REPO / "scripts" / "verify-image.sh"), str(image)], check=False
        )
        timeline.finish()
    except KeyboardInterrupt:
        display.finish("interrupted. Nothing was written to any device.", ok=False)
        return 130
    except PortlinError as exc:
        display.finish(f"failed during {timeline.current or 'setup'}: {exc}", ok=False)
        return 1

    elapsed = time.monotonic() - started
    # Only a complete build produces usable timings; a partial one would teach
    # the next run's estimate that the build is much shorter than it is. Saved
    # even when verification fails, because the durations are still real.
    progress.save_timings(timings_path, timeline.durations())

    if not verified.ok:
        # The image is left in place: it is usually nearly right, and inspecting
        # it is how the failure gets diagnosed. But calling it ready would be a
        # lie, and this is the one line anyone actually reads.
        display.finish(f"{image} was written, but verification FAILED", ok=False)
        return 1

    display.finish(f"{image} ready in {progress.format_duration(elapsed)}")
    return 0


def _header(theme, unicode_ok, args, image, timings) -> list[str]:
    logo = render_logo(theme, unicode_ok)
    dot = " · " if unicode_ok else " - "
    facts = [
        theme(f"portlin {__version__}", Theme.PAPER),
        theme(f"{args.suite}{dot}amd64{dot}{args.image_size} image", Theme.INK),
        theme(describe_host(), Theme.INK),
    ]
    if not timings:
        # Said once, plainly, rather than letting the first run's ETA quietly
        # be wrong and the operator wonder why.
        facts.append(theme("first run: the ETA is a guess until this finishes", Theme.INK))
    if image.exists():
        size = image.stat().st_size / 1e9
        facts.append(theme(f"replacing {image.name} ({size:.1f} GB)", Theme.ACCENT))

    # Facts sit beside the bars, never beside the box's borders, and any that do
    # not fit go underneath rather than being dropped. The overflow line is
    # normally the "replacing" warning, which is the one that most deserves not
    # to be quietly discarded.
    beside, overflow = facts[: len(LOGO_BARS)], facts[len(LOGO_BARS):]
    lines = []
    for index, line in enumerate(logo):
        fact = beside[index - 1] if 0 < index <= len(beside) else ""
        lines.append("  " + line + ("   " + fact if fact else ""))
    lines.extend("  " + fact for fact in overflow)
    return lines


def _parse_size(text: str) -> int:
    from portlin.cli import parse_size

    return parse_size(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/build.py",
        description="Build a portlin image, showing real progress.",
    )
    parser.add_argument("--out-dir", type=Path, default=REPO / "out")
    parser.add_argument("--name", default="portlin", help="base name for the tarball and image")
    parser.add_argument("--suite", default="trixie")
    parser.add_argument("--image-size", default="8G")
    parser.add_argument("--minimal", action="store_true", help="no desktop")
    parser.add_argument("--keep", action="store_true", help="keep the build tree")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="refuse to fall back to a container; fail instead",
    )
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if can_build_here():
        return build(args)
    if args.no_docker or args.in_container:
        print(
            "Cannot build here: needs x86_64 Linux and root.\n"
            f"This is {describe_host()}, uid {os.geteuid()}.",
            file=sys.stderr,
        )
        return 2
    return run_in_container(args)


if __name__ == "__main__":
    sys.exit(main())
