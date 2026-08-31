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
import selectors
import subprocess
from collections.abc import Callable
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
    # Called with (argv, stream, line) for every line a command produces, as it
    # produces it. Set by the build TUI to drive progress bars; None everywhere
    # else, which keeps the fast, simple subprocess.run path for normal use.
    on_output: Callable[[list[str], str, str], None] | None = None

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
            if self.on_output is not None and capture:
                proc = self._run_streaming(
                    argv,
                    stdin_payload,
                    env=full_env,
                    cwd=str(cwd) if cwd is not None else None,
                )
            else:
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

    def _run_streaming(
        self,
        argv: list[str],
        stdin_payload: str | None,
        *,
        env: dict[str, str] | None,
        cwd: str | None,
    ) -> subprocess.CompletedProcess:
        """Run a command, feeding each line to the hook as it arrives.

        The two pipes are read separately rather than merged into one. Merging
        would be simpler, but CommandError quotes stderr, and an error message
        buried in a megabyte of apt's ordinary chatter is not an error message.

        selectors rather than reader threads: the stdlib does this directly, and
        two threads plus a join plus a timeout is a lot of machinery to acquire
        for a progress bar.
        """
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=cwd,
        )

        if stdin_payload is not None and proc.stdin is not None:
            # Written and closed up front. Everything portlin sends this way is
            # a passphrase or a short answer, never enough to fill a pipe
            # buffer, so there is no deadlock to interleave around.
            proc.stdin.write(stdin_payload)
            proc.stdin.close()

        collected = {"stdout": [], "stderr": []}
        selector = selectors.DefaultSelector()
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if pipe is not None:
                selector.register(pipe, selectors.EVENT_READ, name)

        while selector.get_map():
            for key, _events in selector.select():
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                stream = key.data
                line = line.rstrip("\n")
                collected[stream].append(line)
                self._notify(argv, stream, line)
        selector.close()

        proc.wait()
        return subprocess.CompletedProcess(
            argv,
            proc.returncode,
            "\n".join(collected["stdout"]),
            "\n".join(collected["stderr"]),
        )

    def _notify(self, argv: list[str], stream: str, line: str) -> None:
        """Call the hook, and never let it break the build.

        The hook draws a terminal UI. A resize, a closed pipe or a bug in the
        rendering must not destroy an hour of debootstrap.
        """
        try:
            self.on_output(argv, stream, line)
        except Exception:
            log.debug("output hook raised, continuing", exc_info=True)

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
