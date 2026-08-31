"""Turning command output into progress, as pure functions.

Nothing here does I/O, draws anything, or knows what a terminal is. It reads
lines that other programs printed and answers one question: how far along is
this. Keeping that separate from the drawing is what makes the numbers testable,
since a wrong percentage is invisible in a screenshot and obvious in an
assertion.

The parsers are deliberately forgiving. Every one of them is reading output from
a program that is free to change its wording between Debian releases, and a
progress bar that stops moving is a cosmetic problem while an exception raised
mid-build is a lost hour.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

# A tar record is 512 bytes, fixed by the format. Checkpoints are counted in
# records, so everything tar reports has to be scaled by this to mean anything.
TAR_RECORD_BYTES = 512

FILL = "█"
EMPTY = "░"


@dataclass(frozen=True)
class AptStatus:
    """One line of apt's machine-readable status stream."""

    package: str
    fraction: float
    detail: str


# Non-greedy on the package field so that a multiarch name like libc6:amd64,
# which contains the separator, still parses: the engine backtracks until the
# next field is a number, which the architecture never is.
_APT_STATUS = re.compile(r"^(pmstatus|dlstatus):(.*?):(\d+(?:\.\d+)?):(.*)$")

_DEBOOTSTRAP = re.compile(
    r"^I: (Retrieving|Validating|Extracting|Unpacking|Configuring|Installing)\s+(\S*)"
)

_TAR_CHECKPOINT = re.compile(r"^tar: (?:Write|Read) checkpoint (\d+)")


def parse_apt_status(line: str) -> AptStatus | None:
    """Read one ``APT::Status-Fd`` line.

    apt emits several kinds on this stream and only two of them are progress.
    pmconffile is a question about a conffile and media-change is a request for
    a disc; both have the same shape and neither is a percentage, so they are
    matched explicitly rather than by elimination.
    """
    match = _APT_STATUS.match(line.strip())
    if match is None:
        return None
    _kind, package, percent, detail = match.groups()
    return AptStatus(
        package=package,
        fraction=_clamp(float(percent) / 100.0),
        detail=detail.strip(),
    )


def parse_debootstrap(line: str) -> tuple[str, str] | None:
    """Read one debootstrap progress line into (verb, subject).

    debootstrap has no percentage to offer and no total to divide by until it
    has finished retrieving, so the caller counts these itself.
    """
    match = _DEBOOTSTRAP.match(line.strip())
    if match is None:
        return None
    verb, subject = match.groups()
    return verb, subject


def parse_tar_checkpoint(line: str) -> int | None:
    """Read the record count out of a ``--checkpoint-action=echo`` line."""
    match = _TAR_CHECKPOINT.match(line.strip())
    return int(match.group(1)) if match else None


def checkpoint_bytes(records: int) -> int:
    return records * TAR_RECORD_BYTES


def format_duration(seconds: float) -> str:
    """Render a duration the way a person reads a build log.

    Clamped at zero because a container's clock can step backwards relative to
    the host, and "-3s remaining" is worse than useless.
    """
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def render_bar(fraction: float | None, width: int) -> str:
    """Render a bar of exactly ``width`` cells.

    An unknown fraction renders empty rather than full: the stages that cannot
    report a percentage are the ones still waiting to run, and a row of full
    bars would say the opposite of the truth.
    """
    if width <= 0:
        return ""
    if fraction is None:
        return EMPTY * width
    filled = int(round(_clamp(fraction) * width))
    return FILL * filled + EMPTY * (width - filled)


def estimate_remaining(fraction: float | None, elapsed: float) -> float | None:
    """Seconds left, extrapolated linearly from work already done.

    Returns None rather than infinity before anything has happened. Linear is
    the honest model here: the caller has no idea whether the remaining packages
    are large or small, and pretending otherwise would just be a fancier guess.
    """
    if fraction is None or fraction <= 0:
        return None
    return max(0.0, elapsed * (1.0 - fraction) / fraction)


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    weight: float


# Default shares of a whole build. Rough, and replaced by measured timings after
# the first successful run: on an emulated amd64 host the package install
# dominates far more than these assume, and on a fast native host far less.
DEFAULT_STAGES: list[Stage] = [
    Stage("debootstrap", "debootstrap", 0.15),
    Stage("packages", "packages", 0.60),
    Stage("tarball", "tarball", 0.08),
    Stage("partition", "partition", 0.02),
    Stage("unpack", "unpack", 0.08),
    Stage("bootloader", "bootloader", 0.05),
    Stage("verify", "verify", 0.02),
]


# Which command means which stage. Ordered, because the first match wins and
# "tar -cf" and "tar -xf" differ only in a flag.
_STAGE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("debootstrap", ("debootstrap",)),
    ("packages", ("apt-get",)),
    ("bootloader", ("grub-install", "update-grub", "grub-mkconfig", "update-initramfs")),
    ("partition", ("sgdisk", "partprobe", "mkfs.ext4", "mkfs.vfat", "losetup", "cryptsetup")),
]


