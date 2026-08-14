"""The single chokepoint through which every external command passes.

Two properties matter here and they are the reason this module exists:

1.  Every command is recorded. That makes the orchestration in install.py and
    rootfs.py testable as pure data, on any machine, without root and without
    Linux, by replaying it through a Runner in dry-run mode.
2.  Secret stdin is never recorded or logged. LUKS passphrases go in through
    ``secret_input`` and stop here.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CommandError

log = logging.getLogger("portlin")


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Runner:
    """Executes (or records) external commands.

    In ``dry_run`` mode nothing is executed; each call returns ``dry_stdout`` so
    that callers which parse output (blkid, lsblk) still produce plausible values
    and orchestration continues to completion.
    """

    dry_run: bool = False
    commands: list[list[str]] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        secret_input: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        dry_stdout: str = "",
    ) -> CommandResult:
        argv = [str(a) for a in argv]
        self.commands.append(argv)

        if self.dry_run:
            log.debug("dry-run: %s", " ".join(argv))
            return CommandResult(argv, 0, dry_stdout, "")

        log.debug("run: %s", " ".join(argv))
        stdin_payload = secret_input if secret_input is not None else input

        full_env = None
        if env is not None:
            full_env = {**os.environ, **env}

        try:
            proc = subprocess.run(
                argv,
                input=stdin_payload,
                capture_output=capture,
                text=True,
                env=full_env,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            # A missing binary raises at exec time rather than producing an exit
            # code, so check=False would not otherwise cover it. But check=False
            # means "I do not care if this does not work", and a tool that is
            # absent has not worked. Report it the way a shell does, with 127.
            detail = f"command not found: {argv[0]}"
            if check:
                raise CommandError(argv, 127, detail) from exc
            log.debug("%s, continuing", detail)
            return CommandResult(argv, 127, "", detail)

        result = CommandResult(
            argv,
            proc.returncode,
            proc.stdout or "",
            proc.stderr or "",
        )
        if check and not result.ok:
            raise CommandError(argv, result.returncode, result.stderr)
        return result

    def output(self, argv: list[str], *, dry_stdout: str = "", **kwargs) -> str:
        """Run a command and return its stdout, stripped."""
        return self.run(argv, dry_stdout=dry_stdout, **kwargs).stdout.strip()

    def exists(self, argv: list[str]) -> bool:
        """Run a command purely for its exit status."""
        return self.run(argv, check=False).ok

    def write_file(
        self, path: str | Path, content: str, *, mode: int = 0o644
    ) -> None:
        """Write a file, recorded like a command so dry runs stay inspectable."""
        path = Path(path)
        self.commands.append(["write-file", str(path), f"mode={mode:o}"])
        if self.dry_run:
            log.debug("dry-run: write %s (%d bytes)", path, len(content))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)

    def rendered(self) -> list[str]:
        """Recorded commands as shell-ish strings, for assertions and logs."""
        return [" ".join(c) for c in self.commands]
