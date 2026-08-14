"""Exception hierarchy for portlin.

Every failure the user can plausibly cause is a PortlinError subclass, so the CLI
can print a clean message instead of a traceback. Anything else escaping as a
traceback is a genuine bug.
"""

from __future__ import annotations


class PortlinError(Exception):
    """Base class for expected, user-facing failures."""


class PreflightError(PortlinError):
    """The host is not able to run this operation at all."""


class LayoutError(PortlinError):
    """The target cannot hold a usable partition layout."""


class TargetError(PortlinError):
    """The target device or image is unusable or unsafe to write."""


class CommandError(PortlinError):
    """An external command exited non-zero."""

    def __init__(self, argv: list[str], returncode: int, stderr: str = "") -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        rendered = " ".join(argv)
        message = f"command failed ({returncode}): {rendered}"
        if stderr.strip():
            message = f"{message}\n{stderr.strip()}"
        super().__init__(message)


class BuildError(PortlinError):
    """The rootfs build failed for a reason we understand."""


class AbortedError(PortlinError):
    """The user declined a confirmation prompt."""
