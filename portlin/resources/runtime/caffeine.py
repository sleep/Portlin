# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""What both Caffeine applets share: the logind lock, the durations and the settings.

portlin-caffeine is the Xfce panel's tray icon and portlin-caffeine-lite the
lite session's. They differ in how they draw and in how they stop the screen
blanking, which is different software in each session, and agree on
everything here. One file rather than two copies, because both read and write
the same ~/.config/portlin/caffeine.ini, and a stick whose two desktops
disagreed about what that file means would lose someone's preferences on the
way from one to the other.

Imports nothing beyond the standard library, so the lite applet, which runs for
the whole session, never pays for GTK.
"""

from __future__ import annotations

import configparser
import os
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

ICONS = {True: "/usr/share/portlin/caffeine-on.svg",
         False: "/usr/share/portlin/caffeine-off.svg"}


# All three, and in block mode. --mode=delay only postpones a suspend by a few
# seconds before letting it happen, which would look like caffeine working
# right up until the machine slept anyway.
INHIBIT_WHAT = "idle:sleep:handle-lid-switch"


WHO = "Portlin Caffeine"


# Fixed rather than the menu's own status line, which counts down: the reason
# is recorded with the lock when it is taken and never updated, so a countdown
# there would be wrong within a minute of being written.
WHY = "Caffeine is active"


SECTION = "caffeine"


DURATIONS = [
    (15 * 60, "15 minutes"),
    (30 * 60, "30 minutes"),
    (60 * 60, "1 hour"),
    (2 * 60 * 60, "2 hours"),
    (5 * 60 * 60, "5 hours"),
    (None, "Until turned off"),
]


@dataclass(frozen=True)
class Settings:
    """What the applet remembers between sessions."""

    active: bool = False
    deadline: float | None = None
    restore_at_login: bool = True
    default_duration: int | None = None
    notify_on_expiry: bool = True


def inhibit_argv(why: str) -> list[str]:
    """The systemd-inhibit command whose lifetime is the logind lock.

    systemd-inhibit holds the lock for exactly as long as the command it runs,
    so the command has to be one that never exits on its own and the lock is
    released by killing it -- including by the process dying, which is what
    makes a crash here fail safe rather than leaving a machine that will not
    sleep and nothing on screen to say why.

    Asked of systemd-inhibit rather than by holding the D-Bus file descriptor
    directly, because a lock held through Gio's file-descriptor list is a call
    that cannot be exercised anywhere but a booted stick, and this is the layer
    that must not quietly fail. Nothing is lost by shelling out: logind lists
    every lock whoever took it, so this one still answers for itself under the
    name below when someone asks why their laptop stopped suspending.
    """
    return [
        "systemd-inhibit",
        f"--what={INHIBIT_WHAT}",
        f"--who={WHO}",
        f"--why={why}",
        "--mode=block",
        "sleep",
        "infinity",
    ]


def duration_index(seconds: int | None) -> int:
    """Where a stored duration sits in DURATIONS, for the Preferences chooser.

    Falls back to the open-ended entry rather than raising. The settings file
    is editable by hand, and a number matching no entry would otherwise throw
    while opening the one dialog that could correct it.
    """
    for index, (offered, _) in enumerate(DURATIONS):
        if offered == seconds:
            return index
    return len(DURATIONS) - 1


def die_with_parent() -> None:
    """Ask the kernel to kill this process when the one that forked it dies.

    Run in the forked child, before exec. Without it a killed applet leaves
    systemd-inhibit behind holding the machine awake, with no icon left in the
    panel to explain it and nothing left to click: the lock outlives every
    trace of what took it.

    Every failure is swallowed, and that is the point. An exception raised in
    preexec_fn comes back out of Popen, so an unguarded prctl that failed
    would cost the lock itself -- and a lock that is merely hard to clean up
    after a crash beats no lock at all.
    """
    try:
        import ctypes

        # prctl(PR_SET_PDEATHSIG, SIGTERM). systemd-inhibit releases the lock
        # by exiting, so the default terminating disposition is enough and no
        # handler has to survive the exec to do it.
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, signal.SIGTERM
        )
    except Exception:
        pass


def format_remaining(seconds: float) -> str:
    """How much is left, as a person would say it rather than as a clock."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    hours, minutes = divmod(seconds // 60, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    return " ".join(parts)


def status_text(active: bool, deadline: float | None, now: float) -> str:
    """The line at the top of the menu, which is the whole state in one string."""
    if not active:
        return "Caffeine is off"
    if deadline is None:
        return "Caffeine is active"
    return f"Caffeine is active for another {format_remaining(deadline - now)}"


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "portlin" / "caffeine.ini"


def parse_settings(text: str, now: float | None = None) -> Settings:
    """Read stored settings, and decide what this session starts as.

    Two things can turn a remembered `active` back off, and both are decided
    here rather than at the call site so that "what state does a session begin
    in" has one answer. A deadline that has already passed is one: a stick
    shut down with ten minutes left and booted a week later would otherwise
    keep a machine awake on the strength of a countdown that ended before it
    started. A cleared restore preference is the other.
    """
    now = time.time() if now is None else now
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
        if not parser.has_section(SECTION):
            return Settings()
        section = parser[SECTION]
        settings = Settings(
            active=section.getboolean("active", fallback=False),
            deadline=_optional_number(section.get("deadline", "")),
            restore_at_login=section.getboolean("restore_at_login", fallback=True),
            default_duration=_optional_number(section.get("default_duration", ""), int),
            notify_on_expiry=section.getboolean("notify_on_expiry", fallback=True),
        )
    except (configparser.Error, ValueError):
        # A file edited by hand into something unparseable is not a reason to
        # refuse to start, and least of all a reason to start caffeinated.
        return Settings()
    expired = settings.deadline is not None and settings.deadline <= now
    if expired or not settings.restore_at_login:
        settings = replace(settings, active=False, deadline=None)
    return settings


def _optional_number(value: str, cast=float):
    value = value.strip()
    if not value or value.lower() in {"none", "never"}:
        return None
    return cast(float(value))


def render_settings(settings: Settings) -> str:
    return "\n".join(
        [
            "# Written by portlin-caffeine. Edit it or delete it; the applet",
            "# falls back to its defaults for anything it cannot read.",
            f"[{SECTION}]",
            f"active = {str(settings.active).lower()}",
            f"deadline = {'none' if settings.deadline is None else int(settings.deadline)}",
            f"restore_at_login = {str(settings.restore_at_login).lower()}",
            f"default_duration = "
            f"{'none' if settings.default_duration is None else settings.default_duration}",
            f"notify_on_expiry = {str(settings.notify_on_expiry).lower()}",
            "",
        ]
    )


def load_settings() -> Settings:
    try:
        return parse_settings(settings_path().read_text())
    except OSError:
        return Settings()


def save_settings(settings: Settings) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_settings(settings))
    except OSError as exc:
        # A read-only home is a reason to forget the state, not to stop
        # keeping the machine awake.
        print(f"portlin-caffeine: could not save settings: {exc}", file=sys.stderr)
