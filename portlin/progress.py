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
import math
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
    # "dlstatus" or "pmstatus". One install reports two separate 0-100% runs,
    # the download and then dpkg, and only this says which one a line is from.
    kind: str = "pmstatus"


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
    kind, package, percent, detail = match.groups()
    return AptStatus(
        package=package,
        fraction=_clamp(float(percent) / 100.0),
        detail=detail.strip(),
        kind=kind,
    )


# The part of an install's bar given to the download. Nominal: the real split
# depends on the network as much as the machine, and only the bar's shape rests
# on it, since the ETA is calibrated from the last build's progress instead.
APT_DOWNLOAD_SHARE = 0.3


def apt_install_fraction(status: AptStatus) -> float:
    """Place one status line on a single bar for the whole install.

    apt counts the download from 0 to 100% and then dpkg from 0 to 100% again.
    Shown as they come, the bar fills during the download, empties when dpkg
    starts, and the ETA extrapolated from it races to zero and then leaps to
    hours.
    """
    if status.kind == "dlstatus":
        return status.fraction * APT_DOWNLOAD_SHARE
    return APT_DOWNLOAD_SHARE + status.fraction * (1.0 - APT_DOWNLOAD_SHARE)


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

    Returns None rather than infinity before anything has happened. This is
    the fallback for a first build, when there is no earlier run to say where
    the slow parts are; with one, Timeline calibrates against it instead.
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


# How finely a stage's progress is remembered between builds: the moment it
# first reached each 2% step. Coarser and the clamp in Timeline._position lets
# the ETA drift a whole step away from what progress says; finer only makes the
# timings cache longer.
PROFILE_STEPS = 50


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
        profiles: dict[str, list[float]] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.stages = _weighted(stages or DEFAULT_STAGES, timings or {})
        self._by_key = {stage.key: stage for stage in self.stages}
        self._expected = dict(timings or {})
        self._last_profiles = dict(profiles or {})
        self._clock = clock
        self.current: str | None = None
        self.detail: str = ""
        self.fraction: float | None = None
        self._started_at: float | None = None
        self._durations: dict[str, float] = {}
        self._profiles: dict[str, list[float]] = {}
        self._done: list[str] = []
        self._reached: list[float] = []
        self._began: tuple[float, float] | None = None

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
        self._reached = []
        self._began = None

    def update(self, fraction: float | None = None, detail: str | None = None) -> None:
        if fraction is not None:
            self.fraction = _clamp(fraction)
            elapsed = self.elapsed()
            if self._began is None:
                self._began = (elapsed, self.fraction)
            # First arrivals only, so a fraction that wobbles backwards (apt
            # recomputes its totals mid-install) cannot rewrite the profile.
            while len(self._reached) <= _step(self.fraction):
                self._reached.append(elapsed)
        if detail is not None:
            self.detail = detail

    def finish(self) -> None:
        if self.current is None:
            return
        duration = self.elapsed()
        self._durations[self.current] = duration
        if self._reached:
            # Steps never reached were reached by the end: apt stops at 99.97%,
            # and the stage carries on past its last line of progress anyway.
            missing = PROFILE_STEPS + 1 - len(self._reached)
            self._profiles[self.current] = self._reached + [duration] * missing
        self._done.append(self.current)
        self.current = None
        self._started_at = None
        self.fraction = None
        self._reached = []
        self._began = None

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

    def stage_remaining(self) -> float | None:
        """Seconds left in the current stage, or None with nothing to go on.

        A stage's own percentage is a poor clock. apt counts steps, not time:
        it reaches 99% with the initramfs still to build, sits at 81% through
        the kernel's postinst, and says nothing at all through apt update.
        Extrapolating from it races the ETA to zero minutes early. The last
        build met those same stalls at the same percentages, so where it had
        got to by now is the better answer, and the percentage only keeps that
        honest when this build runs faster or slower.
        """
        if self.current is None:
            return None
        elapsed = self.elapsed()
        expected = self._expected.get(self.current)
        profile = self._last_profiles.get(self.current)
        if expected and profile:
            return max(0.0, expected - self._position(profile, elapsed, expected))
        if expected:
            return max(0.0, expected - elapsed)
        if self._began is None or self.fraction is None:
            return None
        # No history. Measured from when progress began rather than from when
        # the stage did: apt update and dist-upgrade, or debootstrap fetching
        # its packages, happen before the percentage moves and are no part of
        # the rate it moves at.
        began_at, began_from = self._began
        if began_from >= 1.0:
            return 0.0
        done = (self.fraction - began_from) / (1.0 - began_from)
        return estimate_remaining(done, elapsed - began_at)

    def _position(self, profile: list[float], elapsed: float, expected: float) -> float:
        """How far into the last build's run of this stage this one has got.

        Wherever the last build was when it reached the step this one has, plus
        the time since this one reached it, but never past where the last build
        reached the next step. So the ETA counts down in real seconds through a
        stall the last build also had, rather than freezing, and holds still
        once this build is slower than that, until progress moves it on.

        Timed from reaching the step rather than from the start of the stage,
        because a build that fell behind earlier - a slow download, say - would
        otherwise already be past the end of every later window, and count the
        initramfs and the triggers off as done before they had started.
        """
        if not self._reached:
            # Not reporting yet, so no further on than the last build was when
            # its progress began.
            return min(elapsed, profile[0])
        # The furthest step reached, not the step of the latest fraction: apt
        # nudges its percentage back now and then, and the ETA should not
        # jump back with it.
        step = len(self._reached) - 1
        since = elapsed - self._reached[step]
        latest = profile[step + 1] if step < PROFILE_STEPS else expected
        return min(profile[step] + since, max(profile[step], latest))

    def remaining(self) -> float | None:
        """Seconds left in the whole build, or None until one has been timed.

        The current stage's estimate plus how long each later stage took last
        time, rather than the overall fraction extrapolated: tar and grub
        report no fraction, so while they run that fraction stands still and
        an ETA extrapolated from it climbs.
        """
        later = [
            stage.key for stage in self.stages
            if stage.key != self.current and stage.key not in self._done
        ]
        if not all(self._expected.get(key) for key in later):
            return None
        current = 0.0
        if self.current is not None:
            current = self.stage_remaining()
            if current is None:
                return None
        return current + sum(self._expected[key] for key in later)

    def durations(self) -> dict[str, float]:
        return dict(self._durations)

    def profiles(self) -> dict[str, list[float]]:
        """When each finished stage reached each step, for the next build's ETA."""
        return {key: list(profile) for key, profile in self._profiles.items()}

    def is_done(self, key: str) -> bool:
        return key in self._done