def stage_for(argv: list[str]) -> str | None:
    """Name the stage a command belongs to, or None if it belongs to no stage.

    Derived from the command rather than announced by the orchestration, which
    keeps presentation out of build_rootfs and write_stick entirely. Commands
    that happen throughout - mkdir, write-file, chroot bookkeeping - deliberately
    match nothing, so they cannot yank the display back to an earlier stage.
    """
    if not argv:
        return None
    tokens = set(argv)
    if "tar" in tokens:
        if "-cf" in tokens:
            return "tarball"
        if "-xf" in tokens:
            return "unpack"
        return None
    for stage, needles in _STAGE_RULES:
        if tokens.intersection(needles):
            return stage
    return None


class Timeline:
    """Which stage is running, how far along it is, and how far along the whole.

    The clock is injected so that the arithmetic can be tested without a build
    and without sleeping.
    """

    def __init__(
        self,
        stages: list[Stage] | None = None,
        *,
        timings: dict[str, float] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.stages = _weighted(stages or DEFAULT_STAGES, timings or {})
        self._by_key = {stage.key: stage for stage in self.stages}
        self._expected = dict(timings or {})
        self._clock = clock
        self.current: str | None = None
        self.detail: str = ""
        self.fraction: float | None = None
        self._started_at: float | None = None
        self._durations: dict[str, float] = {}
        self._done: list[str] = []

    def start(self, key: str) -> None:
        if key not in self._by_key:
            raise KeyError(f"unknown stage: {key}")
        # Stages only ever move forward. Some commands appear at both ends of
        # the build - losetup attaches a loop device early and detaches it
        # during cleanup - and without this the display jumps back to an
        # earlier stage after later ones have already finished.
        if key in self._done:
            return
        if self.current is not None:
            self.finish()
        self.current = key
        self.fraction = None
        self.detail = ""
        self._started_at = self._clock()

    def update(self, fraction: float | None = None, detail: str | None = None) -> None:
        if fraction is not None:
            self.fraction = _clamp(fraction)
        if detail is not None:
            self.detail = detail

    def finish(self) -> None:
        if self.current is None:
            return
        self._durations[self.current] = self.elapsed()
        self._done.append(self.current)
        self.current = None
        self._started_at = None
        self.fraction = None

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    def displayed_fraction(self) -> tuple[float | None, bool]:
        """The current stage's progress, and whether it is an estimate.

        tar and grub report no percentage of their own. Once the timings cache
        knows how long they took last time, elapsed against that is a reasonable
        stand-in - but it is a different kind of number from apt's, so the
        caller is told which it got and marks it in the UI.

        Capped below 1.0 because a stage that overruns its last duration is
        still working, and a bar sitting at 100% while the build continues is
        how a progress display loses its credibility.
        """
        if self.current is None:
            return None, False
        if self.fraction is not None:
            return self.fraction, False
        expected = self._expected.get(self.current)
        if not expected:
            return None, True
        return min(0.99, self.elapsed() / expected), True

    def weight_of(self, key: str) -> float:
        return self._by_key[key].weight

    def overall(self) -> float:
        done = sum(self._by_key[key].weight for key in self._done)
        if self.current is not None and self.fraction is not None:
            done += self._by_key[self.current].weight * self.fraction
        return _clamp(done)

    def durations(self) -> dict[str, float]:
        return dict(self._durations)

    def is_done(self, key: str) -> bool:
        return key in self._done


def _weighted(stages: list[Stage], timings: dict[str, float]) -> list[Stage]:
    """Re-weight stages from measured durations, falling back to the defaults.

    A partial cache is normal: stages added since the last run, or a build that
    stopped early, leave gaps. Those keep their default weight and the whole set
    is renormalised, so the bar still ends at exactly full.
    """
    usable = {k: v for k, v in timings.items() if v > 0}
    if not usable:
        return list(stages)

    measured_total = sum(usable.values())
    raw: list[tuple[Stage, float]] = []
    for stage in stages:
        if stage.key in usable:
            raw.append((stage, usable[stage.key] / measured_total))
        else:
            raw.append((stage, stage.weight))

    total = sum(weight for _, weight in raw)
    if total <= 0:
        return list(stages)
    return [Stage(s.key, s.label, w / total) for s, w in raw]


def load_timings(path: Path) -> dict[str, float]:
    """Read the timings cache, treating every problem as "no cache".

    This file exists only to sharpen an estimate. Nothing about it is worth
    failing a build for, so a missing, unreadable, corrupt or nonsense file all
    mean the same thing.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: float(value)
        for key, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    }


def save_timings(path: Path, durations: dict[str, float]) -> None:
    """Write the timings cache, best effort."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(durations, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
