"""Which source a stick was written from.

Copyright (C) 2026 the portlin authors.
Licensed under the GNU General Public License, version 3 or later.
See the LICENSE file, or <https://www.gnu.org/licenses/gpl-3.0.html>.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Set by scripts/build.py on the container it launches. The container mounts
# the checkout but has no git, so the host has to ask on its behalf.
COMMIT_ENV = "PORTLIN_COMMIT"

REPO = Path(__file__).resolve().parent.parent

# Same suffix git describe --dirty uses. A bare hash on an image built from
# uncommitted changes would name a tree nobody can check out.
DIRTY_SUFFIX = "-dirty"


def _git(*args: str) -> str:
    # safe.directory because the write stage runs as root over a checkout a
    # user owns, and git refuses to read a repository owned by someone else.
    return subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def source_commit() -> str:
    """The short hash of the checkout, with -dirty if it has local changes.

    Empty when it cannot be known: running from an installed copy rather than
    a checkout, or on a host without git. Checking for .git first matters for
    the installed case, where git -C would otherwise walk up and report
    whatever unrelated repository the install happens to sit inside.
    """
    if COMMIT_ENV in os.environ:
        return os.environ[COMMIT_ENV].strip()
    if not (REPO / ".git").exists():
        return ""
    try:
        head = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain")
    except (OSError, subprocess.CalledProcessError):
        return ""
    return f"{head}{DIRTY_SUFFIX}" if dirty else head


def describe(version: str, commit: str) -> str:
    """The one-line identity every surface shows: version, then the commit."""
    return f"{version} ({commit})" if commit else version