def _step(fraction: float) -> int:
    """The last profile step a fraction has reached.

    The epsilon is for fractions that are exactly on a step but land a hair
    under it, as 0.58 * 50 does in floating point.
    """
    return min(PROFILE_STEPS, int(fraction * PROFILE_STEPS + 1e-9))


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
    return {
        key: float(value)
        for key, value in _read_cache(path).items()
        if _is_number(value) and value > 0
    }


# Beside the durations in the same file, under a key no stage will ever have.
# The durations stay at the top level so that a cache written by this version
# still reads correctly in one that predates profiles, which skips the key.
_PROFILES_KEY = "profiles"


def load_profiles(path: Path) -> dict[str, list[float]]:
    """Read the per-stage progress profiles, dropping any that cannot be right.

    A profile from a different PROFILE_STEPS, or one that goes backwards in
    time, would misplace every step after the fault, so it is discarded whole
    and that stage falls back to its plain duration.
    """
    profiles = _read_cache(path).get(_PROFILES_KEY)
    if not isinstance(profiles, dict):
        return {}
    usable = {}
    for key, profile in profiles.items():
        if (
            isinstance(profile, list)
            and len(profile) == PROFILE_STEPS + 1
            and all(_is_number(t) and t >= 0 for t in profile)
            and all(a <= b for a, b in zip(profile, profile[1:]))
        ):
            usable[key] = [float(t) for t in profile]
    return usable


def save_timings(
    path: Path,
    durations: dict[str, float],
    profiles: dict[str, list[float]] | None = None,
) -> None:
    """Write the timings cache, best effort."""
    content: dict = dict(durations)
    if profiles:
        # A tenth of a second is far finer than any ETA is drawn.
        content[_PROFILES_KEY] = {
            key: [round(t, 1) for t in profile] for key, profile in profiles.items()
        }
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def _read_cache(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _is_number(value: object) -> bool:
    # Finite as well: json reads Infinity and NaN, and an infinite duration
    # turns into an infinite ETA, which format_duration cannot print.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
